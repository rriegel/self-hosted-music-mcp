"""Shared fixtures: tiny silent audio files with tag surgery via mutagen.

MP3s get real ID3 frames (TPE1/TALB/TDRC + TXXX for MBIDs — Picard's layout);
FLACs get Vorbis comments. Matches what the reader must handle in the wild.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TALB, TPE1, TXXX

FFMPEG = shutil.which("ffmpeg")

# fixture kwarg name -> (vorbis key, id3 frame factory)
TAG_SPECS = {
    "artist": ("artist", lambda v: TPE1(encoding=3, text=v)),
    "album": ("album", lambda v: TALB(encoding=3, text=v)),
    "artist_mbid": ("musicbrainz_artistid", lambda v: TXXX(encoding=3, desc="MusicBrainz Artist Id", text=v)),
    "album_mbid": ("musicbrainz_albumid", lambda v: TXXX(encoding=3, desc="MusicBrainz Album Id", text=v)),
    "recording_mbid": ("musicbrainz_trackid", lambda v: TXXX(encoding=3, desc="MusicBrainz Track Id", text=v)),
}


def _ffmpeg_silent(target: Path) -> None:
    assert FFMPEG, "ffmpeg required for fixture generation"
    subprocess.run(
        [FFMPEG, "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "0.05", "-y", str(target)],
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def make_audio():
    """Factory: make_audio(dir, 'file.mp3', artist=..., artist_mbid=...)."""

    def _make(directory: Path, filename: str, **tags) -> Path:
        target = directory / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        _ffmpeg_silent(target)
        if target.suffix == ".mp3":
            id3 = ID3()
            for name, (_, make_frame) in TAG_SPECS.items():
                if tags.get(name) is not None:
                    id3.add(make_frame(str(tags[name])))
            id3.save(target)
        else:
            flac = FLAC(target)
            for name, (vorbis_key, _) in TAG_SPECS.items():
                if tags.get(name) is not None:
                    flac[vorbis_key] = str(tags[name])
            flac.save()
        return target

    return _make


@pytest.fixture(scope="session")
def sample_library(make_audio, tmp_path_factory: pytest.TempPathFactory):
    """A tiny fake library: one MBID-tagged artist, one untagged artist, one unreadable file."""
    root = tmp_path_factory.mktemp("library")
    tagged = root / "Tagged Artist" / "Album (2020)"
    make_audio(tagged, "01 - Song.mp3", artist="Tagged Artist", album="Album (2020)",
               artist_mbid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
               album_mbid="11111111-2222-3333-4444-555555555555")
    make_audio(tagged, "02 - Song.flac", artist="Tagged Artist", album="Album (2020)",
               artist_mbid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    untagged = root / "Mystery Artist" / "Unknown"
    make_audio(untagged, "01 - Track.mp3")  # no tags at all
    (root / "Broken Artist").mkdir()
    (root / "Broken Artist" / "bad.mp3").write_bytes(b"not audio at all")
    return root
