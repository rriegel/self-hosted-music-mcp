"""Read-only queries over the library cache: status, artists, albums.

These are the future `library_*` MCP tool handlers — plain functions returning
JSON-able dicts (the SDK owns the protocol; handlers are what we test).
"""

from __future__ import annotations

import sqlite3


def library_status(conn: sqlite3.Connection) -> dict:
    """Index health: counts, last scan, MBID coverage by track and release."""
    totals = conn.execute(
        """
        SELECT
            COUNT(*) AS tracks,
            SUM(readable) AS readable,
            SUM(artist_mbid IS NOT NULL) AS with_artist_mbid,
            SUM(album_mbid IS NOT NULL) AS with_album_mbid,
            SUM(release_track_mbid IS NOT NULL) AS with_release_track_mbid,
            SUM(error IS NOT NULL) AS with_errors
        FROM tracks
        """
    ).fetchone()
    artists = conn.execute("SELECT COUNT(*) AS n FROM artists").fetchone()["n"]
    albums = conn.execute("SELECT COUNT(*) AS n FROM albums").fetchone()["n"]
    last_scan = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'last_scan'"
    ).fetchone()

    readable = totals["readable"] or 0

    def pct(n: int) -> float:
        return round(100 * n / readable, 1) if readable else 0.0

    return {
        "tracks": totals["tracks"] or 0,
        "readable": readable,
        "artists": artists,
        "albums": albums,
        "last_scan": last_scan["value"] if last_scan else None,
        "coverage_pct": {
            "artist_mbid": pct(totals["with_artist_mbid"] or 0),
            "album_mbid": pct(totals["with_album_mbid"] or 0),
            "release_track_mbid": pct(totals["with_release_track_mbid"] or 0),
        },
        "files_with_errors": totals["with_errors"] or 0,
        "formats": {
            row["format"] or "unreadable": row["n"]
            for row in conn.execute(
                "SELECT format, COUNT(*) AS n FROM tracks GROUP BY format ORDER BY n DESC"
            )
        },
    }


def library_artists(conn: sqlite3.Connection, filter_mbid: str | None = None) -> dict:
    """Indexed artists with owned release counts. filter_mbid='missing' → unresolved only."""
    where = ""
    params: tuple = ()
    if filter_mbid == "missing":
        where = "WHERE t.artist_mbid IS NULL OR t.artist_mbid = ''"
    elif filter_mbid:
        where = "WHERE t.artist_mbid = ?"
        params = (filter_mbid,)

    rows = conn.execute(
        f"""
        SELECT
            t.artist_mbid,
            COALESCE(a.name, MAX(t.artist)) AS name,
            COUNT(DISTINCT t.parent_folder) AS albums,
            COUNT(*) AS tracks,
            MIN(t.first_seen) AS first_seen,
            MAX(t.last_seen) AS last_seen
        FROM tracks t
        LEFT JOIN artists a ON a.artist_mbid = t.artist_mbid
        {where}
        GROUP BY t.artist_mbid, a.name
        ORDER BY tracks DESC
        """,
        params,
    ).fetchall()

    return {
        "artists": [
            {
                "artist_mbid": row["artist_mbid"],
                "name": row["name"],
                "albums": row["albums"],
                "tracks": row["tracks"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
            }
            for row in rows
        ],
        "count": len(rows),
    }


def library_albums(conn: sqlite3.Connection, artist_ref: str) -> dict:
    """Owned albums for one artist, by artist_mbid or artist name.

    Name matching goes through the artists table (name -> MBID) so varied file
    credits ('JPEGMAFIA feat. X', 'JPEGMAFIA x Danny Brown') still resolve to the
    one artist's discography — the artist_mbid on the albums rows is the join key;
    the credit string is not.
    """
    by_mbid = conn.execute(
        "SELECT artist_mbid FROM artists WHERE artist_mbid = ?", (artist_ref,)
    ).fetchone()
    if by_mbid:
        resolved_mbid = artist_ref
        matched_by = "artist_mbid"
    else:
        # Exact name first, then case-insensitive fallback (folder-derived names vary in case).
        row = conn.execute(
            """
            SELECT artist_mbid FROM (
                SELECT artist_mbid FROM artists WHERE name = ?
                UNION ALL
                SELECT artist_mbid FROM artists
                WHERE name LIKE ? AND artist_mbid IS NOT NULL
            ) LIMIT 1
            """,
            (artist_ref, artist_ref),
        ).fetchone()
        if not row or row["artist_mbid"] is None:
            return {
                "artist": artist_ref,
                "matched_by": None,
                "matched_note": "no artist with this name or mbid in the index",
                "albums": [],
                "count": 0,
            }
        resolved_mbid = row["artist_mbid"]
        matched_by = "artist_name"

    rows = conn.execute(
        """
        SELECT
            al.folder,
            al.album_mbid,
            COALESCE(al.title, al.folder) AS title,
            al.date,
            COUNT(t.path) AS tracks,
            GROUP_CONCAT(DISTINCT t.suffix) AS formats,
            MIN(t.date) AS min_track_date,
            MAX(t.date) AS max_track_date
        FROM albums al
        JOIN tracks t ON t.parent_folder = al.folder
        WHERE al.artist_mbid = ?
        GROUP BY al.folder
        ORDER BY al.date, al.title
        """,
        (resolved_mbid,),
    ).fetchall()

    return {
        "artist": artist_ref,
        "artist_mbid": resolved_mbid,
        "matched_by": matched_by,
        "albums": [
            {
                "folder": row["folder"],
                "album_mbid": row["album_mbid"],
                "title": row["title"],
                "date": row["date"],
                "tracks": row["tracks"],
                "formats": sorted(f for f in (row["formats"] or "").split(",") if f),
                "tag_date_range": (
                    [row["min_track_date"], row["max_track_date"]]
                    if row["min_track_date"] != row["max_track_date"]
                    else row["min_track_date"]
                ),
            }
            for row in rows
        ],
        "count": len(rows),
    }
