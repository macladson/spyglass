"""Progress indicators for long-running operations."""

import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .beacon_api import BeaconApiPoller

from .constants import SLOTS_PER_EPOCH, SECONDS_PER_SLOT, BOLD, DIM, GREEN, BLUE, CYAN, RESET, format_size

BAR_WIDTH = 32  # One square per slot in an epoch


class ProgressTimer:
    """Prints periodic status updates during a long-running operation.
    
    Usage:
        with ProgressTimer("Building", interval=15):
            subprocess.run(...)
        
        with ProgressTimer("Profiling", interval=10, beacon_poller=poller):
            proc.wait()
    """

    def __init__(
        self,
        label: str,
        interval: float = 15.0,
        watch_file: Path | None = None,
        beacon_poller: "BeaconApiPoller | None" = None,
        total_duration: int | None = None,
    ):
        self.label = label
        self.interval = interval
        self.watch_file = watch_file
        self.beacon_poller = beacon_poller
        self.total_duration = total_duration
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_time: float = 0

    def __enter__(self):
        self._start_time = time.time()
        self._thread = threading.Thread(target=self._tick_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        elapsed = time.time() - self._start_time
        # Clear line and print final status
        sys.stdout.write(f"\r\033[K  {self.label}... done ({_format_duration(elapsed)})\n")
        sys.stdout.flush()

    def _tick_loop(self):
        while not self._stop.is_set():
            self._stop.wait(self.interval)
            if self._stop.is_set():
                break
            self._print_status()

    def _print_status(self):
        elapsed = time.time() - self._start_time
        term_width = _get_terminal_width()

        # Left side: label + elapsed + file size
        left_parts = [f"  {BOLD}{self.label}{RESET} {DIM}[{_format_duration(elapsed)}]{RESET}"]

        if self.watch_file and self.watch_file.exists():
            size = self.watch_file.stat().st_size
            left_parts.append(f"{DIM}{format_size(size)}{RESET}")

        # Additional context from beacon poller
        if self.beacon_poller:
            ctx = self._get_context()
            if ctx:
                left_parts.append(f"{CYAN}{ctx}{RESET}")

        left = " ".join(left_parts)
        # Strip ANSI for length calculation
        left_visible_len = len(_strip_ansi(left))

        # Right side: progress bar
        bar = self._get_bar(elapsed)
        bar_visible_len = len(_strip_ansi(bar)) if bar else 0

        # Compose line with gap between left and right-aligned bar
        if bar:
            gap = term_width - left_visible_len - bar_visible_len - 1
            if gap < 2:
                gap = 2
            line = f"\r{left}{' ' * gap}{bar}"
        else:
            line = f"\r{left}"

        # Clear to end of line, then write
        sys.stdout.write(f"{line}\033[K")
        sys.stdout.flush()

    def _get_bar(self, elapsed: float) -> str:
        """Build the 32-square progress bar."""
        if self.beacon_poller and self.beacon_poller.state.last_slot is not None:
            # Don't show slot bar until sync is complete and slots are advancing
            # normally (single-slot increments = live, not catching up)
            if self.beacon_poller.state.sync_complete_time is None:
                return ""
            if not self.beacon_poller._is_tracking_live:
                return ""

            slot = self.beacon_poller.state.last_slot
            epochs_done = self.beacon_poller._completed_epochs
            epochs_detected = len(self.beacon_poller.state.epoch_boundaries)
            target = self.beacon_poller.target_epochs

            slot_in_epoch = slot % SLOTS_PER_EPOCH

            # If a boundary was detected but cooldown hasn't finished,
            # keep the bar full — but only on the final epoch
            if epochs_detected > epochs_done and epochs_detected >= (target or 1):
                bar = _render_bar(BAR_WIDTH, BAR_WIDTH)
                label = f"{DIM}cooldown...{RESET}"
                if target:
                    return f"{bar} {BLUE}[{epochs_detected}/{target}]{RESET} {label}"
                return f"{bar} {label}"

            # Show epochs_detected for responsive feedback
            display_count = epochs_detected
            remaining_secs = (SLOTS_PER_EPOCH - slot_in_epoch) * SECONDS_PER_SLOT
            eta = _format_duration(remaining_secs)
            bar = _render_bar(slot_in_epoch, BAR_WIDTH)
            if target:
                return f"{bar} {DIM}{slot_in_epoch}/{BAR_WIDTH}{RESET} {BLUE}[{display_count}/{target}]{RESET} {DIM}~{eta}{RESET}"
            else:
                return f"{bar} {DIM}{slot_in_epoch}/{BAR_WIDTH} ~{eta}{RESET}"
        elif self.total_duration:
            # Time-based: fill proportional to elapsed/total
            progress = min(elapsed / self.total_duration, 1.0)
            filled = int(progress * BAR_WIDTH)
            remaining = self.total_duration - elapsed
            eta = _format_duration(max(0, remaining))
            bar = _render_bar(filled, BAR_WIDTH)
            return f"{bar} {DIM}~{eta}{RESET}"

        return ""

    def _get_context(self) -> str | None:
        """Get additional context from the beacon poller."""
        if not self.beacon_poller:
            return None
        slot = self.beacon_poller.state.last_slot
        if slot is None:
            return "(syncing...)"
        epoch = slot // SLOTS_PER_EPOCH
        return f"epoch {epoch}"


def _render_bar(filled: int, total: int) -> str:
    """Render a colored progress bar: ████████░░░░░░░░"""
    filled = max(0, min(filled, total))
    return f"{GREEN}{'█' * filled}{RESET}{DIM}{'░' * (total - filled)}{RESET}"


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences for length calculation."""
    return re.sub(r"\033\[[0-9;]*m", "", text)


def _get_terminal_width() -> int:
    """Get the terminal width, defaulting to 80."""
    try:
        return os.get_terminal_size().columns
    except (OSError, ValueError):
        return 80


def _format_duration(seconds: float) -> str:
    """Format seconds as M:SS or H:MM:SS."""
    total = int(seconds)
    if total < 3600:
        return f"{total // 60}:{total % 60:02d}"
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02d}:{s:02d}"



