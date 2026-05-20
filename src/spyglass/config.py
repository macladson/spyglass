"""Configuration loading and path resolution."""

import os
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    print(
        "ERROR: This tool requires Python 3.11+ (for tomllib).\n"
        "Please upgrade your Python installation.",
        file=sys.stderr,
    )
    sys.exit(1)

DEFAULT_CONFIG_NAME = "config.toml"


def default_config_path() -> Path:
    """Return the default config path (project root, alongside pyproject.toml)."""
    # src/spyglass/config.py → src/spyglass → src → project root
    return Path(__file__).resolve().parent.parent.parent / DEFAULT_CONFIG_NAME


def load_config(config_path: Path | None = None) -> dict:
    """Load and parse the TOML configuration file."""
    path = config_path or default_config_path()
    if not path.exists():
        print(f"ERROR: Config file not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, "rb") as f:
        config = tomllib.load(f)
    # Store config file location for relative path resolution
    config["_config_dir"] = str(path.resolve().parent)
    return config


def resolve_lighthouse_dir(config: dict) -> Path:
    """Resolve the lighthouse directory from config.

    Supports:
      - Absolute paths: /home/user/work/lighthouse
      - Home expansion: ~/work/lighthouse
      - Relative paths: resolved relative to the config file's directory
    """
    raw = config.get("paths", {}).get("lighthouse_dir", ".")
    expanded = Path(os.path.expanduser(raw))
    if expanded.is_absolute():
        return expanded.resolve()
    config_dir = Path(config.get("_config_dir", str(Path(__file__).resolve().parent.parent.parent)))
    return (config_dir / expanded).resolve()


def get(config: dict, section: str, key: str, default=None):
    """Get a config value with a default."""
    return config.get(section, {}).get(key, default)


# Convenience accessors
def profile(config: dict) -> str:
    return get(config, "profiling", "profile", "release")


def lighthouse_binary(config: dict) -> Path:
    """Resolve the path to the lighthouse binary."""
    lh_dir = resolve_lighthouse_dir(config)
    return lh_dir / "target" / profile(config) / "lighthouse"


def lcli_binary(config: dict) -> Path:
    """Resolve the path to the lcli binary."""
    lh_dir = resolve_lighthouse_dir(config)
    return lh_dir / "target" / profile(config) / "lcli"


def duration(config: dict) -> int:
    return get(config, "profiling", "duration_seconds", 1800)


def perf_frequency(config: dict) -> int:
    return get(config, "profiling", "perf_frequency", 1000)


def disable_backfill(config: dict) -> bool:
    return get(config, "profiling", "disable_backfill", True)


def output_dir(config: dict) -> str:
    return get(config, "profiling", "output_dir", "./profiles")


def nickname(config: dict) -> str:
    return get(config, "profiling", "nickname", "")


def checkpoint_sync_url(config: dict) -> str:
    return get(config, "lighthouse", "checkpoint_sync_url", "https://mainnet.checkpoint.sigp.io")


def network(config: dict) -> str:
    return get(config, "lighthouse", "network", "mainnet")


def extra_flags(config: dict) -> list[str]:
    return get(config, "lighthouse", "extra_flags", [])


def http_port(config: dict) -> int:
    return get(config, "lighthouse", "http_port", 5052)


def metrics_port(config: dict) -> int:
    return get(config, "lighthouse", "metrics_port", 5054)


def mock_el_address(config: dict) -> str:
    return get(config, "mock_el", "listen_address", "127.0.0.1")


def mock_el_port(config: dict) -> int:
    return get(config, "mock_el", "listen_port", 8551)


def epoch_boundary_warmup(config: dict) -> float:
    return get(config, "filtering", "epoch_boundary_warmup", 15)


def epoch_boundary_cooldown(config: dict) -> float:
    return get(config, "filtering", "epoch_boundary_cooldown", 15)


def max_wait_seconds(config: dict) -> int:
    return get(config, "filtering", "max_wait_seconds", 7200)


def resolve_output_path(
    config: dict,
    mode: str,
    filter_mode: str,
    output_dir_override: str | None = None,
    nickname_override: str | None = None,
) -> Path:
    """Construct the full output path: <output_dir>/<nickname_or_hash>/<mode>/<filter>/
    
    Args:
        config: Parsed config
        mode: "cpu" or "memory"
        filter_mode: "all", "epoch-boundary", "mid-epoch", "steady-state"
        output_dir_override: Override output_dir from config
        nickname_override: Override nickname from config (CLI --nickname)
    """
    base = Path(os.path.expanduser(output_dir_override or output_dir(config)))
    if not base.is_absolute():
        config_dir = Path(config.get("_config_dir", str(Path(__file__).resolve().parent.parent.parent)))
        base = (config_dir / base).resolve()

    run_name = nickname_override or nickname(config)
    if not run_name:
        # Auto-detect from lighthouse binary
        run_name = _get_lighthouse_commit(config)

    # Normalize filter name for filesystem
    filter_dir = filter_mode.replace("-", "_")

    return base / run_name / mode / filter_dir


def _get_lighthouse_commit(config: dict) -> str:
    """Get the commit hash from the lighthouse binary."""
    import subprocess
    binary = lighthouse_binary(config)
    if not binary.exists():
        return "unknown"
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True, text=True, timeout=5,
        )
        # Output like: "Lighthouse/v8.1.3-a3f2b1c"
        version_str = result.stdout.strip()
        if "-" in version_str:
            # Extract hash after last hyphen
            commit = version_str.rsplit("-", 1)[-1]
            # Clean up any trailing whitespace or extra info
            commit = commit.split()[0] if commit else "unknown"
            return commit
        return version_str.replace("/", "_").replace(" ", "_") or "unknown"
    except Exception:
        return "unknown"
