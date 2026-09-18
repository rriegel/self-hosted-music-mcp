"""Unit tests for join_e2e loaders (no network, no real library)."""

from __future__ import annotations

import json
from pathlib import Path

from music_mcp.spike.join_e2e import load_owned_artists, load_watchlist


def _write(tmp_path: Path, name: str, payload) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return path


def test_load_owned_artists_groups_by_mbid(tmp_path: Path):
    sample = _write(tmp_path, "sample.json", {
        "files": [
            {"readable": True, "artist_mbid": "aaa-1", "artist": "Artist One"},
            {"readable": True, "artist_mbid": "aaa-1", "artist": "Artist One"},
            {"readable": True, "artist_mbid": None, "artist": "Mystery"},
            {"readable": False, "error": "bad file"},
        ]
    })
    owned = load_owned_artists(sample)
    assert owned["by_mbid"]["aaa-1"] == {"names": ["Artist One"], "files": 2}
    assert owned["untagged_by_name"] == {"Mystery": 1}


def test_load_watchlist_requires_mbid(tmp_path: Path):
    wl = _write(tmp_path, "watchlist.json", {
        "artists": [
            {"mbid": "aaa-1", "name": "Artist One", "listen_count": 10, "sources": ["library"]},
            {"mbid": None, "name": "No ID"},  # skipped: names are never join keys
        ]
    })
    parsed = load_watchlist(wl)
    assert list(parsed) == ["aaa-1"]
    assert parsed["aaa-1"]["listen_count"] == 10
