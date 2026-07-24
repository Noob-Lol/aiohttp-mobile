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
from collections.abc import Callable
from pathlib import Path
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")
VERSIONED_LIBPYTHON = re.compile(r"^libpython3\.\d+\.so$")
STABLE_LIB = "libpython3.so"


def make_runner(main_cmd: str | list[str], _target: Callable[P, R] = subprocess.run) -> Callable[P, R]:
    """Create a subprocess runner for the given main command. Check is True by default."""

    def runner(*args: P.args, **kwargs: P.kwargs) -> R:
        check = kwargs.pop("check", True)
        exe = main_cmd
        cmd, *rest = args or ([],)
        if isinstance(exe, str):
            exe = [exe]
        if isinstance(cmd, (str, bytes)):
            cmd = [cmd]
        # trick to not lose typing
        full_cmd = exe + cmd  # type: ignore[arg-type]
        return subprocess.run(full_cmd, *rest, check=check, **kwargs)  # type: ignore[arg-type]

    return runner


patchelf = make_runner("patchelf")
readelf = make_runner("readelf")


def compute_rpath(so_path: Path, wheel_root: Path) -> str:
    """
    Number of directory levels from the .so up to the wheel root
    (== site-packages/ once installed), plus two more levels to
    reach lib/:
        site-packages/ -> pythonX.Y/ -> lib/
    """
    rel = so_path.parent.relative_to(wheel_root)
    depth = len(rel.parts) + 2
    return "/".join([".."] * depth)


def get_needed(so_path: Path) -> list[str]:
    result = patchelf(["--print-needed", str(so_path)], capture_output=True, text=True)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def fix_so(so_path: Path, wheel_root: Path) -> bool:
    needed = get_needed(so_path)
    versioned_entries = [n for n in needed if VERSIONED_LIBPYTHON.match(n)]
    if not versioned_entries:
        return False

    for entry in versioned_entries:
        if entry == STABLE_LIB:
            continue
        patchelf(["--replace-needed", entry, STABLE_LIB, str(so_path)])

    rpath = f"$ORIGIN/{compute_rpath(so_path, wheel_root)}"
    # NOTE: --set-rpath takes the rpath value and the file as two
    # separate arguments -- not one combined string.
    patchelf(["--set-rpath", rpath, str(so_path)])
    print(f"  patched {so_path.relative_to(wheel_root)} -> RPATH={rpath}")
    return True


def is_extension_module(so_path: Path) -> bool:
    result = readelf(["-sW", str(so_path)], capture_output=True, text=True)
    return "PyInit_" in result.stdout


def check_py_link(wheel_root: Path) -> None:
    for so_path in wheel_root.rglob("*.so"):
        print(f"  checking {so_path.relative_to(wheel_root)}")
        needed = get_needed(so_path)
        for line in needed:
            print(f"    {line}")
        if not is_extension_module(so_path):
            print("  not an extension module")
            continue
        if not any("libpython" in line for line in needed):
            msg = f"{so_path.relative_to(wheel_root)}: no libpython in NEEDED"
            raise RuntimeError(msg)
        print("  OK")


def fix_wheel(whl: Path) -> None:
    tmp = Path(tempfile.mkdtemp())
    try:
        with zipfile.ZipFile(whl) as z:
            z.extractall(tmp)

        changed = False
        for so in tmp.rglob("*.abi3.so"):
            if fix_so(so, tmp):
                changed = True

        check_py_link(tmp)

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
        print(f"usage: {Path(sys.argv[0]).name} <wheelhouse-dir>", file=sys.stderr)
        sys.exit(1)

    wheelhouse = Path(sys.argv[1])
    for whl in sorted(wheelhouse.glob("*.whl")):
        fix_wheel(whl)


if __name__ == "__main__":
    main()
