"""Category-based sample classification for profile analysis.

Categories are defined in a TOML file and checked in priority order (first match wins).
A sample is categorized by checking if any frame in its call stack matches a category's
patterns. This measures "time spent in call paths involving X."
"""

import re
from collections import Counter
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None  # type: ignore

UNCATEGORIZED_WARNING_THRESHOLD = 0.15  # Warn if >15% uncategorized


def load_categories(categories_path: Path | None = None) -> list[dict] | None:
    """Load category definitions from a TOML file.

    Returns None if no categories file exists (categories are optional).
    Returns a list of dicts with keys: name, patterns, leaf_patterns (optional).
    """
    if tomllib is None:
        return None

    if categories_path is None:
        # Look for categories.toml at the project root
        default = Path(__file__).resolve().parent.parent.parent / "categories.toml"
        if default.exists():
            categories_path = default
        else:
            return None

    if not categories_path.exists():
        return None

    with open(categories_path, "rb") as f:
        data = tomllib.load(f)

    categories = data.get("categories", [])
    if not categories:
        return None

    # Pre-compile regex patterns
    for cat in categories:
        cat["_compiled"] = [_compile_pattern(p) for p in cat.get("patterns", [])]
        cat["_leaf_compiled"] = [_compile_pattern(p) for p in cat.get("leaf_patterns", [])]

    return categories


def _compile_pattern(pattern: str):
    """Compile a pattern into a matcher function.

    Plain strings use substring match (fast).
    Strings prefixed with "re:" use regex match.
    """
    if pattern.startswith("re:"):
        regex = re.compile(pattern[3:])
        return lambda text: regex.search(text) is not None
    else:
        return lambda text: pattern in text


def categorize_sample(
    stack: str,
    leaf: str,
    categories: list[dict],
) -> str | None:
    """Assign a sample to the first matching category.

    Args:
        stack: The full semicolon-joined stack string
        leaf: The leaf (last) frame
        categories: Priority-ordered list of category definitions

    Returns:
        Category name, or None if no match.
    """
    for cat in categories:
        # If leaf_patterns are specified, the leaf must match one of them
        # AND the stack must match a regular pattern
        if cat["_leaf_compiled"]:
            leaf_match = any(matcher(leaf) for matcher in cat["_leaf_compiled"])
            if not leaf_match:
                continue
            stack_match = any(matcher(stack) for matcher in cat["_compiled"])
            if stack_match:
                return cat["name"]
        else:
            # Just check stack patterns
            if any(matcher(stack) for matcher in cat["_compiled"]):
                return cat["name"]

    return None


def categorize_collapsed(
    collapsed_path: Path,
    categories: list[dict],
) -> dict:
    """Categorize all samples in a collapsed stacks file.

    Returns dict with:
        - category_counts: Counter of category -> sample count
        - total_samples: int
        - uncategorized_leaves: Counter of leaf function -> count (for uncategorized only)
    """
    category_counts = Counter()
    uncategorized_leaves = Counter()
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
            leaf = frames[-1]

            category = categorize_sample(stack, leaf, categories)
            if category:
                category_counts[category] += count
            else:
                category_counts["Uncategorized"] += count
                uncategorized_leaves[leaf] += count

    return {
        "category_counts": category_counts,
        "total_samples": total,
        "uncategorized_leaves": uncategorized_leaves,
    }


def format_category_report(result: dict, categories: list[dict]) -> str:
    """Format the category breakdown as markdown.

    Includes the category table, coverage info, and uncategorized top functions.
    """
    total = result["total_samples"]
    counts = result["category_counts"]
    uncategorized_leaves = result["uncategorized_leaves"]

    lines = [
        "## Category Breakdown",
        "",
        "*Pattern-based classification — first match wins. "
        "See coverage report below for uncategorized samples.*",
        "",
        "| Category | % | Samples |",
        "|----------|---|---------|",
    ]

    # Print categories in definition order, then uncategorized last
    for cat in categories:
        name = cat["name"]
        count = counts.get(name, 0)
        pct = 100.0 * count / total if total else 0
        if pct >= 0.01:
            lines.append(f"| {name} | {pct:.2f}% | {count:,} |")

    uncategorized_count = counts.get("Uncategorized", 0)
    uncategorized_pct = 100.0 * uncategorized_count / total if total else 0
    lines.append(f"| *Uncategorized* | *{uncategorized_pct:.2f}%* | *{uncategorized_count:,}* |")

    # Coverage report
    categorized_pct = 100.0 - uncategorized_pct
    lines.extend([
        "",
        f"**Coverage:** {categorized_pct:.1f}% of samples categorized",
    ])

    if uncategorized_pct > UNCATEGORIZED_WARNING_THRESHOLD * 100:
        lines.append(
            f"\n⚠️  **WARNING:** {uncategorized_pct:.1f}% of samples are uncategorized. "
            "Consider reviewing patterns."
        )

    # Top uncategorized functions
    if uncategorized_leaves:
        lines.extend([
            "",
            "### Uncategorized Top Functions",
            "",
            "| Function | % | Suggestion |",
            "|----------|---|------------|",
        ])
        for func, count in uncategorized_leaves.most_common(10):
            pct = 100.0 * count / total if total else 0
            suggestion = _suggest_category(func, categories)
            display = func if len(func) <= 60 else func[:57] + "..."
            lines.append(f"| `{display}` | {pct:.2f}% | {suggestion} |")

    lines.append("")
    return "\n".join(lines)


def _suggest_category(func: str, categories: list[dict]) -> str:
    """Suggest which category an uncategorized function might belong to."""
    # Simple heuristics based on common patterns
    func_lower = func.lower()

    if any(x in func_lower for x in ["mul", "add", "sub", "mod_384", "sqr", "mont"]):
        return "likely BLS/Crypto"
    if any(x in func_lower for x in ["tokio", "mio", "poll", "future"]):
        return "runtime overhead"
    if any(x in func_lower for x in ["alloc", "malloc", "free", "rjem"]):
        return "allocator"
    if any(x in func_lower for x in ["prometheus", "metric"]):
        return "metrics"
    if "[libc" in func or "[unknown]" in func or "[vdso]" in func:
        return "system/kernel"
    if any(x in func_lower for x in ["hash", "sip"]):
        return "hashing"

    return ""
