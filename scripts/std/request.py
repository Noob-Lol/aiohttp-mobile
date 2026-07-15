import json
import time
import urllib.error
import urllib.request


def fetch_bytes(url: str, timeout: float = 60) -> bytes:
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "aiohttp-mobile-builder"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == 3:
                break
            time.sleep(2**attempt)
    msg = f"failed to download {url}"
    raise RuntimeError(msg) from last_error


def fetch_json(url: str, timeout: float = 20) -> dict:
    return json.loads(fetch_bytes(url, timeout).decode())
