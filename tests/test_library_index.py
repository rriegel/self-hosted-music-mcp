"""Unit tests for the library index: db, scanner, reports.

Uses the checked-in fixture library (tests/fixtures/library) — no ffmpeg, no
network, no real library.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from mutagen.flac import FLAC

from music_mcp.library import db as db_mod
from music_mcp.library import reports, scanner

TAGGED_MBID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture()
def cache(tmp_path: Path) -> Generator[sqlite3.Connection]:
    conn = db_mod.connect(tmp_path / "test-index.db")
    yield conn
    conn.close()


@pytest.fixture()
def library_copy(tmp_path: Path, sample_library: Path) -> Path:
    """A writable copy of the fixture library — tests that mutate files use this.

    The checked-in fixture tree is shared read-only state; mutating it (unlink,
    chmod) would break every other test and race concurrent runs.
    """
    target = tmp_path / "library"
    shutil.copytree(sample_library, target)
    return target


def test_connect_enables_wal(cache: sqlite3.Connection):
    mode = cache.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    assert db_mod.current_schema_version(cache) == 2


def test_scan_indexes_fixture_library(cache: sqlite3.Connection, sample_library: Path):
    summary = scanner.scan_library(sample_library, cache)
    assert summary["files_scanned"] == 5
    assert summary["added"] == 5
    assert summary["unreadable"] == 1  # garbage bad.mp3
    assert summary["unreadable_dirs"] == []
    # 4 readable files: tagged mp3, tagged flac, mystery mp3, m4a
    assert summary["with_artist_mbid"] == 3  # tagged mp3, flac, m4a

    tracks = cache.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    assert tracks == 5
    artists = cache.execute("SELECT artist_mbid, name FROM artists").fetchall()
    by_mbid = {r["artist_mbid"]: r["name"] for r in artists}
    assert by_mbid[TAGGED_MBID] == "Tagged Artist"
    assert by_mbid["99999999-8888-7777-6666-555555555555"] == "M4A Artist"


def test_rescan_incremental_unchanged(cache: sqlite3.Connection, sample_library: Path):
    scanner.scan_library(sample_library, cache)
    again = scanner.scan_library(sample_library, cache)
    assert again["unchanged"] == 5
    assert again["added"] == 0
    assert again["updated"] == 0


def test_full_rescan_reprocesses(cache: sqlite3.Connection, sample_library: Path):
    scanner.scan_library(sample_library, cache)
    full = scanner.scan_library(sample_library, cache, incremental=False)
    assert full["updated"] == 5
    assert full["unchanged"] == 0


def test_scan_prunes_missing_files(cache: sqlite3.Connection, library_copy: Path):
    scanner.scan_library(library_copy, cache)
    (library_copy / "Mystery Artist" / "Unknown" / "01 - Track.mp3").unlink()
    summary = scanner.scan_library(library_copy, cache)
    assert summary["missing_from_disk"] == 1
    assert cache.execute("SELECT COUNT(*) FROM tracks WHERE path LIKE '%Mystery%'").fetchone()[0] == 0


def test_unreadable_dir_recorded_not_fatal(cache: sqlite3.Connection, library_copy: Path):
    # lock the dir against listing to prove tolerance (chmod is local-only, no sudo needed)
    broken = library_copy / "Broken Artist"
    broken.chmod(0o000)
    try:
        summary = scanner.scan_library(library_copy, cache)
    finally:
        broken.chmod(0o755)
    assert summary["files_scanned"] == 4  # bad.mp3 now hidden by the locked dir
    assert any("Broken Artist" in d for d in summary["unreadable_dirs"])


def test_status_reports_coverage(cache: sqlite3.Connection, sample_library: Path):
    scanner.scan_library(sample_library, cache)
    status = reports.library_status(cache)
    assert status["tracks"] == 5
    assert status["artists"] == 2
    assert status["albums"] == 4
    assert status["coverage_pct"]["artist_mbid"] == 75.0  # 3 of 4 readable
    assert status["last_scan"] is not None
    assert set(status["formats"]) == {"MP3", "FLAC", "MP4", "unreadable"}


def test_artists_and_albums_reports(cache: sqlite3.Connection, sample_library: Path):
    scanner.scan_library(sample_library, cache)
    artists = reports.library_artists(cache)
    names = {a["name"] for a in artists["artists"]}
    assert "Tagged Artist" in names and "M4A Artist" in names

    missing = reports.library_artists(cache, filter_mbid="missing")
    missing_names = {a["name"] for a in missing["artists"]}
    assert "Mystery Artist" in missing_names  # folder-derived identity, no MBID
    assert TAGGED_MBID not in {a["artist_mbid"] for a in missing["artists"]}

    albums = reports.library_albums(cache, TAGGED_MBID)
    assert albums["matched_by"] == "artist_mbid"
    assert {al["title"] for al in albums["albums"]} == {"Album (2020)"}

    by_name = reports.library_albums(cache, "M4A Artist")
    assert by_name["matched_by"] == "artist_name"
    assert by_name["count"] == 1


def test_albums_resolve_varied_artist_credits(cache: sqlite3.Connection, library_copy: Path):
    """Regression (real-library bug): file credits vary ('Artist feat. X',
    'Artist x Someone'); discography lookup must resolve via artist_mbid,
    not exact credit-string equality."""
    collab = library_copy / "Tagged Artist" / "Collab Album"
    collab.mkdir(parents=True, exist_ok=True)
    src = next((library_copy / "Tagged Artist" / "Album (2020)").glob("*.flac"))
    dst = collab / "01 - With Someone.flac"
    shutil.copy(src, dst)
    flac = FLAC(dst)
    flac["artist"] = "Tagged Artist feat. Someone"
    flac["musicbrainz_artistid"] = TAGGED_MBID
    flac["album"] = "Collab Album"  # own identity; same MBID across folders = dupe case
    flac["musicbrainz_albumid"] = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    flac.save()

    scanner.scan_library(library_copy, cache)
    result = reports.library_albums(cache, "Tagged Artist")
    titles = {al["title"] for al in result["albums"]}
    assert "Album (2020)" in titles
    assert "Collab Album" in titles  # varied credit still resolves via mbid
    assert result["count"] == 2
    assert result["artist_mbid"] == TAGGED_MBID


def test_albums_unknown_artist_reports_clearly(cache: sqlite3.Connection, sample_library: Path):
    scanner.scan_library(sample_library, cache)
    result = reports.library_albums(cache, "Nobody Known")
    assert result["count"] == 0
    assert result["matched_by"] is None
