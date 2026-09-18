"""Shared fixtures: tiny silent audio files with tag surgery via mutagen.

MP3s get real ID3 frames (TPE1/TALB/TDRC + TXXX for MBIDs — Picard's layout);
FLACs get Vorbis comments; M4As get © atoms + iTunes freeform atoms for MBIDs.
Matches what the reader must handle in the wild.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TALB, TPE1, TXXX
from mutagen.mp4 import MP4

FFMPEG = shutil.which("ffmpeg")

# fixture kwarg name -> (vorbis key, id3 frame factory)
TAG_SPECS = {
    "artist": ("artist", lambda v: TPE1(encoding=3, text=v)),
    "album": ("album", lambda v: TALB(encoding=3, text=v)),
    "artist_mbid": ("musicbrainz_artistid", lambda v: TXXX(encoding=3, desc="MusicBrainz Artist Id", text=v)),
    "album_mbid": ("musicbrainz_albumid", lambda v: TXXX(encoding=3, desc="MusicBrainz Album Id", text=v)),
    "recording_mbid": ("musicbrainz_trackid", lambda v: TXXX(encoding=3, desc="MusicBrainz Track Id", text=v)),
}

# fixture kwarg name -> MP4 atom key (© atoms for basics; freeform for MBIDs)
MP4_TAG_SPECS = {
    "artist": "\xa9ART",
    "album": "\xa9alb",
    "album_mbid": "----:com.apple.iTunes:MusicBrainz Album Id",
    "recording_mbid": "----:com.apple.iTunes:MusicBrainz Track Id",
}


def _ffmpeg_silent(target: Path, with_m4a_mbids: bool = False) -> None:
    assert FFMPEG, "ffmpeg required for fixture generation"
    args = [FFMPEG, "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "0.05", "-y"]
    if with_m4a_mbids:
        # metadata writes freeform atoms usable as MP4 MBID fixtures
        args += [
            "-metadata", "artist=M4A Artist",
            "-metadata", "musicbrainz_artistid=99999999-8888-7777-6666-555555555555",
        ]
    args.append(str(target))
    subprocess.run(args, check=True, capture_output=True)


@pytest.fixture(scope="session")
def make_audio():
    """Factory: make_audio(dir, 'file.mp3', artist=..., artist_mbid=...)."""

    def _make(directory: Path, filename: str, **tags) -> Path:
        target = directory / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        _ffmpeg_silent(target, with_m4a_mbids=target.suffix == ".m4a")
        if target.suffix == ".mp3":
            id3 = ID3()
            for name, (_, make_frame) in TAG_SPECS.items():
                if tags.get(name) is not None:
                    id3.add(make_frame(str(tags[name])))
            id3.save(target)
        elif target.suffix == ".m4a":
            mp4 = MP4(target)
            for name, mp4_key in MP4_TAG_SPECS.items():
                if tags.get(name) is not None:
                    value = str(tags[name])
                    # © text atoms hold str lists; freeform '----' atoms hold bytes lists
                    mp4[mp4_key] = [value.encode()] if mp4_key.startswith("----") else [value]
            if tags.get("artist_mbid") is not None:
                # Picard's freeform atom layout — matches real-library files
                mp4["----:com.apple.iTunes:MusicBrainz Artist Id"] = [str(tags["artist_mbid"]).encode()]
            mp4.save()
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
    m4a_dir = root / "M4A Artist" / "Album"
    make_audio(m4a_dir, "01 - Track.m4a", artist="M4A Artist",
               artist_mbid="99999999-8888-7777-6666-555555555555")
    (root / "Broken Artist").mkdir()
    (root / "Broken Artist" / "bad.mp3").write_bytes(b"not audio at all")
    return root
