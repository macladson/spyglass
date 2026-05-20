"""GitHub PR fetching via shallow clone."""

import subprocess
import sys
from pathlib import Path

# PRs are cloned into this directory (relative to project root, gitignored)
CHECKOUTS_DIR = "checkouts"
LIGHTHOUSE_REPO = "https://github.com/sigp/lighthouse.git"


def fetch_pr(config: dict, pr_number: int, verbose: bool = False) -> Path:
    """Fetch a PR from sigp/lighthouse via shallow clone.
    
    Clones into <project_root>/checkouts/pr-<number>/.
    If the directory already exists, fetches the latest and resets.
    
    Args:
        config: Parsed config
        pr_number: GitHub PR number
        verbose: Show git output
        
    Returns:
        Path to the checkout directory (to be used as lighthouse_dir for building)
    """
    project_root = Path(config.get("_config_dir", Path(__file__).resolve().parent.parent.parent))
    checkouts_dir = project_root / CHECKOUTS_DIR
    checkout_path = checkouts_dir / f"pr-{pr_number}"

    stdout_dest = None if verbose else subprocess.PIPE
    stderr_dest = None if verbose else subprocess.PIPE

    print(f"=== Fetch PR #{pr_number} ===")

    if checkout_path.exists():
        # Already cloned — fetch latest
        print(f"  Updating existing checkout at {checkout_path}...")
        result = subprocess.run(
            ["git", "fetch", "origin",
             f"pull/{pr_number}/head:pr-{pr_number}"],
            cwd=checkout_path,
            stdout=stdout_dest,
            stderr=stderr_dest,
        )
        if result.returncode != 0:
            print(f"ERROR: Failed to fetch PR #{pr_number}", file=sys.stderr)
            if not verbose and result.stderr:
                print(result.stderr.decode(), file=sys.stderr)
            sys.exit(1)

        # Reset to the fetched ref
        subprocess.run(
            ["git", "checkout", f"pr-{pr_number}"],
            cwd=checkout_path,
            stdout=stdout_dest,
            stderr=stderr_dest,
        )
        subprocess.run(
            ["git", "reset", "--hard", f"pr-{pr_number}"],
            cwd=checkout_path,
            stdout=stdout_dest,
            stderr=stderr_dest,
        )
    else:
        # Fresh clone — shallow fetch just this PR
        print(f"  Cloning PR #{pr_number} from {LIGHTHOUSE_REPO}...")
        checkouts_dir.mkdir(parents=True, exist_ok=True)

        # Clone with just enough depth to get the PR
        result = subprocess.run(
            ["git", "clone",
             "--depth", "1",
             "--no-single-branch",
             LIGHTHOUSE_REPO,
             str(checkout_path)],
            stdout=stdout_dest,
            stderr=stderr_dest,
        )
        if result.returncode != 0:
            print(f"ERROR: Failed to clone repository", file=sys.stderr)
            if not verbose and result.stderr:
                print(result.stderr.decode(), file=sys.stderr)
            sys.exit(1)

        # Fetch the PR ref
        print(f"  Fetching pull/{pr_number}/head...")
        result = subprocess.run(
            ["git", "fetch", "origin",
             f"pull/{pr_number}/head:pr-{pr_number}"],
            cwd=checkout_path,
            stdout=stdout_dest,
            stderr=stderr_dest,
        )
        if result.returncode != 0:
            print(f"ERROR: Failed to fetch PR #{pr_number}", file=sys.stderr)
            if not verbose and result.stderr:
                print(result.stderr.decode(), file=sys.stderr)
            sys.exit(1)

        # Checkout the PR branch
        subprocess.run(
            ["git", "checkout", f"pr-{pr_number}"],
            cwd=checkout_path,
            stdout=stdout_dest,
            stderr=stderr_dest,
        )

    # Show what we got
    result = subprocess.run(
        ["git", "--no-pager", "log", "--oneline", "-1"],
        cwd=checkout_path,
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"  HEAD: {result.stdout.strip()}")

    print(f"  PR #{pr_number} ready at {checkout_path}")
    print()

    return checkout_path
