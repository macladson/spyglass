"""Run Lighthouse under CPU or memory profiling."""

import fcntl
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

from .config import SpyglassConfig, get_lighthouse_commit_hash
from .beacon_api import BeaconApiPoller
from .constants import SLOTS_PER_EPOCH, SECONDS_PER_SLOT, BOLD, RESET
from .progress import RunProgress, PHASE_SYNCING, PHASE_SETTLING, PHASE_WAITING, PHASE_PROFILING, PHASE_DONE


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


def _acquire_lock(lock_path: Path):
    """Acquire an exclusive lock file to prevent concurrent runs."""
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        print(
            f"ERROR: Another spyglass process is already running in this output directory.",
            file=sys.stderr,
        )
        print(f"  Lock file: {lock_path}", file=sys.stderr)
        print(f"  If no other process is running, remove the lock file and retry.", file=sys.stderr)
        sys.exit(1)
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


def _release_lock(lock_file, lock_path: Path):
    """Release the lock file and remove it."""
    if lock_file is None:
        return
    try:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
    except OSError:
        pass
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def cmd_run(
    config: SpyglassConfig,
    mode: str,
    output_dir: Path,
    verbose: bool = False,
    duration_override: int | None = None,
    epochs: int = 1,
    force: bool = False,
):
    """Run Lighthouse under profiling.

    Starts lighthouse directly (no perf wrapper), waits for sync + settle,
    then attaches perf at the start slot for precise profiling unit capture.

    Args:
        config: Spyglass configuration object
        mode: "cpu" or "memory"
        output_dir: Directory for profiling output
        verbose: Show lighthouse/mock-el output
        duration_override: Safety timeout in seconds
        epochs: Number of epochs to capture
    """
    output_dir = output_dir.resolve()
    lighthouse_dir = config.paths.lighthouse_dir
    perf_freq = config.profiling.perf_frequency
    sync_url = config.lighthouse.checkpoint_sync_url
    network = config.lighthouse.network
    flags = config.lighthouse.extra_flags
    mel_addr = config.mock_el.listen_address
    mel_port = config.mock_el.listen_port
    http_port = config.lighthouse.http_port
    metrics_port = config.lighthouse.metrics_port

    effective_timeout = duration_override or 7200

    start_slot = config.profiling.start_slot
    end_slot = config.profiling.end_slot

    lighthouse_bin = config.lighthouse_binary
    lcli_bin = config.lcli_binary

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
    print(f"  {BOLD}Target:{RESET}   {epochs} epoch(s) (timeout: {effective_timeout}s)")
    print(f"  {BOLD}Output:{RESET}   {output_dir}")
    print(f"  {BOLD}Binary:{RESET}   {lighthouse_bin}")
    print()

    # Setup output directory
    if output_dir.exists() and any(output_dir.iterdir()):
        if not force:
            print(f"ERROR: Output directory already exists and is not empty: {output_dir}", file=sys.stderr)
            print(f"  Use --force to overwrite.", file=sys.stderr)
            sys.exit(1)
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    lock_path = output_dir / ".spyglass.lock"
    lock_file = _acquire_lock(lock_path)

    datadir = output_dir / "datadir"
    datadir.mkdir(parents=True, exist_ok=True)

    jwt_path = datadir / "jwt.hex"
    generate_jwt(jwt_path)

    # Start mock-el
    mock_el_cmd = [
        str(lcli_bin),
        "--network", network,
        "mock-el",
        "--all-payloads-valid", "true",
        "--jwt-output-path", str(jwt_path),
        "--listen-address", mel_addr,
        "--listen-port", str(mel_port),
    ]
    print(f"  Starting mock-el on {mel_addr}:{mel_port}...")

    devnull = None if verbose else subprocess.DEVNULL
    mock_el_proc = subprocess.Popen(mock_el_cmd, stdout=devnull, stderr=devnull)

    # Process tracking
    lighthouse_proc = None
    perf_proc = None
    beacon_poller = None
    progress = None
    _cleanup_done = False
    _signal_received = False

    def cleanup(signum=None, frame=None):
        nonlocal _cleanup_done, _signal_received
        if _cleanup_done:
            return
        _cleanup_done = True
        if signum:
            _signal_received = True
        if progress:
            progress.stop()
        if beacon_poller:
            beacon_poller.stop()
        if perf_proc and perf_proc.poll() is None:
            perf_proc.send_signal(signal.SIGINT)
            try:
                perf_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                perf_proc.kill()
        if lighthouse_proc and lighthouse_proc.poll() is None:
            lighthouse_proc.terminate()
            try:
                lighthouse_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                lighthouse_proc.kill()
        if mock_el_proc.poll() is None:
            mock_el_proc.terminate()
            try:
                mock_el_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mock_el_proc.kill()

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

        # Build lighthouse command (no perf wrapper)
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

        # Environment for memory profiling
        env = os.environ.copy()
        if mode == "memory":
            heap_prefix = output_dir / "heap"
            env["_RJEM_MALLOC_CONF"] = (
                f"prof_active:true,prof_final:true,prof_prefix:{heap_prefix}"
            )

        if not verbose:
            print("  (logs silenced, use --verbose to see them)")
        print()

        # Start lighthouse directly
        lh_stdout = None if verbose else subprocess.DEVNULL
        lh_stderr = None if verbose else subprocess.DEVNULL
        lighthouse_proc = subprocess.Popen(bn_args, env=env, stdout=lh_stdout, stderr=lh_stderr)

        # Start beacon poller
        beacon_url = f"http://127.0.0.1:{http_port}"
        warmup = config.filtering.epoch_boundary_warmup
        cooldown = config.filtering.epoch_boundary_cooldown
        beacon_poller = BeaconApiPoller(
            beacon_url, output_dir,
            poll_interval=3.0,
            metrics_port=metrics_port,
            epoch_warmup=warmup,
            epoch_cooldown=cooldown,
            target_epochs=epochs,
        )
        beacon_poller.start(recording_start_time=time.time())

        # Start progress display
        perf_data = output_dir / "perf.data" if mode == "cpu" else None
        progress = RunProgress(
            beacon_poller,
            watch_file=perf_data,
            start_slot_offset=start_slot,
            end_slot_offset=end_slot,
            epochs=epochs,
        )
        progress.start()

        # === Phase 1: Wait for sync ===
        deadline = time.time() + effective_timeout
        while time.time() < deadline and lighthouse_proc.poll() is None:
            if beacon_poller.state.sync_complete_time is not None:
                break
            time.sleep(1.0)
        else:
            if lighthouse_proc.poll() is not None:
                print(f"\n  ERROR: Lighthouse exited during sync (code {lighthouse_proc.returncode})")
                sys.exit(1)

        # === Phase 2: Wait for settle ===
        progress.set_phase(PHASE_SETTLING)
        while time.time() < deadline and lighthouse_proc.poll() is None:
            if beacon_poller._settled:
                break
            time.sleep(1.0)

        # === Phase 3: Wait for start slot ===
        # If we're already past start_slot in this epoch, wait for the next one.
        progress.set_phase(PHASE_WAITING)
        slot = beacon_poller.state.last_slot
        if slot is not None and (slot % SLOTS_PER_EPOCH) >= start_slot:
            # Wait until we enter the next epoch
            wait_for_epoch = (slot // SLOTS_PER_EPOCH) + 1
            while time.time() < deadline and lighthouse_proc.poll() is None:
                slot = beacon_poller.state.last_slot
                if slot is not None and (slot // SLOTS_PER_EPOCH) >= wait_for_epoch:
                    break
                time.sleep(1.0)

        # Now wait for start_slot within the current epoch
        while time.time() < deadline and lighthouse_proc.poll() is None:
            slot = beacon_poller.state.last_slot
            if slot is not None:
                if (slot % SLOTS_PER_EPOCH) >= start_slot:
                    break
            time.sleep(1.0)

        # === Phase 4: Attach profiler ===
        progress.set_phase(PHASE_PROFILING)
        recording_start = time.time()
        recording_start_monotonic = time.monotonic()

        if mode == "cpu":
            perf_proc = subprocess.Popen(
                [
                    "perf", "record", "-g",
                    "-F", str(perf_freq),
                    "-p", str(lighthouse_proc.pid),
                    "-o", str(perf_data),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Brief pause to let perf attach
            time.sleep(0.1)

        # === Wait for epoch capture + end slot ===
        # First wait for the epoch boundary to be captured
        while time.time() < deadline and lighthouse_proc.poll() is None:
            if beacon_poller.target_reached.is_set():
                break
            time.sleep(1.0)

        # Then wait until we reach the end slot of the profiling unit
        while time.time() < deadline and lighthouse_proc.poll() is None:
            slot = beacon_poller.state.last_slot
            if slot is not None:
                slot_in_epoch = slot % SLOTS_PER_EPOCH
                if slot_in_epoch >= end_slot and beacon_poller.target_reached.is_set():
                    break
            time.sleep(1.0)

        recording_end = time.time()

        # === Phase 5: Stop profiler ===
        progress.set_phase(PHASE_DONE)
        if perf_proc and perf_proc.poll() is None:
            perf_proc.send_signal(signal.SIGINT)
            try:
                perf_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                perf_proc.kill()

        # Terminate lighthouse
        if lighthouse_proc.poll() is None:
            lighthouse_proc.terminate()
            try:
                lighthouse_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                lighthouse_proc.kill()

    finally:
        cleanup()
        _release_lock(lock_file, lock_path)
        signal.signal(signal.SIGINT, prev_sigint)
        signal.signal(signal.SIGTERM, prev_sigterm)
        if _signal_received:
            sys.exit(0)

    # Save run.json
    elapsed = recording_end - recording_start
    clock_offset = recording_start - recording_start_monotonic
    run_info = {
        "mode": mode,
        "network": network,
        "epochs": epochs,
        "start_slot": start_slot,
        "end_slot": end_slot,
        "elapsed_seconds": elapsed,
        "recording_start": recording_start,
        "recording_start_monotonic": recording_start_monotonic,
        "clock_offset": clock_offset,
        "genesis_time": beacon_poller._genesis_time,
        "sync_complete_time": beacon_poller.state.sync_complete_time,
        "build_profile": config.profiling.profile,
        "perf_frequency": perf_freq,
        "lighthouse_dir": str(lighthouse_dir),
        "lighthouse_bin": str(lighthouse_bin),
        "lighthouse_commit": get_lighthouse_commit_hash(config),
        "checkpoint_sync_url": sync_url,
        "extra_flags": flags,
        "mock_el": f"{mel_addr}:{mel_port}",
        "pr": config.pr_number,
    }
    (output_dir / "run.json").write_text(json.dumps(run_info, indent=2))

    # Clear progress line and print summary
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()
    print(f"\n{BOLD}=== Run complete ({elapsed:.0f}s profiling) ==={RESET}")
    print(f"  {BOLD}Output:{RESET} {output_dir}")
    for f in sorted(output_dir.iterdir()):
        if f.is_file():
            size_mb = f.stat().st_size / 1024 / 1024
            if size_mb > 1:
                print(f"    {f.name} ({size_mb:.1f} MB)")
            else:
                print(f"    {f.name} ({f.stat().st_size / 1024:.1f} KB)")
    print()
