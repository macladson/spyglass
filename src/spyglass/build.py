"""Build Lighthouse with profiling support."""

import os
import subprocess
import sys
from pathlib import Path

from .config import SpyglassConfig
from .constants import BOLD, RESET


def cmd_build(config: SpyglassConfig, mode: str, verbose: bool = False):
    """Build Lighthouse with profiling instrumentation.

    For CPU mode: builds with frame pointers enabled.
    For memory mode: builds with the release-profiling profile and jemalloc-profiling feature.

    No file modifications are needed — everything is controlled via env vars and feature flags.

    Args:
        config: Spyglass configuration object
        mode: "cpu" or "memory"
        verbose: Show build output
    """
    lighthouse_dir = config.paths.lighthouse_dir
    profile = config.profiling.profile
    do_disable_backfill = config.profiling.disable_backfill

    if not lighthouse_dir.exists():
        print(f"ERROR: Lighthouse directory not found: {lighthouse_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"{BOLD}=== Build ==={RESET}")
    print(f"  {BOLD}Mode:{RESET}             {mode}")
    print(f"  {BOLD}Lighthouse dir:{RESET}   {lighthouse_dir}")
    print(f"  {BOLD}Profile:{RESET}          {profile}")
    print(f"  {BOLD}Disable backfill:{RESET} {do_disable_backfill}")

    # Build command
    build_cmd = [
        "cargo",
        "build",
        "--profile",
        profile,
        "--bin",
        "lighthouse",
        "--bin",
        "lcli",
    ]

    # Features
    features = []
    if do_disable_backfill:
        features.append("network/disable-backfill")
    if mode == "memory":
        features.append("malloc_utils/jemalloc-profiling")
    if features:
        build_cmd.extend(["--features", ",".join(features)])

    # Environment
    env = os.environ.copy()
    if mode == "cpu":
        existing = env.get("RUSTFLAGS", "")
        flag = "-C force-frame-pointers=yes"
        if flag not in existing:
            env["RUSTFLAGS"] = f"{existing} {flag}".strip()

    if mode == "memory":
        # Override jemalloc config at compile time to enable profiling support
        env["JEMALLOC_SYS_WITH_MALLOC_CONF"] = "abort_conf:true,narenas:16,prof:true"

    # Force cargo to use color
    build_cmd.extend(["--color", "always"])

    if verbose:
        print()
        result = subprocess.run(
            build_cmd,
            cwd=lighthouse_dir,
            env=env,
        )
    else:
        # Use a pty so cargo thinks it's writing to a terminal and shows
        # its progress bar. We filter to only show the "Building [...]" line.
        print()
        result = _run_build_with_pty(build_cmd, lighthouse_dir, env)

    if result.returncode != 0:
        print(f"\nERROR: Build failed with exit code {result.returncode}", file=sys.stderr)
        sys.exit(result.returncode)

    print(f"{BOLD}=== Build complete ==={RESET}\n")


def _run_build_with_pty(build_cmd: list, cwd: Path, env: dict):
    """Run cargo build with a pty, showing only the progress bar line."""
    import fcntl
    import pty
    import re
    import select
    import struct
    import termios

    # Create a pty so cargo renders its progress bar
    master_fd, slave_fd = pty.openpty()

    # Set terminal size so cargo knows how wide to make the progress bar
    try:
        cols = os.get_terminal_size().columns
        rows = os.get_terminal_size().lines
    except OSError:
        cols, rows = 80, 24
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

    proc = subprocess.Popen(
        build_cmd,
        cwd=cwd,
        env=env,
        stdout=slave_fd,
        stderr=slave_fd,
    )
    os.close(slave_fd)

    # Regex to match progress/finish lines (stripping ANSI codes for matching)
    ansi_re = re.compile(r"\033\[[0-9;]*m")
    output_lines = []
    buf = b""

    try:
        while True:
            ready, _, _ = select.select([master_fd], [], [], 0.1)
            if ready:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                buf += data
                # Process on \r or \n boundaries
                while b"\r" in buf or b"\n" in buf:
                    # Find the earliest separator
                    r_pos = buf.find(b"\r")
                    n_pos = buf.find(b"\n")
                    if r_pos == -1:
                        pos = n_pos
                    elif n_pos == -1:
                        pos = r_pos
                    else:
                        pos = min(r_pos, n_pos)

                    line = buf[:pos].decode("utf-8", errors="replace")
                    buf = buf[pos + 1 :]

                    # Strip ANSI for matching, but display with ANSI intact
                    plain = ansi_re.sub("", line)
                    if "Building [" in plain or "Finished" in plain:
                        sys.stdout.write(f"\r{line}\033[K")
                        sys.stdout.flush()
                    if line.strip():
                        output_lines.append(line)
            elif proc.poll() is not None:
                # Drain remaining output
                while True:
                    try:
                        remaining = os.read(master_fd, 4096)
                        if not remaining:
                            break
                        buf += remaining
                    except OSError:
                        break
                while b"\r" in buf or b"\n" in buf:
                    r_pos = buf.find(b"\r")
                    n_pos = buf.find(b"\n")
                    if r_pos == -1:
                        pos = n_pos
                    elif n_pos == -1:
                        pos = r_pos
                    else:
                        pos = min(r_pos, n_pos)
                    line = buf[:pos].decode("utf-8", errors="replace")
                    buf = buf[pos + 1 :]
                    plain = ansi_re.sub("", line)
                    if "Building [" in plain or "Finished" in plain:
                        sys.stdout.write(f"\r{line}\033[K")
                        sys.stdout.flush()
                    if line.strip():
                        output_lines.append(line)
                break
    finally:
        os.close(master_fd)

    proc.wait()
    sys.stdout.write("\n")
    sys.stdout.flush()

    if proc.returncode != 0:
        # Print full output on failure
        for line in output_lines:
            if line.strip():
                print(line, file=sys.stderr)

    return proc
