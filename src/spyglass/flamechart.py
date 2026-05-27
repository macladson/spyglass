"""Interactive flame chart generation: time-series profiling across epoch boundaries."""

import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from .categories import categorize_sample, load_categories
from .config import SpyglassConfig
from .constants import BOLD, RESET
from .filters import get_clock_offset, load_epochs


def cmd_flamechart(
    config: SpyglassConfig,
    profile_dir: Path,
    bin_size: float = 0.5,
    verbose: bool = False,
):
    """Generate an interactive flame chart for epoch boundary profiling.

    Bins perf samples into time slices and produces a self-contained HTML file
    with an interactive flamegraph + category timeline.

    Args:
        config: Spyglass configuration object
        profile_dir: Directory containing perf.data and epochs.json
        bin_size: Width of each time bin in seconds (default: 0.5s)
        verbose: Show processing details
    """
    profile_dir = Path(profile_dir).resolve()
    perf_data = profile_dir / "perf.data"

    if not perf_data.exists():
        print(f"ERROR: perf.data not found in {profile_dir}", file=sys.stderr)
        sys.exit(1)

    epochs = load_epochs(profile_dir)
    if not epochs:
        print("ERROR: No epoch boundaries found in epochs.json", file=sys.stderr)
        sys.exit(1)

    warmup = config.filtering.epoch_boundary_warmup
    cooldown = config.filtering.epoch_boundary_cooldown
    window = warmup + cooldown

    print(f"{BOLD}=== Flame Chart ==={RESET}")
    print(f"  {BOLD}Window:{RESET}   {window}s ({warmup}s warmup + {cooldown}s cooldown)")
    print(f"  {BOLD}Bin size:{RESET} {bin_size}s")
    print(f"  {BOLD}Epochs:{RESET}   {len(epochs)}")
    print()

    # Compute clock offset
    clock_offset = get_clock_offset(profile_dir)
    if clock_offset is None:
        print("ERROR: Cannot determine clock offset", file=sys.stderr)
        sys.exit(1)

    # Build time ranges for each epoch boundary (in perf monotonic time)
    boundary_windows = []
    for epoch in epochs:
        boundary_time = epoch["slot_start_time"]
        wall_start = boundary_time - warmup
        wall_end = boundary_time + cooldown
        perf_start = wall_start - clock_offset
        perf_end = wall_end - clock_offset
        boundary_windows.append(
            {
                "epoch": epoch["epoch"],
                "boundary_time": boundary_time,
                "perf_start": perf_start,
                "perf_end": perf_end,
                "perf_boundary": boundary_time - clock_offset,
            }
        )

    # Compute bins
    num_bins = int(window / bin_size)
    print(f"  Bins: {num_bins} × {bin_size}s")

    # Process perf script output, binning samples
    print("  Processing perf.data...")
    bins_data = _process_perf_into_bins(perf_data, boundary_windows, num_bins, bin_size, verbose)

    # Category analysis per bin
    categories = load_categories(config.config_dir / "categories.toml")
    bins_categories = None
    if categories:
        print("  Categorizing samples per bin...")
        bins_categories = _categorize_bins(bins_data, categories)

    # Load metadata from run.json
    perf_frequency = None
    pr_number = None
    run_json_path = profile_dir / "run.json"
    if run_json_path.exists():
        run_info = json.loads(run_json_path.read_text())
        perf_frequency = run_info.get("perf_frequency")
        pr_number = run_info.get("pr")

    # Derive nickname from directory structure: profiles/<nickname>/<mode>/
    nickname = profile_dir.parent.name

    # Generate HTML
    view_dir = profile_dir / "views" / "epoch_boundary"
    view_dir.mkdir(parents=True, exist_ok=True)
    output_path = view_dir / "flamechart.html"
    print(f"  Generating {output_path.name}...")
    _generate_html(
        output_path,
        bins_data,
        bins_categories,
        categories,
        bin_size=bin_size,
        warmup=warmup,
        cooldown=cooldown,
        epochs=epochs,
        perf_frequency=perf_frequency,
        nickname=nickname,
        pr_number=pr_number,
    )

    print(f"\n{BOLD}=== Flame chart complete ==={RESET}")
    print(f"  {BOLD}Output:{RESET} {output_path}")
    print()


def _process_perf_into_bins(
    perf_data: Path,
    boundary_windows: list[dict],
    num_bins: int,
    bin_size: float,
    verbose: bool,
) -> list[Counter]:
    """Run perf script and bin samples by time offset within the boundary window.

    Returns a list of Counters, one per bin, mapping stack strings to sample counts.
    Samples from all epoch boundaries are combined (overlaid).
    """
    bins = [Counter() for _ in range(num_bins)]
    env = {**os.environ, "DEBUGINFOD_URLS": ""}

    proc = subprocess.Popen(
        ["perf", "script", "--no-inline", "-i", str(perf_data)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )

    header_re = re.compile(r"\S+\s+\d+.*\s+(\d+\.\d+):")
    current_block = []
    current_ts = None
    total_samples = 0
    binned_samples = 0

    for raw_line in proc.stdout:
        line = raw_line.decode("utf-8", errors="replace")

        if line.strip() == "":
            # End of sample block, assign to a bin
            if current_ts is not None and current_block:
                total_samples += 1
                bin_idx = _assign_to_bin(current_ts, boundary_windows, num_bins, bin_size)
                if bin_idx is not None:
                    # Build stack string from block (skip header line)
                    frames = []
                    for bl in current_block[1:]:
                        bl = bl.strip()
                        if bl:
                            # Extract function name from "addr func+0xNN (dso)" format
                            parts = bl.split(" ", 1)
                            if len(parts) >= 2:
                                func = parts[1].rsplit(" (", 1)[0]
                                # Strip +0x... offset suffixes
                                plus_idx = func.rfind("+0x")
                                if plus_idx > 0:
                                    func = func[:plus_idx]
                                frames.append(func)
                    if frames:
                        stack = ";".join(reversed(frames))
                        bins[bin_idx][stack] += 1
                        binned_samples += 1
            current_block = []
            current_ts = None
            continue

        # Check if this is a header line
        if not line.startswith(("\t", " ")):
            m = header_re.match(line)
            if m:
                current_ts = float(m.group(1))

        current_block.append(line)

    # Flush last sample block (perf script may not end with a blank line)
    if current_ts is not None and current_block:
        total_samples += 1
        bin_idx = _assign_to_bin(current_ts, boundary_windows, num_bins, bin_size)
        if bin_idx is not None:
            frames = []
            for bl in current_block[1:]:
                bl = bl.strip()
                if bl:
                    parts = bl.split(" ", 1)
                    if len(parts) >= 2:
                        func = parts[1].rsplit(" (", 1)[0]
                        plus_idx = func.rfind("+0x")
                        if plus_idx > 0:
                            func = func[:plus_idx]
                        frames.append(func)
            if frames:
                stack = ";".join(reversed(frames))
                bins[bin_idx][stack] += 1
                binned_samples += 1

    proc.wait()

    if verbose:
        print(f"    Total samples: {total_samples:,}")
        print(f"    Binned samples: {binned_samples:,}")
        samples_per_bin = [sum(b.values()) for b in bins]
        print(f"    Samples per bin: min={min(samples_per_bin):,} max={max(samples_per_bin):,}")

    return bins


def _assign_to_bin(
    timestamp: float,
    boundary_windows: list[dict],
    num_bins: int,
    bin_size: float,
) -> int | None:
    """Determine which bin a sample belongs to based on its timestamp.

    Samples from multiple epoch boundaries are overlaid into the same bin structure.
    Returns bin index or None if the sample doesn't fall in any boundary window.
    """
    for w in boundary_windows:
        if w["perf_start"] <= timestamp <= w["perf_end"]:
            offset = timestamp - w["perf_start"]
            idx = int(offset / bin_size)
            if 0 <= idx < num_bins:
                return idx
    return None


def _categorize_bins(
    bins_data: list[Counter],
    categories: list[dict],
) -> list[dict]:
    """Compute category breakdown for each bin.

    Returns list of dicts: {"category_counts": Counter, "total": int}
    """
    result = []
    for bin_counter in bins_data:
        cat_counts = Counter()
        total = 0
        for stack, count in bin_counter.items():
            total += count
            frames = stack.split(";")
            leaf = frames[-1] if frames else ""
            category = categorize_sample(stack, leaf, categories)
            cat_counts[category or "Uncategorized"] += count
        result.append({"category_counts": cat_counts, "total": total})
    return result


def _generate_html(
    output_path: Path,
    bins_data: list[Counter],
    bins_categories: list[dict] | None,
    categories: list[dict] | None,
    bin_size: float,
    warmup: float,
    cooldown: float,
    epochs: list[dict],
    perf_frequency: int | None = None,
    nickname: str | None = None,
    pr_number: int | None = None,
):
    """Generate a self-contained HTML flame chart visualization."""
    num_bins = len(bins_data)
    window = warmup + cooldown

    # Prepare per-bin data for JS
    bins_json = []
    for i, bin_counter in enumerate(bins_data):
        t_start = i * bin_size - warmup
        t_end = t_start + bin_size
        total = sum(bin_counter.values())

        # Top 15 functions by self-time for this bin
        self_time = Counter()
        for stack, count in bin_counter.items():
            leaf = stack.rsplit(";", 1)[-1]
            self_time[leaf] += count

        top_funcs = [
            {"name": func, "samples": count, "pct": round(100.0 * count / total, 2) if total else 0}
            for func, count in self_time.most_common(15)
        ]

        # Category breakdown
        cat_breakdown = {}
        if bins_categories:
            cat_data = bins_categories[i]
            for cat_name, count in cat_data["category_counts"].items():
                cat_breakdown[cat_name] = round(100.0 * count / total, 2) if total else 0

        bins_json.append(
            {
                "index": i,
                "t_start": round(t_start, 2),
                "t_end": round(t_end, 2),
                "total_samples": total,
                "top_functions": top_funcs,
                "categories": cat_breakdown,
            }
        )

    # Prepare collapsed stacks per bin for d3-flame-graph
    bins_collapsed = []
    for bin_counter in bins_data:
        # Convert to list of [stack, count] for JSON
        stacks = [[stack, count] for stack, count in bin_counter.most_common(500)]
        bins_collapsed.append(stacks)

    # Category names in order
    cat_names = [cat["name"] for cat in categories] + ["Uncategorized"] if categories else []

    # Category patterns for client-side highlighting
    cat_patterns = []
    if categories:
        for cat in categories:
            cat_patterns.append(
                {
                    "name": cat["name"],
                    "patterns": [p for p in cat.get("patterns", []) if not p.startswith("re:")],
                    "regex_patterns": [
                        p[3:] for p in cat.get("patterns", []) if p.startswith("re:")
                    ],
                    "leaf_patterns": [
                        p for p in cat.get("leaf_patterns", []) if not p.startswith("re:")
                    ],
                }
            )
        cat_patterns.append(
            {"name": "Uncategorized", "patterns": [], "regex_patterns": [], "leaf_patterns": []}
        )

    cat_colors = [
        "#a02020",  # BLS/Crypto: deep red
        "#8a6838",  # secp256k1/ENR: warm brown
        "#a89040",  # TLS/Noise: brass
        "#684830",  # Database: espresso
        "#30b850",  # Tree Hash: vivid green
        "#78a030",  # Shuffling: yellow-green
        "#9898a8",  # Formatting/Serialization: silver
        "#48a890",  # Allocator: mint teal
        "#e0a020",  # Metrics: bright amber
        "#d04850",  # Tracing/Logging: coral red
        "#7830a8",  # Milhouse Iteration: violet
        "#a068d0",  # Milhouse Tree Hash: light purple
        "#982880",  # Milhouse Mutations: magenta
        "#c83878",  # Milhouse SSZ: hot pink
        "#2a7a48",  # Attestation Verification: forest green
        "#206878",  # Sync Committee: dark teal
        "#3060c0",  # Aggregation Pool: royal blue
        "#c87028",  # Block Import: burnt orange
        "#b8a028",  # State Processing: olive gold
        "#5050b8",  # Fork Choice: indigo
        "#1890a8",  # Networking/libp2p: cerulean
        "#707838",  # Discovery: khaki
        "#607088",  # Tokio Runtime: blue grey
        "#384050",  # Beacon Processor: gunmetal
        "#907898",  # Validator Monitor: dusty purple
        "#282830",  # Uncategorized: near-black
    ]

    max_samples = max((sum(b.values()) for b in bins_data), default=0)

    data = {
        "bins": bins_json,
        "bins_collapsed": bins_collapsed,
        "category_names": cat_names,
        "category_colors": cat_colors[: len(cat_names)],
        "category_patterns": cat_patterns,
        "bin_size": bin_size,
        "warmup": warmup,
        "cooldown": cooldown,
        "window": window,
        "num_bins": num_bins,
        "epochs": [{"epoch": e["epoch"], "slot": e["slot"]} for e in epochs],
        "max_samples": max_samples,
        "perf_frequency": perf_frequency,
        "nickname": nickname,
        "pr_number": pr_number,
    }

    template_path = Path(__file__).parent / "_flamechart_template.html"
    html = template_path.read_text().replace("/*DATA_PLACEHOLDER*/", json.dumps(data))
    output_path.write_text(html)
