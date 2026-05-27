"""CLI entry point for the Lighthouse profiling tool."""

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .analyze import cmd_analyze
from .build import cmd_build
from .clean import cmd_clean
from .compare import cmd_compare
from .config import load_config, resolve_output_path
from .export import cmd_export
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


def _resolve_compare_pairs(config, target_a: str, target_b: str) -> list[tuple[Path, Path]]:
    """Resolve two targets to paired profile directories for comparison.

    Matches directories by mode name (cpu, memory) so cpu compares with cpu, etc.
    """
    dirs_a = _resolve_profile_dirs(config, target_a)
    dirs_b = _resolve_profile_dirs(config, target_b)
    map_a = {d.name: d for d in dirs_a}
    map_b = {d.name: d for d in dirs_b}
    common = sorted(set(map_a.keys()) & set(map_b.keys()))
    if not common:
        print(
            f"ERROR: No common profile modes between '{target_a}' and '{target_b}'",
            file=sys.stderr,
        )
        sys.exit(1)
    return [(map_a[mode], map_b[mode]) for mode in common]


def _reorder_groups(parser):
    """Reorder argument groups so 'global' comes first, then unnamed, then 'config overrides'."""
    groups = parser._action_groups
    named = {g.title: g for g in groups}
    order = []
    if "global" in named:
        order.append(named["global"])
    for g in groups:
        if g.title not in ("global", "config overrides"):
            order.append(g)
    if "config overrides" in named:
        order.append(named["config overrides"])
    parser._action_groups = order


def main():
    parent_parser = argparse.ArgumentParser(add_help=False)
    global_group = parent_parser.add_argument_group("global")
    global_group.add_argument(
        "--help", "-h", action="help", default=argparse.SUPPRESS,
        help="show this help message and exit",
    )
    global_group.add_argument(
        "--config",
        "-c",
        type=Path,
        default=None,
        help="path to config file (default: config.toml in project directory)",
    )
    global_group.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="show build output and lighthouse logs (default: silenced)",
    )

    parser = argparse.ArgumentParser(
        prog="spyglass",
        description="Lighthouse profiling tool — build, run, analyze, and compare profiles.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[parent_parser],
        add_help=False,
        epilog="""\nExamples:
  %(prog)s profile --mode cpu -n my-test
  %(prog)s profile --mode cpu --pr 6789
  %(prog)s build --mode cpu
  %(prog)s run --mode cpu -n my-test
  %(prog)s analyze my-test --filter epoch-boundary
  %(prog)s export my-test perf-script --filter epoch-boundary
  %(prog)s compare baseline my-test --filter epoch-boundary
  %(prog)s clean
""",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", help="available commands")

    # --- build ---
    build_parser = subparsers.add_parser(
        "build", help="build lighthouse with profiling support", parents=[parent_parser], add_help=False
    )
    build_parser.add_argument(
        "--mode",
        "-m",
        choices=["cpu", "memory"],
        default="cpu",
        help="profiling mode (default: cpu)",
    )
    build_parser.add_argument(
        "--pr",
        type=int,
        default=None,
        help="fetch and build a GitHub PR by number",
    )
    _reorder_groups(build_parser)

    # Shared args for run and profile commands
    run_profile_parent = argparse.ArgumentParser(add_help=False)
    run_profile_parent.add_argument(
        "--epochs", "-e", type=int, default=1, help="number of epochs to capture (default: 1)"
    )
    run_profile_parent.add_argument(
        "--force", action="store_true", default=False, help="overwrite existing output directory"
    )
    run_profile_parent.add_argument(
        "--mode", "-m", choices=["cpu", "memory"], default="cpu", help="profiling mode (default: cpu)"
    )
    run_profile_parent.add_argument(
        "--pr", type=int, default=None, help="fetch and profile a GitHub PR by number",
    )
    overrides = run_profile_parent.add_argument_group("config overrides")
    overrides.add_argument(
        "--nickname", "-n", type=str, default=None,
        help="override profiling.nickname (used as output subdirectory name)",
    )
    overrides.add_argument(
        "--output-dir", "-o", type=str, default=None,
        help="override profiling.output_dir",
    )

    # --- run ---
    run_parser = subparsers.add_parser(
        "run", help="run lighthouse under a profiler", parents=[parent_parser, run_profile_parent], add_help=False
    )
    run_parser.add_argument(
        "--attach",
        action="store_true",
        default=False,
        help="attach to an existing lighthouse process (skip build/startup)",
    )
    run_parser.add_argument(
        "--pid",
        type=int,
        default=None,
        help="lighthouse PID to attach to (default: auto-detected from config)",
    )
    _reorder_groups(run_parser)

    # --- analyze ---
    analyze_parser = subparsers.add_parser(
        "analyze", help="analyze profiling output", parents=[parent_parser], add_help=False
    )
    analyze_parser.add_argument(
        "target",
        type=str,
        help="nickname (e.g. pr-1234) or path to profile directory",
    )
    analyze_parser.add_argument(
        "--filter",
        "-f",
        choices=["all", "epoch-boundary", "mid-epoch", "steady-state"],
        default=None,
        help="time filter for samples (required for CPU profiles)",
    )
    analyze_parser.add_argument(
        "--units",
        choices=["cycles", "percentages", "pct"],
        default="cycles",
        help="display units: cycles (default) or percentages (relative %%)",
    )
    _reorder_groups(analyze_parser)

    # --- compare ---
    compare_parser = subparsers.add_parser(
        "compare", help="compare two profile runs", parents=[parent_parser], add_help=False
    )
    compare_parser.add_argument("target_a", type=str, help="baseline nickname or profile directory")
    compare_parser.add_argument("target_b", type=str, help="comparison nickname or profile directory")
    compare_parser.add_argument(
        "--filter",
        "-f",
        choices=["all", "epoch-boundary", "mid-epoch", "steady-state"],
        required=True,
        help="which filtered view to compare",
    )
    compare_parser.add_argument(
        "--units",
        choices=["cycles", "percentages", "pct"],
        default="cycles",
        help="display units: cycles (default) or percentages (relative %% + deltas)",
    )
    _reorder_groups(compare_parser)

    # --- export ---
    export_parser = subparsers.add_parser(
        "export", help="export profile in various formats", parents=[parent_parser], add_help=False
    )
    export_parser.add_argument(
        "target",
        type=str,
        help="nickname (e.g. pr-1234) or path to profile directory",
    )
    export_parser.add_argument(
        "format",
        nargs="?",
        choices=["perf-script", "flamegraph", "flamechart"],
        default="perf-script",
        help="export format (default: perf-script)",
    )
    export_parser.add_argument(
        "--bin-size",
        "-b",
        type=float,
        default=0.5,
        help="bin size for flamechart format (default: 0.5s)",
    )
    export_parser.add_argument(
        "--filter",
        "-f",
        choices=["all", "epoch-boundary", "mid-epoch", "steady-state"],
        default=None,
        help="time filter to apply (required for perf-script and flamegraph)",
    )
    export_parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="output file path (default: auto-generated)",
    )
    _reorder_groups(export_parser)

    # --- clean ---
    clean_parser = subparsers.add_parser(
        "clean", help="remove profiling artifacts", parents=[parent_parser], add_help=False
    )
    clean_parser.add_argument(
        "what",
        nargs="?",
        choices=["all", "checkouts", "profiles"],
        default="checkouts",
        help="what to remove (default: checkouts)",
    )
    clean_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="skip confirmation prompt when deleting profiles",
    )
    _reorder_groups(clean_parser)

    # --- profile (convenience) ---
    profile_parser = subparsers.add_parser(
        "profile", help="build + run in one step", parents=[parent_parser, run_profile_parent], add_help=False
    )
    _reorder_groups(profile_parser)

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
        if hasattr(args, "nickname") and not args.nickname:
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
        pairs = _resolve_compare_pairs(config, args.target_a, args.target_b)
        for dir_a, dir_b in pairs:
            cmd_compare(config, dir_a, dir_b, filter_mode=args.filter, units=units)

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
            epochs=args.epochs,
            force=args.force,
        )

    elif args.command == "export":
        if args.format != "flamechart" and args.filter is None:
            print("ERROR: --filter is required for perf-script and flamegraph formats.", file=sys.stderr)
            sys.exit(1)
        profile_dirs = _resolve_profile_dirs(config, args.target)
        for profile_dir in profile_dirs:
            cmd_export(
                config,
                profile_dir,
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
