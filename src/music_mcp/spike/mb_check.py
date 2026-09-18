"""Spike: minimal rate-limited MusicBrainz client prototype + one real lookup.

Mirrors the release-radar script's proven conventions: 1.1s spacing between
requests, 503/429 backoff, descriptive User-Agent. Proves the pattern we'll
extract into the Phase 3 client.

Usage: uv run python -m music_mcp.spike.mb_check [ARTIST_MBID]
"""

from __future__ import annotations

import json
import sys
import time
from urllib.request import Request, urlopen

MB_BASE = "https://musicbrainz.org/ws/2"
UA = "self-hosted-music-mcp-spike/0.0.1 (https://github.com/rriegel; hermes@riegelmedia.com)"
RATE_SECONDS = 1.1
_last_request_ts = 0.0


def mb_get(path: str, retries: int = 3) -> dict:
    """Rate-limited MB GET with 503/429 backoff. Returns parsed JSON; raises on final failure."""
    global _last_request_ts
    for attempt in range(retries):
        elapsed = time.monotonic() - _last_request_ts
        if elapsed < RATE_SECONDS:
            time.sleep(RATE_SECONDS - elapsed)
        _last_request_ts = time.monotonic()
        req = Request(f"{MB_BASE}{path}", headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001 - spike
            code = getattr(exc, "code", None)
            if code in (503, 429) and attempt < retries - 1:
                wait = (attempt + 1) * (3 if code == 503 else 5)
                print(f"  retry {attempt + 1}/{retries} in {wait}s: {exc}", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("unreachable")


def main() -> int:
    artist_mbid = sys.argv[1] if len(sys.argv) > 1 else "83d91898-7763-47d2-b9cb-065c48cd3809"  # Pixies
    started = time.monotonic()

    artist = mb_get(f"/artist/{artist_mbid}?inc=tags&fmt=json")
    release_groups = mb_get(
        f"/release-group?artist={artist_mbid}&type=album|ep&status=official&fmt=json&limit=10"
    )

    out = {
        "artist": {"name": artist.get("name"), "mbid": artist.get("id"), "tags": artist.get("tags", [])[:5]},
        "release_groups": [
            {"title": rg.get("title"), "first_release": rg.get("first-release-date"), "type": rg.get("primary-type")}
            for rg in release_groups.get("release-groups", [])
        ],
        "elapsed_s": round(time.monotonic() - started, 1),
    }
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
