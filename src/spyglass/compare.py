"""Compare two profiling runs."""

import sys
from pathlib import Path

from .analyze import analyze_collapsed
from .categories import load_categories, categorize_collapsed
from .constants import BOLD, RESET


def cmd_compare(config, dir_a: Path, dir_b: Path, filter_mode: str = "all"):
    """Compare two profile directories and produce a comparison markdown.
    
    Args:
        config: Spyglass configuration object
        dir_a: Baseline profile directory
        dir_b: Comparison profile directory
        filter_mode: Which collapsed file to use (matches suffix convention)
    """
    dir_a = Path(dir_a).resolve()
    dir_b = Path(dir_b).resolve()

    suffix = "" if filter_mode == "all" else f"_{filter_mode.replace('-', '_')}"
    collapsed_a = dir_a / f"profile{suffix}.collapsed"
    collapsed_b = dir_b / f"profile{suffix}.collapsed"

    if not collapsed_a.exists():
        print(f"ERROR: {collapsed_a} not found. Run `analyze` first.", file=sys.stderr)
        sys.exit(1)
    if not collapsed_b.exists():
        print(f"ERROR: {collapsed_b} not found. Run `analyze` first.", file=sys.stderr)
        sys.exit(1)

    print(f"{BOLD}=== Compare ==={RESET}")
    print(f"  {BOLD}Baseline:{RESET}   {dir_a.name}")
    print(f"  {BOLD}Comparison:{RESET} {dir_b.name}")
    print(f"  {BOLD}Filter:{RESET}     {filter_mode}")
    print()

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

    # Category comparison (if categories.toml exists)
    categories_path = config.config_dir / "categories.toml"
    categories = load_categories(categories_path)
    if categories:
        cat_a = categorize_collapsed(collapsed_a, categories)
        cat_b = categorize_collapsed(collapsed_b, categories)

        lines.extend([
            "## Category Comparison",
            "",
            "*Pattern-based classification — first match wins.*",
            "",
            "| Category | Baseline | Comparison | Delta |",
            "|----------|----------|------------|-------|",
        ])

        # Collect all category names in definition order
        all_cats = [cat["name"] for cat in categories] + ["Uncategorized"]
        for name in all_cats:
            count_a = cat_a["category_counts"].get(name, 0)
            count_b = cat_b["category_counts"].get(name, 0)
            pct_a = 100.0 * count_a / total_a if total_a else 0
            pct_b = 100.0 * count_b / total_b if total_b else 0
            delta = pct_b - pct_a
            if pct_a >= 0.01 or pct_b >= 0.01:
                delta_str = f"+{delta:.2f}" if delta >= 0 else f"{delta:.2f}"
                lines.append(f"| {name} | {pct_a:.2f}% | {pct_b:.2f}% | {delta_str}% |")

        lines.append("")

    # Function-level comparison
    top_funcs = set()
    for func, _ in self_a.most_common(30):
        top_funcs.add(func)
    for func, _ in self_b.most_common(30):
        top_funcs.add(func)

    rows = []
    for func in top_funcs:
        pct_a = 100.0 * self_a.get(func, 0) / total_a if total_a else 0
        pct_b = 100.0 * self_b.get(func, 0) / total_b if total_b else 0
        delta = pct_b - pct_a
        rows.append((func, pct_a, pct_b, delta))

    rows.sort(key=lambda r: max(r[1], r[2]), reverse=True)

    lines.extend([
        "## Top Functions by Self Time",
        "",
        "| Function | Baseline | Comparison | Delta |",
        "|----------|----------|------------|-------|",
    ])

    for func, pct_a, pct_b, delta in rows[:40]:
        display = func if len(func) <= 60 else func[:57] + "..."
        delta_str = f"+{delta:.2f}" if delta >= 0 else f"{delta:.2f}"
        lines.append(f"| `{display}` | {pct_a:.2f}% | {pct_b:.2f}% | {delta_str}% |")

    lines.append("")

    output = dir_b / f"comparison{suffix}.md"
    output.write_text("\n".join(lines))
    print(f"  {BOLD}Written:{RESET} {output}")
    print(f"\n{BOLD}=== Compare complete ==={RESET}\n")
