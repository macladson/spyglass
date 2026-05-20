"""Cleanup commands for spyglass artifacts."""

import shutil
from pathlib import Path

from . import config as cfg
from .constants import format_size
from .pr import CHECKOUTS_DIR


def cmd_clean(config: dict, what: str = "all", verbose: bool = False):
    """Remove spyglass artifacts.
    
    Args:
        config: Parsed config dict
        what: What to clean — "all", "checkouts", or "profiles"
    """
    project_root = Path(config.get("_config_dir", Path(__file__).resolve().parent.parent.parent))

    targets = []
    if what in ("all", "checkouts"):
        targets.append(("checkouts", project_root / CHECKOUTS_DIR))
    if what in ("all", "profiles"):
        output_dir = Path(cfg.output_dir(config))
        if not output_dir.is_absolute():
            output_dir = (project_root / output_dir).resolve()
        targets.append(("profiles", output_dir))

    if not targets:
        print("Nothing to clean.")
        return

    print("=== Clean ===")
    total_freed = 0
    for name, path in targets:
        if path.exists():
            size = _dir_size(path)
            total_freed += size
            if verbose:
                print(f"  Removing {path} ({format_size(size)})...")
            else:
                print(f"  Removing {name}/ ({format_size(size)})")
            shutil.rmtree(path)
        else:
            print(f"  {name}/ — not found, skipping")

    print(f"\n  Freed {format_size(total_freed)} total")
    print("=== Clean complete ===\n")


def _dir_size(path: Path) -> int:
    """Calculate total size of a directory in bytes."""
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
    return total



