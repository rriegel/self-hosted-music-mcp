"""Spike: sample the Terra library with mutagen, measure real MBID coverage.

Read-only. Walks a sample of artist directories, reads tags, reports:
- MBID coverage % (musicbrainz_artistid / musicbrainz_albumid / musicbrainz_releasetrackid)
- basic tag presence (artist/album/date)
- format distribution

Usage: uv run python -m music_mcp.spike.library_sample [SAMPLE_DIR] [--max-artists N]
Writes findings JSON to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter
from pathlib import Path

from mutagen import File as mutagen_file

AUDIO_SUFFIXES = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wma", ".wav"}

# Tag keys per tag system. Vorbis comments (FLAC/Ogg) use lowercase names; ID3 (MP3)
# uses frame IDs, with MBIDs in TXXX frames keyed by description (Picard's convention).
# MP4/M4A uses © atoms, MBIDs in '----:com.apple.iTunes:' freeform atoms. easy=True
# would HIDE TXXX frames, so we read raw tags and dispatch on their class.
MBID_TAGS = {
    "artist_mbid": {
        "vorbis": "musicbrainz_artistid",
        "id3": "TXXX:MusicBrainz Artist Id",
        "mp4": "----:com.apple.iTunes:MusicBrainz Artist Id",
    },
    "album_mbid": {
        "vorbis": "musicbrainz_albumid",
        "id3": "TXXX:MusicBrainz Album Id",
        "mp4": "----:com.apple.iTunes:MusicBrainz Album Id",
    },
    "recording_mbid": {
        "vorbis": "musicbrainz_trackid",
        "id3": "TXXX:MusicBrainz Track Id",
        "mp4": "----:com.apple.iTunes:MusicBrainz Track Id",
    },
    "release_track_mbid": {
        "vorbis": "musicbrainz_releasetrackid",
        "id3": "TXXX:MusicBrainz Release Track Id",
        "mp4": "----:com.apple.iTunes:MusicBrainz Release Track Id",
    },
}
BASIC_TAGS = {
    "artist": {"vorbis": "artist", "id3": "TPE1", "mp4": "\xa9ART"},
    "album": {"vorbis": "album", "id3": "TALB", "mp4": "\xa9alb"},
    "date": {"vorbis": "date", "id3": "TDRC", "mp4": "\xa9day"},
}


def _vorbis_get(tags: object, key: str) -> str | None:
    """Vorbis-comment style lookup (FLAC/Ogg: dict-like, values are lists)."""
    getter = getattr(tags, "get", None)
    value = getter(key) if getter else None
    if value:
        first = value[0] if isinstance(value, list) else value
        if first:
            return str(first)
    return None


def _first_str(value: object) -> str | None:
    """Coerce a tag value (str, list, or bytes — MP4 atoms may hold bytes) to str."""
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value) if value else None


def _id3_get(tags: object, key: str) -> str | None:
    """ID3 lookup: plain frames (TPE1) or TXXX frames addressed as 'TXXX:<desc>'."""
    frame = getattr(tags, "get", lambda _k, default=None: default)(key)
    if frame is None:
        return None
    text = getattr(frame, "text", None)
    if text:
        return str(text[0])
    return _first_str(frame)


def _mp4_get(tags: object, key: str) -> str | None:
    """MP4 lookup: freeform '----:com.apple.iTunes:*' atoms hold lists of bytes."""
    value = getattr(tags, "get", lambda _k, default=None: default)(key)
    return _first_str(value)


def read_tags(path: Path) -> dict:
    """Read the tags we care about from one audio file. Never raises."""
    info: dict = {"path": str(path), "suffix": path.suffix.lower(), "readable": False}
    try:
        audio = mutagen_file(path)
    except Exception as exc:  # noqa: BLE001 - spike: any read error is data
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info
    if audio is None:
        info["error"] = "unrecognized format"
        return info
    info["readable"] = True
    tags = audio.tags
    if tags is None:
        info["error"] = "no tags"
        tags = {}
    system = type(tags).__name__
    key_style = {"ID3": "id3", "MP4Tags": "mp4"}.get(system, "vorbis")
    for field, keys in {**MBID_TAGS, **BASIC_TAGS}.items():
        key = keys[key_style]
        if key_style == "id3":
            info[field] = _id3_get(tags, key)
        elif key_style == "mp4":
            info[field] = _mp4_get(tags, key)
        else:
            info[field] = _vorbis_get(tags, key)
    info["bitrate"] = getattr(audio.info, "bitrate", None)
    info["format"] = type(audio).__name__
    return info


def sample_library(root: Path, max_artists: int, seed: int = 17) -> dict:
    artist_dirs = sorted(d for d in root.iterdir() if d.is_dir())
    rng = random.Random(seed)
    sampled = artist_dirs if len(artist_dirs) <= max_artists else rng.sample(artist_dirs, max_artists)

    files: list[dict] = []
    unreadable_dirs: list[str] = []
    for artist_dir in sorted(sampled):
        try:
            found = sorted(p for p in artist_dir.rglob("*") if p.suffix.lower() in AUDIO_SUFFIXES)
        except OSError as exc:
            unreadable_dirs.append(f"{artist_dir.name}: {exc}")
            continue
        for path in found:
            files.append(read_tags(path))

    total = len(files)
    readable = [f for f in files if f["readable"]]

    def coverage(field: str) -> float:
        return round(100 * sum(1 for f in readable if f.get(field)) / len(readable), 1) if readable else 0.0

    summary = {
        "root": str(root),
        "artist_dirs_in_root": len(artist_dirs),
        "artist_dirs_sampled": len(sampled),
        "files_seen": total,
        "files_readable": len(readable),
        "unreadable_dirs": unreadable_dirs,
        "coverage_pct": {field: coverage(field) for field in MBID_TAGS},
        "basic_tag_pct": {field: coverage(field) for field in BASIC_TAGS},
        "formats": dict(Counter(f["format"] for f in readable)),
    }
    return {"summary": summary, "files": files}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "sample_dir", nargs="?", default=None, help="library root (default: $MUSIC_LIBRARY_ROOT)"
    )
    parser.add_argument("--max-artists", type=int, default=40)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--out", type=Path, default=None, help="also write full JSON to this path")
    args = parser.parse_args()

    root_specified = args.sample_dir or os.environ.get("MUSIC_LIBRARY_ROOT")
    if not root_specified:
        print(json.dumps({
            "error": "no library root given",
            "hint": 'pass it as an argument, or: export MUSIC_LIBRARY_ROOT="/mnt/terra-6tb-1/media/music"',
        }))
        return 1
    root = Path(root_specified)
    if not root.is_dir():
        print(json.dumps({
            "error": f"not a directory: {root}",
            "hint": "is the NAS mounted? mount points differ per machine (host vs container)",
        }))
        return 1
    result = sample_library(root, args.max_artists, seed=args.seed)
    if args.out:
        args.out.write_text(json.dumps(result, indent=1))
    print(json.dumps(result["summary"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
