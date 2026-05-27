"""GitHub PR fetching via shallow clone."""

import subprocess
import sys
from pathlib import Path

# PRs are cloned into this directory (relative to project root, gitignored)
CHECKOUTS_DIR = "checkouts"
LIGHTHOUSE_REPO = "https://github.com/sigp/lighthouse.git"


def _run_git(
    args: list[str], cwd: Path | None, verbose: bool, error_msg: str
) -> subprocess.CompletedProcess:
    """Run a git command, exiting with an error message on failure."""
    stdout_dest = None if verbose else subprocess.PIPE
    stderr_dest = None if verbose else subprocess.PIPE
    result = subprocess.run(args, cwd=cwd, stdout=stdout_dest, stderr=stderr_dest)
    if result.returncode != 0:
        print(f"ERROR: {error_msg}", file=sys.stderr)
        if not verbose and result.stderr:
            print(result.stderr.decode(errors="replace"), file=sys.stderr)
        sys.exit(1)
    return result


def fetch_pr(config, pr_number: int, verbose: bool = False) -> Path:
    """Fetch a PR from sigp/lighthouse via shallow clone.

    Clones into <project_root>/checkouts/pr-<number>/.
    If the directory already exists, fetches the latest and resets.

    Args:
        config: Spyglass configuration object
        pr_number: GitHub PR number
        verbose: Show git output

    Returns:
        Path to the checkout directory (to be used as lighthouse_dir for building)
    """
    project_root = config.config_dir
    checkouts_dir = project_root / CHECKOUTS_DIR
    checkout_path = checkouts_dir / f"pr-{pr_number}"

    print(f"=== Fetch PR #{pr_number} ===")

    if checkout_path.exists():
        # Already cloned — fetch latest into the named branch and checkout
        print(f"  Updating existing checkout at {checkout_path}...")
        _run_git(
            ["git", "fetch", "origin", f"pull/{pr_number}/head:pr-{pr_number}", "--force"],
            cwd=checkout_path,
            verbose=verbose,
            error_msg=f"Failed to fetch PR #{pr_number}",
        )
        _run_git(
            ["git", "checkout", f"pr-{pr_number}"],
            cwd=checkout_path,
            verbose=verbose,
            error_msg=f"Failed to checkout pr-{pr_number}",
        )
    else:
        # Fresh clone — shallow single-branch clone, then fetch just this PR
        print(f"  Cloning PR #{pr_number} from {LIGHTHOUSE_REPO}...")
        checkouts_dir.mkdir(parents=True, exist_ok=True)

        _run_git(
            ["git", "clone", "--depth", "1", LIGHTHOUSE_REPO, str(checkout_path)],
            cwd=None,
            verbose=verbose,
            error_msg="Failed to clone repository",
        )

        # Fetch the PR ref
        print(f"  Fetching pull/{pr_number}/head...")
        _run_git(
            ["git", "fetch", "origin", f"pull/{pr_number}/head:pr-{pr_number}"],
            cwd=checkout_path,
            verbose=verbose,
            error_msg=f"Failed to fetch PR #{pr_number}",
        )

        # Checkout the PR branch
        _run_git(
            ["git", "checkout", f"pr-{pr_number}"],
            cwd=checkout_path,
            verbose=verbose,
            error_msg=f"Failed to checkout pr-{pr_number}",
        )

    # Show what we got
    result = subprocess.run(
        ["git", "--no-pager", "log", "--oneline", "-1"],
        cwd=checkout_path,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"  HEAD: {result.stdout.strip()}")

    print(f"  PR #{pr_number} ready at {checkout_path}")
    print()

    return checkout_path
