# Spyglass

A Python CLI tool for CPU and memory profiling of [Lighthouse](https://github.com/sigp/lighthouse).

## Features

- **CPU profiling** via `perf record` with automatic flamegraph generation
- **Memory profiling** via jemalloc heap dumps
- **Epoch boundary isolation** — capture profiles around specific epoch transitions
- **Category-based analysis** — configurable pattern matching to group samples by subsystem
- **Metrics scraping** — Prometheus metrics captured at epoch boundaries for cache/timing analysis
- **Comparison** — side-by-side delta reports between two profiling runs

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Linux with `perf` installed
- [inferno](https://github.com/jonhoo/inferno) (`cargo install inferno`)
- `jeprof` (from the `jemalloc` package) — for memory profiling

## Installation

```bash
# Install uv if you don't have it
# Arch: sudo pacman -S uv
# Other: curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and install
git clone <repo-url> spyglass
cd spyglass
uv venv
uv pip install -e .

# Activate the virtual environment
source .venv/bin/activate    # bash/zsh
source .venv/bin/activate.fish  # fish

# Now `spyglass` is available directly
spyglass --version
```

## Quick Start

```bash
# Configure (edit paths to your Lighthouse checkout)
cp config.toml my_config.toml
vim my_config.toml

# Full workflow: build + run + analyze
python3 -m spyglass profile --mode cpu --filter epoch-boundary -n my-experiment

# Or step by step
python3 -m spyglass build --mode cpu
python3 -m spyglass run --mode cpu --filter steady-state -n baseline
python3 -m spyglass analyze ./profiles/baseline/cpu/steady_state
```

## Commands

### `build`

Builds Lighthouse with profiling instrumentation.

```bash
python3 -m spyglass build --mode cpu      # Frame pointers for perf
python3 -m spyglass build --mode memory   # jemalloc profiling support
python3 -m spyglass build --mode both     # Both
```

- CPU mode: passes `RUSTFLAGS="-C force-frame-pointers=yes"`
- Memory mode: uses `--profile release-profiling` with `--features jemalloc-profiling` and sets `JEMALLOC_SYS_WITH_MALLOC_CONF` with `prof:true`

### `run`

Runs Lighthouse under a profiler with a mock execution layer.

```bash
# Time-based (runs for duration_seconds from config)
python3 -m spyglass run --mode cpu --filter all -n baseline
python3 -m spyglass run --mode cpu --filter steady-state --duration 600 -n short

# Event-based (runs until N epochs captured)
python3 -m spyglass run --mode cpu --filter epoch-boundary -n epoch-test
python3 -m spyglass run --mode cpu --filter epoch-boundary --runs 3 -n multi-epoch
```

The tool automatically:
- Starts `lcli mock-el` for the execution layer
- Enables the beacon HTTP API and metrics server
- Polls the beacon API to detect sync completion and epoch boundaries
- Scrapes Prometheus metrics at epoch boundaries (pre/post delta)
- Terminates cleanly on completion or Ctrl+C

### `analyze`

Processes profiling output into flamegraphs and markdown reports.

```bash
python3 -m spyglass analyze ./profiles/my-run/cpu/epoch_boundary
python3 -m spyglass analyze ./profiles/my-run/cpu/epoch_boundary --filter mid-epoch
```

Produces:
- `profile.collapsed` — collapsed stack format
- `flamegraph.svg` — interactive flamegraph
- `analysis.md` — category breakdown + top functions

### `compare`

Compares two profiling runs.

```bash
python3 -m spyglass compare ./profiles/baseline/cpu/epoch_boundary ./profiles/optimized/cpu/epoch_boundary
```

Produces a `comparison.md` with category-level and function-level deltas.

### `profile`

Convenience command: `build` + `run` + `analyze` in one step.

```bash
python3 -m spyglass profile --mode cpu --filter epoch-boundary -n my-experiment
```

### `clean`

Removes spyglass artifacts (PR checkouts and/or profiling results).

```bash
spyglass clean              # Remove PR checkouts only (default)
spyglass clean all          # Remove checkouts + profiles
spyglass clean profiles     # Remove only profiling results
```

### Profiling a GitHub PR

Fetch and profile a pull request directly from `sigp/lighthouse`:

```bash
# Profile PR #6789 (clones into checkouts/pr-6789/, builds, profiles)
spyglass profile --mode cpu --filter epoch-boundary --pr 6789

# Compare against your local branch
spyglass profile --mode cpu --filter epoch-boundary -n baseline
spyglass compare ./profiles/baseline/cpu/epoch_boundary ./profiles/pr-6789/cpu/epoch_boundary
```

The `--pr` flag works with any command (`build`, `run`, `profile`). It:
- Shallow-clones the Lighthouse repo into `checkouts/pr-<number>/`
- Fetches the PR ref and checks it out
- Uses that checkout for building and profiling
- Defaults the nickname to `pr-<number>`

## Output Structure

```
profiles/
  <nickname-or-commit>/
    cpu/
      epoch_boundary/
        perf.data
        profile.collapsed
        flamegraph.svg
        analysis.md
        epochs.json
        sync_status.json
        run.json
        metrics/
          epoch_<N>_pre.txt
          epoch_<N>_post.txt
          epoch_<N>_delta.json
    memory/
      epoch_boundary/
        heap.*.heap
        heap_analysis.md
```

## Configuration

See `config.toml` for all options:

```toml
[paths]
lighthouse_dir = "~/work/lighthouse"

[lighthouse]
network = "mainnet"
checkpoint_sync_url = "https://mainnet.checkpoint.sigp.io"
extra_flags = ["--subscribe-all-subnets", "--import-all-attestations"]
http_port = 5052
metrics_port = 5054

[profiling]
duration_seconds = 1800
perf_frequency = 1000
profile = "release"
disable_backfill = true
output_dir = "./profiles"
nickname = ""

[filtering]
epoch_boundary_warmup = 15
epoch_boundary_cooldown = 15
max_wait_seconds = 7200

[mock_el]
listen_address = "127.0.0.1"
listen_port = 8551
```

The `network` field is passed to both `lighthouse bn` and `lcli mock-el`. Supported values: `mainnet`, `gnosis`, `holesky`, `sepolia`, etc.

## Categories

The `categories.toml` file defines pattern-based sample classification. Categories are checked in priority order (first match wins). See the file for the default Lighthouse categories.

Categories are optional — without the file, you still get top-function analysis.

## Filters

| Filter | Duration | Description |
|--------|----------|-------------|
| `all` | seconds | All samples from the entire run |
| `steady-state` | seconds | Samples after sync completes |
| `mid-epoch` | seconds | Samples NOT near epoch boundaries |
| `epoch-boundary` | epoch count | Samples within warmup/cooldown of epoch transitions |

## License

Apache 2.0 — see [LICENSE](LICENSE).
