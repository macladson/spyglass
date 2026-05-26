"""Beacon API client for sync detection and epoch boundary tracking.

Uses Server-Sent Events (SSE) for near-instant head slot notifications
and genesis-time-based computation for precise slot/epoch boundary timing.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .constants import SECONDS_PER_SLOT, SLOTS_PER_EPOCH


@dataclass
class EpochBoundary:
    """Record of an observed epoch boundary."""

    epoch: int
    slot: int
    slot_start_time: float  # computed: genesis_time + slot * SECONDS_PER_SLOT
    detected_at: float  # wall-clock time when SSE event arrived


@dataclass
class BeaconState:
    """Tracked state from the beacon API."""

    sync_complete_time: float | None = None
    epoch_boundaries: list[EpochBoundary] = field(default_factory=list)
    last_slot: int | None = None
    recording_start_time: float | None = None


class BeaconApiPoller:
    """Background tracker using SSE for head events and genesis-based timing.

    Architecture:
      - SSE thread: subscribes to /eth/v1/events?topics=head for near-instant
        slot notifications. Detects epoch boundaries and schedules metric snapshots.
      - Poll thread: checks sync status, fetches genesis time, fires scheduled
        metric snapshots at precise times, and handles the target_reached signaling.

    Timing strategy:
      Slot/epoch boundary times are computed deterministically from genesis_time:
        slot_start = genesis_time + slot * SECONDS_PER_SLOT
      This is independent of when blocks arrive or when SSE events fire.

    Metric snapshot strategy:
      - "pre" snapshot: taken `warmup` seconds BEFORE the computed epoch boundary time
        (scheduled when we see the penultimate slot of an epoch)
      - "post" snapshot: taken `cooldown` seconds AFTER the computed epoch boundary time
      - Delta: computed from pre→post, capturing the full epoch processing window

    Writes results to an output directory as JSON files:
      - epochs.json: list of observed epoch boundaries with computed timestamps
      - metrics/: pre/post/delta metric snapshots
    """

    def __init__(
        self,
        base_url: str,
        output_dir: Path,
        poll_interval: float = 2.0,
        metrics_port: int | None = 5054,
        epoch_warmup: float = 6.0,
        epoch_cooldown: float = 6.0,
        target_epochs: int | None = None,
        genesis_time: float | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.output_dir = output_dir
        self.poll_interval = poll_interval
        self.metrics_port = metrics_port
        self.epoch_warmup = epoch_warmup
        self.epoch_cooldown = epoch_cooldown
        self.target_epochs = target_epochs
        self.state = BeaconState()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._sse_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        self._pending_snapshots: list[dict] = []
        self._completed_epochs = 0
        self._is_tracking_live = False
        self._pre_scheduled_for_epoch: int | None = None
        self._genesis_time: float | None = genesis_time
        self._settled = False
        self._tracking_enabled = False  # Enable epoch boundary tracking
        self.target_reached = threading.Event()

    def start(self, recording_start_time: float | None = None):
        """Start the background SSE and polling threads."""
        self.state.recording_start_time = recording_start_time or time.time()
        self._sse_thread = threading.Thread(target=self._sse_loop, daemon=True)
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._sse_thread.start()
        self._poll_thread.start()

    def stop(self):
        """Stop all threads and write results."""
        self._stop_event.set()
        if self._sse_thread:
            self._sse_thread.join(timeout=5)
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
        self._write_results()

    def enable_tracking(self):
        """Enable epoch boundary tracking and metric snapshots.

        Call this when the profiler is attached and recording begins.
        Before this is called, the poller only tracks sync status and slot position.
        """
        with self._lock:
            self._tracking_enabled = True
            self.state.epoch_boundaries.clear()
            self._completed_epochs = 0
            self._pre_scheduled_for_epoch = None
            self._pending_snapshots.clear()
            self.target_reached.clear()

    def slot_start_time(self, slot: int) -> float | None:
        """Compute the wall-clock start time for a given slot.

        Returns None if genesis_time hasn't been fetched yet.
        """
        if self._genesis_time is None:
            return None
        return self._genesis_time + slot * SECONDS_PER_SLOT

    def epoch_start_time(self, epoch: int) -> float | None:
        """Compute the wall-clock start time for a given epoch.

        Returns None if genesis_time hasn't been fetched yet.
        """
        return self.slot_start_time(epoch * SLOTS_PER_EPOCH)

    # ─── SSE Thread ───────────────────────────────────────────────────────────

    @property
    def settled(self) -> bool:
        """Whether the node has settled after sync (safe to start profiling)."""
        return self._settled

    @property
    def genesis_time(self) -> float | None:
        """Genesis time from the beacon API, or None if not yet fetched."""
        return self._genesis_time

    def _sse_loop(self):
        """Connect to SSE endpoint and process head events. Reconnects on failure."""
        while not self._stop_event.is_set():
            try:
                self._sse_listen()
            except (OSError, urllib.error.URLError, TimeoutError):
                pass
            self._stop_event.wait(2.0)

    def _sse_listen(self):
        """Single SSE connection session. Blocks until disconnect or stop."""
        url = f"{self.base_url}/eth/v1/events?topics=head"
        req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
        resp = urllib.request.urlopen(req, timeout=60)  # 60s detects dead connections
        try:
            self._read_sse_stream(resp)
        finally:
            resp.close()

    def _read_sse_stream(self, resp):
        """Parse an SSE stream, dispatching head events."""
        event_data_lines = []
        for raw_line in resp:
            if self._stop_event.is_set():
                break
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")
            if line == "":
                # Empty line = end of event
                if event_data_lines:
                    try:
                        self._handle_head_event(json.loads("\n".join(event_data_lines)))
                    except (json.JSONDecodeError, KeyError, ValueError):
                        pass
                    event_data_lines = []
            elif line.startswith("data:"):
                event_data_lines.append(line[5:].strip())

    _MIN_LEAD_SLOTS = 2  # Slots of lead time required after sync before profiling

    def _handle_head_event(self, data: dict):
        """Process a head SSE event."""
        slot = int(data["slot"])
        epoch_transition = data.get("epoch_transition", False)
        now = time.time()

        with self._lock:
            # Update tracking state
            if self.state.last_slot is not None:
                slot_diff = slot - self.state.last_slot
                if self.state.sync_complete_time is not None and 0 <= slot_diff <= 2:
                    self._is_tracking_live = True
            self.state.last_slot = slot

            if self.state.sync_complete_time is None or self._genesis_time is None:
                return

            current_epoch = slot // SLOTS_PER_EPOCH
            slot_in_epoch = slot % SLOTS_PER_EPOCH

            # After sync, wait until we're far enough from an epoch boundary
            # to avoid profiling while still recovering. Once settled, stays settled.
            if not self._settled:
                if slot_in_epoch < SLOTS_PER_EPOCH - self._MIN_LEAD_SLOTS:
                    self._settled = True
                return

            # Only track epoch boundaries when tracking is enabled (profiling phase)
            if not self._tracking_enabled:
                return

            # Penultimate slot: schedule pre-snapshot before the upcoming boundary
            if slot_in_epoch == SLOTS_PER_EPOCH - 1:
                next_epoch = current_epoch + 1
                if self._pre_scheduled_for_epoch != next_epoch:
                    boundary_time = (
                        self._genesis_time + next_epoch * SLOTS_PER_EPOCH * SECONDS_PER_SLOT
                    )
                    self._pending_snapshots.append(
                        {
                            "epoch": next_epoch,
                            "trigger_time": boundary_time - self.epoch_warmup,
                            "phase": "pre",
                        }
                    )
                    self._pre_scheduled_for_epoch = next_epoch

            # Epoch boundary: record and schedule post-snapshot
            if epoch_transition:
                boundary_time = (
                    self._genesis_time + current_epoch * SLOTS_PER_EPOCH * SECONDS_PER_SLOT
                )
                boundary = EpochBoundary(
                    epoch=current_epoch,
                    slot=slot,
                    slot_start_time=boundary_time,
                    detected_at=now,
                )
                self.state.epoch_boundaries.append(boundary)

                # Fallback pre-snapshot if penultimate slot was missed
                if self._pre_scheduled_for_epoch != current_epoch:
                    self._pending_snapshots.append(
                        {
                            "epoch": current_epoch,
                            "trigger_time": now,  # Immediately (late fallback)
                            "phase": "pre",
                        }
                    )

                self._pending_snapshots.append(
                    {
                        "epoch": current_epoch,
                        "trigger_time": boundary_time + self.epoch_cooldown,
                        "phase": "post",
                    }
                )

    # ─── Poll Thread ──────────────────────────────────────────────────────────

    def _poll_loop(self):
        """Poll sync status, fetch genesis, and fire pending metric snapshots."""
        while not self._stop_event.is_set():
            try:
                now = time.time()
                self._ensure_genesis_time()
                self._check_sync(now)
                self._fire_pending_snapshots(now)
            except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                pass
            self._stop_event.wait(1.0)

    def _ensure_genesis_time(self):
        """Fetch genesis_time from the beacon API if not already known."""
        if self._genesis_time is not None:
            return
        try:
            data = self._api_get("/eth/v1/beacon/genesis")
            self._genesis_time = int(data["data"]["genesis_time"])
        except (OSError, urllib.error.URLError, KeyError, ValueError, TimeoutError):
            pass

    def _check_sync(self, now: float):
        """Check if the node has finished syncing."""
        syncing = self._get_syncing()
        if syncing is not None and not syncing:
            with self._lock:
                if self.state.sync_complete_time is None:
                    self.state.sync_complete_time = now
                    self._on_sync_complete()

    def _on_sync_complete(self):
        """Called when sync completes. Takes a steady-state baseline snapshot."""
        if self.metrics_port is None:
            return
        metrics = self._scrape_metrics()
        if metrics is not None:
            self._save_metrics_snapshot("steady_state", "start", metrics)
            self._pending_snapshots.append(
                {
                    "epoch": "steady_state",
                    "trigger_time": time.time() + (SLOTS_PER_EPOCH * SECONDS_PER_SLOT),
                    "phase": "end",
                }
            )

    def _fire_pending_snapshots(self, now: float):
        """Fire any metric snapshots that are due."""
        with self._lock:
            due = [p for p in self._pending_snapshots if now >= p["trigger_time"]]
            self._pending_snapshots = [
                p for p in self._pending_snapshots if now < p["trigger_time"]
            ]

        # Execute snapshot I/O without holding the lock
        for pending in due:
            if self.metrics_port is not None:
                metrics = self._scrape_metrics()
                if metrics is not None:
                    self._save_metrics_snapshot(pending["epoch"], pending["phase"], metrics)
                    if pending["phase"] == "post":
                        self._compute_and_save_delta(pending["epoch"])

            # Check if this completes an epoch-boundary target
            if (
                self.target_epochs is not None
                and isinstance(pending.get("epoch"), int)
                and pending["phase"] == "post"
            ):
                with self._lock:
                    self._completed_epochs += 1
                    if self._completed_epochs >= self.target_epochs:
                        self.target_reached.set()

    # ─── Metrics I/O ─────────────────────────────────────────────────────────

    def _scrape_metrics(self) -> str | None:
        """Scrape Prometheus metrics from the lighthouse metrics endpoint."""
        try:
            url = f"http://127.0.0.1:{self.metrics_port}/metrics"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.read().decode("utf-8")
        except Exception:
            return None

    def _save_metrics_snapshot(self, epoch: int | str, phase: str, content: str):
        """Save a raw metrics snapshot to disk."""
        metrics_dir = self.output_dir / "metrics"
        metrics_dir.mkdir(parents=True, exist_ok=True)
        path = metrics_dir / f"epoch_{epoch}_{phase}.txt"
        path.write_text(content)

    def _compute_and_save_delta(self, epoch: int | str):
        """Compute delta between pre and post snapshots."""
        metrics_dir = self.output_dir / "metrics"

        # Try both naming conventions: pre/post (epoch) and start/end (steady_state)
        pre_path = metrics_dir / f"epoch_{epoch}_pre.txt"
        post_path = metrics_dir / f"epoch_{epoch}_post.txt"
        if not pre_path.exists():
            pre_path = metrics_dir / f"epoch_{epoch}_start.txt"
        if not post_path.exists():
            post_path = metrics_dir / f"epoch_{epoch}_end.txt"

        if not pre_path.exists() or not post_path.exists():
            return

        pre_values = _parse_prometheus_text(pre_path.read_text())
        post_values = _parse_prometheus_text(post_path.read_text())

        # Compute deltas (include both increases and decreases)
        deltas = {}
        for key, post_val in post_values.items():
            pre_val = pre_values.get(key)
            if pre_val is not None and post_val != pre_val:
                deltas[key] = {
                    "pre": pre_val,
                    "post": post_val,
                    "delta": post_val - pre_val,
                }

        delta_path = metrics_dir / f"epoch_{epoch}_delta.json"
        delta_path.write_text(json.dumps(deltas, indent=2, sort_keys=True))

    # ─── Beacon API Helpers ───────────────────────────────────────────────────

    def _get_syncing(self) -> bool | None:
        """GET /eth/v1/node/syncing -> is_syncing bool."""
        try:
            data = self._api_get("/eth/v1/node/syncing")
            return data["data"]["is_syncing"]
        except Exception:
            return None

    def _api_get(self, path: str) -> dict:
        """Make a GET request to the beacon API."""
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read())

    # ─── Results Output ───────────────────────────────────────────────────────

    def _write_results(self):
        """Write tracking results to the output directory."""
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Write epoch boundaries
        epochs_data = [
            {
                "epoch": eb.epoch,
                "slot": eb.slot,
                "slot_start_time": eb.slot_start_time,
                "detected_at": eb.detected_at,
            }
            for eb in self.state.epoch_boundaries
        ]
        epochs_file = self.output_dir / "epochs.json"
        epochs_file.write_text(json.dumps(epochs_data, indent=2))


def _parse_prometheus_text(text: str) -> dict[str, float]:
    """Parse Prometheus text exposition format into metric_name -> value dict.

    Only parses simple numeric values (counters, gauges). Skips histograms/summaries
    and comment lines.
    """
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Format: metric_name{labels} value [timestamp]
        # or: metric_name value [timestamp]
        parts = line.split()
        if len(parts) >= 2:
            name = parts[0]
            try:
                val = float(parts[1])
                values[name] = val
            except ValueError:
                continue
    return values
