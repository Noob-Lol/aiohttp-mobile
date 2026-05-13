#!/usr/bin/env python3
"""
Download prebuilt Android source dependencies from BeeWare releases.

The positional dependency specs are either NAME or NAME=VERSION. If VERSION is
omitted, the latest matching GitHub release is used.

Examples:
    uv run scripts/download_android_source_dep.py libffi=3.4.4-3 --arch arm64_v8a
    uv run scripts/download_android_source_dep.py libffi openssl=3.5.6-0 --arch x86_64

Output:
    CIBW_ENVIRONMENT_ANDROID-compatible KEY="value" assignments, one per line.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ANDROID_DEP_REPOS = ["beeware/cpython-android-source-deps", "noob-lol/android-static-libs"]
# repo was ANDROID_DEP_REPO previously
RELEASES_API = "https://api.github.com/repos/{repo}/releases"
RELEASE_DOWNLOAD_BASE = "https://github.com/{repo}/releases/download"

ARCH_TRIPLES = {
    "arm64_v8a": "aarch64-linux-android",
    "x86_64": "x86_64-linux-android",
}

SUPPORTED_DEPS = {"libffi", "openssl", "libuv", "libxml2", "libxslt"}


@dataclass(frozen=True)
class DependencySpec:
    name: str
    version: str | None


@dataclass(frozen=True)
class InstalledDependency:
    name: str
    version: str
    repo: str
    root: Path


def fetch_json(url: str) -> object:
    return json.loads(fetch_bytes(url).decode())


def fetch_bytes(url: str) -> bytes:
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "aiohttp-mobile-builder"})
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == 3:
                break
            time.sleep(2**attempt)

    msg = f"failed to download {url}"
    raise RuntimeError(msg) from last_error


def parse_dependency_spec(spec: str) -> DependencySpec:
    name, sep, version = spec.partition("=")
    name = name.strip().lower()
    version = version.strip()

    if not name:
        msg = f"invalid dependency spec {spec!r}: missing name"
        raise ValueError(msg)
    if name not in SUPPORTED_DEPS:
        supported = ", ".join(sorted(SUPPORTED_DEPS))
        msg = f"unsupported Android source dependency {name!r}; supported: {supported}"
        raise ValueError(msg)
    if sep and not version:
        msg = f"invalid dependency spec {spec!r}: missing version after '='"
        raise ValueError(msg)

    return DependencySpec(name=name, version=version or None)


def find_dependency(name: str, requested_version: str | None) -> tuple[str, str]:
    for repo in ANDROID_DEP_REPOS:
        try:
            releases = fetch_json(f"{RELEASES_API.format(repo=repo)}?per_page=100")
            if not isinstance(releases, list):
                continue

            prefix = f"{name}-"
            for release in releases:
                if not isinstance(release, dict):
                    continue
                tag = str(release.get("tag_name", ""))
                if tag.startswith(prefix):
                    version = tag.removeprefix(prefix)
                    if requested_version is None or version == requested_version:
                        return version, repo
        except Exception:
            continue

    msg = f"no GitHub release found for Android source dependency {name!r}"
    if requested_version:
        msg += f" with version {requested_version!r}"
    raise RuntimeError(msg)


def asset_url(repo: str, name: str, version: str, host_triple: str) -> str:
    name_version = f"{name}-{version}"
    filename = f"{name_version}-{host_triple}.tar.gz"
    return f"{RELEASE_DOWNLOAD_BASE.format(repo=repo)}/{name_version}/{filename}"


def safe_extract_tar_gz(archive_data: bytes, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    dest = dest.resolve()

    with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz") as tf:
        members = tf.getmembers()

        for member in members:
            target = (dest / member.name).resolve()
            if dest not in {target, *target.parents}:
                msg = f"refusing to extract archive member outside destination: {member.name}"
                raise RuntimeError(msg)

        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, members, filter="data")
        else:
            tf.extractall(dest, members)

    return dest


def install_dependency(spec: DependencySpec, arch: str, dest: Path) -> InstalledDependency:
    dest = dest.resolve()
    host_triple = ARCH_TRIPLES[arch]
    version, repo = find_dependency(spec.name, spec.version)
    install_dir = dest / spec.name / version / host_triple

    if install_dir.exists():
        shutil.rmtree(install_dir)

    archive = fetch_bytes(asset_url(repo, spec.name, version, host_triple))
    root = safe_extract_tar_gz(archive, install_dir)
    return InstalledDependency(spec.name, version, repo, root)


def quote_env_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")
    return f'"{escaped}"'


def env_assignment(name: str, value: str) -> str:
    return f"{name}={quote_env_value(value)}"


def build_environment(installed: list[InstalledDependency]) -> list[str]:
    by_name = {dep.name: dep for dep in installed}
    assignments = []
    if dep := by_name.get("libffi"):
        # name from upstream pull
        assignments.extend([env_assignment("LIBFFI_ANDROID_DIR", str(dep.root))])
    if dep := by_name.get("openssl"):
        assignments.extend([env_assignment("OPENSSL_DIR", str(dep.root))])
    if dep := by_name.get("libuv"):
        assignments.extend([env_assignment("LIBUV_DIR", str(dep.root))])
    if dep := by_name.get("libxml2"):
        assignments.extend([env_assignment("LIBXML2_DIR", str(dep.root))])
    if dep := by_name.get("libxslt"):
        assignments.extend([env_assignment("LIBXSLT_DIR", str(dep.root))])

    return assignments


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("dependencies", nargs="+", help="Dependency specs, e.g. libffi=3.4.4-3")
    parser.add_argument("--arch", required=True, choices=sorted(ARCH_TRIPLES))
    parser.add_argument("--version", default="", help="Version for a single dependency positional name")
    parser.add_argument("--dest", type=Path, default=Path("work/android-deps"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        specs = [parse_dependency_spec(spec) for spec in args.dependencies]
        version = args.version.strip()
        if version:
            if len(specs) != 1:
                sys.exit("ERROR: --version can only be used with one dependency")
            specs = [DependencySpec(specs[0].name, version)]

        installed = [install_dependency(spec, args.arch, args.dest) for spec in specs]
    except Exception as exc:
        sys.exit(f"ERROR: {exc}")

    for assignment in build_environment(installed):
        print(assignment)


if __name__ == "__main__":
    main()
