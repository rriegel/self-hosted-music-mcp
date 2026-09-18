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
import random
import sys
from collections import Counter
from pathlib import Path

from mutagen import File as mutagen_file

AUDIO_SUFFIXES = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wma", ".wav"}

# Vorbis comments (FLAC/Ogg) and ID3 (MP3) carry the same MBIDs under different keys.
MBID_TAGS = {
    "artist_mbid": ("musicbrainz_artistid", "MusicBrainz Artist Id"),
    "album_mbid": ("musicbrainz_albumid", "MusicBrainz Album Id"),
    "recording_mbid": ("musicbrainz_trackid", "MusicBrainz Track Id"),
    "release_track_mbid": ("musicbrainz_releasetrackid", "MusicBrainz Release Track Id"),
}
BASIC_TAGS = {
    "artist": ("artist", "TPE1"),
    "album": ("album", "TALB"),
    "date": ("date", "TDRC"),
}


def _tag(frame: object, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = frame.get(key) if hasattr(frame, "get") else None
        if value:
            first = value[0] if isinstance(value, list) else value
            if first:
                return str(first)
    return None


def read_tags(path: Path) -> dict:
    """Read the tags we care about from one audio file. Never raises."""
    info: dict = {"path": str(path), "suffix": path.suffix.lower(), "readable": False}
    try:
        audio = mutagen_file(path, easy=True)
    except Exception as exc:  # noqa: BLE001 - spike: any read error is data
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info
    if audio is None:
        info["error"] = "unrecognized format"
        return info
    info["readable"] = True
    tags = audio.tags or {}
    for field, keys in MBID_TAGS.items():
        info[field] = _tag(tags, keys)
    for field, keys in BASIC_TAGS.items():
        info[field] = _tag(tags, keys)
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
    parser.add_argument("sample_dir", nargs="?", default="/beelink/mnt/terra-6tb-1/media/music")
    parser.add_argument("--max-artists", type=int, default=40)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--out", type=Path, default=Path("spike_library_sample.json"))
    args = parser.parse_args()

    root = Path(args.sample_dir)
    if not root.is_dir():
        print(json.dumps({"error": f"not a directory: {root}"}))
        return 1
    result = sample_library(root, args.max_artists, seed=args.seed)
    args.out.write_text(json.dumps(result, indent=1))
    print(json.dumps(result["summary"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
