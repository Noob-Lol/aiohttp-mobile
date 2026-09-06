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
    python resolve.py [--package SPEC[,SPEC...]] [--force]
"""

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import tomllib
from std.request import fetch_json

StrMap = dict[str, str]
AnyVal = str | list[str] | StrMap


def normalize(name: str) -> str:
    """PEP 503 normalize a package name."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def pypi_latest(pkg: str) -> str:
    url = f"https://pypi.org/pypi/{pkg}/json"
    return fetch_json(url)["info"]["version"]


def release_exists(tag: str, repo: str) -> bool:
    result = None
    for attempt in range(4):
        result = subprocess.run(
            ["gh", "release", "view", tag, "--repo", repo, "--json", "tagName", "--jq", ".tagName"],
            capture_output=True,
            check=False,
            text=True,
        )
        if result.returncode == 0:
            return bool(result.stdout.strip())
        if "not found" in result.stderr.lower():
            return False
        if attempt < 3:
            time.sleep(2**attempt)
    assert result is not None
    msg = f"Failed to query release tag {tag!r}: {result.stderr.strip()}"
    raise RuntimeError(msg)


def pypi_release_files(pkg: str, version: str) -> list[StrMap]:
    url = f"https://pypi.org/pypi/{pkg}/{version}/json"
    return fetch_json(url).get("urls", [])


def has_android_wheel_in_release_files(files: list[StrMap]) -> bool:
    for entry in files:
        filename = str(entry.get("filename", ""))
        if not filename.endswith(".whl"):
            continue
        if "android" in filename.lower():
            return True
    return False


def pypi_has_android_wheel(pkg: str, version: str) -> bool:
    return has_android_wheel_in_release_files(pypi_release_files(pkg, version))


def filter_matrix(candidates: list[StrMap], repo: str, *, force: bool) -> list[StrMap]:
    matrix_entries: list[StrMap] = []

    def check_release(c: StrMap) -> tuple[StrMap, str, str | None]:
        tag = f"{c['name']}-v{c['version']}"
        if force:
            return c, tag, None
        if release_exists(tag, repo):
            return c, tag, "release"
        if pypi_has_android_wheel(c["name"], c["version"]):
            return c, tag, "pypi"
        return c, tag, None

    with concurrent.futures.ThreadPoolExecutor() as executor:
        results = list(executor.map(check_release, candidates))

    for c, tag, skip_reason in results:
        if skip_reason is not None:
            print(f"  SKIP  {tag} ({skip_reason})", file=sys.stderr)
            continue
        print(f"  BUILD {tag}", file=sys.stderr)
        matrix_entries.append({**c, "tag": tag})
    return matrix_entries


def maybe_join_list(val: list[str] | str, sep: str = " ") -> str:
    if isinstance(val, list):
        return sep.join(val)
    return val


def quote_cibw_env_value(value: str) -> str:
    # Keep "$VAR" expansion available inside cibuildwheel while protecting
    # separators such as ":" and spaces from shell token splitting.
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")
    return f'"{escaped}"'


def serialize_cibw_environment(val: AnyVal) -> str:
    if isinstance(val, dict):
        return " ".join(f"{k}={quote_cibw_env_value(v)}" for k, v in val.items())
    return maybe_join_list(val)


def serialize_android_source_deps(val: AnyVal) -> str:
    if isinstance(val, dict):
        return " ".join(f"{k}={v}" for k, v in val.items())
    return maybe_join_list(val)


def make_candidate(name: str, version: str, pkg_config: StrMap) -> StrMap:
    entry: StrMap = {"name": name, "version": version}
    if "cibw_environment" in pkg_config:
        entry["cibw_environment"] = serialize_cibw_environment(pkg_config["cibw_environment"])
    if "cibw_before_build" in pkg_config:
        entry["cibw_before_build"] = maybe_join_list(pkg_config["cibw_before_build"], " && ")
    if "android_source_deps" in pkg_config:
        entry["android_source_deps"] = serialize_android_source_deps(pkg_config["android_source_deps"])
    return entry


def expand_csv_values(values: list[str]) -> list[str]:
    expanded: list[str] = []
    for value in values:
        if not value:
            continue
        expanded.extend(part.strip() for part in re.split(r"[\s,]+", value) if part.strip())
    return expanded


def resolve_package_specs(package_values: list[str]) -> list[tuple[str, str]]:
    package_tokens = expand_csv_values(package_values)
    if not package_tokens:
        return []

    specs: list[tuple[str, str]] = []
    for token in package_tokens:
        if "==" not in token:
            pkg_name = token.strip()
            if not pkg_name:
                msg = "package specs must be package names or name==version pairs"
                raise ValueError(msg)
            specs.append((pkg_name, ""))
            continue

        pkg_name, version_value = token.split("==", 1)
        pkg_name = pkg_name.strip()
        version_value = version_value.strip()
        if not pkg_name:
            msg = "package specs must be package names or name==version pairs"
            raise ValueError(msg)
        specs.append((pkg_name, version_value))

    return specs


def main() -> None:
    parser = argparse.ArgumentParser()
    pkg_help = "Ad-hoc package spec(s) to build (package or package==version); may be repeated or comma/whitespace-separated"
    parser.add_argument("--package", action="append", default=[], help=pkg_help)
    parser.add_argument("--force", action="store_true", help="Build even if the release tag already exists")
    args = parser.parse_args()

    repo = os.getenv("GITHUB_REPOSITORY", "Noob-Lol/aiohttp-mobile")

    # Load config
    with Path("packages.toml").open("rb") as f:
        config = tomllib.load(f)
    configured = {normalize(p["name"]): p for p in config["package"]}

    # Build the candidate list
    # Ad-hoc package(s) (from manual workflow_dispatch input) replace or
    # extend the configured list for this run only.
    if args.package:
        try:
            package_specs = resolve_package_specs(args.package)
        except ValueError as exc:
            print(f"::warning::{exc}", file=sys.stderr)
            package_specs = []

        candidates = []
        for pkg_name, version_value in package_specs:
            normalized_name = normalize(pkg_name)
            if normalized_name not in configured:
                print(
                    f"::warning::'{normalized_name}' is not in packages.toml — "
                    "it will be built this run but won't be tracked automatically.",
                    file=sys.stderr,
                )
                pkg_config = {}
            else:
                pkg_config = configured[normalized_name]

            version = version_value.strip() or pypi_latest(normalized_name)
            candidates.append(make_candidate(normalized_name, version, pkg_config))
    else:

        def process_pkg(pkg: StrMap) -> StrMap:
            name = normalize(pkg["name"])
            version = pkg.get("pin", "") or pypi_latest(name)
            return make_candidate(name, version, pkg)

        with concurrent.futures.ThreadPoolExecutor() as executor:
            candidates = list(executor.map(process_pkg, config["package"]))

    # Filter out already-released entries
    matrix_entries = filter_matrix(candidates, repo, force=args.force)

    any_to_build = bool(matrix_entries)
    # GHA requires at least one matrix entry to be syntactically valid
    if not matrix_entries:
        matrix_entries = [{"name": "__skip__", "version": "", "tag": ""}]

    output_file = os.environ.get("GITHUB_OUTPUT")
    lines = [f"matrix={json.dumps({'include': matrix_entries})}", f"any_to_build={'true' if any_to_build else 'false'}"]
    if output_file:
        with Path(output_file).open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    else:
        print("\n".join(lines))


if __name__ == "__main__":
    main()
