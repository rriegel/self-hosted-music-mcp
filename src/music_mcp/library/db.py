"""SQLite cache for the library index.

WAL mode (proven in the Phase 0 smoke test): concurrent agent readers while a
cron-style writer commits. MB data later joins the same DB — schema stays
library-only for now.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artists (
    artist_mbid TEXT PRIMARY KEY,
    name TEXT,                          -- best known name; may be NULL until resolved
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS albums (
    album_mbid TEXT,
    artist_mbid TEXT,
    title TEXT,                         -- album tag; folder-derived title when untagged
    folder TEXT NOT NULL,               -- relative album dir, e.g. 'Artist/Album (2020)'
    date TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (folder),
    FOREIGN KEY (artist_mbid) REFERENCES artists (artist_mbid)
);
CREATE INDEX IF NOT EXISTS idx_albums_artist ON albums (artist_mbid);

CREATE TABLE IF NOT EXISTS tracks (
    path TEXT PRIMARY KEY,              -- absolute path; identity of a file
    parent_folder TEXT NOT NULL,        -- relative album dir (join key to albums)
    filename TEXT NOT NULL,
    suffix TEXT NOT NULL,
    format TEXT,                        -- mutagen class name (MP3, FLAC, MP4...)
    bitrate INTEGER,
    artist_mbid TEXT,
    album_mbid TEXT,
    release_track_mbid TEXT,
    recording_mbid TEXT,
    artist TEXT,
    album TEXT,
    date TEXT,
    readable INTEGER NOT NULL DEFAULT 1,
    error TEXT,                         -- last read error, NULL when healthy
    mtime REAL NOT NULL,                -- source-of-truth for incremental scans
    size INTEGER NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    FOREIGN KEY (artist_mbid) REFERENCES artists (artist_mbid)
);
CREATE INDEX IF NOT EXISTS idx_tracks_parent ON tracks (parent_folder);
CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks (artist_mbid);

-- ListenBrainz listens (append-only; incremental sync by timestamp, schema v2)
CREATE TABLE IF NOT EXISTS listens (
    ts INTEGER NOT NULL,                -- unix seconds of the listen
    artist_name TEXT NOT NULL,          -- LB credit string; names are search inputs only
    artist_mbid TEXT,                   -- LB-provided; usually NULL for Pano scrobbles
    track_name TEXT NOT NULL,
    track_mbid TEXT,
    release_name TEXT,
    release_mbid TEXT,
    PRIMARY KEY (ts, track_name, artist_name)
);
CREATE INDEX IF NOT EXISTS idx_listens_artist_name ON listens (artist_name);
CREATE INDEX IF NOT EXISTS idx_listens_ts ON listens (ts);

-- Listen-time artist stats keyed by the LB credit string (the resolution key
-- for name→MBID goes through a separate resolver; this stays raw LB truth)
CREATE TABLE IF NOT EXISTS listened_artists (
    artist_name TEXT PRIMARY KEY,       -- LB credit string, as scrobbled
    listen_count INTEGER NOT NULL,
    first_listen_ts INTEGER,
    last_listen_ts INTEGER
);

-- MusicBrainz response cache (schema v3): MB metadata is effectively immutable
CREATE TABLE IF NOT EXISTS mb_cache (
    key TEXT PRIMARY KEY,               -- sha256 of full request URL
    url TEXT NOT NULL,
    response TEXT NOT NULL,             -- raw JSON payload
    fetched_at REAL NOT NULL
);

-- Resolver proposals (folder/display artist name -> proposed MBID)
CREATE TABLE IF NOT EXISTS mb_resolutions (
    folder_artist TEXT PRIMARY KEY,
    proposed_mbid TEXT,
    proposed_name TEXT,
    score INTEGER,
    confidence TEXT,                    -- 'exact' | 'case-insensitive' | 'fuzzy' | 'none'
    status TEXT NOT NULL DEFAULT 'proposed',   -- proposed | applied | rejected | ambiguous
    proposed_at REAL NOT NULL
);

-- Watchlist (schema v4): MCP-owned artist watchlist for release radar / discovery.
-- Imported from the Friday cron's watchlist.json; the cron keeps reading its own
-- file — this table is the MCP-side source of truth.
CREATE TABLE IF NOT EXISTS watchlist (
    artist_mbid TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    sources TEXT NOT NULL DEFAULT '[]', -- JSON array: library | listenbrainz | similar
    listen_count INTEGER NOT NULL DEFAULT 0,
    added_at REAL NOT NULL
);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open the cache with WAL enabled and the schema applied. Rows use dict access."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _ensure_schema_version(conn)
    return conn


def _ensure_schema_version(conn: sqlite3.Connection) -> None:
    """Record the current schema version (idempotent upgrades, additive only)."""
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_meta (key, value) VALUES ('schema_version', '4')")
        conn.commit()
    elif int(row["value"]) < 4:
        # additive upgrades (v3 mb tables, v4 watchlist) are CREATE IF NOT EXISTS
        conn.execute("UPDATE schema_meta SET value = '4' WHERE key = 'schema_version'")
        conn.commit()


def current_schema_version(conn: sqlite3.Connection) -> int:
    """Read the schema version, creating the marker row on first call."""
    _ensure_schema_version(conn)
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    return int(row["value"])


def db_file_sidecars(db_path: Path | str) -> list[Path]:
    """The WAL/SHM sidecar files for a db path (informational; SQLite manages them)."""
    base = Path(db_path)
    return [base.with_suffix(base.suffix + suffix) for suffix in ("-wal", "-shm")]
