# aiohttp Mobile Wheels

Pre-built Android and iOS Python wheels for **aiohttp** and its C-extension dependencies, automatically rebuilt whenever a new aiohttp release lands on PyPI.

## Packages built

| Package | Why |
|---|---|
| `frozenlist` | C extension, aiohttp dep |
| `multidict` | C extension, aiohttp dep |
| `propcache` | C extension, yarl/aiohttp dep |
| `yarl` | C extension, aiohttp dep |
| `aiohttp` | The main package |

## Platforms

| Platform | Architectures |
|---|---|
| Android | `arm64-v8a` (devices), `x86_64` (emulators) |
| iOS | `arm64_iphoneos` (devices), `arm64_iphonesimulator`, `x86_64_iphonesimulator` |

Python version: **3.14**

---

## Installation

### Option A — GitHub Releases (manual download)

Download `.whl` files from the [Releases page](../../releases) and install them with `pip install <file>.whl`.

### Option B — PEP 503 simple index (pip / poetry / uv)

After enabling GitHub Pages on this repo (Settings → Pages → Source: `gh-pages` branch, `/ (root)`), you can point pip at the generated index:

```bash
pip install aiohttp \
  --extra-index-url https://noob-lol.github.io/aiohttp-mobile/simple/
```

For `pyproject.toml` (uv / poetry):
```toml
[[tool.uv.index]]
url = "https://noob-lol.github.io/aiohttp-mobile/simple/"
explicit = true
```

---

## How it works

1. **Daily schedule** (`0 6 * * *`) — the `resolve-version` job queries PyPI for the latest aiohttp version.
2. If no GitHub Release exists for that version yet, a full build is triggered.
3. `build-android` and `build-ios` jobs run in parallel using a **matrix** over packages × architectures, each using `cibuildwheel`.
4. Wheels are cached by package + version + arch so reruns are instant.
5. The `publish` job collects all wheels, creates a **GitHub Release**, and regenerates the **PEP 503 simple index** on the `gh-pages` branch.

### Manual / forced rebuild

Go to **Actions → Build aiohttp Mobile Wheels → Run workflow** and optionally:
- Supply a specific version (e.g. `3.13.5`)
- Check **Force rebuild** to overwrite an existing release

---

## Repository setup checklist

1. **Enable GitHub Pages**: Settings → Pages → Source: `gh-pages` branch, `/ (root)` directory.
2. **Workflow permissions**: Settings → Actions → General → Workflow permissions → set to *"Read and write permissions"* (required to create releases and push to gh-pages).
3. That's it — no secrets needed beyond the default `GITHUB_TOKEN`.

---

## Adding more packages or Python versions

Edit the `matrix.package` list in the workflow to add packages. To support multiple Python versions, add a `python` dimension to the matrix and adjust `CIBW_BUILD` accordingly.

## Pinning dependency versions

By default `frozenlist`, `multidict`, and `yarl` are resolved to their latest PyPI versions. To pin them, replace the empty `pin: ''` value in the matrix with an explicit version string, e.g. `pin: '3.0.0'`.
