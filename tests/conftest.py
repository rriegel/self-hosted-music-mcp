"""Shared fixtures: tiny silent audio files with tag surgery via mutagen."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.mp3 import MP3

FFMPEG = shutil.which("ffmpeg")


def _ffmpeg_silent(target: Path) -> None:
    assert FFMPEG, "ffmpeg required for fixture generation"
    subprocess.run(
        [FFMPEG, "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "0.05", "-y", str(target)],
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def make_audio(tmp_path_factory: pytest.TempPathFactory):
    """Factory: make_audio(dir, 'file.mp3', artist_mbid=..., artist=...)."""

    def _make(directory: Path, filename: str, **tags) -> Path:
        target = directory / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        _ffmpeg_silent(target)
        audio = MP3(target) if target.suffix == ".mp3" else FLAC(target)
        for key, value in tags.items():
            if value is None:
                audio.pop(key, None)
            else:
                audio[key] = str(value)
        audio.save()
        return target

    return _make


@pytest.fixture(scope="session")
def sample_library(make_audio, tmp_path_factory: pytest.TempPathFactory):
    """A tiny fake library: one MBID-tagged artist, one untagged artist, one unreadable file."""
    root = tmp_path_factory.mktemp("library")
    tagged = root / "Tagged Artist" / "Album (2020)"
    make_audio(tagged, "01 - Song.mp3", artist="Tagged Artist", album="Album (2020)",
               musicbrainz_artistid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
               musicbrainz_albumid="11111111-2222-3333-4444-555555555555")
    make_audio(tagged, "02 - Song.flac", artist="Tagged Artist", album="Album (2020)",
               musicbrainz_artistid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    untagged = root / "Mystery Artist" / "Unknown"
    make_audio(untagged, "01 - Track.mp3")  # no tags at all
    (root / "Broken Artist").mkdir()
    (root / "Broken Artist" / "bad.mp3").write_bytes(b"not audio at all")
    return root
