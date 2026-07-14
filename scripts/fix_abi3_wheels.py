#!/usr/bin/env python3
import os
import sys
import glob
import subprocess
import tempfile
import zipfile
import shutil
import re

def fix_wheel(whl_path):
    print(f"Checking wheel: {os.path.basename(whl_path)}")
    if "-abi3-" not in os.path.basename(whl_path):
        print("  Not an abi3 wheel, skipping.")
        return

    print(f"Fixing ABI3 linkage for: {os.path.basename(whl_path)}")
    
    with tempfile.TemporaryDirectory() as temp_dir:
        # Extract the wheel, preserving permissions
        with zipfile.ZipFile(whl_path, 'r') as z:
            for info in z.infolist():
                extracted_path = z.extract(info, temp_dir)
                mode = info.external_attr >> 16
                if mode:
                    os.chmod(extracted_path, mode)

        # Search for .so files
        so_files = []
        for root, _, files in os.walk(temp_dir):
            for file in files:
                if file.endswith('.so'):
                    so_files.append(os.path.join(root, file))

        if not so_files:
            print("  No .so files found in wheel.")
            return

        modified = False
        for so_file in so_files:
            # Run readelf -d to get dependencies
            try:
                out = subprocess.check_output(['readelf', '-d', so_file], text=True)
            except subprocess.CalledProcessError as e:
                print(f"  Error running readelf on {so_file}: {e}")
                continue

            needed_libs = []
            for line in out.splitlines():
                if 'NEEDED' in line:
                    # e.g., " 0x0000000000000001 (NEEDED)             Shared library: [libpython3.13.so]"
                    match = re.search(r'Shared library:\s+\[(.*?)\]', line)
                    if match:
                        needed_libs.append(match.group(1))

            print(f"  {os.path.basename(so_file)} links to: {needed_libs}")

            # Find libpython dependencies (e.g. libpython3.13.so, libpython3.13t.so, libpython3.14.so, etc.)
            for lib in needed_libs:
                if re.match(r'^libpython3\.\d+t?\.so$', lib):
                    print(f"  Replacing dependency: {lib} -> libpython3.so in {os.path.basename(so_file)}")
                    try:
                        subprocess.check_call([
                            'patchelf',
                            '--replace-needed',
                            lib,
                            'libpython3.so',
                            so_file
                        ])
                        modified = True
                    except subprocess.CalledProcessError as e:
                        print(f"  Failed to run patchelf on {so_file}: {e}")
                elif lib == 'libpython3.so':
                    print(f"  {os.path.basename(so_file)} already links to libpython3.so correctly.")

        if modified:
            # Repack the zip file, preserving permissions
            temp_whl_path = whl_path + '.tmp'
            with zipfile.ZipFile(temp_whl_path, 'w', zipfile.ZIP_DEFLATED) as z:
                for root, _, files in os.walk(temp_dir):
                    for file in files:
                        full_path = os.path.join(root, file)
                        rel_path = os.path.relpath(full_path, temp_dir)
                        info = zipfile.ZipInfo(rel_path)
                        # Preserve permissions
                        stat = os.stat(full_path)
                        info.external_attr = stat.st_mode << 16
                        with open(full_path, 'rb') as f:
                            z.writestr(info, f.read())
            shutil.move(temp_whl_path, whl_path)
            print(f"  Successfully updated {os.path.basename(whl_path)}")
        else:
            print(f"  No modifications needed for {os.path.basename(whl_path)}")

def main():
    if len(sys.argv) > 1:
        whl_paths = sys.argv[1:]
    else:
        # Default to checking wheelhouse/
        whl_paths = glob.glob('wheelhouse/**/*.whl', recursive=True) + glob.glob('dist/**/*.whl', recursive=True)

    if not whl_paths:
        print("No wheels found to check.")
        return

    for path in whl_paths:
        if os.path.exists(path):
            fix_wheel(path)

if __name__ == '__main__':
    main()
