"""Export profile data in various formats for external tools."""

import os
import re
import subprocess
import sys
from pathlib import Path

from .config import SpyglassConfig
from .constants import log, log_end, log_start, log_step
from .filters import (
    compute_time_ranges,
    get_clock_offset,
    in_ranges_bisect,
    merge_ranges,
)


def cmd_export(
    config: SpyglassConfig,
    profile_dir: Path,
    format: str = "perf-script",
    output_file: Path | None = None,
    filter_mode: str = "auto",
    bin_size: float = 0.5,
    verbose: bool = False,
):
    """Export profile data in various formats.

    Formats:
      - perf-script: filtered perf script text (for Firefox Profiler)
      - flamegraph: SVG flamegraph of filtered samples
      - flamechart: interactive HTML flame chart with time bins

    Args:
        config: Spyglass configuration object
        profile_dir: Directory containing perf.data and epochs.json
        format: Output format
        output_file: Output path (default: auto-generated)
        filter_mode: Time filter to apply ("auto" uses run.json's filter_mode)
        bin_size: Bin size for flamechart format
        verbose: Show processing details
    """
    if format == "flamechart":
        from .flamechart import cmd_flamechart

        cmd_flamechart(config, profile_dir, bin_size=bin_size, verbose=verbose)
        return

    if format == "flamegraph":
        _export_flamegraph(config, profile_dir, output_file, filter_mode, verbose)
        return

    _export_perf_script(config, profile_dir, output_file, filter_mode, verbose)


def _export_flamegraph(
    config: SpyglassConfig,
    profile_dir: Path,
    output_file: Path | None,
    filter_mode: str,
    verbose: bool,
):
    """Export a filtered flamegraph SVG."""
    from .analyze import collapse_perf_data, generate_flamegraph
    from .filters import filter_collapsed_stacks

    profile_dir = Path(profile_dir).resolve()
    perf_data = profile_dir / "perf.data"

    if not perf_data.exists():
        print(f"ERROR: perf.data not found in {profile_dir}", file=sys.stderr)
        sys.exit(1)

    view_name = filter_mode.replace("-", "_")
    view_dir = profile_dir / "views" / view_name
    view_dir.mkdir(parents=True, exist_ok=True)

    if output_file is None:
        output_file = view_dir / "flamegraph.svg"

    nickname = profile_dir.parent.name
    log_start("export", f"{nickname}/flamegraph/{filter_mode}")

    # Ensure collapsed stacks exist in the view dir
    collapsed_path = view_dir / "profile.collapsed"
    if not collapsed_path.exists():
        # Need to collapse full first, then filter
        collapsed_full = profile_dir / "profile.collapsed"
        if not collapsed_full.exists():
            collapse_perf_data(perf_data, collapsed_full)

        if filter_mode == "all":
            import shutil

            shutil.copy2(collapsed_full, collapsed_path)
        else:
            warmup = config.filtering.epoch_boundary_warmup
            cooldown = config.filtering.epoch_boundary_cooldown
            time_ranges = compute_time_ranges(profile_dir, filter_mode, warmup, cooldown)
            if time_ranges is None:
                print(f"ERROR: No data available for '{filter_mode}' filter.", file=sys.stderr)
                sys.exit(1)
            log_step(f"filter {filter_mode}")
            filter_collapsed_stacks(collapsed_full, collapsed_path, time_ranges, perf_data)

    # Generate flamegraph
    generate_flamegraph(collapsed_path, output_file)

    size_kb = output_file.stat().st_size / 1024
    log_end(f"done → {output_file} ({size_kb:.0f} KB)")


def _export_perf_script(
    config: SpyglassConfig,
    profile_dir: Path,
    output_file: Path | None,
    filter_mode: str,
    verbose: bool,
):
    """Export filtered perf script text (for Firefox Profiler)."""
    profile_dir = Path(profile_dir).resolve()
    perf_data = profile_dir / "perf.data"

    if not perf_data.exists():
        print(f"ERROR: perf.data not found in {profile_dir}", file=sys.stderr)
        sys.exit(1)

    # Compute time ranges
    warmup = config.filtering.epoch_boundary_warmup
    cooldown = config.filtering.epoch_boundary_cooldown
    time_ranges = compute_time_ranges(profile_dir, filter_mode, warmup, cooldown)

    if output_file is None:
        view_name = filter_mode.replace("-", "_")
        view_dir = profile_dir / "views" / view_name
        view_dir.mkdir(parents=True, exist_ok=True)
        output_file = view_dir / "profile.linux-perf.txt"

    nickname = profile_dir.parent.name
    log_start("export", f"{nickname}/perf-script/{filter_mode}")

    # Get clock offset for time range conversion
    clock_offset = get_clock_offset(profile_dir)
    if clock_offset is None and time_ranges is not None:
        log("WARNING: cannot determine clock offset, exporting all samples")
        time_ranges = None

    # Convert to perf monotonic time
    perf_ranges = None
    if time_ranges is not None and clock_offset is not None:
        perf_ranges = sorted(
            [(start - clock_offset, end - clock_offset) for start, end in time_ranges],
            key=lambda r: r[0],
        )
        perf_ranges = merge_ranges(perf_ranges)

    # Stream perf script, filtering by timestamp
    env = {**os.environ, "DEBUGINFOD_URLS": ""}
    perf_proc = subprocess.Popen(
        ["perf", "script", "-i", str(perf_data)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )

    header_re = re.compile(r"\S+\s+\d+.*\s+(\d+\.\d+):")
    range_starts = [r[0] for r in perf_ranges] if perf_ranges else []
    range_ends = [r[1] for r in perf_ranges] if perf_ranges else []

    total_samples = 0
    written_samples = 0
    current_block = []
    current_in_range = False if perf_ranges else True

    with open(output_file, "w") as out:
        for raw_line in perf_proc.stdout:
            line = raw_line.decode("utf-8", errors="replace")

            if line.strip() == "":
                if current_in_range and current_block:
                    for bl in current_block:
                        out.write(bl)
                    out.write("\n")
                    written_samples += 1
                current_block = []
                current_in_range = False if perf_ranges else True
                continue

            if not line.startswith(("\t", " ")):
                m = header_re.match(line)
                if m:
                    total_samples += 1
                    ts = float(m.group(1))
                    if perf_ranges:
                        current_in_range = in_ranges_bisect(ts, range_starts, range_ends)
                    else:
                        current_in_range = True

            current_block.append(line)

        # Flush last block
        if current_in_range and current_block:
            for bl in current_block:
                out.write(bl)
            out.write("\n")
            written_samples += 1

    perf_proc.wait()

    size_mb = output_file.stat().st_size / 1024 / 1024
    pct = 100 * written_samples / total_samples if total_samples else 0
    log(f"samples: {written_samples:,} / {total_samples:,} ({pct:.1f}%)")
    log(f"upload to https://profiler.firefox.com")
    log_end(f"done → {output_file} ({size_mb:.1f} MB)")
