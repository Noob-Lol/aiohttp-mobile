#!/usr/bin/env python3
"""
download_sdist.py — Fetch a source distribution directly from PyPI.

Queries the PyPI JSON API for the sdist URL, downloads it to the current
directory (or --dest), and verifies the sha256 hash.  No pip, no uv, no
build backend invocation.

Usage:
    python download_sdist.py <package> <version> [--dest DIR]

Examples:
    python download_sdist.py aiohttp 3.13.5
    python download_sdist.py frozenlist 1.5.0 --dest /tmp/sdists
"""

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def fetch_sdist_info(pkg: str, ver: str) -> tuple[str, str, str]:
    """Return (url, filename, expected_sha256) from the PyPI JSON API."""
    url = f"https://pypi.org/pypi/{pkg}/{ver}/json"
    try:
        with urllib.request.urlopen(url) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            sys.exit(f"ERROR: {pkg}=={ver} not found on PyPI (404)")
        raise

    sdists = [f for f in data["urls"] if f["packagetype"] == "sdist"]
    if not sdists:
        sys.exit(f"ERROR: no sdist found for {pkg}=={ver} on PyPI")

    # Prefer .tar.gz, fall back to whatever is available
    sdist = next((f for f in sdists if f["filename"].endswith(".tar.gz")), sdists[0])
    return sdist["url"], sdist["filename"], sdist["digests"]["sha256"]


def download(url: str, dest: Path) -> None:
    print(f"  downloading {url}")
    with urllib.request.urlopen(url) as r, dest.open("wb") as f:
        total = int(r.headers.get("Content-Length", 0))
        downloaded = 0
        while True:
            block = r.read(65536)
            if not block:
                break
            f.write(block)
            downloaded += len(block)
            if total:
                pct = downloaded * 100 // total
                print(f"\r  {downloaded // 1024} / {total // 1024} KB  ({pct}%)", end="", flush=True)
    print()


def verify(path: Path, expected: str) -> None:
    print("  verifying sha256...", end=" ", flush=True)
    h = hashlib.sha256()
    h.update(path.read_bytes())
    actual = h.hexdigest()
    if actual != expected:
        path.unlink()
        sys.exit(f"HASH MISMATCH\n  expected: {expected}\n  actual:   {actual}\nFile deleted.")
    print("OK")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("package", help="PyPI package name")
    parser.add_argument("version", help="Exact version to download")
    parser.add_argument("-d", "--dest", default=".", type=Path, help="Directory to save the sdist (default: current dir)")
    args = parser.parse_args()

    pkg, ver = args.package.strip(), args.version.strip()
    dest_path: Path = args.dest
    dest_path.mkdir(parents=True, exist_ok=True)

    print(f"Fetching sdist info for {pkg}=={ver}")
    url, filename, sha256 = fetch_sdist_info(pkg, ver)

    out = dest_path / filename
    resolved = out.resolve()
    if out.exists():
        print(f"  {out} already exists, verifying...")
        verify(out, sha256)
        print(f"  already up to date: {resolved}")
        return

    download(url, out)
    verify(out, sha256)
    print(f"  saved: {resolved}")


if __name__ == "__main__":
    main()
