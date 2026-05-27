"""Compare two profiling runs."""

import shutil
import subprocess
import sys
from pathlib import Path

from .analyze import analyze_collapsed
from .categories import categorize_collapsed, load_categories
from .constants import log, log_end, log_start, log_step


def cmd_compare(config, dir_a: Path, dir_b: Path, filter_mode: str = "all", units: str = "cycles"):
    """Compare two profile directories and produce a comparison markdown.

    Args:
        config: Spyglass configuration object
        dir_a: Baseline profile directory
        dir_b: Comparison profile directory
        filter_mode: Which filtered view to compare ("all" compares every available view)
        units: "pct" for percentages, "cycles" for absolute cycle counts
    """
    dir_a = Path(dir_a).resolve()
    dir_b = Path(dir_b).resolve()

    if filter_mode == "all":
        views_a = {d.name for d in (dir_a / "views").iterdir() if d.is_dir()} if (dir_a / "views").is_dir() else set()
        views_b = {d.name for d in (dir_b / "views").iterdir() if d.is_dir()} if (dir_b / "views").is_dir() else set()
        common = sorted(views_a & views_b)
        if not common:
            print("ERROR: No common views found. Run `spyglass analyze` on both profiles first.", file=sys.stderr)
            sys.exit(1)
        for view in common:
            cmd_compare(config, dir_a, dir_b, filter_mode=view.replace("_", "-"), units=units)
        return

    view_name = filter_mode.replace("-", "_")
    collapsed_a = dir_a / "views" / view_name / "profile.collapsed"
    collapsed_b = dir_b / "views" / view_name / "profile.collapsed"

    if not collapsed_a.exists():
        print(
            f"ERROR: {collapsed_a} not found. Run `spyglass analyze {dir_a} --filter {filter_mode}` first.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not collapsed_b.exists():
        print(
            f"ERROR: {collapsed_b} not found. Run `spyglass analyze {dir_b} --filter {filter_mode}` first.",
            file=sys.stderr,
        )
        sys.exit(1)

    name_a = dir_a.parent.name
    name_b = dir_b.parent.name
    log_start("compare", f"{name_a} vs {name_b} / {filter_mode}")

    analysis_a = analyze_collapsed(collapsed_a)
    analysis_b = analyze_collapsed(collapsed_b)

    total_a = analysis_a["total_samples"]
    total_b = analysis_b["total_samples"]
    self_a = analysis_a["self_time"]
    self_b = analysis_b["self_time"]

    lines = [
        "# Profile Comparison",
        "",
        f"- **Baseline:** `{dir_a.name}` ({total_a:,} samples)",
        f"- **Comparison:** `{dir_b.name}` ({total_b:,} samples)",
        f"- **Filter:** {filter_mode}",
        "",
    ]

    show_cycles = units != "percentages"

    # Category comparison (if categories.toml exists)
    categories_path = config.config_dir / "categories.toml"
    categories = load_categories(categories_path)
    if categories:
        cat_a = categorize_collapsed(collapsed_a, categories)
        cat_b = categorize_collapsed(collapsed_b, categories)

        lines.extend(
            [
                "## Category Comparison",
                "",
                "*Pattern-based classification — first match wins.*",
                "",
            ]
        )

        all_cats = [cat["name"] for cat in categories] + ["Uncategorized"]
        if show_cycles:
            lines.extend(
                [
                    "| Category | Baseline | Comparison | Delta |",
                    "|----------|----------|------------|-------|",
                ]
            )
            for name in all_cats:
                count_a = cat_a["category_counts"].get(name, 0)
                count_b = cat_b["category_counts"].get(name, 0)
                delta = count_b - count_a
                if count_a > 0 or count_b > 0:
                    delta_str = f"+{delta:,}" if delta >= 0 else f"{delta:,}"
                    lines.append(
                        f"| {name} | {count_a:,} | {count_b:,} | {delta_str} |"
                    )
        else:
            lines.extend(
                [
                    "| Category | Baseline | Comparison | Delta |",
                    "|----------|----------|------------|-------|",
                ]
            )
            for name in all_cats:
                count_a = cat_a["category_counts"].get(name, 0)
                count_b = cat_b["category_counts"].get(name, 0)
                pct_a = 100.0 * count_a / total_a if total_a else 0
                pct_b = 100.0 * count_b / total_b if total_b else 0
                delta = pct_b - pct_a
                if pct_a >= 0.01 or pct_b >= 0.01:
                    delta_str = f"+{delta:.2f}" if delta >= 0 else f"{delta:.2f}"
                    lines.append(
                        f"| {name} | {pct_a:.2f}% | {pct_b:.2f}% | {delta_str}% |"
                    )

        lines.append("")

    # Function-level comparison
    top_funcs = set()
    for func, _ in self_a.most_common(30):
        top_funcs.add(func)
    for func, _ in self_b.most_common(30):
        top_funcs.add(func)

    if show_cycles:
        rows = []
        for func in top_funcs:
            ca = self_a.get(func, 0)
            cb = self_b.get(func, 0)
            rows.append((func, ca, cb, cb - ca))
        rows.sort(key=lambda r: max(r[1], r[2]), reverse=True)

        lines.extend(
            [
                "## Top Functions by Self Time",
                "",
                "| Function | Baseline | Comparison | Delta |",
                "|----------|----------|------------|-------|",
            ]
        )
        for func, ca, cb, delta in rows[:40]:
            display = func if len(func) <= 60 else func[:57] + "..."
            delta_str = f"+{delta:,}" if delta >= 0 else f"{delta:,}"
            lines.append(f"| `{display}` | {ca:,} | {cb:,} | {delta_str} |")
    else:
        rows = []
        for func in top_funcs:
            pct_a = 100.0 * self_a.get(func, 0) / total_a if total_a else 0
            pct_b = 100.0 * self_b.get(func, 0) / total_b if total_b else 0
            delta = pct_b - pct_a
            rows.append((func, pct_a, pct_b, delta))
        rows.sort(key=lambda r: max(r[1], r[2]), reverse=True)

        lines.extend(
            [
                "## Top Functions by Self Time",
                "",
                "| Function | Baseline | Comparison | Delta |",
                "|----------|----------|------------|-------|",
            ]
        )
        for func, pct_a, pct_b, delta in rows[:40]:
            display = func if len(func) <= 60 else func[:57] + "..."
            delta_str = f"+{delta:.2f}" if delta >= 0 else f"{delta:.2f}"
            lines.append(f"| `{display}` | {pct_a:.2f}% | {pct_b:.2f}% | {delta_str}% |")

    lines.append("")

    view_dir = dir_b / "views" / view_name
    view_dir.mkdir(parents=True, exist_ok=True)
    output = view_dir / "comparison.md"
    output.write_text("\n".join(lines))
    log_step(f"comparison.md")

    # Generate differential flamegraph
    diff_svg = _generate_diff_flamegraph(collapsed_a, collapsed_b, view_dir)
    if diff_svg:
        log_step(f"diff.svg")

    log_end(f"done → {view_dir}")


def _generate_diff_flamegraph(
    collapsed_a: Path, collapsed_b: Path, output_dir: Path
) -> Path | None:
    """Generate a differential flamegraph SVG from two collapsed stack files.

    Uses inferno-diff-folded to compute the diff, then inferno-flamegraph to render.
    Red = grew in B relative to A, blue = shrank.

    Returns the output path, or None if the required tools aren't available.
    """
    if shutil.which("inferno-diff-folded") is None:
        print("  (skipping diff flamegraph — inferno-diff-folded not found)")
        return None

    svg_path = output_dir / "diff.svg"

    diff_proc = subprocess.Popen(
        ["inferno-diff-folded", "--normalize", str(collapsed_a), str(collapsed_b)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    with open(svg_path, "w") as out:
        fg_proc = subprocess.run(
            ["inferno-flamegraph", "--negate"],
            stdin=diff_proc.stdout,
            stdout=out,
            stderr=subprocess.PIPE,
        )

    diff_proc.stdout.close()
    diff_proc.wait()

    if diff_proc.returncode != 0 or fg_proc.returncode != 0:
        stderr = fg_proc.stderr.decode()
        print(f"  WARNING: Diff flamegraph generation failed: {stderr.strip()}", file=sys.stderr)
        svg_path.unlink(missing_ok=True)
        return None

    return svg_path
