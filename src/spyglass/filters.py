"""Time-based sample filtering for perf profile data."""

import bisect
import json
import os
import re
import subprocess
from pathlib import Path


def load_epochs(output_dir: Path) -> list[dict]:
    """Load epoch boundary data from epochs.json.
    
    Returns list of dicts with keys: epoch, slot, slot_start_time, detected_at.
    Falls back to legacy format (timestamp field) if slot_start_time is absent.
    """
    epochs_file = output_dir / "epochs.json"
    if not epochs_file.exists():
        return []
    epochs = json.loads(epochs_file.read_text())
    # Normalize: ensure each entry has a "slot_start_time" key
    for e in epochs:
        if "slot_start_time" not in e:
            # Legacy format: use "timestamp" as fallback
            e["slot_start_time"] = e.get("timestamp", 0)
    return epochs


def load_sync_status(output_dir: Path) -> dict:
    """Load sync/timing status from run.json (or legacy sync_status.json).
    
    Returns dict with keys: sync_complete_time, recording_start_time, genesis_time.
    """
    result = {}
    # Legacy: sync_status.json
    sync_file = output_dir / "sync_status.json"
    if sync_file.exists():
        result = json.loads(sync_file.read_text())
    # Primary: run.json (overrides legacy values where present)
    run_file = output_dir / "run.json"
    if run_file.exists():
        data = json.loads(run_file.read_text())
        if data.get("sync_complete_time") is not None:
            result["sync_complete_time"] = data["sync_complete_time"]
        if data.get("recording_start") is not None:
            result["recording_start_time"] = data["recording_start"]
        if data.get("genesis_time") is not None:
            result["genesis_time"] = data["genesis_time"]
    return result


def _load_clock_offset(output_dir: Path) -> float | None:
    """Load the precise wall-clock-to-monotonic offset from run.json.
    
    The offset is: wall_clock - monotonic, captured simultaneously at recording start.
    This allows converting wall-clock epoch timestamps to perf's monotonic timestamps:
        perf_time = wall_clock_time - clock_offset
    
    Returns None if run.json doesn't contain clock_offset.
    """
    run_json = output_dir / "run.json"
    if not run_json.exists():
        return None
    run_info = json.loads(run_json.read_text())
    return run_info.get("clock_offset")


def compute_time_ranges(
    output_dir: Path,
    filter_mode: str,
    warmup: float = 6.0,
    cooldown: float = 6.0,
) -> list[tuple[float, float]] | None:
    """Compute time ranges (wall-clock timestamps) to include for the given filter mode.
    
    Uses slot_start_time from epochs.json for precise, genesis-derived epoch boundary
    times rather than SSE event arrival times.
    
    Args:
        output_dir: Profile output directory containing epochs.json and sync_status.json
        filter_mode: One of "epoch-boundary", "mid-epoch", "steady-state", or "all"
        warmup: Seconds before epoch boundary to include
        cooldown: Seconds after epoch boundary to include
    
    Returns:
        List of (start, end) time ranges to include, or None for "all" (no filtering).
    """
    if filter_mode == "all":
        return None

    epochs = load_epochs(output_dir)
    sync_status = load_sync_status(output_dir)

    if filter_mode == "steady-state":
        sync_complete = sync_status.get("sync_complete_time")
        if sync_complete is None:
            print("  WARNING: sync_complete_time not found, using all samples")
            return None
        # Everything from sync completion to infinity
        return [(sync_complete, float("inf"))]

    if not epochs:
        print("  WARNING: No epoch boundaries found in epochs.json")
        return None

    # Build epoch boundary windows using precise slot_start_time
    boundary_ranges = []
    for epoch in epochs:
        t = epoch["slot_start_time"]
        boundary_ranges.append((t - warmup, t + cooldown))

    if filter_mode == "epoch-boundary":
        return boundary_ranges

    if filter_mode == "mid-epoch":
        # Invert: everything NOT in a boundary range, but after sync
        sync_complete = sync_status.get("sync_complete_time")
        start_time = sync_complete if sync_complete else epochs[0]["slot_start_time"] - 300

        # Build the complement of boundary_ranges within [start_time, inf)
        # Sort and merge boundary ranges first
        merged = _merge_ranges(boundary_ranges)
        mid_ranges = []
        cursor = start_time
        for (rs, re_) in merged:
            if cursor < rs:
                mid_ranges.append((cursor, rs))
            cursor = max(cursor, re_)
        # Add trailing range
        mid_ranges.append((cursor, float("inf")))
        return mid_ranges

    return None


def _merge_ranges(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge overlapping time ranges."""
    if not ranges:
        return []
    sorted_ranges = sorted(ranges, key=lambda r: r[0])
    merged = [sorted_ranges[0]]
    for start, end in sorted_ranges[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def filter_collapsed_stacks(
    fallback_path: Path,
    output_path: Path,
    time_ranges: list[tuple[float, float]] | None,
    perf_data_path: Path,
) -> Path:
    """Filter a collapsed stacks file to only include samples within time ranges.
    
    Since collapsed stacks don't have timestamps, we need to re-process from perf.data
    with timestamp filtering. This function runs perf script with time filtering
    and re-collapses.
    
    Time ranges are in wall-clock (unix) time. They get converted to perf's monotonic
    clock using the clock_offset from run.json (captured precisely at recording start).
    
    Args:
        fallback_path: Path to pre-collapsed stacks used when no filtering is needed.
        output_path: Where to write the filtered collapsed stacks.
        time_ranges: List of (start, end) wall-clock time ranges to include.
        perf_data_path: Path to the perf.data file for re-processing.
    """
    if time_ranges is None:
        # No filtering needed
        if fallback_path != output_path:
            output_path.write_text(fallback_path.read_text())
        return output_path

    # Get the precise clock offset (wall - monotonic)
    clock_offset = _load_clock_offset(perf_data_path.parent)

    if clock_offset is None:
        # Fallback: estimate offset from first perf sample + recording_start_time
        sync_status = load_sync_status(perf_data_path.parent)
        recording_start = sync_status.get("recording_start_time")
        env = {**os.environ, "DEBUGINFOD_URLS": ""}
        first_ts = _get_first_perf_timestamp(perf_data_path, env)
        if first_ts is None or recording_start is None:
            print("  WARNING: Cannot determine clock offset, skipping filter")
            if fallback_path != output_path:
                output_path.write_text(fallback_path.read_text())
            return output_path
        clock_offset = recording_start - first_ts
        print("  (using estimated clock offset — run.json missing clock_offset)")

    env = {**os.environ, "DEBUGINFOD_URLS": ""}

    # Convert wall-clock ranges to monotonic (perf) time, then sort and merge
    perf_ranges = sorted(
        [(start - clock_offset, end - clock_offset) for start, end in time_ranges],
        key=lambda r: r[0],
    )
    perf_ranges = _merge_ranges(perf_ranges)

    perf_proc = subprocess.Popen(
        ["perf", "script", "--no-inline", "-i", str(perf_data_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    collapse_proc = subprocess.Popen(
        ["inferno-collapse-perf"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Process perf script output line by line, filtering by timestamp
    _filter_perf_output(perf_proc.stdout, collapse_proc.stdin, perf_ranges)

    collapse_proc.stdin.close()
    collapsed_data = collapse_proc.stdout.read()
    collapse_proc.wait()
    perf_proc.wait()

    output_path.write_bytes(collapsed_data)
    return output_path


def _get_first_perf_timestamp(perf_data_path: Path, env: dict) -> float | None:
    """Read the first timestamp from perf script output (legacy fallback)."""
    proc = subprocess.Popen(
        ["perf", "script", "--no-inline", "-i", str(perf_data_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    for raw_line in proc.stdout:
        line = raw_line.decode("utf-8", errors="replace")
        if not line.startswith(("\t", " ")) and ":" in line:
            m = re.search(r"(\d+\.\d+):", line)
            if m:
                proc.terminate()
                proc.wait()
                return float(m.group(1))
    proc.wait()
    return None


def _filter_perf_output(input_stream, output_stream, time_ranges):
    """Filter perf script output, passing through only samples in time ranges.
    
    perf script output format:
      command  pid  [cpu] timestamp: event
           addr symbol (dso)
           addr symbol (dso)
           <blank line>
    
    We track timestamps and forward entire sample blocks if within range.
    
    time_ranges must be sorted and non-overlapping for bisect to work correctly.
    """
    # Regex to match the header line of a sample (contains timestamp)
    header_re = re.compile(r"\S+\s+\d+.*\s+(\d+\.\d+):")

    # Pre-compute sorted range starts/ends for binary search
    range_starts = [r[0] for r in time_ranges]
    range_ends = [r[1] for r in time_ranges]

    current_block = []
    current_in_range = False

    for raw_line in input_stream:
        line = raw_line.decode("utf-8", errors="replace")

        if line.strip() == "":
            # End of sample block
            if current_in_range and current_block:
                for bl in current_block:
                    output_stream.write(bl.encode())
                output_stream.write(b"\n")
            current_block = []
            current_in_range = False
            continue

        # Check if this is a header line (contains timestamp)
        if not line.startswith(("\t", " ")):
            m = header_re.match(line)
            if m:
                ts = float(m.group(1))
                current_in_range = _in_ranges_bisect(ts, range_starts, range_ends)

        current_block.append(line)

    # Flush last block
    if current_in_range and current_block:
        for bl in current_block:
            output_stream.write(bl.encode())
        output_stream.write(b"\n")


def _in_ranges_bisect(
    timestamp: float,
    range_starts: list[float],
    range_ends: list[float],
) -> bool:
    """Check if a timestamp falls within any range using binary search.
    
    Requires ranges to be sorted and non-overlapping.
    O(log n) per lookup instead of O(n).
    """
    # Find the rightmost range whose start <= timestamp
    idx = bisect.bisect_right(range_starts, timestamp) - 1
    if idx < 0:
        return False
    # Check if timestamp is within that range's end
    return timestamp <= range_ends[idx]
