"""Cleanup commands for spyglass artifacts."""

import shutil
import sys
from pathlib import Path

from .config import SpyglassConfig
from .constants import format_size, log, log_end, log_start, log_step
from .pr import CHECKOUTS_DIR


def cmd_clean(
    config: SpyglassConfig, what: str = "all", verbose: bool = False, force: bool = False
):
    """Remove spyglass artifacts.

    Args:
        config: Spyglass configuration object
        what: What to clean — "all", "checkouts", or "profiles"
        force: Skip confirmation prompt for profiles
    """
    project_root = config.config_dir

    targets = []
    if what in ("all", "checkouts"):
        targets.append(("checkouts", project_root / CHECKOUTS_DIR))
    if what in ("all", "profiles"):
        output_dir = Path(config.profiling.output_dir)
        if not output_dir.is_absolute():
            output_dir = (project_root / output_dir).resolve()
        targets.append(("profiles", output_dir))

    if not targets:
        print("Nothing to clean.")
        return

    # Confirm before deleting profiles (user-configured path)
    if not force:
        profile_targets = [(n, p) for n, p in targets if n == "profiles" and p.exists()]
        if profile_targets:
            path = profile_targets[0][1]
            size = _dir_size(path)
            print(f"This will delete {path} ({format_size(size)})")
            try:
                answer = input("Continue? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer not in ("y", "yes"):
                print("Aborted.")
                return

    log_start("clean")
    total_freed = 0
    for name, path in targets:
        if path.exists():
            size = _dir_size(path)
            log_step(f"{name}/ ({format_size(size)})")
            try:
                shutil.rmtree(path)
                total_freed += size
            except OSError as e:
                print(f"  WARNING: Failed to remove {path}: {e}", file=sys.stderr)
        else:
            log(f"{name}/ — not found, skipping")

    log_end(f"freed {format_size(total_freed)}")


def _dir_size(path: Path) -> int:
    """Calculate total size of a directory in bytes."""
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
    return total
