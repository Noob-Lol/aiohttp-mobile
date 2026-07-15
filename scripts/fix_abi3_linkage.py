#!/usr/bin/env python3
"""
Fix abi3 wheels' libpython linkage for Android/Termux.

For each *.abi3.so inside an abi3-tagged wheel:
  1. Replace any versioned `libpython3.X.so` NEEDED entry with the
     unversioned `libpython3.so` stub name.
  2. Set an $ORIGIN-relative RPATH so the dynamic linker can actually
     find libpython3.so at runtime, without hardcoding any absolute
     Termux path.

Depth for the RPATH is computed per-.so from its location relative to
the wheel root, not hardcoded -- this works regardless of package
layout (cryptography/hazmat/bindings/, or a flat mypkg/_core.abi3.so).

Assumption: the target environment installs wheels to a layout like
    <prefix>/lib/pythonX.Y/site-packages/<package files>
so that going up (depth_in_wheel + 1) directories from the wheel root
reaches <prefix>/lib/, where libpython3.so lives. If some other Android
embedder uses a different install-relative depth, this will need
adjusting for that environment.
"""

import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

VERSIONED_LIBPYTHON = re.compile(r"^libpython3\.\d+\.so$")


def compute_rpath(so_path: Path, wheel_root: Path) -> str:
    """
    Number of directory levels from the .so up to the wheel root,
    plus one more level to get from site-packages/ up to lib/
    once installed.
    """
    rel = so_path.parent.relative_to(wheel_root)
    depth = len(rel.parts) + 1
    return "/".join([".."] * depth)


def get_needed(so_path: Path) -> list[str]:
    result = subprocess.run(
        ["patchelf", "--print-needed", str(so_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def fix_so(so_path: Path, wheel_root: Path) -> bool:
    needed = get_needed(so_path)
    versioned_entries = [n for n in needed if VERSIONED_LIBPYTHON.match(n)]
    if not versioned_entries:
        return False

    for entry in versioned_entries:
        if entry == "libpython3.so":
            continue
        subprocess.run(
            ["patchelf", "--replace-needed", entry, "libpython3.so", str(so_path)],
            check=True,
        )

    rpath = f"$ORIGIN/{compute_rpath(so_path, wheel_root)}"
    # NOTE: --set-rpath takes the rpath value and the file as two
    # separate arguments -- not one combined string.
    subprocess.run(["patchelf", "--set-rpath", rpath, str(so_path)], check=True)
    print(f"  patched {so_path.relative_to(wheel_root)} -> RPATH={rpath}")
    return True


def fix_wheel(whl: Path) -> None:
    if "abi3" not in whl.name:
        return

    tmp = Path(tempfile.mkdtemp())
    try:
        with zipfile.ZipFile(whl) as z:
            z.extractall(tmp)

        changed = False
        for so in tmp.rglob("*.abi3.so"):
            if fix_so(so, tmp):
                changed = True

        if not changed:
            return

        whl.unlink()
        with zipfile.ZipFile(whl, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(tmp.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(tmp))
        print(f"repacked {whl.name}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: fix_abi3_linkage.py <wheelhouse-dir>", file=sys.stderr)
        sys.exit(1)

    wheelhouse = Path(sys.argv[1])
    for whl in sorted(wheelhouse.glob("*.whl")):
        fix_wheel(whl)


if __name__ == "__main__":
    main()
