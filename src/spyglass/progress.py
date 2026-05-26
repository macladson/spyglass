"""Progress display for profiling runs — ANSI multi-line block."""

import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .beacon_api import BeaconApiPoller

from .constants import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    RESET,
    SECONDS_PER_SLOT,
    SLOTS_PER_EPOCH,
    YELLOW,
    format_size,
)

# Bar is 64 characters wide = 2 epochs
BAR_WIDTH = 64

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
        start_slot: int = 16,
        end_slot: int = 15,
        epochs: int = 1,
    ):
        self.beacon_poller = beacon_poller
        self.watch_file = watch_file
        self.start_slot = start_slot
        self.end_slot = end_slot
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
        self._clear()

    def set_phase(self, phase: str):
        self.phase = phase

    def _tick_loop(self):
        while not self._stop.is_set():
            self._render()
            self._stop.wait(1.0)

    def _clear(self):
        if self._lines_drawn > 0:
            sys.stdout.write(f"\033[{self._lines_drawn}A")
            for _ in range(self._lines_drawn):
                sys.stdout.write("\033[K\n")
            sys.stdout.write(f"\033[{self._lines_drawn}A")
            sys.stdout.flush()
            self._lines_drawn = 0

    def _render(self):
        elapsed = time.time() - self._start_time
        bp = self.beacon_poller

        slot = bp.state.last_slot
        epoch = slot // SLOTS_PER_EPOCH if slot is not None else None
        slot_in_epoch = slot % SLOTS_PER_EPOCH if slot is not None else None

        # Phase label
        phase_str = {
            PHASE_SYNCING: f"{YELLOW}●{RESET} {BOLD}Syncing{RESET}",
            PHASE_SETTLING: f"{YELLOW}●{RESET} {BOLD}Settling{RESET}",
            PHASE_WAITING: f"{CYAN}●{RESET} {BOLD}Waiting{RESET}",
            PHASE_PROFILING: f"{GREEN}●{RESET} {BOLD}Profiling{RESET}",
            PHASE_DONE: f"{GREEN}✓{RESET} {BOLD}Done{RESET}",
        }.get(self.phase, self.phase)

        # Line 1: phase + context
        line1_parts = [f"  {phase_str}"]
        if epoch is not None:
            line1_parts.append(f"{DIM}epoch {epoch} slot {slot_in_epoch}{RESET}")
        line1_parts.append(f"{DIM}{_format_duration(elapsed)}{RESET}")
        line1 = "  ".join(line1_parts)

        # Line 2: progress bar
        line2 = f"  {self._build_bar(slot_in_epoch)}"

        # Line 3: details
        details = []
        if self.watch_file and self.watch_file.exists():
            size = self.watch_file.stat().st_size
            if size > 0:
                details.append(f"perf.data: {format_size(size)}")
        epochs_detected = len(bp.state.epoch_boundaries)
        if epochs_detected > 0:
            details.append(f"epochs: {epochs_detected}/{self.epochs}")
        eta = self._get_eta(slot_in_epoch)
        if eta:
            details.append(eta)
        line3 = f"  {DIM}{' · '.join(details)}{RESET}" if details else ""

        # Draw
        lines = [line1, line2]
        if line3:
            lines.append(line3)

        self._clear()
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
        self._lines_drawn = len(lines)

    def _build_bar(self, slot_in_epoch: int | None) -> str:
        """Build the 64-char progress bar spanning 2 epochs.

        Layout (64 chars = 2 epochs):
          [epoch N-1 slots 0..31][epoch N slots 0..31]
                                 ^ epoch boundary (centre)

        Recording window: start_slot (first epoch) to end_slot (second epoch)
        Separators: start_slot (green), centre/boundary (white), end_slot (green)
        """
        # Map positions:
        # 0..31 = first epoch (contains start_slot)
        # 32..63 = second epoch (contains end_slot, boundary is at 32)
        #
        # Separators at:
        #   start_slot (green) — where recording begins
        #   32 (white) — epoch boundary
        #   32 + end_slot (green) — where recording ends

        start_pos = self.start_slot
        boundary_pos = 32
        end_pos = 32 + self.end_slot

        # Determine fill position based on phase and current slot
        fill_to = self._get_fill_position(slot_in_epoch)

        # Build bar character by character
        chars = []
        for i in range(BAR_WIDTH):
            is_separator = False
            sep_color = ""

            if i == start_pos:
                is_separator = True
                sep_color = GREEN
            elif i == boundary_pos:
                is_separator = True
                sep_color = "\033[37m"  # white
            elif i == end_pos:
                is_separator = True
                sep_color = GREEN

            if is_separator:
                chars.append(f"{sep_color}│{RESET}")
            elif i < fill_to:
                # Filled — green if in profiling zone, dim white otherwise
                if start_pos <= i < end_pos:
                    chars.append(f"{GREEN}━{RESET}")
                else:
                    chars.append(f"{DIM}━{RESET}")
            else:
                chars.append(f"{DIM}─{RESET}")

        return "".join(chars)

    def _get_fill_position(self, slot_in_epoch: int | None) -> int:
        """Convert current slot + phase to a fill position in the 64-char bar."""
        if slot_in_epoch is None:
            return 0

        if self.phase in (PHASE_SYNCING, PHASE_SETTLING):
            return 0

        elif self.phase == PHASE_WAITING:
            # Only fill if we're approaching start_slot (not past it waiting for next epoch)
            if slot_in_epoch < self.start_slot:
                return slot_in_epoch
            return 0

        elif self.phase == PHASE_PROFILING:
            bp = self.beacon_poller
            if bp.target_reached.is_set():
                # Post-boundary: we're in the second epoch
                # Fill = full first epoch + slots into second epoch
                return 32 + min(slot_in_epoch + 1, self.end_slot + 1)
            else:
                # Pre-boundary: filling through first epoch
                return min(slot_in_epoch + 1, 32)

        elif self.phase == PHASE_DONE:
            return BAR_WIDTH

        return 0

    def _get_eta(self, slot_in_epoch: int | None) -> str:
        """Compute ETA string based on current phase."""
        if slot_in_epoch is None:
            return ""

        if self.phase == PHASE_WAITING:
            remaining = (self.start_slot - slot_in_epoch) * SECONDS_PER_SLOT
            if remaining > 0:
                return f"~{_format_duration(remaining)} to start"
            return ""

        elif self.phase == PHASE_PROFILING:
            bp = self.beacon_poller
            if bp.target_reached.is_set():
                remaining = (self.end_slot - slot_in_epoch) * SECONDS_PER_SLOT
                if remaining > 0:
                    return f"~{_format_duration(remaining)} remaining"
                return ""
            else:
                remaining = (SLOTS_PER_EPOCH - slot_in_epoch) * SECONDS_PER_SLOT
                return f"~{_format_duration(remaining)} to boundary"

        return ""


def _format_duration(seconds: float) -> str:
    total = int(seconds)
    if total < 0:
        return "0:00"
    if total < 3600:
        return f"{total // 60}:{total % 60:02d}"
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02d}:{s:02d}"
