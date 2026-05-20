"""Beacon API client for sync detection and epoch boundary tracking."""

import json
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .constants import SLOTS_PER_EPOCH, SECONDS_PER_SLOT


@dataclass
class EpochBoundary:
    """Record of an observed epoch boundary."""
    epoch: int
    slot: int
    timestamp: float  # unix timestamp when this was observed


@dataclass
class BeaconState:
    """Tracked state from the beacon API."""
    sync_complete_time: float | None = None
    epoch_boundaries: list[EpochBoundary] = field(default_factory=list)
    last_slot: int | None = None
    recording_start_time: float | None = None


class BeaconApiPoller:
    """Background thread that polls the beacon API to track epochs and sync status.
    
    Writes results to an output directory as JSON files:
      - epochs.json: list of observed epoch boundaries with timestamps
      - sync_status.json: when sync completed
    
    Optionally scrapes Prometheus metrics around epoch boundaries.
    """

    def __init__(
        self,
        base_url: str,
        output_dir: Path,
        poll_interval: float = 2.0,
        metrics_port: int | None = 5054,
        epoch_warmup: float = 15.0,
        epoch_cooldown: float = 15.0,
        target_epochs: int | None = None,
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
        self._thread: threading.Thread | None = None
        self._pending_snapshots: list[dict] = []
        self._sync_snapshot_taken = False
        self._completed_epochs = 0  # Epochs that have finished their cooldown
        self._is_tracking_live = False  # True once we see single-slot advances
        # Set when target_epochs have been captured (+ cooldown elapsed)
        self.target_reached = threading.Event()

    def start(self, recording_start_time: float | None = None):
        """Start the background polling thread."""
        self.state.recording_start_time = recording_start_time or time.time()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the polling thread and write results."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._write_results()

    def _poll_loop(self):
        """Main polling loop."""
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception:
                pass  # Network errors are expected during startup
            self._stop_event.wait(self.poll_interval)

    def _poll_once(self):
        """Single poll iteration: check sync + head slot."""
        now = time.time()

        # Check sync status
        syncing = self._get_syncing()
        if syncing is not None:
            if not syncing and self.state.sync_complete_time is None:
                self.state.sync_complete_time = now
                self._on_sync_complete()

        # Check head slot for epoch boundary detection
        head_slot = self._get_head_slot()
        if head_slot is not None:
            current_epoch = head_slot // SLOTS_PER_EPOCH
            if self.state.last_slot is not None:
                slot_diff = head_slot - self.state.last_slot
                # Detect live tracking: slot advances by 0-2 (normal, including missed slots)
                if self.state.sync_complete_time is not None and 0 <= slot_diff <= 2:
                    self._is_tracking_live = True

                last_epoch = self.state.last_slot // SLOTS_PER_EPOCH
                # Only track boundaries after sync completes (during sync, epochs
                # fly by as the node catches up and shouldn't count toward target)
                if current_epoch > last_epoch and self.state.sync_complete_time is not None:
                    boundary = EpochBoundary(
                        epoch=current_epoch,
                        slot=current_epoch * SLOTS_PER_EPOCH,
                        timestamp=now,
                    )
                    self.state.epoch_boundaries.append(boundary)
                    self._on_epoch_boundary(boundary)
            self.state.last_slot = head_slot

        # Check for pending post-boundary metric snapshots
        self._check_pending_snapshots(now)

    def _on_sync_complete(self):
        """Called when sync completes. Takes a steady-state baseline snapshot."""
        if self.metrics_port is None:
            return
        metrics = self._scrape_metrics()
        if metrics is not None:
            self._save_metrics_snapshot("steady_state", "start", metrics)
            # Schedule an end snapshot after one epoch duration
            self._pending_snapshots.append({
                "epoch": "steady_state",
                "trigger_time": time.time() + (SLOTS_PER_EPOCH * SECONDS_PER_SLOT),
                "phase": "end",
            })

    def _on_epoch_boundary(self, boundary: EpochBoundary):
        """Called when an epoch boundary is detected. Scrapes pre-snapshot metrics."""
        if self.metrics_port is None:
            return

        # Take the "pre" snapshot immediately (we just detected the boundary)
        pre_metrics = self._scrape_metrics()
        if pre_metrics is not None:
            self._save_metrics_snapshot(boundary.epoch, "pre", pre_metrics)

        # Schedule a post-snapshot after cooldown
        self._pending_snapshots.append({
            "epoch": boundary.epoch,
            "trigger_time": boundary.timestamp + self.epoch_cooldown,
            "phase": "post",
        })

    def _check_pending_snapshots(self, now: float):
        """Check if any scheduled metric snapshots are due."""
        remaining = []
        for pending in self._pending_snapshots:
            if now >= pending["trigger_time"]:
                if self.metrics_port is not None:
                    phase = pending.get("phase", "post")
                    metrics = self._scrape_metrics()
                    if metrics is not None:
                        self._save_metrics_snapshot(pending["epoch"], phase, metrics)
                        self._compute_and_save_delta(pending["epoch"])

                # Check if this completes an epoch-boundary target
                if (
                    self.target_epochs is not None
                    and isinstance(pending.get("epoch"), int)
                    and pending.get("phase", "post") == "post"
                ):
                    self._completed_epochs += 1
                    if self._completed_epochs >= self.target_epochs:
                        self.target_reached.set()
            else:
                remaining.append(pending)
        self._pending_snapshots = remaining

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
        """Compute delta between start/pre and end/post snapshots."""
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

        # Compute deltas for counters (values that increased)
        deltas = {}
        for key, post_val in post_values.items():
            pre_val = pre_values.get(key)
            if pre_val is not None and post_val > pre_val:
                deltas[key] = {
                    "pre": pre_val,
                    "post": post_val,
                    "delta": post_val - pre_val,
                }

        delta_path = metrics_dir / f"epoch_{epoch}_delta.json"
        delta_path.write_text(json.dumps(deltas, indent=2, sort_keys=True))

    def _get_syncing(self) -> bool | None:
        """GET /eth/v1/node/syncing -> is_syncing bool."""
        try:
            data = self._api_get("/eth/v1/node/syncing")
            return data["data"]["is_syncing"]
        except Exception:
            return None

    def _get_head_slot(self) -> int | None:
        """GET /eth/v1/beacon/headers/head -> slot."""
        try:
            data = self._api_get("/eth/v1/beacon/headers/head")
            return int(data["data"]["header"]["message"]["slot"])
        except Exception:
            return None

    def _api_get(self, path: str) -> dict:
        """Make a GET request to the beacon API."""
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read())

    def _write_results(self):
        """Write tracking results to the output directory."""
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Write epoch boundaries
        epochs_data = [
            {
                "epoch": eb.epoch,
                "slot": eb.slot,
                "timestamp": eb.timestamp,
            }
            for eb in self.state.epoch_boundaries
        ]
        epochs_file = self.output_dir / "epochs.json"
        epochs_file.write_text(json.dumps(epochs_data, indent=2))

        # Write sync status
        sync_data = {
            "sync_complete_time": self.state.sync_complete_time,
            "recording_start_time": self.state.recording_start_time,
            "epochs_observed": len(self.state.epoch_boundaries),
        }
        sync_file = self.output_dir / "sync_status.json"
        sync_file.write_text(json.dumps(sync_data, indent=2))


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
