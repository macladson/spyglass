"""Shared constants for the Lighthouse profiler."""

SLOTS_PER_EPOCH = 32
SECONDS_PER_SLOT = 12

# ANSI escape codes
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"
RESET = "\033[0m"


def log_start(command: str, detail: str = ""):
    detail_str = f" ({detail})" if detail else ""
    print(f"┌ {command}{detail_str}")


def log(msg: str = ""):
    print(f"│ {msg}" if msg else "│")


def log_step(msg: str):
    print(f"│ → {msg}")


def log_end(msg: str = "done"):
    print(f"└ {msg}")
    print()


def format_size(size_bytes: int) -> str:
    """Format bytes as human-readable size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f} MB"
    else:
        return f"{size_bytes / 1024 / 1024 / 1024:.2f} GB"
