"""Interactive flame chart generation — time-series profiling across epoch boundaries."""

import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from .categories import load_categories, categorize_sample
from .config import SpyglassConfig
from .constants import BOLD, RESET
from .filters import load_epochs, load_sync_status, _load_clock_offset, _get_first_perf_timestamp


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

    print(f"{BOLD}=== Flame Chart ==={RESET}")
    print(f"  {BOLD}Window:{RESET}   {window}s ({warmup}s warmup + {cooldown}s cooldown)")
    print(f"  {BOLD}Bin size:{RESET} {bin_size}s")
    print(f"  {BOLD}Epochs:{RESET}   {len(epochs)}")
    print()

    # Compute clock offset
    clock_offset = _load_clock_offset(profile_dir)
    if clock_offset is None:
        sync_status = load_sync_status(profile_dir)
        recording_start = sync_status.get("recording_start_time")
        env = {**os.environ, "DEBUGINFOD_URLS": ""}
        first_ts = _get_first_perf_timestamp(perf_data, env)
        if first_ts is None or recording_start is None:
            print("ERROR: Cannot determine clock offset", file=sys.stderr)
            sys.exit(1)
        clock_offset = recording_start - first_ts

    # Build time ranges for each epoch boundary (in perf monotonic time)
    boundary_windows = []
    for epoch in epochs:
        boundary_time = epoch["slot_start_time"]
        wall_start = boundary_time - warmup
        wall_end = boundary_time + cooldown
        perf_start = wall_start - clock_offset
        perf_end = wall_end - clock_offset
        boundary_windows.append({
            "epoch": epoch["epoch"],
            "boundary_time": boundary_time,
            "perf_start": perf_start,
            "perf_end": perf_end,
            "perf_boundary": boundary_time - clock_offset,
        })

    # Compute bins
    num_bins = int(window / bin_size)
    print(f"  Bins: {num_bins} × {bin_size}s")

    # Process perf script output, binning samples
    print(f"  Processing perf.data...")
    bins_data = _process_perf_into_bins(
        perf_data, boundary_windows, num_bins, bin_size, verbose
    )

    # Category analysis per bin
    categories = load_categories()
    bins_categories = None
    if categories:
        print(f"  Categorizing samples per bin...")
        bins_categories = _categorize_bins(bins_data, categories)

    # Generate HTML
    view_dir = profile_dir / "views" / "epoch_boundary"
    view_dir.mkdir(parents=True, exist_ok=True)
    output_path = view_dir / "flamechart.html"
    print(f"  Generating {output_path.name}...")
    _generate_html(
        output_path, bins_data, bins_categories, categories,
        bin_size=bin_size, warmup=warmup, cooldown=cooldown,
        epochs=epochs,
    )

    print(f"\n{BOLD}=== Flame chart complete ==={RESET}")
    print(f"  {BOLD}Output:{RESET} {output_path}")
    print()


def _process_perf_into_bins(
    perf_data: Path,
    boundary_windows: list[dict],
    num_bins: int,
    bin_size: float,
    verbose: bool,
) -> list[Counter]:
    """Run perf script and bin samples by time offset within the boundary window.

    Returns a list of Counters, one per bin, mapping stack strings to sample counts.
    Samples from all epoch boundaries are combined (overlaid).
    """
    bins = [Counter() for _ in range(num_bins)]
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
    total_samples = 0
    binned_samples = 0

    for raw_line in proc.stdout:
        line = raw_line.decode("utf-8", errors="replace")

        if line.strip() == "":
            # End of sample block — assign to a bin
            if current_ts is not None and current_block:
                total_samples += 1
                bin_idx = _assign_to_bin(current_ts, boundary_windows, num_bins, bin_size)
                if bin_idx is not None:
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
                    if frames:
                        stack = ";".join(reversed(frames))
                        bins[bin_idx][stack] += 1
                        binned_samples += 1
            current_block = []
            current_ts = None
            continue

        # Check if this is a header line
        if not line.startswith(("\t", " ")):
            m = header_re.match(line)
            if m:
                current_ts = float(m.group(1))

        current_block.append(line)

    proc.wait()

    if verbose:
        print(f"    Total samples: {total_samples:,}")
        print(f"    Binned samples: {binned_samples:,}")
        samples_per_bin = [sum(b.values()) for b in bins]
        print(f"    Samples per bin: min={min(samples_per_bin):,} max={max(samples_per_bin):,}")

    return bins


def _assign_to_bin(
    timestamp: float,
    boundary_windows: list[dict],
    num_bins: int,
    bin_size: float,
) -> int | None:
    """Determine which bin a sample belongs to based on its timestamp.

    Samples from multiple epoch boundaries are overlaid into the same bin structure.
    Returns bin index or None if the sample doesn't fall in any boundary window.
    """
    for w in boundary_windows:
        if w["perf_start"] <= timestamp <= w["perf_end"]:
            offset = timestamp - w["perf_start"]
            idx = int(offset / bin_size)
            if 0 <= idx < num_bins:
                return idx
    return None


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

        bins_json.append({
            "index": i,
            "t_start": round(t_start, 2),
            "t_end": round(t_end, 2),
            "total_samples": total,
            "top_functions": top_funcs,
            "categories": cat_breakdown,
        })

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
            cat_patterns.append({
                "name": cat["name"],
                "patterns": [p for p in cat.get("patterns", []) if not p.startswith("re:")],
                "regex_patterns": [p[3:] for p in cat.get("patterns", []) if p.startswith("re:")],
                "leaf_patterns": [p for p in cat.get("leaf_patterns", []) if not p.startswith("re:")],
            })
        cat_patterns.append({"name": "Uncategorized", "patterns": [], "regex_patterns": [], "leaf_patterns": []})

    # Category colors
    cat_colors = [
        "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
        "#42d4f4", "#f032e6", "#bfef45", "#fabed4", "#469990",
        "#dcbeff", "#9A6324", "#fffac8", "#800000", "#aaffc3",
    ]

    data = {
        "bins": bins_json,
        "bins_collapsed": bins_collapsed,
        "category_names": cat_names,
        "category_colors": cat_colors[:len(cat_names)],
        "category_patterns": cat_patterns,
        "bin_size": bin_size,
        "warmup": warmup,
        "cooldown": cooldown,
        "window": window,
        "num_bins": num_bins,
        "epochs": [{"epoch": e["epoch"], "slot": e["slot"]} for e in epochs],
    }

    html = _HTML_TEMPLATE.replace("/*DATA_PLACEHOLDER*/", json.dumps(data))
    output_path.write_text(html)


_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Spyglass Flame Chart</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace; background: #1a1a2e; color: #eee; padding: 20px; }
h1 { font-size: 1.4em; margin-bottom: 10px; color: #fff; }
.subtitle { color: #888; font-size: 0.85em; margin-bottom: 20px; }
.container { max-width: 1400px; margin: 0 auto; }

/* Timeline */
.timeline { position: relative; height: 200px; margin-bottom: 20px; background: #16213e; border-radius: 8px; padding: 10px; }
.timeline-title { font-size: 0.8em; color: #888; margin-bottom: 5px; }
.stacked-chart { position: relative; height: 150px; display: flex; align-items: end; gap: 1px; }
.bin-bar { display: flex; flex-direction: column-reverse; flex: 1; cursor: pointer; border-radius: 2px 2px 0 0; overflow: hidden; opacity: 0.8; transition: opacity 0.1s; }
.bin-bar:hover, .bin-bar.active { opacity: 1; }
.bin-segment { width: 100%; transition: height 0.1s; }
.epoch-marker { position: absolute; top: 0; bottom: 20px; width: 2px; background: #fff3; z-index: 5; }
.epoch-marker::after { content: "epoch boundary"; position: absolute; top: -18px; left: 4px; font-size: 0.65em; color: #888; white-space: nowrap; }
.time-axis { display: flex; justify-content: space-between; font-size: 0.7em; color: #666; margin-top: 4px; }

/* Slider */
.slider-container { margin-bottom: 20px; }
.slider-container input[type=range] { width: 100%; cursor: pointer; }
.slider-label { display: flex; justify-content: space-between; font-size: 0.8em; color: #888; }

/* Main content */
.content { display: grid; grid-template-columns: 1fr 300px; gap: 20px; }
.flamegraph-panel { background: #16213e; border-radius: 8px; padding: 15px; }
.sidebar { background: #16213e; border-radius: 8px; padding: 15px; }
.sidebar h3 { font-size: 0.9em; margin-bottom: 10px; color: #ccc; }

/* Functions table */
.func-table { width: 100%; font-size: 0.75em; }
.func-table tr { cursor: pointer; transition: background 0.1s; }
.func-table tr:hover { background: #ffffff10; }
.func-table tr.active { background: #ffffff20; }
.func-table td { padding: 3px 5px; border-bottom: 1px solid #ffffff10; }
.func-table .pct { text-align: right; color: #4fc3f7; font-weight: bold; white-space: nowrap; }
.func-table .name { max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #ccc; }

/* Category legend */
.cat-legend { margin-bottom: 15px; }
.cat-item { display: flex; align-items: center; gap: 6px; font-size: 0.75em; margin-bottom: 3px; padding: 2px 4px; border-radius: 3px; transition: background 0.1s, opacity 0.1s; cursor: pointer; }
.cat-item:hover { background: #ffffff10; }
.cat-item.active { background: #ffffff20; outline: 1px solid #4fc3f7; }
.cat-item.dimmed { opacity: 0.4; }
.cat-swatch { width: 10px; height: 10px; border-radius: 2px; flex-shrink: 0; }
.cat-pct { color: #4fc3f7; min-width: 40px; text-align: right; }
.clear-btn { font-size: 0.7em; color: #4fc3f7; cursor: pointer; margin-left: auto; padding: 2px 8px; border: 1px solid #4fc3f7; border-radius: 3px; display: none; }
.clear-btn:hover { background: #4fc3f7; color: #1a1a2e; }

/* Flamegraph */
.fg-container { width: 100%; overflow-x: auto; }
.fg-frame { stroke: #1a1a2e; stroke-width: 0.5px; cursor: pointer; }
.fg-frame:hover { stroke: #fff; stroke-width: 1px; }
.fg-label { font-size: 11px; fill: #fff; pointer-events: none; }
.fg-info { font-size: 0.8em; color: #888; margin-bottom: 10px; }

/* Playback */
.controls { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.controls button { background: #4fc3f7; border: none; color: #1a1a2e; padding: 5px 15px; border-radius: 4px; cursor: pointer; font-weight: bold; }
.controls button:hover { background: #81d4fa; }
.controls .time-display { font-size: 1.1em; font-weight: bold; color: #4fc3f7; min-width: 80px; }
</style>
</head>
<body>
<div class="container">
<h1>🔬 Spyglass Flame Chart</h1>
<div class="subtitle" id="subtitle"></div>

<div class="controls">
  <button id="play-btn" onclick="togglePlay()">▶ Play</button>
  <span class="time-display" id="time-display">-6.00s</span>
  <span style="color:#666;font-size:0.8em" id="sample-count"></span>
</div>

<div class="timeline">
  <div class="timeline-title">Category breakdown over time</div>
  <div class="stacked-chart" id="stacked-chart"></div>
  <div class="time-axis" id="time-axis"></div>
</div>

<div class="slider-container">
  <input type="range" id="bin-slider" min="0" max="23" value="0" oninput="selectBin(+this.value)">
  <div class="slider-label"><span id="slider-start"></span><span>epoch boundary</span><span id="slider-end"></span></div>
</div>

<div class="content">
  <div class="flamegraph-panel">
    <div class="fg-info" id="fg-info"></div>
    <div class="fg-container" id="fg-container"></div>
  </div>
  <div class="sidebar">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
      <h3 style="margin:0">Categories</h3>
      <span class="clear-btn" id="clear-filters" onclick="clearFilters()">clear</span>
    </div>
    <div class="cat-legend" id="cat-legend"></div>
    <h3>Top Functions</h3>
    <table class="func-table" id="func-table"></table>
  </div>
</div>
</div>

<script>
const DATA = /*DATA_PLACEHOLDER*/;
let currentBin = 0;
let playing = false;
let playInterval = null;

function init() {
  document.getElementById("subtitle").textContent =
    `${DATA.epochs.length} epoch(s) overlaid | ${DATA.window}s window (${DATA.warmup}s + ${DATA.cooldown}s) | ${DATA.bin_size}s bins`;
  document.getElementById("bin-slider").max = DATA.num_bins - 1;
  document.getElementById("slider-start").textContent = `-${DATA.warmup}s`;
  document.getElementById("slider-end").textContent = `+${DATA.cooldown}s`;
  buildStackedChart();
  buildCategoryLegend();
  selectBin(0);
}

function buildStackedChart() {
  const chart = document.getElementById("stacked-chart");
  const maxSamples = Math.max(...DATA.bins.map(b => b.total_samples));

  for (let i = 0; i < DATA.num_bins; i++) {
    const bin = DATA.bins[i];
    const bar = document.createElement("div");
    bar.className = "bin-bar";
    bar.dataset.bin = i;
    bar.onclick = () => { selectBin(i); document.getElementById("bin-slider").value = i; };

    const barHeight = (bin.total_samples / maxSamples) * 100;
    bar.style.height = barHeight + "%";

    // Stack category segments
    DATA.category_names.forEach((cat, ci) => {
      const pct = bin.categories[cat] || 0;
      if (pct > 0) {
        const seg = document.createElement("div");
        seg.className = "bin-segment";
        seg.style.height = pct + "%";
        seg.style.background = DATA.category_colors[ci % DATA.category_colors.length];
        bar.appendChild(seg);
      }
    });

    chart.appendChild(bar);
  }

  // Epoch boundary marker
  const markerPos = (DATA.warmup / DATA.window) * 100;
  const marker = document.createElement("div");
  marker.className = "epoch-marker";
  marker.style.left = markerPos + "%";
  chart.appendChild(marker);

  // Time axis
  const axis = document.getElementById("time-axis");
  for (let t = -DATA.warmup; t <= DATA.cooldown; t += (DATA.window / 4)) {
    const span = document.createElement("span");
    span.textContent = (t >= 0 ? "+" : "") + t.toFixed(1) + "s";
    axis.appendChild(span);
  }
}

function buildCategoryLegend() {
  // Initial render — will be re-sorted on each selectBin call
}

function selectBin(idx) {
  currentBin = idx;
  const bin = DATA.bins[idx];

  // Update active bar
  document.querySelectorAll(".bin-bar").forEach((b, i) => {
    b.classList.toggle("active", i === idx);
  });

  // Time display
  document.getElementById("time-display").textContent =
    (bin.t_start >= 0 ? "+" : "") + bin.t_start.toFixed(2) + "s";
  document.getElementById("sample-count").textContent =
    bin.total_samples.toLocaleString() + " samples";

  // Categories — sort by current percentage descending
  const legend = document.getElementById("cat-legend");
  const catEntries = DATA.category_names.map((cat, ci) => ({
    name: cat, ci, pct: bin.categories[cat] || 0
  })).sort((a, b) => b.pct - a.pct);
  const hasFilters = activeCategories.size > 0 || activeFunctions.size > 0;
  legend.innerHTML = catEntries.map(({name, ci, pct}) => {
    const isActive = activeCategories.has(ci);
    const dimmed = hasFilters && !isActive ? ' dimmed' : '';
    return `<div class="cat-item${isActive ? ' active' : ''}${dimmed}" onclick="toggleCategory(${ci})">` +
      `<div class="cat-swatch" style="background:${DATA.category_colors[ci]}"></div>` +
      `<span class="cat-pct">${pct.toFixed(1)}%</span><span>${name}</span></div>`;
  }).join("");

  // Top functions
  const table = document.getElementById("func-table");
  table.innerHTML = bin.top_functions.map((f, fi) => {
    const isActive = activeFunctions.has(f.name);
    return `<tr class="${isActive ? 'active' : ''}" onclick="toggleFunction(${fi})">` +
      `<td class="pct">${f.pct.toFixed(1)}%</td>` +
      `<td class="name" title="${escHtml(f.name)}">${escHtml(f.name)}</td></tr>`;
  }).join("");

  // Update clear button visibility
  document.getElementById("clear-filters").style.display = hasFilters ? '' : 'none';

  // Flamegraph
  renderFlamegraph(idx);
}

function renderFlamegraph(idx) {
  const container = document.getElementById("fg-container");
  const collapsed = DATA.bins_collapsed[idx];
  if (!collapsed || collapsed.length === 0) {
    container.innerHTML = '<div style="color:#666;padding:20px">No samples in this bin</div>';
    return;
  }

  // Build tree from collapsed stacks
  const root = {name: "all", value: 0, children: {}};
  for (const [stack, count] of collapsed) {
    const frames = stack.split(";");
    let node = root;
    node.value += count;
    for (const frame of frames) {
      if (!node.children[frame]) {
        node.children[frame] = {name: frame, value: 0, children: {}};
      }
      node = node.children[frame];
      node.value += count;
    }
  }

  // Collapse single-child chains: skip frames where one child has >80% of
  // the parent's value. This removes uninformative runtime wrapper layers
  // (tokio runtime, thread start, [unknown], etc.) and starts where branching occurs.
  function collapse(node) {
    const children = Object.values(node.children);
    if (children.length === 1 && children[0].value > node.value * 0.80) {
      return collapse(children[0]);
    }
    // Also collapse if the top child dominates and others are negligible
    if (children.length > 1) {
      const sorted = children.sort((a,b) => b.value - a.value);
      if (sorted[0].value > node.value * 0.80) {
        return collapse(sorted[0]);
      }
    }
    return node;
  }
  const displayRoot = collapse(root);

  // Flatten tree for rendering
  const width = container.clientWidth || 900;
  const frameH = 18;
  const rects = [];
  const totalValue = displayRoot.value;

  function layout(node, x, y, w) {
    if (w < 2) return; // Skip frames narrower than 2px
    rects.push({name: node.name, x, y, w, value: node.value});
    let childX = x;
    const childEntries = Object.values(node.children).sort((a,b) => b.value - a.value);
    for (const child of childEntries) {
      const childW = (child.value / node.value) * w;
      layout(child, childX, y + frameH, childW);
      childX += childW;
    }
  }

  layout(displayRoot, 0, 0, width);
  const maxY = rects.length ? Math.max(...rects.map(r => r.y)) + frameH : frameH;

  // Render SVG — icicle style (root at top, stacks grow downward)
  const svg = rects.map(r => {
    const pct = (100 * r.value / totalValue).toFixed(1);
    const hue = hashColor(r.name);
    const esc = escSvg(truncate(r.name, r.w));
    const label = r.w > 40 ? `<text class="fg-label" x="${r.x+2}" y="${r.y + 13}">${esc}</text>` : "";
    return `<rect class="fg-frame" x="${r.x}" y="${r.y}" width="${r.w}" height="${frameH - 1}"
      fill="hsl(${hue},60%,45%)"><title>${escSvg(r.name)}\\n${pct}% (${r.value.toLocaleString()} samples)</title></rect>${label}`;
  }).join("");

  container.innerHTML = `<svg width="${width}" height="${maxY + 5}" style="display:block">${svg}</svg>`;
  document.getElementById("fg-info").textContent =
    `${collapsed.length} unique stacks | ${totalValue.toLocaleString()} samples`;
  if (activeCategories.size > 0 || activeFunctions.size > 0) applyHighlight();
}

function hashColor(str) {
  // Strip generic parameters so the same function gets the same color
  // across builds with different generic args (e.g. Tree<T> vs Tree<T,_>)
  const stripped = str.replace(/<[^>]*>/g, '');
  let h = 0;
  for (let i = 0; i < stripped.length; i++) h = ((h << 5) - h + stripped.charCodeAt(i)) | 0;
  return Math.abs(h) % 360;
}

function escSvg(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function truncate(s, maxW) {
  const maxChars = Math.floor(maxW / 7);
  return s.length <= maxChars ? s : s.slice(0, maxChars - 1) + "\u2026";
}

function togglePlay() {
  playing = !playing;
  document.getElementById("play-btn").textContent = playing ? "\u23f8 Pause" : "\u25b6 Play";
  if (playing) {
    playInterval = setInterval(() => {
      const next = (currentBin + 1) % DATA.num_bins;
      document.getElementById("bin-slider").value = next;
      selectBin(next);
      if (next === 0) togglePlay(); // Stop at end
    }, 300);
  } else {
    clearInterval(playInterval);
  }
}

let activeCategories = new Set();
let activeFunctions = new Set();

function toggleCategory(ci) {
  if (activeCategories.has(ci)) {
    activeCategories.delete(ci);
  } else {
    activeCategories.add(ci);
  }
  applyHighlight();
  selectBin(currentBin); // re-render sidebar
}

function toggleFunction(fi) {
  const bin = DATA.bins[currentBin];
  const name = bin.top_functions[fi].name;
  if (activeFunctions.has(name)) {
    activeFunctions.delete(name);
  } else {
    activeFunctions.add(name);
  }
  applyHighlight();
  selectBin(currentBin); // re-render sidebar
}

function clearFilters() {
  activeCategories.clear();
  activeFunctions.clear();
  applyHighlight();
  selectBin(currentBin);
}

function applyHighlight() {
  const frames = document.querySelectorAll(".fg-frame");
  const hasFilters = activeCategories.size > 0 || activeFunctions.size > 0;
  if (!hasFilters) {
    frames.forEach(f => { f.style.opacity = ""; });
    return;
  }
  frames.forEach(f => {
    const title = f.querySelector("title");
    if (!title) { f.style.opacity = "0.15"; return; }
    const name = title.textContent.split("\\n")[0];
    f.style.opacity = frameMatchesFilters(name) ? "1" : "0.15";
  });
}

function frameMatchesFilters(frameName) {
  // Check function name filters
  for (const fn of activeFunctions) {
    if (frameName.includes(fn)) return true;
  }
  // Check category filters
  for (const ci of activeCategories) {
    const cat = DATA.category_patterns[ci];
    if (!cat) continue;
    for (const p of cat.patterns) {
      if (frameName.includes(p)) return true;
    }
    for (const rp of cat.regex_patterns) {
      if (new RegExp(rp).test(frameName)) return true;
    }
  }
  return false;
}

function escHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

init();
</script>
</body>
</html>
"""
