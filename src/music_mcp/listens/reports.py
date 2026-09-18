"""Listen/library reports: recent, top, gap analysis, stale library, discoveries.

All joins run through listened_artists (LB credit string) × library tracks.
Credit strings are matched to library artist MBIDs by exact name (the library's
canonical names are already credit-normalized); unmatched LB names are reported
as unresolved — that's the Phase 3 name→MBID resolver's input, never silent loss.
"""

from __future__ import annotations

import sqlite3
import time


def listens_recent(conn: sqlite3.Connection, limit: int = 20) -> dict:
    rows = conn.execute(
        """
        SELECT ts, artist_name, track_name, release_name
        FROM listens ORDER BY ts DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return {
        "listens": [
            {
                "ts": r["ts"],
                "artist": r["artist_name"],
                "track": r["track_name"],
                "release": r["release_name"],
            }
            for r in rows
        ],
        "count": len(rows),
    }


def listens_top_artists(conn: sqlite3.Connection, limit: int = 20) -> dict:
    rows = conn.execute(
        """
        SELECT artist_name, listen_count, first_listen_ts, last_listen_ts
        FROM listened_artists ORDER BY listen_count DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return {
        "artists": [dict(r) for r in rows],
        "count": len(rows),
        "total_distinct_artists": conn.execute(
            "SELECT COUNT(*) FROM listened_artists"
        ).fetchone()[0],
    }


def listens_gap_analysis(conn: sqlite3.Connection, min_listens: int = 5) -> dict:
    """Artists I listen to but don't own (the shopping list).

    Join: listened_artists (LB credit string) → library tracks (canonical names +
    artist MBIDs). Matched by exact name; LB names that don't match any owned
    artist are candidates — but truly-unowned and credit-mismatch both land here,
    so the result reports both counts honestly.
    """
    total = conn.execute("SELECT COUNT(*) FROM listened_artists").fetchone()[0]
    rows = conn.execute(
        """
        SELECT la.artist_name, la.listen_count, la.last_listen_ts
        FROM listened_artists la
        WHERE la.listen_count >= ?
          AND la.artist_name NOT IN (
              SELECT t.artist FROM tracks t WHERE t.artist IS NOT NULL
          )
          AND la.artist_name NOT IN (
              SELECT a.name FROM artists a WHERE a.name IS NOT NULL
          )
        ORDER BY la.listen_count DESC
        LIMIT 100
        """,
        (min_listens,),
    ).fetchall()
    return {
        "criterion": f"listens >= {min_listens} and not in library (exact name match)",
        "listened_artists_total": total,
        "not_owned": [
            {"artist": r["artist_name"], "listens": r["listen_count"], "last_listen_ts": r["last_listen_ts"]}
            for r in rows
        ],
        "count": len(rows),
    }


def listens_stale_library(conn: sqlite3.Connection, months: int = 6) -> dict:
    """Owned artists with zero listens in the window, ranked by past play intensity."""
    cutoff = int(time.time()) - months * 30 * 86400
    rows = conn.execute(
        """
        SELECT a.artist_mbid, a.name,
               COUNT(DISTINCT t.parent_folder) AS owned_albums,
               MAX(l.last_listen_ts) AS last_listen_ts,
               SUM(l.listen_count) AS total_listens
        FROM artists a
        JOIN tracks t ON t.artist_mbid = a.artist_mbid
        LEFT JOIN listened_artists l ON l.artist_name = a.name
        WHERE a.name IS NOT NULL
          AND (l.last_listen_ts IS NULL OR l.last_listen_ts < ?)
        GROUP BY a.artist_mbid, a.name
        ORDER BY total_listens DESC NULLS LAST, a.name
        LIMIT 100
        """,
        (cutoff,),
    ).fetchall()
    # SQLite lacks NULLS LAST on older versions; emulate in python to be safe.
    ordered = sorted(
        (dict(r) for r in rows),
        key=lambda r: (r["total_listens"] is None, -(r["total_listens"] or 0), r["name"]),
    )
    return {
        "criterion": f"owned artists with no listens in the last {months} months",
        "stale": ordered[:100],
        "count": len(ordered),
    }


def listens_new_discoveries(conn: sqlite3.Connection, days: int = 90) -> dict:
    """Artists whose first-ever listen falls inside the window (discovery rate)."""
    cutoff = int(time.time()) - days * 86400
    rows = conn.execute(
        """
        SELECT artist_name, listen_count, first_listen_ts
        FROM listened_artists
        WHERE first_listen_ts >= ?
        ORDER BY first_listen_ts DESC
        LIMIT 100
        """,
        (cutoff,),
    ).fetchall()
    return {
        "criterion": f"first listen within the last {days} days",
        "discovered": [dict(r) for r in rows],
        "count": len(rows),
    }
