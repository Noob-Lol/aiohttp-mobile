#!/usr/bin/env python3
"""
resolve.py — Determine which packages need building and emit a GitHub Actions
matrix JSON.

For each package in packages.toml (plus any ad-hoc package supplied via
--package / --version), this script:
  1. Resolves the target version (pin > --version flag > PyPI latest).
  2. Checks whether a GitHub Release tagged {pypi_name}-v{version} already
     exists in this repo.
  3. Emits a matrix entry if the release is missing or --force is set.

Output written to $GITHUB_OUTPUT (or stdout if not set):
  matrix       – JSON object suitable for fromJson() in a GHA matrix
  any_to_build – "true" | "false"

Usage:
    python resolve.py [--package NAME] [--version VER] [--force]
"""

import argparse
import json
import os
import subprocess
import sys
import tomllib
import urllib.request


def pypi_latest(pkg: str) -> str:
    url = f"https://pypi.org/pypi/{pkg}/json"
    with urllib.request.urlopen(url) as r:
        return json.load(r)["info"]["version"]


def release_exists(tag: str, repo: str) -> bool:
    result = subprocess.run(
        ["gh", "release", "view", tag,
         "--repo", repo,
         "--json", "tagName", "--jq", ".tagName"],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and result.stdout.strip() != ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", default="",
                        help="Ad-hoc package name to build (not required to be in packages.toml)")
    parser.add_argument("--version", default="",
                        help="Exact version to build for --package (omit for latest)")
    parser.add_argument("--force", action="store_true",
                        help="Build even if the release tag already exists")
    args = parser.parse_args()

    repo = os.environ["GITHUB_REPOSITORY"]

    # Load config
    with open("packages.toml", "rb") as f:
        config = tomllib.load(f)
    configured = {p["pypi_name"]: p for p in config["package"]}

    # Build the candidate list
    # Ad-hoc package (from manual workflow_dispatch input) replaces or
    # extends the configured list for this run only.
    if args.package:
        pkg_name = args.package.strip()
        if pkg_name not in configured:
            print(
                f"::warning::'{pkg_name}' is not in packages.toml — "
                "it will be built this run but won't be tracked automatically.",
                file=sys.stderr,
            )
        version  = args.version.strip() if args.version.strip() \
                   else pypi_latest(pkg_name)
        candidates = [{"pypi_name": pkg_name, "version": version}]
    else:
        candidates = []
        for pkg in config["package"]:
            pypi_name = pkg["pypi_name"]
            version   = pkg.get("pin", "") or pypi_latest(pypi_name)
            candidates.append({"pypi_name": pypi_name, "version": version})

    # Filter out already-released entries
    matrix_entries = []
    for c in candidates:
        tag = f"{c['pypi_name']}-v{c['version']}"
        if not args.force and release_exists(tag, repo):
            print(f"  SKIP  {tag}", file=sys.stderr)
            continue
        print(f"  BUILD {tag}", file=sys.stderr)
        matrix_entries.append({
            "pypi_name": c["pypi_name"],
            "version":   c["version"],
            "tag":       tag,
        })

    any_to_build = bool(matrix_entries)
    # GHA requires at least one matrix entry to be syntactically valid
    if not matrix_entries:
        matrix_entries = [{"pypi_name": "__skip__", "version": "", "tag": ""}]

    output_file = os.environ.get("GITHUB_OUTPUT")
    lines = [
        f"matrix={json.dumps({'include': matrix_entries})}",
        f"any_to_build={'true' if any_to_build else 'false'}",
    ]
    if output_file:
        with open(output_file, "a") as f:
            f.write("\n".join(lines) + "\n")
    else:
        print("\n".join(lines))


if __name__ == "__main__":
    main()
