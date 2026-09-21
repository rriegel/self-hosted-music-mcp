"""Phase 4a tests: watchlist import/manage/export + recommendations joins."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest

from music_mcp.discovery import recommend
from music_mcp.discovery import watchlist as wl
from music_mcp.library import db as db_mod


@pytest.fixture()
def cache(tmp_path: Path) -> Generator[sqlite3.Connection]:
    conn = db_mod.connect(tmp_path / "t.db")
    yield conn
    conn.close()


def _radar(tmp_path: Path) -> Path:
    payload = {
        "created": "2026-08-03T17:56:10",
        "total_artists": 3,
        "artists": [
            {"mbid": "aaa-1", "name": "Owned Band", "sources": ["library"], "listen_count": 50},
            {"mbid": "aaa-2", "name": "Listened Only", "sources": ["listenbrainz"], "listen_count": 20},
            {"mbid": None, "name": "No MBID", "sources": ["similar"], "listen_count": 1},
            {"mbid": "aaa-3", "name": "Similar Pick", "sources": ["similar"], "listen_count": 0},
        ],
    }
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps(payload))
    return path


def _seed_index(conn: sqlite3.Connection):
    conn.execute(
        "INSERT OR IGNORE INTO artists (artist_mbid, name, first_seen, last_seen) "
        "VALUES ('owned-1', 'Owned Band', 't', 't')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO tracks (path, parent_folder, filename, suffix, artist, artist_mbid, "
        "readable, mtime, size, first_seen, last_seen) VALUES "
        "('/o/x.mp3','o','x.mp3','.mp3','Owned Band','owned-1',1,0,0,'t','t')"
    )
    # listened artists: Owned Band (mapped), Mystery Collab (no MBID)
    conn.execute("INSERT OR IGNORE INTO listened_artists VALUES ('Owned Band', 100, 0, 0)")
    conn.execute("INSERT OR IGNORE INTO listened_artists VALUES ('Mystery Collab', 60, 0, 0)")
    conn.commit()


def test_import_skips_mbid_less_and_merges(cache: sqlite3.Connection, tmp_path: Path):
    result = wl.import_radar_json(cache, _radar(tmp_path))
    assert result["total_in_watchlist"] == 3  # MBID-less entry skipped
    names = {r["name"] for r in cache.execute("SELECT name FROM watchlist")}
    assert "No MBID" not in names
    # re-import with bumped listen_count → MAX wins
    payload = json.loads((_radar(tmp_path)).read_text())
    payload["artists"][0]["listen_count"] = 99
    (tmp_path / "watchlist2.json").write_text(json.dumps(payload))
    wl.import_radar_json(cache, tmp_path / "watchlist2.json")
    lc = cache.execute("SELECT listen_count FROM watchlist WHERE artist_mbid='aaa-1'").fetchone()[0]
    assert lc == 99


def test_add_merge_and_remove(cache: sqlite3.Connection):
    first = wl.add_artists(cache, [{"mbid": "x-1", "name": "X", "sources": ["manual"], "listen_count": 5}])
    assert first["added"] == 1
    second = wl.add_artists(
        cache, [{"mbid": "x-1", "name": "X", "sources": ["similar"], "listen_count": 3}]
    )
    assert second["merged"] == 1
    row = cache.execute("SELECT sources, listen_count FROM watchlist WHERE artist_mbid='x-1'").fetchone()
    assert json.loads(row["sources"]) == ["manual", "similar"]
    assert row["listen_count"] == 5  # max kept
    assert wl.remove_artists(cache, ["x-1"])["removed"] == 1


def test_export_radar_shape(cache: sqlite3.Connection, tmp_path: Path):
    wl.add_artists(cache, [{"mbid": "x-1", "name": "X", "sources": ["manual"], "listen_count": 2}])
    out = tmp_path / "export.json"
    result = wl.export_radar_format(cache, out)
    data = json.loads(out.read_text())
    assert result["exported"] == 1
    assert data["total_artists"] == 1
    assert data["artists"][0]["mbid"] == "x-1"
    assert set(data["artists"][0]) == {"mbid", "name", "sources", "listen_count"}


def test_similar_excludes_owned(cache: sqlite3.Connection, monkeypatch):
    _seed_index(cache)
    fake_rows = [
        {"artist_mbid": "s-1", "name": "Owned Band", "score": 100},  # owned → excluded
        {"artist_mbid": "s-2", "name": "New Neighbor", "score": 80},
    ]
    monkeypatch.setattr(recommend, "similar_artists", lambda mbid, limit=25: fake_rows)
    result = recommend.discovery_similar_artists(cache, "seed-1", exclude="owned")
    names = [s["name"] for s in result["similar"]]
    assert names == ["New Neighbor"]
    assert result["exclude"] == "owned artists"


def test_recommendations_not_in_library(cache: sqlite3.Connection, monkeypatch):
    _seed_index(cache)
    cache.execute("INSERT OR IGNORE INTO artists (artist_mbid, name, first_seen, last_seen) "
                  "VALUES ('owned-1', 'Owned Band', 't', 't')")

    def fake_similar(mbid, limit=25):
        if mbid == "owned-1":
            return [
                {"artist_mbid": "n-1", "name": "Fresh Find", "score": 90},
                {"artist_mbid": "owned-1", "name": "Owned Band", "score": 99},  # self → skipped
            ]
        return []

    monkeypatch.setattr(recommend, "similar_artists", fake_similar)
    result = recommend.discovery_recommendations(
        cache, filter_mode="not_in_library", min_seed_listens=5, seed_limit=10
    )
    assert result["seeds_used"] == 1
    names = [r["name"] for r in result["recommendations"]]
    assert "Fresh Find" in names
    assert all(r["owned"] is False for r in result["recommendations"])
    assert result["recommendations"][0]["affinity"] >= 90
    assert result["recommendations"][0]["recommended_via"] == ["Owned Band"]


def test_recommendations_in_library_unplayed(cache: sqlite3.Connection, monkeypatch):
    _seed_index(cache)  # Owned Band owned AND listened; Ghost Owned owned, NOT listened
    cache.execute(
        "INSERT OR IGNORE INTO artists (artist_mbid, name, first_seen, last_seen) "
        "VALUES ('ghost-1', 'Ghost Owned', 't', 't')"
    )
    conn_rows = [
        {"artist_mbid": "owned-1", "name": "Owned Band", "score": 100},
        {"artist_mbid": "ghost-1", "name": "Ghost Owned", "score": 90},
    ]
    monkeypatch.setattr(recommend, "similar_artists", lambda mbid, limit=25: conn_rows)
    result = recommend.discovery_recommendations(
        cache, filter_mode="in_library_unplayed", seed_mbids=["some-seed"]
    )
    names = [r["name"] for r in result["recommendations"]]
    assert "Ghost Owned" in names          # owned but zero listens
    assert "Owned Band" not in names       # owned AND listened → excluded


def test_recommendations_seeds_need_mbids(cache: sqlite3.Connection):
    _seed_index(cache)
    # Mystery Collab listened a lot but has no MBID → cannot seed the graph
    result = recommend.discovery_recommendations(
        cache, filter_mode="all", min_seed_listens=5, seed_limit=10
    )
    assert result["seeds_used"] == 1  # only Owned Band (has MBID)
    assert "Mystery Collab" not in result["seed_names"]
