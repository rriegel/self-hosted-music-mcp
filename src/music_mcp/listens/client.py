"""ListenBrainz API client: paginated listens pull + top-artist stats.

Public endpoints work unauthenticated for public profiles (proven in Phase 0);
LB_TOKEN is used when present (private-data robustness, future write endpoints).
Rate limits are generous but we pace modestly and use a descriptive UA.
"""

from __future__ import annotations

import os
import time
from urllib.parse import quote

import httpx

LB_BASE = "https://api.listenbrainz.org/1"
UA = "self-hosted-music-mcp/0.1.0 (https://github.com/rriegel/self-hosted-music-mcp)"
PAGE_SIZE = 100  # LB max per page for /listens


class LBClient:
    def __init__(self, user: str | None = None, token: str | None = None, base: str = LB_BASE):
        self.user = user or os.environ.get("LB_USER") or ""
        self.token = token or os.environ.get("LB_TOKEN")
        self.base = base.rstrip("/")
        self._last_request = 0.0
        if not self.user:
            raise ValueError("no ListenBrainz user given (pass user= or set LB_USER)")

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": UA, "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Token {self.token}"
        return headers

    def _get(self, path: str, params: dict | None = None, retries: int = 3) -> dict:
        """Paced GET with 429/5xx backoff. Raises httpx.HTTPStatusError on final failure."""
        for attempt in range(retries):
            elapsed = time.monotonic() - self._last_request
            if elapsed < 0.3:  # modest pacing; LB allows 1/s but we stay polite
                time.sleep(0.3 - elapsed)
            self._last_request = time.monotonic()
            try:
                resp = httpx.get(
                    f"{self.base}{path}", params=params, headers=self._headers(), timeout=30
                )
                if resp.status_code in (429,) or resp.status_code >= 500:
                    wait = (attempt + 1) * 5
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.TimeoutException:
                if attempt < retries - 1:
                    time.sleep((attempt + 1) * 2)
                    continue
                raise
        raise RuntimeError(f"LB GET failed after {retries} attempts: {path}")

    def listens_page(
        self, max_ts: int | None = None, min_ts: int | None = None, count: int = PAGE_SIZE
    ) -> dict:
        """One page of listens. LB semantics: max_ts paginates backward in time;
        omitting both returns the newest listens."""
        params: dict = {"count": count}
        if max_ts is not None:
            params["max_ts"] = max_ts
        if min_ts is not None:
            params["min_ts"] = min_ts
        return self._get(f"/user/{quote(self.user)}/listens", params)

    def iter_all_listens(self, since_ts: int | None = None, stop_after_pages: int | None = None):
        """Yield listen dicts newest→oldest. Stops when a page comes back empty or
        (incremental mode) when listens older than since_ts start appearing.

        since_ts: only yield listens with ts > since_ts (used for incremental sync).
        stop_after_pages: hard cap for bounded syncs (tests, first runs).
        """
        max_ts: int | None = None
        pages = 0
        while True:
            payload = self.listens_page(max_ts=max_ts)
            listens = payload.get("payload", {}).get("listens", [])
            if not listens:
                return
            for listen in listens:
                ts = listen.get("listened_at") or 0
                if since_ts is not None and ts <= since_ts:
                    return  # reached already-synced territory
                yield listen
            pages += 1
            if stop_after_pages is not None and pages >= stop_after_pages:
                return
            oldest = listens[-1].get("listened_at")
            if oldest is None:
                return
            max_ts = oldest  # continue backward in time

    def top_artists(self, range_: str = "all_time", count: int = 200) -> list[dict]:
        """User's top artists (name, listen_count). Name-based: LB stats carry no MBIDs
        for Pano Scrobbler scrobbles (Phase 0 finding)."""
        data = self._get(
            f"/stats/user/{quote(self.user)}/artists", {"range": range_, "count": count}
        )
        return data.get("payload", {}).get("artists", [])
