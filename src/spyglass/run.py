"""Run Lighthouse under CPU or memory profiling."""

import json
import os
import secrets
import signal
import socket
import subprocess
import shutil
import sys
import time
from pathlib import Path

from . import config as cfg
from .beacon_api import BeaconApiPoller
from .constants import BOLD, RESET
from .progress import ProgressTimer


def _check_required_tools(mode: str):
    """Verify required external tools are available."""
    required = []
    if mode == "cpu":
        required = ["perf"]
    elif mode == "memory":
        required = ["jeprof"]

    missing = [tool for tool in required if shutil.which(tool) is None]
    if missing:
        print(f"ERROR: Required tool(s) not found: {', '.join(missing)}", file=sys.stderr)
        print("  Install them before running.", file=sys.stderr)
        sys.exit(1)


def wait_for_port(host: str, port: int, timeout: float = 5.0) -> bool:
    """Wait for a TCP port to become available."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except (OSError, ConnectionRefusedError):
            time.sleep(0.2)
    return False


def generate_jwt(path: Path):
    """Generate a random JWT hex secret file."""
    path.write_text(secrets.token_hex(32))


def cmd_run(
    config: dict,
    mode: str,
    output_dir: Path,
    verbose: bool = False,
    filter_mode: str = "all",
    duration_override: int | None = None,
    runs: int = 1,
    force: bool = False,
):
    """Run Lighthouse under profiling.
    
    Args:
        config: Parsed config dict
        mode: "cpu" or "memory"
        output_dir: Directory for profiling output
        verbose: Show lighthouse/mock-el output
        filter_mode: "all", "steady-state", "mid-epoch", or "epoch-boundary"
        duration_override: Override duration_seconds from config
        runs: Number of epochs to capture (only for epoch-boundary mode)
    """
    output_dir = output_dir.resolve()
    lighthouse_dir = cfg.resolve_lighthouse_dir(config)
    duration = duration_override or cfg.duration(config)
    max_wait = cfg.max_wait_seconds(config)
    perf_freq = cfg.perf_frequency(config)
    sync_url = cfg.checkpoint_sync_url(config)
    network = cfg.network(config)
    flags = cfg.extra_flags(config)
    mel_addr = cfg.mock_el_address(config)
    mel_port = cfg.mock_el_port(config)
    http_port = cfg.http_port(config)
    metrics_port = cfg.metrics_port(config)

    # For epoch-boundary mode, use safety timeout instead of fixed duration
    is_event_based = filter_mode == "epoch-boundary"
    effective_timeout = max_wait if is_event_based else duration

    lighthouse_bin = cfg.lighthouse_binary(config)
    lcli_bin = cfg.lcli_binary(config)

    if not lighthouse_bin.exists():
        print(f"ERROR: Binary not found: {lighthouse_bin}", file=sys.stderr)
        print("  Run `spyglass build` first.", file=sys.stderr)
        sys.exit(1)
    if not lcli_bin.exists():
        print(f"ERROR: lcli not found: {lcli_bin}", file=sys.stderr)
        sys.exit(1)

    _check_required_tools(mode)

    print(f"{BOLD}=== Run ==={RESET}")
    print(f"  {BOLD}Mode:{RESET}     {mode}")
    print(f"  {BOLD}Network:{RESET}  {network}")
    print(f"  {BOLD}Filter:{RESET}   {filter_mode}")
    if is_event_based:
        print(f"  {BOLD}Target:{RESET}   {runs} epoch(s) (safety timeout: {max_wait}s)")
    else:
        print(f"  {BOLD}Duration:{RESET} {duration}s")
    print(f"  {BOLD}Output:{RESET}   {output_dir}")
    print(f"  {BOLD}Binary:{RESET}   {lighthouse_bin}")
    print()

    # Setup output directory
    if output_dir.exists() and any(output_dir.iterdir()):
        if not force:
            print(f"ERROR: Output directory already exists and is not empty: {output_dir}", file=sys.stderr)
            print(f"  Use --force to overwrite.", file=sys.stderr)
            sys.exit(1)
        # Clean out all existing contents
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Generate JWT
    jwt_path = output_dir / "jwt.hex"
    generate_jwt(jwt_path)

    # Start mock-el
    mock_el_cmd = [
        str(lcli_bin),
        "--network", network,
        "mock-el",
        "--jwt-output-path", str(jwt_path),
        "--listen-address", mel_addr,
        "--listen-port", str(mel_port),
    ]
    print(f"  Starting mock-el on {mel_addr}:{mel_port}...")

    devnull = None if verbose else subprocess.DEVNULL
    mock_el_proc = subprocess.Popen(
        mock_el_cmd, stdout=devnull, stderr=devnull,
    )

    # Process tracking for signal cleanup
    main_proc = None
    beacon_poller = None

    def cleanup(signum=None, frame=None):
        """Graceful shutdown of all child processes."""
        if beacon_poller:
            beacon_poller.stop()
        if main_proc and main_proc.poll() is None:
            main_proc.terminate()
            try:
                main_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                main_proc.kill()
        if mock_el_proc.poll() is None:
            mock_el_proc.terminate()
            try:
                mock_el_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mock_el_proc.kill()
        if signum:
            sys.exit(0)

    prev_sigint = signal.getsignal(signal.SIGINT)
    prev_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    try:
        # Wait for mock-el
        if not wait_for_port(mel_addr, mel_port):
            print("ERROR: mock-el did not start within 5 seconds", file=sys.stderr)
            cleanup()
            sys.exit(1)
        print("  mock-el ready.")

        # Create datadir
        datadir = output_dir / "datadir"
        datadir.mkdir(parents=True, exist_ok=True)

        # Build lighthouse command
        execution_endpoint = f"http://{mel_addr}:{mel_port}"
        bn_args = [
            str(lighthouse_bin), "bn",
            "--network", network,
            "--execution-endpoint", execution_endpoint,
            "--execution-jwt", str(jwt_path),
            "--datadir", str(datadir),
            "--purge-db-force",
            "--checkpoint-sync-url", sync_url,
            "--http",
            "--http-port", str(http_port),
            "--metrics",
            "--metrics-port", str(metrics_port),
        ]
        bn_args.extend(flags)

        # Build the full run command based on mode
        env = os.environ.copy()
        perf_data = None
        if mode == "cpu":
            perf_data = output_dir / "perf.data"
            run_cmd = [
                "timeout", str(effective_timeout),
                "perf", "record", "-g",
                "-F", str(perf_freq),
                "-o", str(perf_data),
                "--",
            ] + bn_args
            print(f"  CPU profiling at {perf_freq} Hz")
        elif mode == "memory":
            heap_prefix = output_dir / "heap"
            env["_RJEM_MALLOC_CONF"] = (
                f"prof_active:true,prof_final:true,prof_prefix:{heap_prefix}"
            )
            run_cmd = ["timeout", str(effective_timeout)] + bn_args
            print(f"  Memory profiling (jemalloc)")
        else:
            print(f"ERROR: Invalid mode: {mode}", file=sys.stderr)
            sys.exit(1)

        if not verbose:
            print("  (logs silenced, use --verbose to see them)")
        print()

        # Start beacon API poller for epoch boundary detection + metrics scraping
        beacon_url = f"http://127.0.0.1:{http_port}"
        warmup = cfg.epoch_boundary_warmup(config)
        cooldown = cfg.epoch_boundary_cooldown(config)
        beacon_poller = BeaconApiPoller(
            beacon_url, output_dir,
            poll_interval=3.0,
            metrics_port=metrics_port,
            epoch_warmup=warmup,
            epoch_cooldown=cooldown,
            target_epochs=runs if is_event_based else None,
        )

        # Launch lighthouse
        lh_stdout = None if verbose else subprocess.DEVNULL
        lh_stderr = None if verbose else subprocess.DEVNULL
        recording_start = time.time()
        main_proc = subprocess.Popen(run_cmd, env=env, stdout=lh_stdout, stderr=lh_stderr)

        # Start poller after lighthouse is launched (it needs time to start the API)
        beacon_poller.start(recording_start_time=recording_start)

        # Wait for completion
        watch_file = perf_data if mode == "cpu" else None
        if is_event_based:
            # Wait until target epochs captured or process exits
            label = f"Waiting for {runs} epoch(s)"
            with ProgressTimer(label, interval=1, watch_file=watch_file, beacon_poller=beacon_poller):
                while main_proc.poll() is None:
                    if beacon_poller.target_reached.wait(timeout=1.0):
                        print(f"\n  Target reached: {runs} epoch(s) captured")
                        main_proc.terminate()
                        try:
                            main_proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            main_proc.kill()
                        break
        else:
            with ProgressTimer("Profiling", interval=1, watch_file=watch_file, beacon_poller=beacon_poller, total_duration=duration):
                main_proc.wait()
        recording_end = time.time()

        if main_proc.returncode not in (0, 124, -signal.SIGTERM):
            print(f"  WARNING: Lighthouse exited with code {main_proc.returncode}")

    finally:
        # Stop beacon poller (writes epochs.json, sync_status.json)
        if beacon_poller:
            beacon_poller.stop()
        # Kill mock-el
        if mock_el_proc.poll() is None:
            mock_el_proc.terminate()
            try:
                mock_el_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mock_el_proc.kill()
        # Restore signals
        signal.signal(signal.SIGINT, prev_sigint)
        signal.signal(signal.SIGTERM, prev_sigterm)

    # Save run.json — single source of truth for what happened
    elapsed = recording_end - recording_start
    run_info = {
        "mode": mode,
        "network": network,
        "filter_mode": filter_mode,
        "duration": duration if not is_event_based else None,
        "runs": runs if is_event_based else None,
        "elapsed_seconds": elapsed,
        "build_profile": cfg.profile(config),
        "perf_frequency": perf_freq,
        "lighthouse_dir": str(lighthouse_dir),
        "lighthouse_bin": str(lighthouse_bin),
        "checkpoint_sync_url": sync_url,
        "extra_flags": flags,
        "mock_el": f"{mel_addr}:{mel_port}",
        "pr": config.get("_pr_number"),
    }
    (output_dir / "run.json").write_text(json.dumps(run_info, indent=2))

    # Summary
    print(f"\n{BOLD}=== Run complete ({elapsed:.0f}s) ==={RESET}")
    print(f"  {BOLD}Output:{RESET} {output_dir}")
    for f in sorted(output_dir.iterdir()):
        if f.is_file():
            size_mb = f.stat().st_size / 1024 / 1024
            if size_mb > 1:
                print(f"    {f.name} ({size_mb:.1f} MB)")
            else:
                print(f"    {f.name} ({f.stat().st_size / 1024:.1f} KB)")
    print()
