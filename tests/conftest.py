"""Shared fixtures.

The unit-test library is CHECKED IN under tests/fixtures/library (tiny silent files,
generated once with ffmpeg) so tests run anywhere without ffmpeg installed:
- Tagged Artist: MP3 with ID3 TXXX MBIDs (Picard layout) + FLAC with Vorbis comments
- M4A Artist: MP4 with (c) atoms + iTunes freeform MBID atom
- Mystery Artist: MP3 with no tags
- Broken Artist: garbage bytes with an .mp3 suffix

make_audio() keeps mutagen tag surgery available for tmp_path-based tests (Phase 1+),
but skips cleanly when ffmpeg is not installed — CI never needs it.
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
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "library"

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


@pytest.fixture(scope="session")
def sample_library() -> Path:
    """Read-only reference to the committed tiny library."""
    assert FIXTURES_DIR.is_dir(), f"missing checked-in fixtures: {FIXTURES_DIR}"
    return FIXTURES_DIR


@pytest.fixture()
def library_copy(tmp_path: Path, sample_library: Path) -> Path:
    """A writable copy of the fixture library — tests that mutate files use this.

    The checked-in fixture tree is shared read-only state; mutating it (unlink,
    chmod) would break every other test and race concurrent runs.
    """
    target = tmp_path / "library"
    shutil.copytree(sample_library, target)
    return target


@pytest.fixture(scope="session")
def make_audio():
    """Factory for NEW silent audio files with tag surgery: make_audio(dir, 'x.mp3', artist=...).

    Requires ffmpeg on PATH; tests using it are skipped when unavailable.
    """

    def _ffmpeg_silent(target: Path) -> None:
        if FFMPEG is None:
            pytest.skip("ffmpeg not installed — fixture generation unavailable")
        subprocess.run(
            [FFMPEG, "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "0.05", "-y", str(target)],
            check=True,
            capture_output=True,
        )

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
