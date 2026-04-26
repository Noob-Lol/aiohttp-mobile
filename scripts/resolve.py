#!/usr/bin/env python3
"""
resolve.py — Determine which packages need building and emit a GitHub Actions
matrix JSON.

For each package in packages.toml (plus any ad-hoc package supplied via
--package / --version), this script:
  1. Resolves the target version (pin > --version flag > PyPI latest).
  2. Checks whether a GitHub Release tagged {name}-v{version} already
     exists in this repo.
  3. Emits a matrix entry if the release is missing or --force is set.

Output written to $GITHUB_OUTPUT (or stdout if not set):
  matrix       - JSON object suitable for fromJson() in a GHA matrix
  any_to_build - "true" | "false"

Usage:
    python resolve.py [--package NAME] [--version VER] [--force]
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import tomllib


def normalize(name: str) -> str:
    """PEP 503 normalize a package name."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def pypi_latest(pkg: str) -> str:
    url = f"https://pypi.org/pypi/{pkg}/json"
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return json.load(r)["info"]["version"]
        except Exception as exc:  # network/HTTP errors are transient often enough to retry
            last_error = exc
            if attempt == 3:
                break
            time.sleep(2**attempt)
    assert last_error is not None
    msg = f"Failed to fetch latest version for {pkg!r} from PyPI"
    raise RuntimeError(msg) from last_error


def release_exists(tag: str, repo: str) -> bool:
    result = None
    for attempt in range(4):
        result = subprocess.run(
            ["gh", "release", "view", tag, "--repo", repo, "--json", "tagName", "--jq", ".tagName"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip() != ""
        if "not found" in result.stderr.lower():
            return False
        if attempt < 3:
            time.sleep(2**attempt)
    assert result is not None
    msg = f"Failed to query release tag {tag!r}: {result.stderr.strip()}"
    raise RuntimeError(msg)


def filter_matrix(candidates: list[dict[str, str]], repo: str, *, force: bool) -> list[dict[str, str]]:
    matrix_entries = []
    for c in candidates:
        tag = f"{c['name']}-v{c['version']}"
        if not force and release_exists(tag, repo):
            print(f"  SKIP  {tag}", file=sys.stderr)
            continue
        print(f"  BUILD {tag}", file=sys.stderr)
        entry = {
            "name": c["name"],
            "version": c["version"],
            "tag": tag,
        }
        # Pass through optional cibuildwheel overrides
        if "cibw_environment" in c:
            entry["cibw_environment"] = c["cibw_environment"]
        if "cibw_before_build" in c:
            entry["cibw_before_build"] = c["cibw_before_build"]
        matrix_entries.append(entry)
    return matrix_entries


def serialize_cibw_environment(val) -> str:
    if isinstance(val, dict):
        # Produce: KEY=value KEY2="value with spaces"
        parts = []
        for k, v in val.items():
            if " " in str(v) or '"' in str(v):
                parts.append(f'{k}="{v}"')
            else:
                parts.append(f"{k}={v}")
        return " ".join(parts)
    if isinstance(val, list):
        return " ".join(val)
    return val  # already a string


def make_candidate(name: str, version: str, pkg_config: dict) -> dict[str, str]:
    entry: dict[str, str] = {"name": name, "version": version}
    if "cibw_environment" in pkg_config:
        entry["cibw_environment"] = serialize_cibw_environment(pkg_config["cibw_environment"])
    if "cibw_before_build" in pkg_config:
        entry["cibw_before_build"] = pkg_config["cibw_before_build"]
    return entry


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", default="", help="Ad-hoc package name to build (not required to be in packages.toml)")
    parser.add_argument("--version", default="", help="Exact version to build for --package (omit for latest)")
    parser.add_argument("--force", action="store_true", help="Build even if the release tag already exists")
    args = parser.parse_args()

    repo = os.environ["GITHUB_REPOSITORY"]

    # Load config
    with Path("packages.toml").open("rb") as f:
        config = tomllib.load(f)
    configured = {normalize(p["name"]): p for p in config["package"]}

    # Build the candidate list
    # Ad-hoc package (from manual workflow_dispatch input) replaces or
    # extends the configured list for this run only.
    if args.package:
        pkg_name = normalize(args.package)
        if pkg_name not in configured:
            print(
                f"::warning::'{pkg_name}' is not in packages.toml — "
                "it will be built this run but won't be tracked automatically.",
                file=sys.stderr,
            )
            pkg_config = {}
        else:
            pkg_config = configured[pkg_name]
        version = args.version.strip() or pypi_latest(pkg_name)
        candidates = [make_candidate(pkg_name, version, pkg_config)]
    else:
        candidates = []
        for pkg in config["package"]:
            name = normalize(pkg["name"])
            version = pkg.get("pin", "") or pypi_latest(name)
            candidates.append(make_candidate(name, version, pkg))

    # Filter out already-released entries
    matrix_entries = filter_matrix(candidates, repo, force=args.force)

    any_to_build = bool(matrix_entries)
    # GHA requires at least one matrix entry to be syntactically valid
    if not matrix_entries:
        matrix_entries = [{"name": "__skip__", "version": "", "tag": ""}]

    output_file = os.environ.get("GITHUB_OUTPUT")
    lines = [
        f"matrix={json.dumps({'include': matrix_entries})}",
        f"any_to_build={'true' if any_to_build else 'false'}",
    ]
    if output_file:
        with Path(output_file).open("a") as f:
            f.write("\n".join(lines) + "\n")
    else:
        print("\n".join(lines))


if __name__ == "__main__":
    main()
