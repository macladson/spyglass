"""Progress display for profiling runs — ANSI multi-line block."""

import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .beacon_api import BeaconApiPoller

from .constants import SLOTS_PER_EPOCH, SECONDS_PER_SLOT, BOLD, DIM, GREEN, BLUE, CYAN, YELLOW, RESET, format_size

BAR_WIDTH = 32

# Profiling phases
PHASE_SYNCING = "syncing"
PHASE_SETTLING = "settling"
PHASE_WAITING = "waiting"
PHASE_PROFILING = "profiling"
PHASE_DONE = "done"


class RunProgress:
    """Displays a live-updating multi-line progress block during profiling."""

    def __init__(
        self,
        beacon_poller: "BeaconApiPoller",
        watch_file: Path | None = None,
        start_slot_offset: int = 16,
        end_slot_offset: int = 15,
        epochs: int = 1,
    ):
        self.beacon_poller = beacon_poller
        self.watch_file = watch_file
        self.start_slot_offset = start_slot_offset
        self.end_slot_offset = end_slot_offset
        self.epochs = epochs
        self.phase = PHASE_SYNCING
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_time: float = 0
        self._lines_drawn = 0

    def start(self):
        self._start_time = time.time()
        self._thread = threading.Thread(target=self._tick_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        # Clear the block
        self._clear()

    def set_phase(self, phase: str):
        self.phase = phase

    def _tick_loop(self):
        while not self._stop.is_set():
            self._render()
            self._stop.wait(1.0)

    def _clear(self):
        """Erase the previously drawn block."""
        if self._lines_drawn > 0:
            sys.stdout.write(f"\033[{self._lines_drawn}A")
            for _ in range(self._lines_drawn):
                sys.stdout.write("\033[K\n")
            sys.stdout.write(f"\033[{self._lines_drawn}A")
            sys.stdout.flush()
            self._lines_drawn = 0

    def _render(self):
        """Render the progress block."""
        elapsed = time.time() - self._start_time
        width = _get_terminal_width()
        bp = self.beacon_poller

        # Build display data
        slot = bp.state.last_slot
        epoch = slot // SLOTS_PER_EPOCH if slot is not None else None
        slot_in_epoch = slot % SLOTS_PER_EPOCH if slot is not None else None

        phase_str = {
            PHASE_SYNCING: f"{YELLOW}●{RESET} {BOLD}Syncing{RESET}",
            PHASE_SETTLING: f"{YELLOW}●{RESET} {BOLD}Settling{RESET}",
            PHASE_WAITING: f"{CYAN}●{RESET} {BOLD}Waiting{RESET}",
            PHASE_PROFILING: f"{GREEN}●{RESET} {BOLD}Profiling{RESET}",
            PHASE_DONE: f"{GREEN}✓{RESET} {BOLD}Done{RESET}",
        }.get(self.phase, self.phase)

        # Line 1: phase + elapsed + epoch info
        line1_parts = [f"  {phase_str}"]
        if epoch is not None:
            line1_parts.append(f"{DIM}epoch {epoch}{RESET}")
        line1_parts.append(f"{DIM}{_format_duration(elapsed)}{RESET}")
        line1 = "  ".join(line1_parts)

        # Line 2: progress bar
        bar, bar_info = self._build_bar(slot_in_epoch)
        if bar:
            line2 = f"  {bar}  {bar_info}"
        else:
            line2 = f"  {DIM}waiting for head slot...{RESET}"

        # Line 3: details
        details = []
        if self.watch_file and self.watch_file.exists():
            size = self.watch_file.stat().st_size
            if size > 0:
                details.append(f"perf.data: {format_size(size)}")
        if bp.target_reached.is_set():
            details.append(f"epochs: {len(bp.state.epoch_boundaries)}/{self.epochs} ✓")
        elif len(bp.state.epoch_boundaries) > 0:
            details.append(f"epochs: {len(bp.state.epoch_boundaries)}/{self.epochs}")
        if bp._genesis_time and slot is not None:
            details.append(f"freq: {self._get_freq_display()}")
        line3 = f"  {DIM}{' · '.join(details)}{RESET}" if details else ""

        # Compose and draw
        lines = [line1, line2]
        if line3:
            lines.append(line3)

        self._clear()
        output = "\n".join(lines) + "\n"
        sys.stdout.write(output)
        sys.stdout.flush()
        self._lines_drawn = len(lines)

    def _build_bar(self, slot_in_epoch: int | None) -> tuple[str, str]:
        """Build the progress bar and info text for the current phase."""
        if slot_in_epoch is None:
            return "", ""

        bp = self.beacon_poller

        if self.phase == PHASE_PROFILING:
            mid = self.start_slot_offset
            if bp.target_reached.is_set():
                # Post-boundary: counting to end_slot
                target = self.end_slot_offset
                if slot_in_epoch >= target:
                    bar = _render_bar(BAR_WIDTH, BAR_WIDTH, midpoint=mid)
                    return bar, f"{DIM}slot {slot_in_epoch}/32 · finishing...{RESET}"
                remaining = (target - slot_in_epoch) * SECONDS_PER_SLOT
                bar = _render_bar(slot_in_epoch, BAR_WIDTH, midpoint=mid)
                return bar, f"slot {slot_in_epoch}/32  {DIM}~{_format_duration(remaining)} remaining{RESET}"
            else:
                # Pre-boundary: counting to epoch boundary
                remaining = (SLOTS_PER_EPOCH - slot_in_epoch) * SECONDS_PER_SLOT
                bar = _render_bar(slot_in_epoch, BAR_WIDTH, midpoint=mid)
                return bar, f"slot {slot_in_epoch}/32  {DIM}~{_format_duration(remaining)} to boundary{RESET}"

        elif self.phase == PHASE_WAITING:
            target = self.start_slot_offset
            filled = min(slot_in_epoch, target)
            bar = _render_bar(filled, target)
            remaining = (target - slot_in_epoch) * SECONDS_PER_SLOT if slot_in_epoch < target else 0
            return bar, f"slot {slot_in_epoch} → {target}  {DIM}~{_format_duration(remaining)}{RESET}"

        elif self.phase in (PHASE_SETTLING, PHASE_SYNCING):
            if not bp._is_tracking_live:
                return "", ""
            bar = _render_bar(slot_in_epoch, BAR_WIDTH)
            return bar, f"{DIM}slot {slot_in_epoch}/32{RESET}"

        return "", ""

    def _get_freq_display(self) -> str:
        """Show effective sampling info."""
        return "1000 Hz"


def _render_bar(filled: int, total: int, midpoint: int | None = None) -> str:
    """Render a progress bar with optional midpoint marker."""
    filled = max(0, min(filled, total))
    chars = []
    for i in range(total):
        if i == midpoint:
            if i < filled:
                chars.append(f"{CYAN}┃{RESET}")
            else:
                chars.append(f"{DIM}┃{RESET}")
        elif i < filled:
            chars.append(f"{GREEN}━{RESET}")
        else:
            chars.append(f"{DIM}─{RESET}")
    return "".join(chars)


def _get_terminal_width() -> int:
    try:
        return os.get_terminal_size().columns
    except (OSError, ValueError):
        return 80


def _format_duration(seconds: float) -> str:
    total = int(seconds)
    if total < 3600:
        return f"{total // 60}:{total % 60:02d}"
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02d}:{s:02d}"
