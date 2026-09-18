"""Unit tests for the library sampling spike (uses ffmpeg-generated fixtures)."""

from __future__ import annotations

from pathlib import Path

from music_mcp.spike import library_sample as ls


def test_read_tags_extracts_mbids(sample_library: Path):
    tagged_file = next((sample_library / "Tagged Artist" / "Album (2020)").glob("*.mp3"))
    tags = ls.read_tags(tagged_file)
    assert tags["readable"] is True
    assert tags["artist_mbid"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert tags["album_mbid"] == "11111111-2222-3333-4444-555555555555"
    assert tags["artist"] == "Tagged Artist"


def test_read_tags_survives_garbage_file(sample_library: Path):
    tags = ls.read_tags(sample_library / "Broken Artist" / "bad.mp3")
    assert tags["readable"] is False
    assert "error" in tags


def test_sample_library_counts_and_coverage(sample_library: Path):
    result = ls.sample_library(sample_library, max_artists=10)
    summary = result["summary"]
    assert summary["files_seen"] == 3
    assert summary["files_readable"] == 2
    assert summary["coverage_pct"]["artist_mbid"] == 50.0  # 1 of 2 readable files tagged
    assert summary["unreadable_dirs"] == []  # garbage file is per-file, not per-dir


def test_mbid_tags_registry_has_both_naming_schemes():
    from music_mcp.spike.library_sample import MBID_TAGS  # noqa: F401  (re-import documents intent)

    for keys in MBID_TAGS.values():
        assert len(keys) == 2  # vorbis-comment style + ID3 frame style
