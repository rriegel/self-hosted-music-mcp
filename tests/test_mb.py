"""Phase 3 tests: MB client cache, resolver propose/apply, dupes, quality."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
import respx
from httpx import Response

from music_mcp.library import db as db_mod
from music_mcp.library import quality, scanner
from music_mcp.mb import resolver
from music_mcp.mb.client import MBClient

TAGGED_MBID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture()
def cache(tmp_path: Path) -> Generator[sqlite3.Connection]:
    conn = db_mod.connect(tmp_path / "t.db")
    yield conn
    conn.close()


class FakeMB:
    """Test double for MBClient: canned search/lookup responses, records calls."""

    def __init__(self, results: dict[str, list[dict]]):
        self.results = results  # query name -> candidate list
        self.calls: list[str] = []

    def search_artist(self, name: str, limit: int = 5) -> dict:
        self.calls.append(name)
        return {"artists": self.results.get(name, [])}


def _mb_candidate(mbid: str, name: str, score: int) -> dict:
    return {"id": mbid, "name": name, "score": score}


@respx.mock
def test_mb_client_caches_responses(cache: sqlite3.Connection):
    route = respx.get("https://musicbrainz.org/ws/2/artist").mock(
        return_value=Response(200, json={"artists": []})
    )
    client = MBClient(cache)
    client.search_artist("Foo", limit=1)
    client.search_artist("Foo", limit=1)  # second search: every request served from cache
    # field query + plain-query fallback = 2 network hits per uncached search
    assert route.call_count == 2


@respx.mock
def test_mb_client_rate_limits():
    import time

    respx.get("https://musicbrainz.org/ws/2/artist").mock(
        return_value=Response(200, json={"artists": []})
    )
    client = MBClient(db_mod.connect(":memory:"))
    t0 = time.monotonic()
    client.search_artist("A")
    client.search_artist("B")
    # two searches x (field+fallback) requests must respect 1.1s spacing
    assert time.monotonic() - t0 >= 2.0


@respx.mock
def test_mb_client_retries_on_503():
    route = respx.get("https://musicbrainz.org/ws/2/artist").mock(
        side_effect=[Response(503), Response(200, json={"artists": []})] * 8
    )
    client = MBClient(db_mod.connect(":memory:"))
    data = client.search_artist("X")
    assert isinstance(data.get("artists"), list)
    assert route.call_count >= 2  # the 503 was retried


def test_propose_stores_candidates(cache: sqlite3.Connection, sample_library: Path):
    scanner.scan_library(sample_library, cache)
    fake = FakeMB({
        "Mystery Artist": [_mb_candidate("mmmm-1111", "Mystery Artist", 100)],
    })
    result = resolver.propose(fake, cache, max_artists=10)
    assert result["artists_considered"] >= 1
    row = cache.execute(
        "SELECT proposed_mbid, confidence, status FROM mb_resolutions WHERE folder_artist = 'Mystery Artist'"
    ).fetchone()
    assert row is not None
    assert row["proposed_mbid"] == "mmmm-1111"
    assert row["confidence"] == "exact"
    assert row["status"] == "proposed"


def test_propose_marks_no_match(cache: sqlite3.Connection, sample_library: Path):
    scanner.scan_library(sample_library, cache)
    fake = FakeMB({})  # nothing matches anything
    result = resolver.propose(fake, cache, max_artists=10)
    row = cache.execute(
        "SELECT proposed_mbid, confidence FROM mb_resolutions WHERE folder_artist = 'Mystery Artist'"
    ).fetchone()
    assert row["proposed_mbid"] is None
    assert row["confidence"] == "none"
    assert result["no_mb_match"] >= 1


def test_commit_applies_confident_and_skips_weak(cache: sqlite3.Connection, sample_library: Path):
    scanner.scan_library(sample_library, cache)
    cache.executemany(
        "INSERT INTO mb_resolutions VALUES (?,?,?,?,?,?,?)",
        [
            ("Mystery Artist", "mmmm-1111", "Mystery Artist", 100, "exact", "proposed", 0),
            ("Weak Match", "wwww-2222", "Weak Matching", 40, "fuzzy", "proposed", 0),
        ],
    )
    cache.commit()
    fake = FakeMB({})
    result = resolver.commit(fake, cache)

    assert {a["artist"] for a in result["applied"]} == {"Mystery Artist"}
    assert {s["artist"] for s in result["skipped"]} == {"Weak Match"}
    # index now carries the MBID on tracks
    track_mbid = cache.execute(
        "SELECT artist_mbid FROM tracks WHERE artist = 'Mystery Artist'"
    ).fetchone()["artist_mbid"]
    assert track_mbid == "mmmm-1111"
    # applied status recorded; weak stays proposed
    assert cache.execute(
        "SELECT status FROM mb_resolutions WHERE folder_artist = 'Mystery Artist'"
    ).fetchone()["status"] == "applied"


def test_dupes_same_release_multiple_folders(cache: sqlite3.Connection, sample_library: Path):
    scanner.scan_library(sample_library, cache)
    # simulate a copy: second folder with the same album_mbid
    cache.execute(
        "INSERT INTO albums (album_mbid, artist_mbid, title, folder, date, first_seen, last_seen) "
        "VALUES ('11111111-2222-3333-4444-555555555555', ?, 'Album (2020) (Copy)',"
        " 'Tagged Artist/Album (2020) [Copy]', NULL, 't', 't')",
        (TAGGED_MBID,),
    )
    cache.commit()
    dupes = quality.library_find_dupes(cache)
    groups = dupes["findings"].get("same_release_multiple_folders", [])
    assert any(g["album_mbid"] == "11111111-2222-3333-4444-555555555555" for g in groups)


def test_dupes_folder_multiple_releases(cache: sqlite3.Connection, sample_library: Path):
    scanner.scan_library(sample_library, cache)
    # two files in one folder carry different release identities → mislabeled folder
    cache.execute(
        "UPDATE tracks SET album_mbid = '11111111-2222-3333-4444-555555555555' WHERE path LIKE '%01 - Song.mp3'"
    )
    cache.execute(
        "UPDATE tracks SET album_mbid = '99999999-9999-9999-9999-999999999999' WHERE path LIKE '%02 - Song.flac'"
    )
    cache.commit()
    dupes = quality.library_find_dupes(cache, scope="folder")
    rows = dupes["findings"].get("folder_multiple_releases", [])
    assert any("Album (2020)" in r["folder"] and r["mbids"] == 2 for r in rows)


def test_quality_report_shape(cache: sqlite3.Connection, sample_library: Path):
    scanner.scan_library(sample_library, cache)
    q = quality.library_quality_report(cache)
    assert q["tracks"] == 5
    assert q["missing_pct"]["artist_mbid"] == 40.0  # 2 of 5 lack MBID (mystery + broken)
    assert "below_min_bitrate" in q
    assert set(q["formats"]) >= {"MP3", "FLAC", "MP4", "unreadable"}


def test_propose_is_idempotent_and_respects_decisions(cache: sqlite3.Connection, sample_library: Path):
    scanner.scan_library(sample_library, cache)
    fake = FakeMB({"Mystery Artist": [_mb_candidate("mmmm-1111", "Mystery Artist", 100)]})
    resolver.propose(fake, cache, max_artists=10)
    # user applied one
    cache.execute("UPDATE mb_resolutions SET status='applied' WHERE folder_artist='Mystery Artist'")
    cache.commit()
    # re-propose must not clobber the applied status
    resolver.propose(fake, cache, max_artists=10)
    assert cache.execute(
        "SELECT status FROM mb_resolutions WHERE folder_artist='Mystery Artist'"
    ).fetchone()["status"] == "applied"
