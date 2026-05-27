"""Run Lighthouse under CPU or memory profiling."""

import fcntl
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from .beacon_api import BeaconApiPoller
from .config import SpyglassConfig, get_lighthouse_commit_hash
from .constants import SLOTS_PER_EPOCH, log, log_end, log_start, log_step
from .progress import (
    PHASE_DONE,
    PHASE_PROFILING,
    PHASE_SETTLING,
    PHASE_WAITING,
    RunProgress,
)


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
            "ERROR: Another spyglass process is already running in this output directory.",
            file=sys.stderr,
        )
        print(f"  Lock file: {lock_path}", file=sys.stderr)
        print("  If no other process is running, remove the lock file and retry.", file=sys.stderr)
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


def _pid_alive(pid: int) -> bool:
    """Check if a process is still running."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _find_lighthouse_pid(network: str, http_port: int) -> int | None:
    """Find the PID of a running lighthouse beacon node via /proc.

    Matches on process name 'lighthouse' with 'bn', the network name, and
    the HTTP port. This uniquely identifies the instance even when multiple
    lighthouse nodes run on the same network (e.g. stable vs unstable
    side-by-side with different ports).

    Returns None if not found or ambiguous.
    """
    matches = []
    network_bytes = network.encode()
    port_bytes = str(http_port).encode()
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                comm = (entry / "comm").read_text().strip()
                if comm != "lighthouse":
                    continue
                cmdline = (entry / "cmdline").read_bytes().split(b"\x00")
                if b"bn" not in cmdline or network_bytes not in cmdline:
                    continue
                # Match --http-port <port>
                if b"--http-port" in cmdline:
                    try:
                        idx = list(cmdline).index(b"--http-port")
                        if cmdline[idx + 1] == port_bytes:
                            matches.append(int(entry.name))
                    except (IndexError, ValueError):
                        continue
                elif http_port == 5052:
                    # Default port — matches if --http-port isn't specified
                    matches.append(int(entry.name))
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    if len(matches) == 1:
        return matches[0]
    return None


def cmd_run(
    config: SpyglassConfig,
    mode: str,
    output_dir: Path,
    verbose: bool = False,
    epochs: int = 1,
    force: bool = False,
    attach: bool = False,
    attach_pid: int | None = None,
):
    """Run Lighthouse under profiling.

    In standard mode: starts mock-el + lighthouse, waits for sync + settle,
    then attaches perf at the start slot for precise profiling unit capture.

    In attach mode: skips process startup and attaches to an existing lighthouse
    process (resolved from --pid or the systemd service in config). Leaves the
    process running after profiling.

    Args:
        config: Spyglass configuration object
        mode: "cpu" or "memory"
        output_dir: Directory for profiling output
        verbose: Show lighthouse/mock-el output
        epochs: Number of epochs to capture
        attach: Enable attach mode
        attach_pid: PID of running lighthouse process (auto-resolved if omitted)
    """
    attach_mode = attach

    output_dir = output_dir.resolve()
    lighthouse_dir = config.paths.lighthouse_dir
    perf_freq = config.profiling.perf_frequency
    network = config.lighthouse.network
    metrics_port = config.lighthouse.metrics_port

    effective_timeout = config.profiling.safety_timeout

    start_slot = config.profiling.start_slot
    end_slot = config.profiling.end_slot

    if attach_mode:
        if attach_pid is None:
            http_port = config.lighthouse.http_port
            attach_pid = _find_lighthouse_pid(network, http_port)
            if attach_pid is None:
                print(
                    f"ERROR: Could not find a running lighthouse bn"
                    f" (network={network}, http-port={http_port})",
                    file=sys.stderr,
                )
                print("  Provide --pid explicitly, or check network/http_port in config.", file=sys.stderr)
                sys.exit(1)
            log_step(f"found lighthouse bn ({network}, port {http_port}) at PID {attach_pid}")
        if not _pid_alive(attach_pid):
            print(f"ERROR: PID {attach_pid} not found", file=sys.stderr)
            sys.exit(1)
    else:
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

    nickname = output_dir.parent.name
    log_start("run", nickname)
    log(f"mode: {mode}  network: {network}  epochs: {epochs}  timeout: {effective_timeout}s")
    log(f"output: {output_dir}")
    if attach_mode:
        log(f"attach: PID {attach_pid}  beacon: {config.lighthouse.beacon_url}")
    else:
        log(f"binary: {lighthouse_bin}")
    log()

    # Setup output directory
    if output_dir.exists() and any(output_dir.iterdir()):
        if not force:
            print(
                f"ERROR: Output directory already exists and is not empty: {output_dir}",
                file=sys.stderr,
            )
            print("  Use --force to overwrite.", file=sys.stderr)
            sys.exit(1)
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    lock_path = output_dir / ".spyglass.lock"
    lock_file = _acquire_lock(lock_path)

    # In standard mode, start mock-el and lighthouse
    mock_el_proc = None
    lighthouse_proc = None

    if not attach_mode:
        sync_url = config.lighthouse.checkpoint_sync_url
        flags = config.lighthouse.extra_flags
        mel_addr = config.mock_el.listen_address
        mel_port = config.mock_el.listen_port
        http_port = config.lighthouse.http_port

        datadir = output_dir / "datadir"
        datadir.mkdir(parents=True, exist_ok=True)

        jwt_path = datadir / "jwt.hex"
        generate_jwt(jwt_path)

        mock_el_cmd = [
            str(lcli_bin),
            "--network",
            network,
            "mock-el",
            "--all-payloads-valid",
            "true",
            "--jwt-output-path",
            str(jwt_path),
            "--listen-address",
            mel_addr,
            "--listen-port",
            str(mel_port),
        ]
        log_step(f"starting mock-el on {mel_addr}:{mel_port}")

        devnull = None if verbose else subprocess.DEVNULL
        mock_el_proc = subprocess.Popen(mock_el_cmd, stdout=devnull, stderr=devnull)

    # Process tracking
    perf_proc = None
    beacon_poller = None
    progress = None
    _cleanup_done = False
    _signal_received = False

    def cleanup():
        nonlocal _cleanup_done
        if _cleanup_done:
            return
        _cleanup_done = True
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
        if not attach_mode:
            if lighthouse_proc and lighthouse_proc.poll() is None:
                lighthouse_proc.terminate()
                try:
                    lighthouse_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    lighthouse_proc.kill()
            if mock_el_proc and mock_el_proc.poll() is None:
                mock_el_proc.terminate()
                try:
                    mock_el_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    mock_el_proc.kill()

    def _signal_handler(signum, frame):
        nonlocal _signal_received
        _signal_received = True

    # Only register signal handlers from the main thread
    _in_main_thread = threading.current_thread() is threading.main_thread()
    prev_sigint = None
    prev_sigterm = None
    if _in_main_thread:
        prev_sigint = signal.getsignal(signal.SIGINT)
        prev_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

    # Target PID for perf and liveness checks
    target_pid = attach_pid if attach_mode else None

    def target_alive() -> bool:
        if attach_mode:
            return _pid_alive(attach_pid)
        return lighthouse_proc is not None and lighthouse_proc.poll() is None

    try:
        if not attach_mode:
            # Wait for mock-el
            if not wait_for_port(mel_addr, mel_port):
                print("ERROR: mock-el did not start within 5 seconds", file=sys.stderr)
                cleanup()
                sys.exit(1)
            log_step("mock-el ready")

            # Build lighthouse command (no perf wrapper)
            execution_endpoint = f"http://{mel_addr}:{mel_port}"
            bn_args = [
                str(lighthouse_bin),
                "bn",
                "--network",
                network,
                "--execution-endpoint",
                execution_endpoint,
                "--execution-jwt",
                str(jwt_path),
                "--datadir",
                str(datadir),
                "--purge-db-force",
                "--checkpoint-sync-url",
                sync_url,
                "--http",
                "--http-port",
                str(http_port),
                "--metrics",
                "--metrics-port",
                str(metrics_port),
            ]
            bn_args.extend(flags)

            env = os.environ.copy()
            if mode == "memory":
                heap_prefix = output_dir / "heap"
                env["_RJEM_MALLOC_CONF"] = (
                    f"prof_active:true,prof_final:true,prof_prefix:{heap_prefix}"
                )

            if not verbose:
                log_step("lighthouse started (logs silenced, use --verbose to see them)")
            else:
                log_step("lighthouse started")

            lh_stdout = None if verbose else subprocess.DEVNULL
            lh_stderr = None if verbose else subprocess.DEVNULL
            lighthouse_proc = subprocess.Popen(
                bn_args, env=env, stdout=lh_stdout, stderr=lh_stderr
            )
            target_pid = lighthouse_proc.pid

        # Start beacon poller
        effective_beacon_url = (
            config.lighthouse.beacon_url if attach_mode else f"http://127.0.0.1:{http_port}"
        )
        warmup = config.filtering.epoch_boundary_warmup
        cooldown = config.filtering.epoch_boundary_cooldown
        beacon_poller = BeaconApiPoller(
            effective_beacon_url,
            output_dir,
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
            start_slot=start_slot,
            end_slot=end_slot,
            epochs=epochs,
        )
        progress.start()

        # === Phase 1: Wait for sync ===
        deadline = time.time() + effective_timeout
        while time.time() < deadline and target_alive() and not _signal_received:
            if beacon_poller.state.sync_complete_time is not None:
                break
            time.sleep(1.0)

        if _signal_received:
            sys.exit(0)
        if beacon_poller.state.sync_complete_time is None:
            if not target_alive():
                print("\n  ERROR: Lighthouse process exited during sync")
            else:
                print(f"\n  ERROR: Timed out waiting for sync ({effective_timeout}s)")
            cleanup()
            sys.exit(1)

        # === Phase 2: Wait for settle ===
        progress.set_phase(PHASE_SETTLING)
        while time.time() < deadline and target_alive() and not _signal_received:
            if beacon_poller.settled:
                break
            time.sleep(1.0)

        if _signal_received:
            sys.exit(0)
        if not beacon_poller.settled:
            if not target_alive():
                print("\n  ERROR: Lighthouse process exited during settle")
            else:
                print(f"\n  ERROR: Timed out waiting for settle ({effective_timeout}s)")
            cleanup()
            sys.exit(1)

        # === Phase 3: Wait for start slot ===
        progress.set_phase(PHASE_WAITING)
        slot = beacon_poller.state.last_slot
        if slot is not None and (slot % SLOTS_PER_EPOCH) >= start_slot:
            wait_for_epoch = (slot // SLOTS_PER_EPOCH) + 1
            while time.time() < deadline and target_alive() and not _signal_received:
                slot = beacon_poller.state.last_slot
                if slot is not None and (slot // SLOTS_PER_EPOCH) >= wait_for_epoch:
                    break
                time.sleep(1.0)

        while time.time() < deadline and target_alive() and not _signal_received:
            slot = beacon_poller.state.last_slot
            if slot is not None:
                if (slot % SLOTS_PER_EPOCH) >= start_slot:
                    break
            time.sleep(1.0)

        if _signal_received:
            sys.exit(0)
        if not target_alive():
            print("\n  ERROR: Lighthouse process exited while waiting for start slot")
            cleanup()
            sys.exit(1)
        if time.time() >= deadline:
            print(f"\n  ERROR: Timed out waiting for start slot ({effective_timeout}s)")
            cleanup()
            sys.exit(1)

        # === Phase 4: Attach profiler ===
        beacon_poller.enable_tracking()

        progress.set_phase(PHASE_PROFILING)
        recording_start = time.time()
        recording_start_monotonic = time.monotonic()

        if mode == "cpu":
            perf_proc = subprocess.Popen(
                [
                    "perf",
                    "record",
                    "-g",
                    "-F",
                    str(perf_freq),
                    "-p",
                    str(target_pid),
                    "-o",
                    str(perf_data),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.1)

        # === Wait for epoch capture + end slot ===
        while time.time() < deadline and target_alive() and not _signal_received:
            if beacon_poller.target_reached.is_set():
                break
            time.sleep(1.0)

        while time.time() < deadline and target_alive() and not _signal_received:
            slot = beacon_poller.state.last_slot
            if slot is not None:
                slot_in_epoch = slot % SLOTS_PER_EPOCH
                if slot_in_epoch >= end_slot and beacon_poller.target_reached.is_set():
                    break
            time.sleep(1.0)

        recording_end = time.time()

        if _signal_received:
            sys.exit(0)
        if not target_alive():
            print("\n  ERROR: Lighthouse process exited during profiling")
            cleanup()
            sys.exit(1)
        if time.time() >= deadline:
            print(f"\n  ERROR: Timed out during profiling ({effective_timeout}s)")
            cleanup()
            sys.exit(1)

        # === Phase 5: Stop profiler ===
        progress.set_phase(PHASE_DONE)
        if perf_proc and perf_proc.poll() is None:
            perf_proc.send_signal(signal.SIGINT)
            try:
                perf_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                perf_proc.kill()

        if not attach_mode:
            if lighthouse_proc and lighthouse_proc.poll() is None:
                lighthouse_proc.terminate()
                try:
                    lighthouse_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    lighthouse_proc.kill()

    finally:
        cleanup()
        _release_lock(lock_file, lock_path)
        if _in_main_thread:
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
        "genesis_time": beacon_poller.genesis_time,
        "sync_complete_time": beacon_poller.state.sync_complete_time,
        "build_profile": config.profiling.profile,
        "perf_frequency": perf_freq,
        "pr": config.pr_number,
    }
    if attach_mode:
        run_info["attach_pid"] = attach_pid
        run_info["lighthouse_bin"] = "attached"
        run_info["lighthouse_dir"] = None
        run_info["lighthouse_commit"] = None
    else:
        run_info["lighthouse_dir"] = str(lighthouse_dir)
        run_info["lighthouse_bin"] = str(lighthouse_bin)
        run_info["lighthouse_commit"] = get_lighthouse_commit_hash(config)
        run_info["checkpoint_sync_url"] = config.lighthouse.checkpoint_sync_url
        run_info["extra_flags"] = config.lighthouse.extra_flags
        run_info["mock_el"] = f"{config.mock_el.listen_address}:{config.mock_el.listen_port}"
    (output_dir / "run.json").write_text(json.dumps(run_info, indent=2))

    sys.stdout.write("\r\033[K")
    sys.stdout.flush()
    log_end(f"done ({elapsed:.0f}s) → {output_dir}")
