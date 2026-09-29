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


def generate_diff(original: str, modified: str, filepath: str) -> str:
    """Generate a unified diff between two file contents."""
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            fromfile=f"a/{filepath}",
            tofile=f"b/{filepath}",
            n=3,
        )
    )


class PatchError(Exception):
    """Raised when an inline patch operation cannot be applied."""


def _apply_search_replace(content: str, search_str: str, replace_str: str) -> tuple[str, str]:
    if search_str not in content:
        msg = f"search string {search_str!r} not found"
        raise PatchError(msg)
    count = content.count(search_str)
    new_content = content.replace(search_str, replace_str)
    return new_content, f"replaced {count} occurrence(s) of exact string"


def _apply_regex_replace(content: str, pattern_str: str, replace_str: str) -> tuple[str, str]:
    try:
        rx = re.compile(pattern_str, re.MULTILINE)
    except re.error as err:
        msg = f"invalid regex {pattern_str!r}: {err}"
        raise PatchError(msg) from err
    new_content, count = rx.subn(replace_str, content)
    if count == 0:
        msg = f"regex pattern {pattern_str!r} matched 0 times"
        raise PatchError(msg)
    return new_content, f"replaced {count} regex match(es)"


def _apply_line_insertion(content: str, anchor: str, insertion: str, *, after: bool) -> tuple[str, str]:
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
        msg = f"anchor {anchor!r} not found"
        raise PatchError(msg)
    direction = "after" if after else "before"
    return "".join(new_lines), f"inserted line {direction} anchor {anchor!r}"


def _transform_content(patch_def: dict[str, PatchConfig], content: str) -> tuple[str, str]:
    if "search" in patch_def and "replace" in patch_def:
        return _apply_search_replace(content, str(patch_def["search"]), str(patch_def["replace"]))
    if "pattern" in patch_def and "replace" in patch_def:
        return _apply_regex_replace(content, str(patch_def["pattern"]), str(patch_def["replace"]))
    if "after" in patch_def and "insert" in patch_def:
        return _apply_line_insertion(content, str(patch_def["after"]), str(patch_def["insert"]), after=True)
    if "before" in patch_def and "insert" in patch_def:
        return _apply_line_insertion(content, str(patch_def["before"]), str(patch_def["insert"]), after=False)
    msg = f"unrecognized patch operation: {list(patch_def.keys())}"
    raise PatchError(msg)


class SdistPatcher:
    """Applies recipes, declarative patches, and external patch files to an unpacked sdist."""

    def __init__(self, package: str, version: str, sdist_dir: Path, *, dry_run: bool = False) -> None:
        self.package = package
        self.version = version
        self.sdist_dir = sdist_dir
        self.dry_run = dry_run
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

    def apply_recipe_abi3_min_version(self, target_tag: str = DEFAULT_ABI3_TAG) -> None:
        """Rewrite the active abi3-py3X marker to target_tag in Cargo.toml and pyproject.toml."""
        target_files = ["Cargo.toml", "pyproject.toml"]
        pattern = re.compile(r"abi3-py3\d+")

        for rel_path in target_files:
            file_path = self.sdist_dir / rel_path
            if not file_path.is_file():
                self.log_skip(rel_path, "file does not exist in sdist")
                continue

            try:
                content = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                self.log_skip(rel_path, f"error reading file: {exc}")
                continue

            match = pattern.search(content)
            if not match:
                self.log_skip(rel_path, f"pattern '{pattern.pattern}' not found")
                continue

            old_tag = match.group(0)
            if old_tag == target_tag:
                self.log_skip(rel_path, f"already at target version '{target_tag}'")
                continue

            if target_tag in content:
                # Target feature is already defined; only update the default/first reference
                new_content, count = pattern.subn(target_tag, content, count=1)
            else:
                # Replace occurrences of this specific tag (updating both default reference and its definition)
                old_tag_rx = re.compile(rf"\b{re.escape(old_tag)}\b")
                new_content, count = old_tag_rx.subn(target_tag, content)

            diff = generate_diff(content, new_content, rel_path)
            if not self.dry_run:
                file_path.write_text(new_content, encoding="utf-8")
            self.log_applied(rel_path, f"updated {count} occurrence(s) of '{old_tag}' to '{target_tag}'", diff)

    def apply_inline_patch(self, patch_def: dict[str, PatchConfig]) -> None:
        """Apply an inline search/replace, regex, or insertion patch definition."""
        target_rel = patch_def.get("file")
        target_rel_list = patch_def.get("files")

        file_list: list[str] = []
        if isinstance(target_rel, str):
            file_list.append(target_rel)
        if isinstance(target_rel_list, list):
            file_list.extend(str(f) for f in target_rel_list)

        if not file_list:
            self.log_skip("inline_patch", "missing 'file' or 'files' key in patch definition")
            return

        for rel_file in file_list:
            file_path = self.sdist_dir / rel_file
            if not file_path.is_file():
                self.log_skip(rel_file, "file does not exist in sdist")
                continue

            try:
                content = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                self.log_skip(rel_file, f"error reading file: {exc}")
                continue

            try:
                new_content, detail = _transform_content(patch_def, content)
            except PatchError as exc:
                self.log_skip(rel_file, str(exc))
                continue

            if new_content == content:
                self.log_skip(rel_file, "content was not modified")
                continue

            diff = generate_diff(content, new_content, rel_file)
            if not self.dry_run:
                file_path.write_text(new_content, encoding="utf-8")
            self.log_applied(rel_file, detail, diff)

    def apply_single_patch_file(self, patch_file: Path) -> None:
        """Attempt to apply a single .patch file via patch or git apply."""
        cmd = ["patch", "-p1", "-N", "-s", "-i", str(patch_file.resolve())]
        if self.dry_run:
            cmd.insert(1, "--dry-run")
        try:
            res = subprocess.run(cmd, cwd=self.sdist_dir, capture_output=True, text=True, check=False)
            if res.returncode == 0:
                self.log_applied(patch_file.name, f"applied patch from {patch_file}")
                return
        except (OSError, subprocess.SubprocessError) as exc:
            self.log_skip(patch_file.name, f"error executing patch command: {exc}")

        git_cmd = ["git", "apply", "--whitespace=nowarn", str(patch_file.resolve())]
        if self.dry_run:
            git_cmd.append("--check")
        try:
            git_res = subprocess.run(git_cmd, cwd=self.sdist_dir, capture_output=True, text=True, check=False)
            if git_res.returncode == 0:
                self.log_applied(patch_file.name, f"applied patch via git apply from {patch_file}")
            else:
                self.log_skip(patch_file.name, f"failed to apply cleanly: {git_res.stderr.strip()}")
        except (OSError, subprocess.SubprocessError) as exc:
            self.log_skip(patch_file.name, f"error executing git apply: {exc}")

    def apply_directory_patches(self) -> None:
        """Find and apply any .patch files in patches/<package_name>/."""
        candidates = [Path("patches") / self.package, Path("patches") / normalize(self.package)]
        patch_dir = next((p for p in candidates if p.is_dir()), None)
        if not patch_dir:
            return

        for patch_file in sorted(patch_dir.glob("*.patch")):
            self.apply_single_patch_file(patch_file)

    def apply_legacy_patch(self, patch_val: str | list[str]) -> None:
        """Execute legacy shell command(s) configured in `patch`."""
        commands = [patch_val] if isinstance(patch_val, str) else [str(c) for c in patch_val]

        for cmd in commands:
            expanded_cmd = cmd.replace("{project}", self.sdist_dir.as_posix())
            if self.dry_run:
                self.log_applied("legacy_patch", f"[dry-run] would run: {expanded_cmd}")
                continue

            try:
                res = subprocess.run(expanded_cmd, shell=True, cwd=self.sdist_dir, capture_output=True, text=True, check=False)
                if res.returncode == 0:
                    self.log_applied("legacy_patch", f"command succeeded: {expanded_cmd}")
                else:
                    self.log_skip(
                        "legacy_patch", f"command exited with {res.returncode}: {res.stderr.strip() or res.stdout.strip()}"
                    )
            except (OSError, subprocess.SubprocessError) as exc:
                self.log_skip("legacy_patch", f"error running command: {exc}")

    def execute(self, pkg_config: dict[str, PatchConfig]) -> None:
        """Execute all configured patches and recipes for this package."""
        # 1. Apply recipes
        recipes = pkg_config.get("recipes", [])
        recipe_list = [recipes] if isinstance(recipes, str) else recipes
        if isinstance(recipe_list, list):
            for recipe_name in recipe_list:
                if str(recipe_name) == "abi3_min_version":
                    self.apply_recipe_abi3_min_version()
                else:
                    self.log_skip("recipe", f"unknown recipe '{recipe_name}'")

        # 2. Apply compact declarative patches
        inline_patches = pkg_config.get("patches", [])
        if isinstance(inline_patches, list):
            for patch_def in inline_patches:
                if isinstance(patch_def, dict):
                    self.apply_inline_patch(patch_def)

        # 3. Apply directory .patch files
        self.apply_directory_patches()

        # 4. Apply legacy patch shell commands if present
        legacy_patch = pkg_config.get("patch")
        if isinstance(legacy_patch, (str, list)):
            self.apply_legacy_patch(legacy_patch)


def patch_package(
    config_path: Path, package_name: str, version: str, sdist_dir: Path, *, dry_run: bool = False
) -> SdistPatcher:
    """Perform best-effort patching on an unpacked source distribution."""
    patcher = SdistPatcher(package_name, version, sdist_dir, dry_run=dry_run)

    if not config_path.is_file():
        patcher.log_skip("config", f"configuration file '{config_path}' not found")
        return patcher

    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        patcher.log_skip("config", f"failed to parse '{config_path}': {exc}")
        return patcher

    packages = config.get("package", [])
    norm_name = normalize(package_name)
    pkg_config = next((p for p in packages if isinstance(p, dict) and normalize(str(p.get("name", ""))) == norm_name), None)

    if not pkg_config and not (Path("patches") / package_name).is_dir() and not (Path("patches") / norm_name).is_dir():
        print(f"No patch configuration or patches/ directory found for {package_name}. Nothing to patch.")
        return patcher

    patcher.execute(pkg_config or {})
    print(patcher.summary())
    return patcher


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


if __name__ == "__main__":
    main()
