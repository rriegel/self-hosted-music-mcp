"""Incremental, resumable library scanner.

Design (per planning doc):
- Incremental: unchanged files (same mtime+size) are skipped, so re-scans after the
  first are fast even over the network mount.
- Resumable/tolerant: one unreadable directory or file never aborts the scan; unreadable
  directories are recorded and scanning continues.
- Batched: results commit in batches so a dropout mid-scan loses nothing already scanned.
- Read-only toward the library; writes go only to the SQLite cache.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from music_mcp.library.tags import AUDIO_SUFFIXES, read_tags

BATCH_SIZE = 500


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def album_folder(path: Path, root: Path) -> str:
    """Relative album directory for a track path ('Artist/Album') — the album join key."""
    try:
        return str(path.parent.relative_to(root))
    except ValueError:  # path not under root (defensive)
        return str(path.parent)


def folder_identity(folder: str) -> tuple[str, str]:
    """(artist, album) derived from the folder layout — fallback identity for
    untagged files. Collections are overwhelmingly 'Artist/Album' on disk."""
    parts = [p for p in Path(folder).parts if p not in (".", "/")]
    if not parts:
        return "", ""
    artist = parts[0]
    album = parts[-1] if len(parts) > 1 else parts[0]
    return artist, album


def scan_library(root: Path, conn, incremental: bool = True) -> dict:
    """Walk root, upsert tags into the cache. Never raises on bad dirs/files.

    Returns a summary dict: files_scanned, added, updated, unchanged, unreadable,
    with_artist_mbid, unreadable_dirs, missing_from_disk, duration_ms.
    """
    started = time.monotonic()
    now = _now()
    seen_paths: set[str] = set()
    stats: Counter = Counter()
    unreadable_dirs: list[str] = []
    batch: list[tuple] = []

    for dirpath, _dirnames, filenames in os.walk(root, onerror=lambda e: unreadable_dirs.append(str(e))):
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if path.suffix.lower() not in AUDIO_SUFFIXES:
                continue
            seen_paths.add(str(path))
            try:
                st = path.stat()
            except OSError as exc:
                unreadable_dirs.append(f"{path}: {exc}")
                continue

            existing = conn.execute(
                "SELECT mtime, size FROM tracks WHERE path = ?", (str(path),)
            ).fetchone()
            if incremental and existing and existing["mtime"] == st.st_mtime and existing["size"] == st.st_size:
                stats["unchanged"] += 1
                continue

            info = read_tags(path)
            batch.append(_track_row(path, root, st, info, now))
            if existing:
                stats["updated"] += 1
            else:
                stats["added"] += 1
            if info.get("readable"):
                if info.get("artist_mbid"):
                    stats["with_artist_mbid"] += 1
            else:
                stats["unreadable"] += 1
            if len(batch) >= BATCH_SIZE:
                _commit_batch(conn, batch, now)
                batch.clear()

    if batch:
        _commit_batch(conn, batch, now)

    # Temp table of paths seen this scan: used for last_seen touch + stale-row prune.
    # (NOT chunked NOT IN for the prune — that would delete rows absent from the
    # *current chunk*.)
    conn.execute("CREATE TEMP TABLE seen_paths (path TEXT PRIMARY KEY)")
    conn.executemany(
        "INSERT OR IGNORE INTO seen_paths (path) VALUES (?)", [(p,) for p in sorted(seen_paths)]
    )

    # Touch last_seen for unchanged rows (rows scanned this run already have last_seen=now)
    if stats["unchanged"]:
        conn.execute(
            "UPDATE tracks SET last_seen = ? WHERE last_seen < ? AND path IN (SELECT path FROM seen_paths)",
            (now, now),
        )

    # Prune rows whose files vanished (library truth = disk)
    cur = conn.execute("DELETE FROM tracks WHERE path NOT IN (SELECT path FROM seen_paths)")
    removed = cur.rowcount
    conn.execute("DROP TABLE seen_paths")
    conn.commit()

    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('last_scan', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (now,),
    )
    _refresh_canonical_names(conn)
    conn.commit()

    return {
        "root": str(root),
        "files_scanned": len(seen_paths),
        "added": stats["added"],
        "updated": stats["updated"],
        "unchanged": stats["unchanged"],
        "unreadable": stats["unreadable"],
        "with_artist_mbid": stats["with_artist_mbid"],
        "unreadable_dirs": unreadable_dirs,
        "missing_from_disk": removed,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def _refresh_canonical_names(conn) -> None:
    """Canonical display names for artists, recomputed after each scan.

    File credits vary ('Drake', 'Drake feat. X', 'Drake & Y'); the stored name was
    whichever credit the first scanned file happened to carry, so name lookups
    ('albums \"Drake\"') missed. Canonical = shortest credit among the artist's own
    tracks (bare name beats credit variants); most frequent wins ties. Computed
    from the cache, so it repairs an existing index on the next scan — even an
    incremental one that reads no new files.
    """
    conn.execute(
        """
        UPDATE artists SET name = (
            SELECT t.artist FROM tracks t
            WHERE t.artist_mbid = artists.artist_mbid AND t.artist IS NOT NULL
            GROUP BY t.artist
            ORDER BY LENGTH(t.artist) ASC, COUNT(*) DESC
            LIMIT 1
        )
        WHERE artist_mbid IS NOT NULL
          AND EXISTS (SELECT 1 FROM tracks t WHERE t.artist_mbid = artists.artist_mbid
                      AND t.artist IS NOT NULL)
        """
    )


def _track_row(path: Path, root: Path, st: os.stat_result, info: dict, now: str) -> tuple:
    folder = album_folder(path, root)
    fallback_artist, fallback_album = folder_identity(folder)
    return (
        str(path),                          # 0 path
        folder,                             # 1 parent_folder
        path.name,                          # 2 filename
        path.suffix.lower(),                # 3 suffix
        info.get("format"),                 # 4 format
        info.get("bitrate"),                # 5 bitrate
        info.get("artist_mbid"),            # 6 artist_mbid
        info.get("album_mbid"),             # 7 album_mbid
        info.get("release_track_mbid"),     # 8 release_track_mbid
        info.get("recording_mbid"),         # 9 recording_mbid
        info.get("artist") or fallback_artist,      # 10 artist
        info.get("album") or fallback_album,        # 11 album
        info.get("date"),                   # 12 date
        1 if info.get("readable") else 0,   # 13 readable
        info.get("error"),                  # 14 error
        st.st_mtime,                        # 15 mtime
        st.st_size,                         # 16 size
        now,                                # 17 first_seen
        now,                                # 18 last_seen
    )


def _commit_batch(conn, batch: list[tuple], now: str) -> None:
    """Upsert a batch: artists first, then albums, then tracks (FK ordering)."""
    # Artists: distinct artist_mbids in this batch (name = first non-null seen)
    artists: dict[str, str | None] = {}
    albums: dict[str, tuple] = {}
    for row in batch:
        mbid = row[6]
        if mbid and mbid not in artists:
            artists[mbid] = row[10]
        folder = row[1]
        if folder not in albums:
            albums[folder] = (row[7], row[6], row[11], row[12])  # album_mbid, artist_mbid, title, date

    if artists:
        conn.executemany(
            """
            INSERT INTO artists (artist_mbid, name, first_seen, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(artist_mbid) DO UPDATE SET
                name = COALESCE(artists.name, excluded.name),
                last_seen = excluded.last_seen
            """,
            [(mbid, name, now, now) for mbid, name in artists.items()],
        )

    if albums:
        conn.executemany(
            """
            INSERT INTO albums (album_mbid, artist_mbid, title, folder, date, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(folder) DO UPDATE SET
                album_mbid = COALESCE(albums.album_mbid, excluded.album_mbid),
                artist_mbid = COALESCE(albums.artist_mbid, excluded.artist_mbid),
                title = COALESCE(albums.title, excluded.title),
                date = COALESCE(albums.date, excluded.date),
                last_seen = excluded.last_seen
            """,
            [
                (mbid, artist, title, folder, date, now, now)
                for folder, (mbid, artist, title, date) in albums.items()
            ],
        )

    conn.executemany(
        """
        INSERT INTO tracks (
            path, parent_folder, filename, suffix, format, bitrate,
            artist_mbid, album_mbid, release_track_mbid, recording_mbid,
            artist, album, date, readable, error, mtime, size, first_seen, last_seen
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(path) DO UPDATE SET
            parent_folder=excluded.parent_folder, filename=excluded.filename,
            suffix=excluded.suffix, format=excluded.format, bitrate=excluded.bitrate,
            artist_mbid=excluded.artist_mbid, album_mbid=excluded.album_mbid,
            release_track_mbid=excluded.release_track_mbid,
            recording_mbid=excluded.recording_mbid,
            artist=excluded.artist, album=excluded.album, date=excluded.date,
            readable=excluded.readable, error=excluded.error, mtime=excluded.mtime,
            size=excluded.size, last_seen=excluded.last_seen
        """,
        batch,
    )
    conn.commit()
