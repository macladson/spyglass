"""Configuration loading and path resolution."""

import os
import subprocess
import sys
from dataclasses import dataclass, field
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


def _default_config_path() -> Path:
    """Return the default config path (project root, alongside pyproject.toml)."""
    # src/spyglass/config.py → src/spyglass → src → project root
    return Path(__file__).resolve().parent.parent.parent / DEFAULT_CONFIG_NAME


@dataclass
class PathsConfig:
    """Paths configuration section."""

    lighthouse_dir: Path

    @classmethod
    def from_toml(cls, data: dict, config_dir: Path) -> "PathsConfig":
        raw = data.get("lighthouse_dir", ".")
        expanded = Path(os.path.expanduser(raw))
        if expanded.is_absolute():
            resolved = expanded.resolve()
        else:
            resolved = (config_dir / expanded).resolve()
        return cls(lighthouse_dir=resolved)


@dataclass
class LighthouseConfig:
    """Lighthouse node configuration section."""

    network: str = "mainnet"
    checkpoint_sync_url: str = "https://mainnet.checkpoint.sigp.io"
    extra_flags: list[str] = field(default_factory=list)
    http_port: int = 5052
    metrics_port: int = 5054

    @classmethod
    def from_toml(cls, data: dict) -> "LighthouseConfig":
        return cls(
            network=data.get("network", cls.network),
            checkpoint_sync_url=data.get("checkpoint_sync_url", cls.checkpoint_sync_url),
            extra_flags=data.get("extra_flags", []),
            http_port=data.get("http_port", cls.http_port),
            metrics_port=data.get("metrics_port", cls.metrics_port),
        )

    @property
    def beacon_url(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"


@dataclass
class ProfilingConfig:
    """Profiling configuration section."""

    perf_frequency: int = 1000
    profile: str = "release"
    disable_backfill: bool = True
    output_dir: str = "./profiles"
    nickname: str = ""
    start_slot: int = 16
    end_slot: int = 15
    safety_timeout: int = 7200

    @classmethod
    def from_toml(cls, data: dict) -> "ProfilingConfig":
        return cls(
            perf_frequency=data.get("perf_frequency", cls.perf_frequency),
            profile=data.get("profile", cls.profile),
            disable_backfill=data.get("disable_backfill", cls.disable_backfill),
            output_dir=data.get("output_dir", cls.output_dir),
            nickname=data.get("nickname", cls.nickname),
            start_slot=data.get("start_slot", cls.start_slot),
            end_slot=data.get("end_slot", cls.end_slot),
            safety_timeout=data.get("safety_timeout", cls.safety_timeout),
        )


@dataclass
class FilteringConfig:
    """Filtering configuration section."""

    epoch_boundary_warmup: float = 6.0
    epoch_boundary_cooldown: float = 6.0

    @classmethod
    def from_toml(cls, data: dict) -> "FilteringConfig":
        return cls(
            epoch_boundary_warmup=data.get("epoch_boundary_warmup", cls.epoch_boundary_warmup),
            epoch_boundary_cooldown=data.get(
                "epoch_boundary_cooldown", cls.epoch_boundary_cooldown
            ),
        )


@dataclass
class MockElConfig:
    """Mock execution layer configuration section."""

    listen_address: str = "127.0.0.1"
    listen_port: int = 8551

    @classmethod
    def from_toml(cls, data: dict) -> "MockElConfig":
        return cls(
            listen_address=data.get("listen_address", cls.listen_address),
            listen_port=data.get("listen_port", cls.listen_port),
        )


@dataclass
class SpyglassConfig:
    """Top-level configuration object for Spyglass.

    Provides typed, IDE-friendly access to all configuration values.
    """

    paths: PathsConfig
    lighthouse: LighthouseConfig
    profiling: ProfilingConfig
    filtering: FilteringConfig
    mock_el: MockElConfig
    config_dir: Path
    pr_number: int | None = None

    @property
    def lighthouse_binary(self) -> Path:
        """Resolve the path to the lighthouse binary."""
        return self.paths.lighthouse_dir / "target" / self.profiling.profile / "lighthouse"

    @property
    def lcli_binary(self) -> Path:
        """Resolve the path to the lcli binary."""
        return self.paths.lighthouse_dir / "target" / self.profiling.profile / "lcli"


def load_config(config_path: Path | None = None) -> "SpyglassConfig":
    """Load and parse the TOML configuration file into a SpyglassConfig object."""
    path = config_path or _default_config_path()
    if not path.exists():
        print(f"ERROR: Config file not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    config_dir = path.resolve().parent

    config = SpyglassConfig(
        paths=PathsConfig.from_toml(raw.get("paths", {}), config_dir),
        lighthouse=LighthouseConfig.from_toml(raw.get("lighthouse", {})),
        profiling=ProfilingConfig.from_toml(raw.get("profiling", {})),
        filtering=FilteringConfig.from_toml(raw.get("filtering", {})),
        mock_el=MockElConfig.from_toml(raw.get("mock_el", {})),
        config_dir=config_dir,
    )
    _validate_config(config)
    return config


def _validate_config(config: "SpyglassConfig"):
    """Validate configuration values."""
    errors = []
    p = config.profiling
    if not (0 <= p.start_slot < 32):
        errors.append(f"profiling.start_slot must be 0-31, got {p.start_slot}")
    if not (0 <= p.end_slot < 32):
        errors.append(f"profiling.end_slot must be 0-31, got {p.end_slot}")
    if p.perf_frequency <= 0:
        errors.append(f"profiling.perf_frequency must be positive, got {p.perf_frequency}")
    if config.lighthouse.http_port <= 0 or config.lighthouse.http_port > 65535:
        errors.append(f"lighthouse.http_port must be 1-65535, got {config.lighthouse.http_port}")
    if config.lighthouse.metrics_port <= 0 or config.lighthouse.metrics_port > 65535:
        errors.append(
            f"lighthouse.metrics_port must be 1-65535, got {config.lighthouse.metrics_port}"
        )
    if config.mock_el.listen_port <= 0 or config.mock_el.listen_port > 65535:
        errors.append(f"mock_el.listen_port must be 1-65535, got {config.mock_el.listen_port}")
    if errors:
        print("ERROR: Invalid configuration:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)


def resolve_output_path(
    config: "SpyglassConfig",
    mode: str,
    output_dir_override: str | None = None,
    nickname_override: str | None = None,
) -> Path:
    """Construct the full output path: <output_dir>/<nickname_or_hash>/<mode>/

    Args:
        config: Parsed SpyglassConfig
        mode: "cpu" or "memory"
        output_dir_override: Override output_dir from config
        nickname_override: Override nickname from config (CLI --nickname)
    """
    base = Path(os.path.expanduser(output_dir_override or config.profiling.output_dir))
    if not base.is_absolute():
        base = (config.config_dir / base).resolve()

    run_name = nickname_override or config.profiling.nickname
    if not run_name:
        # Auto-detect from git branch name in the lighthouse repo
        run_name = _get_lighthouse_branch(config)

    return base / run_name / mode


def _get_lighthouse_branch(config: "SpyglassConfig") -> str:
    """Get the current git branch name from the lighthouse repo."""
    lh_dir = config.paths.lighthouse_dir
    if not lh_dir.exists():
        return "unknown"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=lh_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            # HEAD means detached — fall back to short commit hash
            if branch == "HEAD":
                return get_lighthouse_commit_hash(config) or "unknown"
            # Sanitize branch name for use as directory name
            return branch.replace("/", "_").replace(" ", "_") or "unknown"
    except Exception:
        pass
    return "unknown"


def get_lighthouse_commit_hash(config: "SpyglassConfig") -> str | None:
    """Get the short git commit hash from the lighthouse repo."""
    lh_dir = config.paths.lighthouse_dir
    if not lh_dir.exists():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=lh_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except Exception:
        pass
    return None
