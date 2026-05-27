## Project Overview

Spyglass (`spyglass`) is a Python CLI tool for profiling the Lighthouse Ethereum consensus client. It automates the build → run → analyze → compare workflow for both CPU and memory profiling.

## Quick Reference

```bash
# Run from the spyglass directory
cd spyglass

# Full workflow
python3 -m spyglass profile --mode cpu -n my-test

# Individual steps
python3 -m spyglass build --mode cpu
python3 -m spyglass run --mode cpu -n my-test
python3 -m spyglass analyze my-test --filter epoch-boundary
python3 -m spyglass analyze my-test --filter all              # runs all three filters
python3 -m spyglass compare ./profiles/baseline/cpu ./profiles/opt/cpu --filter epoch-boundary
python3 -m spyglass compare ./profiles/baseline/cpu ./profiles/opt/cpu --filter all  # compares all available views

# Profile a GitHub PR
spyglass profile --mode cpu --pr 6789

# Clean up checkouts
spyglass clean
spyglass clean all --force
```

## Project Structure

```
spyglass/
  src/
    spyglass/
      __init__.py       # Package version
      __main__.py       # python -m entry point
      cli.py            # Argument parsing, subcommand dispatch
      config.py         # TOML config loading, path resolution, output path construction
      constants.py      # Shared constants (SLOTS_PER_EPOCH, SECONDS_PER_SLOT)
      build.py          # Cargo build with profiling flags (no file modifications)
      run.py            # Process orchestration: mock-el + lighthouse + perf/jemalloc
      beacon_api.py     # Background thread: epoch detection, sync polling, metrics scraping
      filters.py        # Time-based sample filtering (epoch-boundary, mid-epoch, steady-state)
      analyze.py        # perf script → collapse → flamegraph → markdown analysis
      compare.py        # Side-by-side profile comparison with category deltas
      categories.py     # Pattern-based sample classification with coverage reporting
      progress.py       # Background progress timer with slot progress bar
      flamechart.py     # Interactive flame chart HTML generation
      export.py         # Profile export in various formats
      pr.py             # GitHub PR fetching via shallow clone
      clean.py          # Artifact cleanup (checkouts, profiles)
  config.toml           # Default configuration
  categories.toml       # Category definitions for Lighthouse subsystems
  pyproject.toml        # Package metadata and build config
  AGENTS.md             # Project instructions (CLAUDE.md symlinks here)
  README.md
  LICENSE
```

## Key Design Decisions

1. **No file modifications during build** — CPU profiling uses `RUSTFLAGS` env var; memory profiling uses a dedicated `release-profiling` cargo profile + `jemalloc-profiling` feature flag + `JEMALLOC_SYS_WITH_MALLOC_CONF` env override. Nothing in the Lighthouse repo gets modified.

2. **Epoch-boundary mode is event-based, not time-based** — It runs until N epochs are captured (default 1), not for a fixed duration. This is because epoch boundaries are infrequent (~6.4 min apart) and unpredictable in timing relative to when sync completes.

3. **Output paths are auto-constructed** — `<output_dir>/<nickname_or_commit>/<mode>/` for run output, with filtered views in `views/<filter>/`. The nickname comes from CLI `--nickname`, config `nickname`, or auto-detected commit hash.

4. **Categories are optional and configurable** — The `categories.toml` file defines subsystem patterns. Analysis works without it (just shows raw top functions). Categories use first-match-wins priority ordering.

5. **Clock offset correction** — `perf` uses monotonic timestamps, the beacon API uses wall-clock timestamps. The filter module computes the offset between them using the first perf sample + recording start time.

6. **PR checkout isolation** — PRs are shallow-cloned into `checkouts/pr-<number>/` rather than using the user's lighthouse_dir. This avoids disturbing the working tree and allows multiple PRs to be checked out simultaneously.


## Dependencies

- Python 3.11+ (for `tomllib`)
- External tools: `perf`, `inferno-collapse-perf`, `inferno-flamegraph`, `jeprof`
- No Python third-party packages required (pure stdlib)

## Common Tasks

### Adding a new category
Edit `categories.toml`. Categories are checked in order — put more specific patterns before general ones.

### Changing profiling parameters
Edit `config.toml` or pass CLI flags (`--duration`, `--filter`, `--nickname`).

### Analyzing existing perf.data from outside the tool
```bash
python3 -m spyglass analyze /path/to/dir/containing/perf.data --filter all
```

### Profiling a PR
```bash
spyglass profile --mode cpu --pr 6789
```
The checkout persists in `checkouts/pr-6789/` for re-profiling. Use `spyglass clean` to remove.

### Comparing across different Lighthouse branches
Use `--nickname` to label each run:
```bash
git checkout main
python3 -m spyglass profile --mode cpu -n main
git checkout my-optimization
python3 -m spyglass profile --mode cpu -n my-opt
python3 -m spyglass analyze main --filter all
python3 -m spyglass analyze my-opt --filter all
python3 -m spyglass compare ./profiles/main/cpu ./profiles/my-opt/cpu --filter all
```
