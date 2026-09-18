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

    tracks = totals["tracks"] or 0
    readable = totals["readable"] or 0

    def pct(n: int) -> float:
        return round(100 * n / readable, 1) if readable else 0.0

    return {
        "tracks": tracks,
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
    """Owned albums for one artist (by artist_mbid or exact artist tag name)."""
    by_mbid = conn.execute(
        "SELECT artist_mbid FROM artists WHERE artist_mbid = ?", (artist_ref,)
    ).fetchone()
    if by_mbid:
        where, param = "t.artist_mbid = ?", artist_ref
    else:
        where, param = "t.artist = ?", artist_ref

    rows = conn.execute(
        f"""
        SELECT
            al.folder,
            al.album_mbid,
            al.title,
            al.date,
            COUNT(t.path) AS tracks,
            MIN(t.suffix) AS one_format_sample,
            GROUP_CONCAT(DISTINCT t.suffix) AS formats,
            MIN(t.date) AS min_track_date,
            MAX(t.date) AS max_track_date
        FROM tracks t
        LEFT JOIN albums al ON al.folder = t.parent_folder
        WHERE {where}
        GROUP BY t.parent_folder
        ORDER BY al.date, al.title
        """,
        (param,),
    ).fetchall()

    return {
        "artist": artist_ref,
        "matched_by": "artist_mbid" if by_mbid else "artist_name",
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
