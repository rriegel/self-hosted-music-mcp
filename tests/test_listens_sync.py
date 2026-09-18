"""Sync + report tests with a fake LBClient (no network, no respx needed)."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Generator

import pytest

from music_mcp.library import db as db_mod
from music_mcp.listens import reports
from music_mcp.listens.sync import refresh_listened_artists, sync_listens

DAY = 86400
NOW = int(time.time())  # reports use the real clock; tests anchor to it too


class FakeClient:
    """Mimics LBClient.iter_all_listens without network."""

    def __init__(self, listens: list[dict], user: str = "tester"):
        self.listens = listens
        self.user = user

    def iter_all_listens(self, since_ts=None, stop_after_pages=None):
        for listen in self.listens:
            ts = listen.get("listened_at") or 0
            if since_ts is not None and ts <= since_ts:
                return
            yield listen


@pytest.fixture()
def cache(tmp_path) -> Generator[sqlite3.Connection]:
    conn = db_mod.connect(tmp_path / "t.db")
    yield conn
    conn.close()


def _listen(ts: int, artist: str, track: str) -> dict:
    return {"listened_at": ts, "track_metadata": {"artist_name": artist, "track_name": track}}


def _seed_library(conn: sqlite3.Connection, owned: dict[str, str], folder_prefix: str = "lib"):
    """owned: artist name -> mbid. Inserts minimal tracks/artists rows."""
    for i, (name, mbid) in enumerate(owned.items()):
        conn.execute(
            "INSERT OR IGNORE INTO artists (artist_mbid, name, first_seen, last_seen) VALUES (?,?,?,?)",
            (mbid, name, "t", "t"),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO tracks (path, parent_folder, filename, suffix, artist,
                                          artist_mbid, readable, mtime, size, first_seen, last_seen)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (f"/{folder_prefix}{i}/x.mp3", f"{folder_prefix}{i}", "x.mp3", ".mp3", name, mbid, 1, 0, 0, "t", "t"),
        )
    conn.commit()


def test_sync_is_idempotent_and_incremental(cache: sqlite3.Connection):
    listens = [_listen(NOW - 100, "Owned Band", "s1"), _listen(NOW - 200, "Ghost Artist", "g1")]
    first = sync_listens(cache, FakeClient(listens))
    assert first["listens_synced"] == 2
    # same data again via incremental (client stops at since boundary)
    again = sync_listens(cache, FakeClient(listens))
    assert again["listens_synced"] == 0
    # full mode re-upserts the same PKs without duplicating
    full = sync_listens(cache, FakeClient(listens), incremental=False)
    assert full["listens_synced"] == 2
    assert cache.execute("SELECT COUNT(*) FROM listens").fetchone()[0] == 2


def test_listened_artists_rollup(cache: sqlite3.Connection):
    sync_listens(cache, FakeClient([_listen(5, "A", "1"), _listen(6, "A", "2"), _listen(7, "B", "3")]))
    n = refresh_listened_artists(cache)
    assert n == 2
    top = reports.listens_top_artists(cache)
    assert top["artists"][0]["artist_name"] == "A"
    assert top["artists"][0]["listen_count"] == 2


def test_gap_analysis_separates_owned_and_unowned(cache: sqlite3.Connection):
    _seed_library(cache, {"Owned Band": "mbid-owned"})
    listens = [
        _listen(NOW - i, "Owned Band", f"s{i}") for i in range(6)
    ] + [_listen(NOW - i, "Ghost Artist", f"g{i}") for i in range(9)]
    sync_listens(cache, FakeClient(listens))
    refresh_listened_artists(cache)

    gap = reports.listens_gap_analysis(cache, min_listens=5)
    names = {g["artist"] for g in gap["not_owned"]}
    assert "Ghost Artist" in names
    assert "Owned Band" not in names


def test_stale_library_uses_past_intensity(cache: sqlite3.Connection):
    _seed_library(cache, {"Stale Band": "mbid-1", "Active Band": "mbid-2"})
    # Stale Band: high past intensity, but all listens > 6 months old
    listens = [_listen(NOW - 400 * DAY - i * DAY, "Stale Band", f"s{i}") for i in range(10)]
    # Active Band: listened within the window
    listens += [_listen(NOW - DAY, "Active Band", "recent") for _ in range(3)]
    sync_listens(cache, FakeClient(listens))
    refresh_listened_artists(cache)

    stale = reports.listens_stale_library(cache, months=6)
    names = [s["name"] for s in stale["stale"]]
    assert "Stale Band" in names
    assert "Active Band" not in names
    assert stale["stale"][0]["name"] == "Stale Band"  # ranked by total listens
    assert stale["stale"][0]["total_listens"] == 10


def test_discoveries_window(cache: sqlite3.Connection):
    sync_listens(
        cache,
        FakeClient([
            _listen(NOW - 10 * DAY, "New Love", "a"),
            _listen(NOW - 400 * DAY, "Old Flame", "b"),
        ]),
    )
    refresh_listened_artists(cache)
    disc = reports.listens_new_discoveries(cache, days=90)
    names = {d["artist_name"] for d in disc["discovered"]}
    assert "New Love" in names
    assert "Old Flame" not in names
