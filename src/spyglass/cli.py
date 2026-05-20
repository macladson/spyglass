"""CLI entry point for the Lighthouse profiling tool."""

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import load_config, resolve_output_path
from .build import cmd_build
from .run import cmd_run
from .analyze import cmd_analyze
from .compare import cmd_compare
from .clean import cmd_clean


def main():
    # Shared parent parser for global flags (--config, --verbose)
    # This allows them to appear before OR after the subcommand.
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument(
        "--config", "-c",
        type=Path,
        default=None,
        help="Path to config file (default: config.toml in project directory)",
    )
    parent_parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Show build output and lighthouse logs (default: silenced)",
    )
    parent_parser.add_argument(
        "--nickname", "-n",
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
  %(prog)s profile --mode cpu --filter epoch-boundary -n my-test
  %(prog)s profile --mode cpu --filter epoch-boundary --pr 6789
  %(prog)s build --mode cpu
  %(prog)s run --mode cpu --filter epoch-boundary -n my-test
  %(prog)s analyze ./profiles/my-test/cpu/epoch_boundary
  %(prog)s compare ./profiles/baseline/cpu/epoch_boundary ./profiles/opt/cpu/epoch_boundary
  %(prog)s clean
""",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- build ---
    build_parser = subparsers.add_parser("build", help="Build Lighthouse with profiling support", parents=[parent_parser])
    build_parser.add_argument(
        "--mode", "-m",
        choices=["cpu", "memory", "both"],
        default="cpu",
        help="Profiling mode (default: cpu)",
    )

    # --- run ---
    run_parser = subparsers.add_parser("run", help="Run Lighthouse under a profiler", parents=[parent_parser])
    run_parser.add_argument(
        "--mode", "-m",
        choices=["cpu", "memory"],
        required=True,
        help="Profiling mode",
    )
    run_parser.add_argument(
        "--output-dir", "-o",
        type=str,
        default=None,
        help="Base output directory (default: from config, path auto-constructed)",
    )
    run_parser.add_argument(
        "--filter", "-f",
        choices=["all", "epoch-boundary", "mid-epoch", "steady-state"],
        default="all",
        help="Profiling filter (default: all)",
    )
    run_parser.add_argument(
        "--duration", "-d",
        type=int,
        default=None,
        help="Duration in seconds (overrides config; not used for epoch-boundary)",
    )
    run_parser.add_argument(
        "--runs", "-r",
        type=int,
        default=1,
        help="Number of epochs to capture (only for --filter epoch-boundary, default: 1)",
    )
    run_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Overwrite existing output directory",
    )

    # --- analyze ---
    analyze_parser = subparsers.add_parser("analyze", help="Analyze profiling output", parents=[parent_parser])
    analyze_parser.add_argument(
        "directory",
        type=Path,
        help="Profile output directory to analyze",
    )
    analyze_parser.add_argument(
        "--filter", "-f",
        choices=["all", "epoch-boundary", "mid-epoch", "steady-state"],
        default="all",
        help="Time-based filter for samples (default: all)",
    )

    # --- compare ---
    compare_parser = subparsers.add_parser("compare", help="Compare two profile runs", parents=[parent_parser])
    compare_parser.add_argument("dir_a", type=Path, help="Baseline profile directory")
    compare_parser.add_argument("dir_b", type=Path, help="Comparison profile directory")
    compare_parser.add_argument(
        "--filter", "-f",
        choices=["all", "epoch-boundary", "mid-epoch", "steady-state"],
        default="all",
        help="Which filtered profile to compare (default: all)",
    )

    # --- clean ---
    clean_parser = subparsers.add_parser("clean", help="Remove profiling artifacts", parents=[parent_parser])
    clean_parser.add_argument(
        "what",
        nargs="?",
        choices=["all", "checkouts", "profiles"],
        default="checkouts",
        help="What to remove (default: checkouts)",
    )

    # --- profile (convenience) ---
    profile_parser = subparsers.add_parser(
        "profile", help="Build + run + analyze in one step", parents=[parent_parser]
    )
    profile_parser.add_argument(
        "--mode", "-m",
        choices=["cpu", "memory"],
        required=True,
        help="Profiling mode",
    )
    profile_parser.add_argument(
        "--output-dir", "-o",
        type=str,
        default=None,
        help="Base output directory (default: from config, path auto-constructed)",
    )
    profile_parser.add_argument(
        "--filter", "-f",
        choices=["all", "epoch-boundary", "mid-epoch", "steady-state"],
        default="all",
        help="Profiling filter (default: all)",
    )
    profile_parser.add_argument(
        "--duration", "-d",
        type=int,
        default=None,
        help="Duration in seconds (overrides config; not used for epoch-boundary)",
    )
    profile_parser.add_argument(
        "--runs", "-r",
        type=int,
        default=1,
        help="Number of epochs to capture (only for --filter epoch-boundary, default: 1)",
    )
    profile_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Overwrite existing output directory",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    config = load_config(args.config)
    verbose = args.verbose

    # Handle --pr: fetch the PR and override lighthouse_dir
    if getattr(args, 'pr', None):
        from .pr import fetch_pr
        worktree_path = fetch_pr(config, args.pr, verbose=verbose)
        # Override lighthouse_dir in config to point at the checkout
        config["paths"] = config.get("paths", {})
        config["paths"]["lighthouse_dir"] = str(worktree_path)
        config["_pr_number"] = args.pr
        # Default nickname to pr-<number> if not explicitly set
        if not args.nickname:
            args.nickname = f"pr-{args.pr}"

    # Dispatch
    if args.command == "build":
        cmd_build(config, args.mode, verbose=verbose)

    elif args.command == "run":
        output_path = resolve_output_path(
            config, args.mode, args.filter,
            output_dir_override=args.output_dir,
            nickname_override=args.nickname,
        )
        cmd_run(
            config, args.mode, output_path,
            verbose=verbose,
            filter_mode=args.filter,
            duration_override=args.duration,
            runs=args.runs,
            force=args.force,
        )

    elif args.command == "analyze":
        cmd_analyze(config, args.directory, filter_mode=args.filter, verbose=verbose)

    elif args.command == "compare":
        cmd_compare(config, args.dir_a, args.dir_b, filter_mode=args.filter)

    elif args.command == "profile":
        output_path = resolve_output_path(
            config, args.mode, args.filter,
            output_dir_override=args.output_dir,
            nickname_override=args.nickname,
        )
        cmd_build(config, args.mode, verbose=verbose)
        cmd_run(
            config, args.mode, output_path,
            verbose=verbose,
            filter_mode=args.filter,
            duration_override=args.duration,
            runs=args.runs,
            force=args.force,
        )
        cmd_analyze(config, output_path, filter_mode=args.filter, verbose=verbose)

    elif args.command == "clean":
        cmd_clean(config, what=args.what, verbose=verbose)


if __name__ == "__main__":
    main()
