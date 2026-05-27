"""CLI entry point for the Lighthouse profiling tool."""

import argparse
import sys
from pathlib import Path

from . import __version__
from .analyze import cmd_analyze
from .build import cmd_build
from .clean import cmd_clean
from .compare import cmd_compare
from .config import load_config, resolve_output_path
from .export import cmd_export
from .flamechart import cmd_flamechart
from .run import cmd_run


def _resolve_profile_dirs(config, target: str) -> list[Path]:
    """Resolve a target (nickname or path) to one or more profile directories.

    If target is an existing directory, use it directly.
    Otherwise treat it as a nickname and look under <output_dir>/<nickname>/ for
    cpu/ and memory/ subdirectories.
    """
    target_path = Path(target)
    if target_path.is_dir():
        return [target_path]

    import os

    base = Path(os.path.expanduser(config.profiling.output_dir))
    if not base.is_absolute():
        base = (config.config_dir / base).resolve()
    nickname_dir = base / target

    if not nickname_dir.is_dir():
        print(f"ERROR: Not found: {target_path} or {nickname_dir}", file=sys.stderr)
        sys.exit(1)

    dirs = []
    for mode in ("cpu", "memory"):
        mode_dir = nickname_dir / mode
        if mode_dir.is_dir():
            dirs.append(mode_dir)

    if not dirs:
        print(f"ERROR: No cpu/ or memory/ directory found in {nickname_dir}", file=sys.stderr)
        sys.exit(1)

    return dirs


def main():
    # Shared parent parser for global flags (--config, --verbose)
    # This allows them to appear before OR after the subcommand.
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument(
        "--config",
        "-c",
        type=Path,
        default=None,
        help="Path to config file (default: config.toml in project directory)",
    )
    parent_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Show build output and lighthouse logs (default: silenced)",
    )
    parent_parser.add_argument(
        "--nickname",
        "-n",
        type=str,
        default=None,
        help="Run nickname (overrides config; used as output subdirectory name)",
    )
    parent_parser.add_argument(
        "--pr",
        type=int,
        default=None,
        help="Fetch and profile a GitHub PR by number (creates a git worktree)",
    )

    parser = argparse.ArgumentParser(
        prog="spyglass",
        description="Lighthouse profiling tool — build, run, analyze, and compare profiles.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[parent_parser],
        epilog="""\nExamples:
  %(prog)s profile --mode cpu -n my-test
  %(prog)s profile --mode cpu --pr 6789
  %(prog)s build --mode cpu
  %(prog)s run --mode cpu -n my-test
  %(prog)s analyze ./profiles/my-test/cpu --filter epoch-boundary
  %(prog)s export ./profiles/my-test/cpu perf-script --filter epoch-boundary
  %(prog)s compare ./profiles/baseline/cpu ./profiles/opt/cpu --filter epoch-boundary
  %(prog)s clean
""",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- build ---
    build_parser = subparsers.add_parser(
        "build", help="Build Lighthouse with profiling support", parents=[parent_parser]
    )
    build_parser.add_argument(
        "--mode",
        "-m",
        choices=["cpu", "memory"],
        default="cpu",
        help="Profiling mode (default: cpu)",
    )

    # Shared args for run and profile commands
    run_profile_parent = argparse.ArgumentParser(add_help=False)
    run_profile_parent.add_argument(
        "--mode", "-m", choices=["cpu", "memory"], required=True, help="Profiling mode"
    )
    run_profile_parent.add_argument(
        "--output-dir", "-o", type=str, default=None, help="Base output directory"
    )
    run_profile_parent.add_argument(
        "--duration", "-d", type=int, default=None, help="Duration in seconds (safety timeout)"
    )
    run_profile_parent.add_argument(
        "--epochs", "-e", type=int, default=1, help="Number of epochs to capture (default: 1)"
    )
    run_profile_parent.add_argument(
        "--force", action="store_true", default=False, help="Overwrite existing output directory"
    )

    # --- run ---
    run_parser = subparsers.add_parser(
        "run", help="Run Lighthouse under a profiler", parents=[parent_parser, run_profile_parent]
    )
    run_parser.add_argument(
        "--attach",
        action="store_true",
        default=False,
        help="Attach to an existing Lighthouse process (skip build/startup)",
    )
    run_parser.add_argument(
        "--pid",
        type=int,
        default=None,
        help="Lighthouse PID to attach to (default: resolved from lighthouse.service in config)",
    )

    # --- analyze ---
    analyze_parser = subparsers.add_parser(
        "analyze", help="Analyze profiling output", parents=[parent_parser]
    )
    analyze_parser.add_argument(
        "target",
        type=str,
        help="Nickname (e.g. pr-1234) or path to profile directory",
    )
    analyze_parser.add_argument(
        "--filter",
        "-f",
        choices=["all", "epoch-boundary", "mid-epoch", "steady-state"],
        default=None,
        help="Time-based filter for samples (required for CPU profiles)",
    )
    analyze_parser.add_argument(
        "--units",
        choices=["cycles", "percentages", "pct"],
        default="cycles",
        help="Display units: cycles (default) or percentages (relative %%)",
    )

    # --- compare ---
    compare_parser = subparsers.add_parser(
        "compare", help="Compare two profile runs", parents=[parent_parser]
    )
    compare_parser.add_argument("dir_a", type=Path, help="Baseline profile directory")
    compare_parser.add_argument("dir_b", type=Path, help="Comparison profile directory")
    compare_parser.add_argument(
        "--filter",
        "-f",
        choices=["all", "epoch-boundary", "mid-epoch", "steady-state"],
        required=True,
        help="Which filtered view to compare",
    )
    compare_parser.add_argument(
        "--units",
        choices=["cycles", "percentages", "pct"],
        default="cycles",
        help="Display units: cycles (default) or percentages (relative %% + deltas)",
    )

    # --- flamechart ---
    flamechart_parser = subparsers.add_parser(
        "flamechart", help="Generate interactive flame chart HTML", parents=[parent_parser]
    )
    flamechart_parser.add_argument(
        "directory",
        type=Path,
        help="Profile output directory to visualize",
    )
    flamechart_parser.add_argument(
        "--bin-size",
        "-b",
        type=float,
        default=0.5,
        help="Time bin width in seconds (default: 0.5)",
    )

    # --- export ---
    export_parser = subparsers.add_parser(
        "export", help="Export profile in various formats", parents=[parent_parser]
    )
    export_parser.add_argument(
        "directory",
        type=Path,
        help="Profile output directory containing perf.data",
    )
    export_parser.add_argument(
        "format",
        nargs="?",
        choices=["perf-script", "flamegraph", "flamechart"],
        default="perf-script",
        help="Export format (default: perf-script)",
    )
    export_parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output file path (default: auto-generated)",
    )
    export_parser.add_argument(
        "--filter",
        "-f",
        choices=["all", "epoch-boundary", "mid-epoch", "steady-state"],
        required=True,
        help="Time filter to apply",
    )
    export_parser.add_argument(
        "--bin-size",
        "-b",
        type=float,
        default=0.5,
        help="Bin size for flamechart format (default: 0.5s)",
    )

    # --- clean ---
    clean_parser = subparsers.add_parser(
        "clean", help="Remove profiling artifacts", parents=[parent_parser]
    )
    clean_parser.add_argument(
        "what",
        nargs="?",
        choices=["all", "checkouts", "profiles"],
        default="checkouts",
        help="What to remove (default: checkouts)",
    )
    clean_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Skip confirmation prompt when deleting profiles",
    )

    # --- profile (convenience) ---
    subparsers.add_parser(
        "profile", help="Build + run in one step", parents=[parent_parser, run_profile_parent]
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    config = load_config(args.config)
    verbose = args.verbose

    # Handle --pr: fetch the PR and override lighthouse_dir
    if getattr(args, "pr", None):
        from .pr import fetch_pr

        worktree_path = fetch_pr(config, args.pr, verbose=verbose)
        # Override lighthouse_dir in config to point at the checkout
        config.paths.lighthouse_dir = worktree_path
        config.pr_number = args.pr
        # Default nickname to pr-<number> if not explicitly set
        if not args.nickname:
            args.nickname = f"pr-{args.pr}"

    # Dispatch
    if args.command == "build":
        cmd_build(config, args.mode, verbose=verbose)

    elif args.command == "run":
        output_path = resolve_output_path(
            config,
            args.mode,
            output_dir_override=args.output_dir,
            nickname_override=args.nickname,
        )
        cmd_run(
            config,
            args.mode,
            output_path,
            verbose=verbose,
            duration_override=args.duration,
            epochs=args.epochs,
            force=args.force,
            attach=args.attach,
            attach_pid=getattr(args, "pid", None),
        )

    elif args.command == "analyze":
        units = "percentages" if args.units == "pct" else args.units
        profile_dirs = _resolve_profile_dirs(config, args.target)
        for profile_dir in profile_dirs:
            cmd_analyze(
                config, profile_dir, filter_mode=args.filter, verbose=verbose, units=units
            )

    elif args.command == "compare":
        units = "percentages" if args.units == "pct" else args.units
        cmd_compare(config, args.dir_a, args.dir_b, filter_mode=args.filter, units=units)

    elif args.command == "profile":
        output_path = resolve_output_path(
            config,
            args.mode,
            output_dir_override=args.output_dir,
            nickname_override=args.nickname,
        )
        cmd_build(config, args.mode, verbose=verbose)
        cmd_run(
            config,
            args.mode,
            output_path,
            verbose=verbose,
            duration_override=args.duration,
            epochs=args.epochs,
            force=args.force,
        )

    elif args.command == "flamechart":
        cmd_flamechart(config, args.directory, bin_size=args.bin_size, verbose=verbose)

    elif args.command == "export":
        cmd_export(
            config,
            args.directory,
            format=args.format,
            output_file=args.output,
            filter_mode=args.filter,
            bin_size=args.bin_size,
            verbose=verbose,
        )

    elif args.command == "clean":
        cmd_clean(config, what=args.what, verbose=verbose, force=args.force)


if __name__ == "__main__":
    main()
