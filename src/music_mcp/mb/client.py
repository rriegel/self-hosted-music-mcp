"""Rate-limited MusicBrainz client with SQLite response cache.

Conventions (mirrors the release-radar script's proven pattern):
- 1.1s minimum spacing between requests; 503/429 backoff with retry
- descriptive User-Agent with a contact URL (MB etiquette; no personal email)
- responses cached in the shared SQLite cache (mb_cache table): MB metadata is
  effectively immutable, so TTL is long and misses hit the network
"""

from __future__ import annotations

import hashlib
import json
import time
from urllib.parse import quote

import httpx

MB_BASE = "https://musicbrainz.org/ws/2"
UA = "self-hosted-music-mcp/0.1.0 (https://github.com/rriegel/self-hosted-music-mcp)"
RATE_SECONDS = 1.1
CACHE_TTL_SECONDS = 30 * 86400  # 30 days; MB entity data is effectively immutable


def cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


class MBClient:
    def __init__(self, conn, base: str = MB_BASE):
        self.conn = conn
        self.base = base.rstrip("/")
        self._last_request = 0.0

    def get(self, path: str, params: dict | None = None, use_cache: bool = True) -> dict:
        """GET a ws/2 endpoint (fmt=json enforced), via cache when available."""
        params = dict(params or {})
        params["fmt"] = "json"
        from urllib.parse import urlencode

        url = f"{self.base}{path}?{urlencode(params)}"
        key = cache_key(url)

        if use_cache:
            row = self.conn.execute(
                "SELECT response, fetched_at FROM mb_cache WHERE key = ?", (key,)
            ).fetchone()
            if row and time.time() - row["fetched_at"] < CACHE_TTL_SECONDS:
                return json.loads(row["response"])

        data = self._fetch(url)
        if use_cache:
            self.conn.execute(
                "INSERT INTO mb_cache (key, url, response, fetched_at) VALUES (?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET response=excluded.response, "
                "fetched_at=excluded.fetched_at",
                (key, url, json.dumps(data), time.time()),
            )
            self.conn.commit()
        return data

    def _fetch(self, url: str, retries: int = 3) -> dict:
        for attempt in range(retries):
            elapsed = time.monotonic() - self._last_request
            if elapsed < RATE_SECONDS:
                time.sleep(RATE_SECONDS - elapsed)
            self._last_request = time.monotonic()
            try:
                resp = httpx.get(url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30)
                if resp.status_code in (503, 429):
                    wait = (attempt + 1) * (3 if resp.status_code == 503 else 5)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.TimeoutException:
                if attempt < retries - 1:
                    continue
                raise
        raise RuntimeError(f"MB request failed after {retries} attempts: {url}")

    # -- typed helpers ---------------------------------------------------------

    def lookup_artist(self, artist_mbid: str, inc: str = "") -> dict:
        params = {}
        if inc:
            params["inc"] = inc
        return self.get(f"/artist/{quote(artist_mbid)}", params)

    def search_artist(self, name: str, limit: int = 5) -> dict:
        """Artist search: exact field query first, then a plain query.

        The artist:"name" field query matches MB's stored name only — renamed
        artists (e.g. Mos Def → Yasiin Bey) come back empty even though MB has
        the old name as an alias. The plain query searches aliases too, so we
        run it as fallback and merge candidates (deduped by id, keeping the
        higher score).
        """
        field_data = self._search_raw(f'artist:"{name}"', limit)
        by_id: dict[str, dict] = {}
        for a in field_data.get("artists", []):
            by_id[a["id"]] = a
        if len(by_id) < limit:
            plain_data = self._search_raw(name, limit)
            for a in plain_data.get("artists", []):
                current = by_id.get(a["id"])
                if current is None or int(a.get("score") or 0) > int(current.get("score") or 0):
                    by_id[a["id"]] = a
        merged = sorted(by_id.values(), key=lambda a: -int(a.get("score") or 0))
        return {"artists": merged[:limit]}

    def _search_raw(self, query: str, limit: int) -> dict:
        return self.get("/artist", {"query": query, "limit": limit})

    def release_groups(self, artist_mbid: str, limit: int = 100) -> list[dict]:
        data = self.get(
            "/release-group",
            {"artist": artist_mbid, "type": "album|ep", "status": "official", "limit": limit},
        )
        return data.get("release-groups", [])
