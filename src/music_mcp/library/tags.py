"""Tag reading across ID3 (MP3), Vorbis (FLAC/Ogg), and MP4 (M4A) tag systems.

Canonical implementation for the library index. The spike modules re-use it.

Key facts this module encodes (learned in Phase 0):
- mutagen's easy=True mode HIDES ID3 TXXX frames (how Picard stores MBIDs), so we
  read raw tags and dispatch on the tags class.
- MP3 MBIDs live in TXXX frames addressed as "TXXX:<description>".
- M4A MBIDs live in "----:com.apple.iTunes:<description>" freeform atoms (byte lists);
  (c) text atoms hold str lists.
- FLAC/Ogg use lowercase vorbis comment keys with list values.
"""

from __future__ import annotations

from pathlib import Path

from mutagen import File as mutagen_file

AUDIO_SUFFIXES = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wma", ".wav"}

# Per-field tag keys across the three tag systems we must read.
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

_TAG_STYLE_BY_CLASS = {"ID3": "id3", "MP4Tags": "mp4"}


def _first_str(value: object) -> str | None:
    """Coerce a tag value (str, list, or bytes — MP4 atoms may hold bytes) to str."""
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value) if value else None


def _dict_like_get(tags: object, key: str) -> str | None:
    """Lookup on dict-like tags (Vorbis comments, MP4 atoms)."""
    value = getattr(tags, "get", lambda _k, default=None: default)(key)
    return _first_str(value)


def _id3_get(tags: object, key: str) -> str | None:
    """ID3 lookup: plain frames (TPE1) or TXXX frames addressed as 'TXXX:<desc>'."""
    frame = getattr(tags, "get", lambda _k, default=None: default)(key)
    if frame is None:
        return None
    text = getattr(frame, "text", None)
    if text:
        return str(text[0])
    return _first_str(frame)


def read_tags(path: Path) -> dict:
    """Read the tags we care about from one audio file. Never raises."""
    info: dict = {"path": str(path), "suffix": path.suffix.lower(), "readable": False}
    try:
        audio = mutagen_file(path)
    except Exception as exc:  # noqa: BLE001 - any read error is data, not a crash
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info
    if audio is None:
        info["error"] = "unrecognized format"
        return info
    tags = audio.tags
    if tags is None:
        info["readable"] = True
        info["error"] = "no tags"
    else:
        info["readable"] = True
        style = _TAG_STYLE_BY_CLASS.get(type(tags).__name__, "vorbis")
        for field, keys in {**MBID_TAGS, **BASIC_TAGS}.items():
            key = keys[style]
            info[field] = _id3_get(tags, key) if style == "id3" else _dict_like_get(tags, key)
    info["bitrate"] = getattr(getattr(audio, "info", None), "bitrate", None)
    info["format"] = type(audio).__name__
    return info
