"""Incremental listens sync into the cache + artist-name rollups.

Incremental: remembers the newest synced listen ts; the next sync pulls only
listens newer than that (client stops at already-synced territory). Idempotent:
re-syncing a range re-upserts the same (ts, track, artist) primary keys.
"""

from __future__ import annotations

import time

from music_mcp.listens.client import LBClient

NEWEST_TS_KEY = "listens_newest_ts"


def sync_listens(
    conn,
    client=None,
    stop_after_pages: int | None = None,
    incremental: bool = True,
) -> dict:
    """Pull listens from LB and upsert into the cache. Returns a summary dict.

    client: any object with iter_all_listens() (LBClient or a test double).
    """
    client = client or LBClient()
    started = time.monotonic()

    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (NEWEST_TS_KEY,)
    ).fetchone()
    since_ts = int(row["value"]) if (incremental and row) else None

    upserted = 0
    batch: list[tuple] = []

    def flush() -> None:
        if not batch:
            return
        conn.executemany(
            """
            INSERT INTO listens (ts, artist_name, artist_mbid, track_name, track_mbid,
                                 release_name, release_mbid)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(ts, track_name, artist_name) DO UPDATE SET
                artist_mbid=excluded.artist_mbid, track_mbid=excluded.track_mbid,
                release_name=excluded.release_name, release_mbid=excluded.release_mbid
            """,
            batch,
        )
        conn.commit()
        batch.clear()

    newest_seen = since_ts or 0
    for listen in client.iter_all_listens(since_ts=since_ts, stop_after_pages=stop_after_pages):
        meta = listen.get("track_metadata", {}) or {}
        mbids = meta.get("mbids") or {}
        ts = listen.get("listened_at") or 0
        artist = meta.get("artist_name") or ""
        if not artist:
            continue  # unattributable listen; skip rather than store junk
        batch.append(
            (
                ts,
                artist,
                (mbids.get("artist_mbid") if isinstance(mbids, dict) else None),
                meta.get("track_name") or "",
                (mbids.get("track_mbid") if isinstance(mbids, dict) else None),
                meta.get("release_name"),
                (mbids.get("release_mbid") if isinstance(mbids, dict) else None),
            )
        )
        upserted += 1
        newest_seen = max(newest_seen, ts)
        if len(batch) >= 500:
            flush()
    flush()

    if upserted:
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (NEWEST_TS_KEY, str(newest_seen)),
        )
        conn.commit()

    return {
        "user": client.user,
        "mode": "incremental" if incremental and since_ts else "full",
        "listens_synced": upserted,
        "newest_ts": newest_seen or None,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def refresh_listened_artists(conn) -> int:
    """Rebuild listened_artists rollup from the listens table (cheap, always consistent)."""
    conn.execute("DELETE FROM listened_artists")
    conn.execute(
        """
        INSERT INTO listened_artists (artist_name, listen_count, first_listen_ts, last_listen_ts)
        SELECT artist_name, COUNT(*), MIN(ts), MAX(ts)
        FROM listens
        GROUP BY artist_name
        """
    )
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM listened_artists").fetchone()[0]
