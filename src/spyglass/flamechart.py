"""Interactive flame chart generation: time-series profiling across epoch boundaries."""

import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from .categories import categorize_sample, load_categories
from .config import SpyglassConfig
from .constants import SECONDS_PER_SLOT, log, log_end, log_start, log_step
from .filters import get_clock_offset, load_epochs

# A block flows through three phases inside Lighthouse, each visible as a
# distinct burst of perf stacks. We tag every in-window sample by phase and group
# contiguous samples into a per-block "processing burst" (see
# `_build_block_markers`). All patterns are matched as substrings against the
# (offset-stripped) stack, kept lenient to survive symbol-form differences across
# Lighthouse builds.
#
# 1. ARRIVAL — the block is received over gossip and gossip-verified; the moment
#    it "first arrives". Matches both "GossipVerifiedBlock<T>::new" (unstable) and
#    "GossipVerifiedBlock::new" (the experimental / upstreaming branch).
BLOCK_ARRIVAL_PATTERNS = (
    "GossipVerifiedBlock",
    "process_gossip_unverified_block",
    "process_gossip_block",
    "verify_block_for_gossip",
)
# 2. IMPORT — full block verification + state transition + import. Bridges the
#    arrival and head phases of one block into a single burst.
BLOCK_IMPORT_PATTERNS = (
    "::process_block",
    "import_block",
    "process_gossip_verified_block",
)
# 3. HEAD — fork choice re-runs after import and enthrones the block as the new
#    canonical head. `recompute_head_at_slot_internal` is reliably sampled even in
#    optimized builds; `after_new_head` runs only when the head actually changes
#    (so its presence confirms the block truly *became* head).
BLOCK_HEAD_PATTERNS = ("recompute_head", "after_new_head")
BLOCK_HEAD_CHANGED_PATTERN = "after_new_head"

# One block's phases are a tight, near-contiguous run of samples; distinct blocks
# are seconds apart (typically one per slot) and periodic fork-choice recomputes
# sit well clear of any block burst. Samples within this gap collapse into one
# burst.
BLOCK_ARRIVAL_CLUSTER_GAP = 1.0


def cmd_flamechart(
    config: SpyglassConfig,
    profile_dir: Path,
    bin_size: float = 0.5,
    verbose: bool = False,
):
    """Generate an interactive flame chart for epoch boundary profiling.

    Bins perf samples into time slices and produces a self-contained HTML file
    with an interactive flamegraph + category timeline.

    Args:
        config: Spyglass configuration object
        profile_dir: Directory containing perf.data and epochs.json
        bin_size: Width of each time bin in seconds (default: 0.5s)
        verbose: Show processing details
    """
    profile_dir = Path(profile_dir).resolve()
    perf_data = profile_dir / "perf.data"

    if not perf_data.exists():
        print(f"ERROR: perf.data not found in {profile_dir}", file=sys.stderr)
        sys.exit(1)

    epochs = load_epochs(profile_dir)
    if not epochs:
        print("ERROR: No epoch boundaries found in epochs.json", file=sys.stderr)
        sys.exit(1)

    warmup = config.filtering.epoch_boundary_warmup
    cooldown = config.filtering.epoch_boundary_cooldown
    window = warmup + cooldown

    nickname = profile_dir.parent.name
    log_start("flamechart", nickname)
    log(f"window: {window}s ({warmup}s warmup + {cooldown}s cooldown)  bin: {bin_size}s  epochs: {len(epochs)}")

    # Compute clock offset
    clock_offset = get_clock_offset(profile_dir)
    if clock_offset is None:
        print("ERROR: Cannot determine clock offset", file=sys.stderr)
        sys.exit(1)

    # Build time ranges for each epoch boundary (in perf monotonic time)
    boundary_windows = []
    for epoch in epochs:
        boundary_time = epoch["slot_start_time"]
        wall_start = boundary_time - warmup
        wall_end = boundary_time + cooldown
        perf_start = wall_start - clock_offset
        perf_end = wall_end - clock_offset
        boundary_windows.append(
            {
                "epoch": epoch["epoch"],
                "boundary_time": boundary_time,
                "perf_start": perf_start,
                "perf_end": perf_end,
                "perf_boundary": boundary_time - clock_offset,
            }
        )

    # Compute bins
    num_bins = int(window / bin_size)
    print(f"  Bins: {num_bins} × {bin_size}s")

    # Process perf script output, binning samples and detecting block processing
    log_step("processing perf.data")
    bins_data, block_events = _process_perf_into_bins(
        perf_data, boundary_windows, num_bins, bin_size, verbose
    )

    categories = load_categories(config.config_dir / "categories.toml")
    bins_categories = None
    if categories:
        log_step("categorizing samples per bin")
        bins_categories = _categorize_bins(bins_data, categories)

    perf_frequency = None
    pr_number = None
    genesis_time = None
    run_json_path = profile_dir / "run.json"
    if run_json_path.exists():
        run_info = json.loads(run_json_path.read_text())
        perf_frequency = run_info.get("perf_frequency")
        pr_number = run_info.get("pr")
        genesis_time = run_info.get("genesis_time")

    block_markers = _build_block_markers(block_events, boundary_windows, genesis_time)
    if block_markers:
        paired = sum(1 for m in block_markers if "arrival" in m and "head" in m)
        log(f"blocks: {len(block_markers)} marked ({paired} with arrival→head span)")

    view_dir = profile_dir / "views" / "epoch_boundary"
    view_dir.mkdir(parents=True, exist_ok=True)
    output_path = view_dir / "flamechart.html"
    log_step("generating flamechart.html")
    _generate_html(
        output_path,
        bins_data,
        bins_categories,
        categories,
        bin_size=bin_size,
        warmup=warmup,
        cooldown=cooldown,
        epochs=epochs,
        perf_frequency=perf_frequency,
        nickname=nickname,
        pr_number=pr_number,
        block_markers=block_markers,
    )

    log_end(f"done → {output_path}")


def _process_perf_into_bins(
    perf_data: Path,
    boundary_windows: list[dict],
    num_bins: int,
    bin_size: float,
    verbose: bool,
) -> tuple[list[Counter], list[list[tuple]]]:
    """Run perf script and bin samples by time offset within the boundary window.

    Returns a tuple of:
      - a list of Counters, one per bin, mapping stack strings to sample counts.
        Samples from all epoch boundaries are combined (overlaid).
      - a list (parallel to ``boundary_windows``) of block-processing events
        observed within each window. Each event is a tuple
        ``(timestamp, arrival, import_, head, head_changed)`` of booleans flagging
        which block phase(s) the sample's stack matched.
    """
    bins = [Counter() for _ in range(num_bins)]
    block_events: list[list[tuple]] = [[] for _ in boundary_windows]
    env = {**os.environ, "DEBUGINFOD_URLS": ""}

    proc = subprocess.Popen(
        ["perf", "script", "--no-inline", "-i", str(perf_data)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )

    header_re = re.compile(r"\S+\s+\d+.*\s+(\d+\.\d+):")
    current_block = []
    current_ts = None
    stats = [0, 0]  # [total_samples, binned_samples]

    for raw_line in proc.stdout:
        line = raw_line.decode("utf-8", errors="replace")

        if line.strip() == "":
            # End of sample block, process it
            _process_sample(
                current_ts, current_block, boundary_windows, num_bins, bin_size,
                bins, block_events, stats,
            )
            current_block = []
            current_ts = None
            continue

        # Check if this is a header line
        if not line.startswith(("\t", " ")):
            m = header_re.match(line)
            if m:
                current_ts = float(m.group(1))

        current_block.append(line)

    # Flush last sample block (perf script may not end with a blank line)
    _process_sample(
        current_ts, current_block, boundary_windows, num_bins, bin_size,
        bins, block_events, stats,
    )

    proc.wait()

    if verbose:
        print(f"    Total samples: {stats[0]:,}")
        print(f"    Binned samples: {stats[1]:,}")
        samples_per_bin = [sum(b.values()) for b in bins]
        print(f"    Samples per bin: min={min(samples_per_bin):,} max={max(samples_per_bin):,}")
        print(f"    Block-processing samples: {sum(len(h) for h in block_events):,}")

    return bins, block_events


def _process_sample(
    current_ts: float | None,
    current_block: list[str],
    boundary_windows: list[dict],
    num_bins: int,
    bin_size: float,
    bins: list[Counter],
    block_events: list[list[tuple]],
    stats: list[int],
):
    """Bin one completed perf sample and tag it by block-processing phase.

    Updates ``bins`` (per-bin stack counts), ``block_events`` (per-window
    phase-tagged events) and ``stats`` ([total, binned]) in place.
    """
    if current_ts is None or not current_block:
        return
    stats[0] += 1
    bin_idx, window_idx = _locate_sample(current_ts, boundary_windows, num_bins, bin_size)
    if bin_idx is None:
        return

    # Build stack string from block (skip header line)
    frames = []
    for bl in current_block[1:]:
        bl = bl.strip()
        if bl:
            # Extract function name from "addr func+0xNN (dso)" format
            parts = bl.split(" ", 1)
            if len(parts) >= 2:
                func = parts[1].rsplit(" (", 1)[0]
                # Strip +0x... offset suffixes
                plus_idx = func.rfind("+0x")
                if plus_idx > 0:
                    func = func[:plus_idx]
                frames.append(func)
    if not frames:
        return

    stack = ";".join(reversed(frames))
    bins[bin_idx][stack] += 1
    stats[1] += 1

    # Block-processing detection: tag the sample by which phase its stack shows
    # (a block arriving over gossip, being imported, or being set as fork-choice
    # head). Events are grouped into per-block bursts later in `_build_block_markers`.
    if window_idx is None:
        return
    arrival = any(p in stack for p in BLOCK_ARRIVAL_PATTERNS)
    import_ = any(p in stack for p in BLOCK_IMPORT_PATTERNS)
    head = any(p in stack for p in BLOCK_HEAD_PATTERNS)
    if arrival or import_ or head:
        head_changed = BLOCK_HEAD_CHANGED_PATTERN in stack
        block_events[window_idx].append((current_ts, arrival, import_, head, head_changed))


def _locate_sample(
    timestamp: float,
    boundary_windows: list[dict],
    num_bins: int,
    bin_size: float,
) -> tuple[int | None, int | None]:
    """Locate a sample's bin and owning epoch-boundary window by timestamp.

    Samples from multiple epoch boundaries are overlaid into the same bin
    structure. Returns ``(bin_idx, window_idx)``, or ``(None, None)`` if the
    sample doesn't fall in any boundary window.
    """
    for w_idx, w in enumerate(boundary_windows):
        if w["perf_start"] <= timestamp <= w["perf_end"]:
            offset = timestamp - w["perf_start"]
            idx = int(offset / bin_size)
            if 0 <= idx < num_bins:
                return idx, w_idx
    return None, None


def _build_block_markers(
    block_events: list[list[tuple]],
    boundary_windows: list[dict],
    genesis_time: float | None,
) -> list[dict]:
    """Group per-window block-processing events into per-block markers.

    Events (arrival / import / head, see ``_process_sample``) are clustered into
    contiguous "processing bursts" (samples within ``BLOCK_ARRIVAL_CLUSTER_GAP``
    seconds). Each burst that contains a block arrival or import is one block; we
    record when it arrived (first arrival sample) and when fork choice set it as
    head (first head sample after the arrival). Periodic fork-choice recomputes
    form head-only bursts and are skipped — they aren't a block being processed.

    Each marker carries ``arrival`` and/or ``head`` time offsets relative to the
    epoch boundary (so they overlay onto the shared timeline like the bins), the
    processing latency between them, and the approximate wall-clock slot.
    """
    markers = []
    for w_idx, window in enumerate(boundary_windows):
        events = sorted(block_events[w_idx])
        if not events:
            continue
        # Group into contiguous processing bursts.
        bursts: list[list[tuple]] = []
        for ev in events:
            if not bursts or ev[0] - bursts[-1][-1][0] > BLOCK_ARRIVAL_CLUSTER_GAP:
                bursts.append([])
            bursts[-1].append(ev)

        for burst in bursts:
            arrivals = [e[0] for e in burst if e[1]]
            imports = [e[0] for e in burst if e[2]]
            heads = [e[0] for e in burst if e[3]]
            # A block burst must show the block arriving or being imported;
            # head-only bursts are periodic fork-choice recomputes, not blocks.
            if not arrivals and not imports:
                continue

            arrival_ts = min(arrivals) if arrivals else None
            # Head is set after the block is processed: take the first head
            # sample at/after the arrival (or import) so a periodic recompute that
            # merged into the burst before it can't be mistaken for the head-set.
            ref_ts = arrival_ts if arrival_ts is not None else min(imports)
            head_after = [t for t in heads if t >= ref_ts]
            head_ts = min(head_after) if head_after else None

            anchor_ts = arrival_ts if arrival_ts is not None else head_ts
            if anchor_ts is None:
                continue  # import-only fragment, nothing meaningful to mark

            pb = window["perf_boundary"]
            marker = {
                "epoch": window["epoch"],
                "samples": len(burst),
                "head_changed": any(e[4] for e in burst),
            }
            if arrival_ts is not None:
                marker["arrival"] = round(arrival_ts - pb, 4)
            if head_ts is not None:
                marker["head"] = round(head_ts - pb, 4)
            if arrival_ts is not None and head_ts is not None:
                marker["process_time"] = round(head_ts - arrival_ts, 4)
            if genesis_time is not None:
                # Approximate slot the block was processed in (by anchor time).
                wall = window["boundary_time"] + (anchor_ts - pb)
                slot = int((wall - genesis_time) // SECONDS_PER_SLOT)
                marker["slot"] = slot
                marker["slot_offset"] = round(wall - genesis_time - slot * SECONDS_PER_SLOT, 2)
            markers.append(marker)

    markers.sort(key=lambda m: m.get("arrival", m.get("head", 0.0)))
    return markers


def _categorize_bins(
    bins_data: list[Counter],
    categories: list[dict],
) -> list[dict]:
    """Compute category breakdown for each bin.

    Returns list of dicts: {"category_counts": Counter, "total": int}
    """
    result = []
    for bin_counter in bins_data:
        cat_counts = Counter()
        total = 0
        for stack, count in bin_counter.items():
            total += count
            frames = stack.split(";")
            leaf = frames[-1] if frames else ""
            category = categorize_sample(stack, leaf, categories)
            cat_counts[category or "Uncategorized"] += count
        result.append({"category_counts": cat_counts, "total": total})
    return result


def _generate_html(
    output_path: Path,
    bins_data: list[Counter],
    bins_categories: list[dict] | None,
    categories: list[dict] | None,
    bin_size: float,
    warmup: float,
    cooldown: float,
    epochs: list[dict],
    perf_frequency: int | None = None,
    nickname: str | None = None,
    pr_number: int | None = None,
    block_markers: list[dict] | None = None,
):
    """Generate a self-contained HTML flame chart visualization."""
    num_bins = len(bins_data)
    window = warmup + cooldown

    # Prepare per-bin data for JS
    bins_json = []
    for i, bin_counter in enumerate(bins_data):
        t_start = i * bin_size - warmup
        t_end = t_start + bin_size
        total = sum(bin_counter.values())

        # Top 15 functions by self-time for this bin
        self_time = Counter()
        for stack, count in bin_counter.items():
            leaf = stack.rsplit(";", 1)[-1]
            self_time[leaf] += count

        top_funcs = [
            {"name": func, "samples": count, "pct": round(100.0 * count / total, 2) if total else 0}
            for func, count in self_time.most_common(15)
        ]

        # Category breakdown
        cat_breakdown = {}
        if bins_categories:
            cat_data = bins_categories[i]
            for cat_name, count in cat_data["category_counts"].items():
                cat_breakdown[cat_name] = round(100.0 * count / total, 2) if total else 0

        bins_json.append(
            {
                "index": i,
                "t_start": round(t_start, 6),
                "t_end": round(t_end, 6),
                "total_samples": total,
                "top_functions": top_funcs,
                "categories": cat_breakdown,
            }
        )

    # Prepare collapsed stacks per bin for d3-flame-graph
    bins_collapsed = []
    for bin_counter in bins_data:
        # Convert to list of [stack, count] for JSON
        stacks = [[stack, count] for stack, count in bin_counter.most_common(500)]
        bins_collapsed.append(stacks)

    # Category names in order
    cat_names = [cat["name"] for cat in categories] + ["Uncategorized"] if categories else []

    # Category patterns for client-side highlighting
    cat_patterns = []
    if categories:
        for cat in categories:
            cat_patterns.append(
                {
                    "name": cat["name"],
                    "patterns": [p for p in cat.get("patterns", []) if not p.startswith("re:")],
                    "regex_patterns": [
                        p[3:] for p in cat.get("patterns", []) if p.startswith("re:")
                    ],
                    "leaf_patterns": [
                        p for p in cat.get("leaf_patterns", []) if not p.startswith("re:")
                    ],
                }
            )
        cat_patterns.append(
            {"name": "Uncategorized", "patterns": [], "regex_patterns": [], "leaf_patterns": []}
        )

    cat_colors = [
        "#a02020",  # BLS/Crypto: deep red
        "#8a6838",  # secp256k1/ENR: warm brown
        "#a89040",  # TLS/Noise: brass
        "#684830",  # Database: espresso
        "#30b850",  # Tree Hash: vivid green
        "#78a030",  # Shuffling: yellow-green
        "#9898a8",  # Formatting/Serialization: silver
        "#48a890",  # Allocator: mint teal
        "#e0a020",  # Metrics: bright amber
        "#d04850",  # Tracing/Logging: coral red
        "#7830a8",  # Milhouse Iteration: violet
        "#a068d0",  # Milhouse Tree Hash: light purple
        "#982880",  # Milhouse Mutations: magenta
        "#c83878",  # Milhouse SSZ: hot pink
        "#2a7a48",  # Attestation Verification: forest green
        "#206878",  # Sync Committee: dark teal
        "#3060c0",  # Aggregation Pool: royal blue
        "#c87028",  # Block Import: burnt orange
        "#b8a028",  # State Processing: olive gold
        "#5050b8",  # Fork Choice: indigo
        "#1890a8",  # Networking/libp2p: cerulean
        "#707838",  # Discovery: khaki
        "#607088",  # Tokio Runtime: blue grey
        "#384050",  # Beacon Processor: gunmetal
        "#907898",  # Validator Monitor: dusty purple
        "#282830",  # Uncategorized: near-black
    ]

    max_samples = max((sum(b.values()) for b in bins_data), default=0)

    data = {
        "bins": bins_json,
        "bins_collapsed": bins_collapsed,
        "category_names": cat_names,
        "category_colors": cat_colors[: len(cat_names)],
        "category_patterns": cat_patterns,
        "bin_size": bin_size,
        "warmup": warmup,
        "cooldown": cooldown,
        "window": window,
        "num_bins": num_bins,
        "epochs": [{"epoch": e["epoch"], "slot": e["slot"]} for e in epochs],
        "max_samples": max_samples,
        "perf_frequency": perf_frequency,
        "nickname": nickname,
        "pr_number": pr_number,
        "block_markers": block_markers or [],
    }

    template_path = Path(__file__).parent / "_flamechart_template.html"
    html = template_path.read_text().replace("/*DATA_PLACEHOLDER*/", json.dumps(data))
    output_path.write_text(html)
