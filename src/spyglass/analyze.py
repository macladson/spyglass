"""Analyze profiling output — CPU (perf) and memory (jemalloc)."""

import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

from .categories import categorize_collapsed, format_category_report, load_categories
from .config import SpyglassConfig
from .constants import BOLD, RESET
from .filters import compute_time_ranges, filter_collapsed_stacks


def collapse_perf_data(perf_data: Path, collapsed_path: Path):
    """Pipe perf script directly into inferno-collapse-perf.

    Avoids creating a multi-GB intermediate .perf text file.
    Sets DEBUGINFOD_URLS="" and uses --no-inline for speed.
    """
    env = {**os.environ, "DEBUGINFOD_URLS": ""}

    perf_size_mb = perf_data.stat().st_size / 1024 / 1024
    print(f"  Collapsing stacks ({perf_size_mb:.0f} MB perf.data)...")

    perf_proc = subprocess.Popen(
        ["perf", "script", "--no-inline", "-i", str(perf_data)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    out = open(collapsed_path, "w")
    try:
        collapse_proc = subprocess.Popen(
            ["inferno-collapse-perf"],
            stdin=perf_proc.stdout,
            stdout=out,
            stderr=subprocess.PIPE,
        )
        # Allow perf_proc to receive SIGPIPE if collapse_proc exits early
        perf_proc.stdout.close()

        collapse_stderr = collapse_proc.stderr.read()
        collapse_proc.wait()
        perf_proc.wait()
    finally:
        out.close()

    if perf_proc.returncode != 0:
        print(f"ERROR: perf script failed (exit code {perf_proc.returncode})", file=sys.stderr)
        sys.exit(1)
    if collapse_proc.returncode != 0:
        print(f"ERROR: inferno-collapse-perf failed: {collapse_stderr.decode()}", file=sys.stderr)
        sys.exit(1)

    size_kb = collapsed_path.stat().st_size / 1024
    print(f"    -> {collapsed_path.name} ({size_kb:.0f} KB)")


def generate_flamegraph(collapsed_path: Path, svg_path: Path):
    """Generate a flamegraph SVG from collapsed stacks."""
    print("  Generating flamegraph...")
    with open(collapsed_path) as inp, open(svg_path, "w") as out:
        result = subprocess.run(
            ["inferno-flamegraph"],
            stdin=inp,
            stdout=out,
            stderr=subprocess.PIPE,
        )
    if result.returncode != 0:
        print(f"ERROR: inferno-flamegraph failed: {result.stderr.decode()}", file=sys.stderr)
        sys.exit(1)
    size_kb = svg_path.stat().st_size / 1024
    print(f"    -> {svg_path.name} ({size_kb:.0f} KB)")


def analyze_collapsed(collapsed_path: Path) -> dict:
    """Parse a collapsed stacks file and compute self-time and inclusive-time.

    Returns dict with keys: total_samples, self_time (Counter), inclusive_time (Counter)
    """
    self_time = Counter()
    inclusive_time = Counter()
    total = 0

    with open(collapsed_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.rsplit(" ", 1)
            if len(parts) != 2:
                continue
            stack, count_str = parts
            try:
                count = int(count_str)
            except ValueError:
                continue
            total += count
            frames = stack.split(";")
            # Self time = leaf function
            self_time[frames[-1]] += count
            # Inclusive time = each unique function in the stack
            seen = set()
            for frame in frames:
                if frame not in seen:
                    inclusive_time[frame] += count
                    seen.add(frame)

    return {
        "total_samples": total,
        "self_time": self_time,
        "inclusive_time": inclusive_time,
    }


def write_analysis_md(
    analysis: dict,
    output_path: Path,
    title: str = "Profile Analysis",
    category_report: str | None = None,
    units: str = "cycles",
):
    """Write analysis results to a markdown file."""
    total = analysis["total_samples"]
    self_time = analysis["self_time"]
    inclusive_time = analysis["inclusive_time"]
    show_cycles = units != "percentages"

    lines = [
        f"# {title}",
        "",
        f"**Total samples:** {total:,}",
        "",
    ]

    # Insert category breakdown before raw function tables
    if category_report:
        lines.append(category_report)
        lines.append("")

    if show_cycles:
        lines.extend(
            [
                "## Top 30 Functions by Self Time",
                "",
                "| # | Function | Cycles | % |",
                "|---|----------|--------|---|",
            ]
        )
        for i, (func, count) in enumerate(self_time.most_common(30), 1):
            pct = 100.0 * count / total if total else 0
            display = func if len(func) <= 80 else func[:77] + "..."
            lines.append(f"| {i} | `{display}` | {count:,} | {pct:.2f}% |")

        lines.extend(
            [
                "",
                "## Top 30 Functions by Inclusive Time",
                "",
                "| # | Function | Cycles | % |",
                "|---|----------|--------|---|",
            ]
        )
        for i, (func, count) in enumerate(inclusive_time.most_common(30), 1):
            pct = 100.0 * count / total if total else 0
            display = func if len(func) <= 80 else func[:77] + "..."
            lines.append(f"| {i} | `{display}` | {count:,} | {pct:.2f}% |")
    else:
        lines.extend(
            [
                "## Top 30 Functions by Self Time",
                "",
                "| # | Function | % |",
                "|---|----------|---|",
            ]
        )
        for i, (func, count) in enumerate(self_time.most_common(30), 1):
            pct = 100.0 * count / total if total else 0
            display = func if len(func) <= 80 else func[:77] + "..."
            lines.append(f"| {i} | `{display}` | {pct:.2f}% |")

        lines.extend(
            [
                "",
                "## Top 30 Functions by Inclusive Time",
                "",
                "| # | Function | % |",
                "|---|----------|---|",
            ]
        )
        for i, (func, count) in enumerate(inclusive_time.most_common(30), 1):
            pct = 100.0 * count / total if total else 0
            display = func if len(func) <= 80 else func[:77] + "..."
            lines.append(f"| {i} | `{display}` | {pct:.2f}% |")

    lines.append("")
    output_path.write_text("\n".join(lines))
    print(f"    -> {output_path.name}")


def cmd_analyze(
    config: SpyglassConfig,
    profile_dir: Path,
    filter_mode: str = "all",
    verbose: bool = False,
    units: str = "cycles",
):
    """Analyze profiling output.

    Args:
        config: Spyglass configuration object
        profile_dir: Directory containing profiling output
        filter_mode: "all", "epoch-boundary", "mid-epoch", or "steady-state".
        verbose: Show tool output
    """
    profile_dir = Path(profile_dir).resolve()

    if not profile_dir.exists():
        print(f"ERROR: Directory not found: {profile_dir}", file=sys.stderr)
        sys.exit(1)

    perf_data = profile_dir / "perf.data"
    heap_files = list(profile_dir.glob("heap*.heap"))

    if perf_data.exists():
        if filter_mode is None:
            print("ERROR: --filter is required for CPU profiles.", file=sys.stderr)
            print(f"  Try: spyglass analyze {profile_dir} --filter epoch-boundary", file=sys.stderr)
            sys.exit(1)
        if filter_mode == "all":
            for fm in ["epoch-boundary", "mid-epoch", "steady-state"]:
                _analyze_cpu(config, profile_dir, perf_data, fm, units=units, skip_on_missing=True)
        else:
            _analyze_cpu(config, profile_dir, perf_data, filter_mode, units=units)
    elif heap_files:
        _analyze_memory(config, profile_dir, heap_files)
    else:
        print("ERROR: No perf.data or heap*.heap found in directory", file=sys.stderr)
        sys.exit(1)


def _analyze_cpu(
    config: SpyglassConfig,
    profile_dir: Path,
    perf_data: Path,
    filter_mode: str,
    units: str = "cycles",
    skip_on_missing: bool = False,
):
    """CPU profile analysis pipeline."""
    print(f"{BOLD}=== Analyze (CPU) ==={RESET}")
    print(f"  {BOLD}Filter:{RESET} {filter_mode}")
    print()

    # Step 1: Collapse stacks (full, unfiltered)
    collapsed_full = profile_dir / "profile.collapsed"
    if not collapsed_full.exists():
        collapse_perf_data(perf_data, collapsed_full)
    else:
        print(f"  Using existing {collapsed_full.name}")

    # Step 2: Apply time-based filtering
    warmup = config.filtering.epoch_boundary_warmup
    cooldown = config.filtering.epoch_boundary_cooldown
    time_ranges = compute_time_ranges(profile_dir, filter_mode, warmup, cooldown)

    if time_ranges is None:
        if skip_on_missing:
            print(f"  Skipping {filter_mode} (no epoch data available)\n")
            return
        print(f"ERROR: No data available for '{filter_mode}' filter.", file=sys.stderr)
        print(
            "  This usually means no epoch boundaries were recorded during the run.",
            file=sys.stderr,
        )
        sys.exit(1)

    view_dir = profile_dir / "views" / filter_mode.replace("-", "_")
    view_dir.mkdir(parents=True, exist_ok=True)
    collapsed_path = view_dir / "profile.collapsed"
    print(f"  Filtering to {filter_mode} time ranges...")
    filter_collapsed_stacks(collapsed_full, collapsed_path, time_ranges, perf_data)
    size_kb = collapsed_path.stat().st_size / 1024
    print(f"    -> {collapsed_path.name} ({size_kb:.0f} KB)")

    # Step 3: Analysis markdown
    print("  Analyzing stacks...")
    analysis = analyze_collapsed(collapsed_path)

    # Step 4: Category breakdown (if categories.toml exists)
    category_report = None
    categories = load_categories(config.config_dir / "categories.toml")
    if categories:
        print("  Categorizing samples...")
        cat_result = categorize_collapsed(collapsed_path, categories)
        category_report = format_category_report(cat_result, categories)

    md_path = view_dir / "analysis.md"
    write_analysis_md(
        analysis,
        md_path,
        title=f"CPU Profile Analysis ({filter_mode})",
        category_report=category_report,
        units=units,
    )

    # Step 5: Generate flamegraph SVG
    svg_path = view_dir / "flamegraph.svg"
    generate_flamegraph(collapsed_path, svg_path)

    print(f"\n{BOLD}=== Analysis complete ==={RESET}")
    print(f"  {BOLD}Analysis:{RESET}   {md_path}")
    print(f"  {BOLD}Flamegraph:{RESET} {svg_path}")
    print()


def _analyze_memory(config: SpyglassConfig, profile_dir: Path, heap_files: list[Path]):
    """Memory profile analysis pipeline."""
    print(f"{BOLD}=== Analyze (Memory) ==={RESET}")

    lighthouse_bin = config.lighthouse_binary

    if not lighthouse_bin.exists():
        print(f"ERROR: Binary not found: {lighthouse_bin}", file=sys.stderr)
        sys.exit(1)

    # Use the last heap file (final dump)
    heap_file = sorted(heap_files)[-1]
    print(f"  Heap file: {heap_file.name}")

    result = subprocess.run(
        ["jeprof", "--text", "--cum", str(lighthouse_bin), str(heap_file)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: jeprof failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    md_path = profile_dir / "heap_analysis.md"
    lines = [
        "# Memory Profile Analysis",
        "",
        "```",
        result.stdout.strip(),
        "```",
        "",
    ]
    md_path.write_text("\n".join(lines))
    print(f"    -> {md_path.name}")
    print(f"\n{BOLD}=== Analysis complete ==={RESET}\n")
