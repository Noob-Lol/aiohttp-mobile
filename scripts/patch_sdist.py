#!/usr/bin/env python3
"""
patch_sdist.py — Apply recipes, declarative patches, and external patch files to an unpacked sdist.

Usage:
    uv run scripts/patch_sdist.py <package_name> <version> <sdist_dir> [--config packages.toml] [--dry-run]

Patching is best-effort: if a target file or pattern is not found, a warning is
logged and the process continues without failing. Build setup checks or compilers
will naturally catch errors if an essential patch was skipped.
"""

from __future__ import annotations

import argparse
import difflib
import re
import subprocess
import sys
from pathlib import Path

import tomllib

PatchConfig = dict[str, str | list[str] | list[dict[str, str]] | dict[str, str]]

# Default ABI3 minimum tag for Android (PEP 738 introduced Android support in Python 3.13)
DEFAULT_ABI3_TAG = "abi3-py313"


def normalize(name: str) -> str:
    """PEP 503 normalize a package name."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


class PatchReport:
    """Collects and displays results of patch attempts."""

    def __init__(self, package: str, version: str) -> None:
        self.package = package
        self.version = version
        self.applied: list[str] = []
        self.skipped: list[str] = []

    def log_applied(self, target: str, detail: str, diff: str = "") -> None:
        msg = f"[APPLIED] {target}: {detail}"
        self.applied.append(msg)
        print(msg)
        if diff.strip():
            print(diff)

    def log_skip(self, target: str, reason: str) -> None:
        msg = f"[SKIP]    {target}: {reason}"
        self.skipped.append(msg)
        print(msg)

    def summary(self) -> str:
        lines = [
            f"=== Patch Summary for {self.package} {self.version} ===",
            f"  Applied: {len(self.applied)}",
            f"  Skipped: {len(self.skipped)}",
        ]
        lines.extend(f"  + {item}" for item in self.applied)
        lines.extend(f"  - {item}" for item in self.skipped)
        lines.append("=" * 40)
        return "\n".join(lines)


def generate_diff(original: str, modified: str, filepath: str) -> str:
    """Generate a unified diff between two file contents."""
    diff_lines = list(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            fromfile=f"a/{filepath}",
            tofile=f"b/{filepath}",
            n=3,
        )
    )
    return "".join(diff_lines)


def apply_recipe_abi3_min_version(
    sdist_dir: Path, report: PatchReport, *, target_tag: str = DEFAULT_ABI3_TAG, dry_run: bool = False
) -> None:
    """Rewrite abi3-py3X markers to the minimum supported version in Cargo.toml and pyproject.toml."""
    target_files = ["Cargo.toml", "pyproject.toml"]
    pattern = re.compile(r"abi3-py3[0-9][0-9]*")

    for rel_path in target_files:
        file_path = sdist_dir / rel_path
        if not file_path.is_file():
            report.log_skip(rel_path, "file does not exist in sdist")
            continue

        try:
            content = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.log_skip(rel_path, f"error reading file: {exc}")
            continue

        new_content, count = pattern.subn(target_tag, content)
        if count == 0:
            report.log_skip(rel_path, f"pattern '{pattern.pattern}' not found")
            continue

        diff = generate_diff(content, new_content, rel_path)
        if not dry_run:
            file_path.write_text(new_content, encoding="utf-8")
        report.log_applied(rel_path, f"updated {count} ABI3 occurrence(s) to '{target_tag}'", diff)


RECIPES = {"abi3_min_version": apply_recipe_abi3_min_version}


def _apply_search_replace(content: str, search_str: str, replace_str: str) -> tuple[str, str | None, str | None]:
    if search_str not in content:
        return content, None, f"search string {search_str!r} not found"
    count = content.count(search_str)
    new_content = content.replace(search_str, replace_str)
    return new_content, f"replaced {count} occurrence(s) of exact string", None


def _apply_regex_replace(content: str, pattern_str: str, replace_str: str) -> tuple[str, str | None, str | None]:
    try:
        rx = re.compile(pattern_str, re.MULTILINE)
    except re.error as err:
        return content, None, f"invalid regex {pattern_str!r}: {err}"
    new_content, count = rx.subn(replace_str, content)
    if count == 0:
        return content, None, f"regex pattern {pattern_str!r} matched 0 times"
    return new_content, f"replaced {count} regex match(es)", None


def _apply_line_insertion(content: str, anchor: str, insertion: str, *, after: bool) -> tuple[str, str | None, str | None]:
    lines = content.splitlines(keepends=True)
    matched = False
    new_lines: list[str] = []
    formatted_insert = insertion if insertion.endswith("\n") else f"{insertion}\n"

    for line in lines:
        if not after and anchor in line and not matched:
            new_lines.append(formatted_insert)
            matched = True
        new_lines.append(line)
        if after and anchor in line and not matched:
            if not line.endswith("\n"):
                new_lines.append("\n")
            new_lines.append(formatted_insert)
            matched = True

    if not matched:
        return content, None, f"anchor {anchor!r} not found"
    direction = "after" if after else "before"
    return "".join(new_lines), f"inserted line {direction} anchor {anchor!r}", None


def _transform_content(patch_def: dict[str, str], content: str) -> tuple[str, str | None, str | None]:
    if "search" in patch_def and "replace" in patch_def:
        return _apply_search_replace(content, patch_def["search"], patch_def["replace"])
    if "pattern" in patch_def and "replace" in patch_def:
        return _apply_regex_replace(content, patch_def["pattern"], patch_def["replace"])
    if "after" in patch_def and "insert" in patch_def:
        return _apply_line_insertion(content, patch_def["after"], patch_def["insert"], after=True)
    if "before" in patch_def and "insert" in patch_def:
        return _apply_line_insertion(content, patch_def["before"], patch_def["insert"], after=False)
    return content, None, f"unrecognized patch operation: {list(patch_def.keys())}"


def apply_inline_patch(patch_def: dict[str, str], sdist_dir: Path, report: PatchReport, *, dry_run: bool = False) -> None:
    """Apply an inline search/replace, regex, or insertion patch definition."""
    target_rel = patch_def.get("file")
    target_rel_list = patch_def.get("files")

    file_list: list[str] = []
    if target_rel:
        file_list.append(str(target_rel))
    if isinstance(target_rel_list, list):
        file_list.extend(str(f) for f in target_rel_list)

    if not file_list:
        report.log_skip("inline_patch", "missing 'file' or 'files' key in patch definition")
        return

    for rel_file in file_list:
        file_path = sdist_dir / rel_file
        if not file_path.is_file():
            report.log_skip(rel_file, "file does not exist in sdist")
            continue

        try:
            content = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.log_skip(rel_file, f"error reading file: {exc}")
            continue

        new_content, detail, skip_reason = _transform_content(patch_def, content)
        if skip_reason is not None or detail is None:
            report.log_skip(rel_file, skip_reason or "patch could not be applied")
            continue

        if new_content == content:
            report.log_skip(rel_file, "content was not modified")
            continue

        diff = generate_diff(content, new_content, rel_file)
        if not dry_run:
            file_path.write_text(new_content, encoding="utf-8")
        report.log_applied(rel_file, detail, diff)


def apply_single_patch_file(patch_file: Path, sdist_dir: Path, report: PatchReport, *, dry_run: bool = False) -> None:
    """Attempt to apply a single .patch file via patch or git apply."""
    cmd = ["patch", "-p1", "-N", "-s", "-i", str(patch_file.resolve())]
    if dry_run:
        cmd.insert(1, "--dry-run")
    try:
        res = subprocess.run(cmd, cwd=sdist_dir, capture_output=True, text=True, check=False)
        if res.returncode == 0:
            report.log_applied(patch_file.name, f"applied patch from {patch_file}")
            return
    except (OSError, subprocess.SubprocessError) as exc:
        report.log_skip(patch_file.name, f"error executing patch command: {exc}")

    git_cmd = ["git", "apply", "--whitespace=nowarn", str(patch_file.resolve())]
    if dry_run:
        git_cmd.append("--check")
    try:
        git_res = subprocess.run(git_cmd, cwd=sdist_dir, capture_output=True, text=True, check=False)
        if git_res.returncode == 0:
            report.log_applied(patch_file.name, f"applied patch via git apply from {patch_file}")
        else:
            report.log_skip(patch_file.name, f"failed to apply cleanly: {git_res.stderr.strip()}")
    except (OSError, subprocess.SubprocessError) as exc:
        report.log_skip(patch_file.name, f"error executing git apply: {exc}")


def apply_directory_patches(package_name: str, sdist_dir: Path, report: PatchReport, *, dry_run: bool = False) -> None:
    """Find and apply any .patch files in patches/<package_name>/."""
    candidates = [Path("patches") / package_name, Path("patches") / normalize(package_name)]
    patch_dir = next((p for p in candidates if p.is_dir()), None)
    if not patch_dir:
        return

    for patch_file in sorted(patch_dir.glob("*.patch")):
        apply_single_patch_file(patch_file, sdist_dir, report, dry_run=dry_run)


def apply_legacy_patch(patch_val: str | list[str], sdist_dir: Path, report: PatchReport, *, dry_run: bool = False) -> None:
    """Execute legacy shell command(s) configured in `patch`."""
    commands: list[str] = [patch_val] if isinstance(patch_val, str) else [str(c) for c in patch_val]

    for cmd in commands:
        expanded_cmd = cmd.replace("{project}", sdist_dir.as_posix())
        if dry_run:
            report.log_applied("legacy_patch", f"[dry-run] would run: {expanded_cmd}")
            continue

        try:
            res = subprocess.run(expanded_cmd, shell=True, cwd=sdist_dir, capture_output=True, text=True, check=False)
            if res.returncode == 0:
                report.log_applied("legacy_patch", f"command succeeded: {expanded_cmd}")
            else:
                report.log_skip(
                    "legacy_patch", f"command exited with {res.returncode}: {res.stderr.strip() or res.stdout.strip()}"
                )
        except (OSError, subprocess.SubprocessError) as exc:
            report.log_skip("legacy_patch", f"error running command: {exc}")


def _execute_patches_for_pkg(
    pkg_config: PatchConfig, package_name: str, sdist_dir: Path, report: PatchReport, *, dry_run: bool
) -> None:
    # 1. Apply recipes
    recipes = pkg_config.get("recipes", [])
    recipe_list = [recipes] if isinstance(recipes, str) else recipes
    if isinstance(recipe_list, list):
        for recipe_name in recipe_list:
            recipe_fn = RECIPES.get(str(recipe_name))
            if recipe_fn:
                recipe_fn(sdist_dir, report, dry_run=dry_run)
            else:
                report.log_skip("recipe", f"unknown recipe '{recipe_name}'")

    # 2. Apply compact declarative patches
    inline_patches = pkg_config.get("patches", [])
    if isinstance(inline_patches, list):
        for patch_def in inline_patches:
            if isinstance(patch_def, dict):
                apply_inline_patch(patch_def, sdist_dir, report, dry_run=dry_run)

    # 3. Apply directory .patch files
    apply_directory_patches(package_name, sdist_dir, report, dry_run=dry_run)

    # 4. Apply legacy patch shell commands if present
    legacy_patch = pkg_config.get("patch")
    if isinstance(legacy_patch, str):
        apply_legacy_patch(legacy_patch, sdist_dir, report, dry_run=dry_run)
    elif isinstance(legacy_patch, list):
        commands = [c for c in legacy_patch if isinstance(c, str)]
        if commands:
            apply_legacy_patch(commands, sdist_dir, report, dry_run=dry_run)


def patch_package(
    config_path: Path, package_name: str, version: str, sdist_dir: Path, *, dry_run: bool = False
) -> PatchReport:
    """Perform best-effort patching on an unpacked source distribution."""
    report = PatchReport(package_name, version)

    if not config_path.is_file():
        report.log_skip("config", f"configuration file '{config_path}' not found")
        return report

    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        report.log_skip("config", f"failed to parse '{config_path}': {exc}")
        return report

    packages = config.get("package", [])
    norm_name = normalize(package_name)
    pkg_config = next((p for p in packages if isinstance(p, dict) and normalize(str(p.get("name", ""))) == norm_name), None)

    if not pkg_config and not (Path("patches") / package_name).is_dir() and not (Path("patches") / norm_name).is_dir():
        print(f"No patch configuration or patches/ directory found for {package_name}. Nothing to patch.")
        return report

    _execute_patches_for_pkg(pkg_config or {}, package_name, sdist_dir, report, dry_run=dry_run)

    print(report.summary())
    return report


def main() -> None:
    """CLI entry point for scripts/patch_sdist.py."""
    parser = argparse.ArgumentParser(description="Apply best-effort patches to an unpacked sdist.")
    parser.add_argument("package", help="Name of the package")
    parser.add_argument("version", help="Version of the package")
    parser.add_argument("sdist_dir", type=Path, help="Directory of the unpacked sdist")
    parser.add_argument("--config", type=Path, default=Path("packages.toml"), help="Path to packages.toml")
    parser.add_argument("--dry-run", action="store_true", help="Simulate patch operations without writing changes")
    args = parser.parse_args()

    sdist_dir = args.sdist_dir.resolve()
    if not sdist_dir.is_dir():
        print(f"Warning: sdist directory '{sdist_dir}' does not exist. Skipping patching.")
        sys.exit(0)

    patch_package(args.config, args.package, args.version, sdist_dir, dry_run=args.dry_run)
    sys.exit(0)


if __name__ == "__main__":
    main()
