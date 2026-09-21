"""library_find_dupes and quality report — Phase 3 intelligence over the index.

Dupe semantics (per planning doc, release vs release-group matters):
- same album_mbid in multiple folders → true duplicate copies (strongest signal)
- same normalized title in one artist → likely dupes (releases/editions mismatch)
- same folder containing multiple album_mbids → mislabeled folder
Quality: coverage gaps, tag inconsistencies, per-format bitrates.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections import defaultdict


def _norm_title(title: str | None) -> str:
    if not title:
        return ""
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return "".join(ch for ch in folded.lower() if ch.isalnum())


def _rg_overlap(titles_a: list[str], titles_b: list[str]) -> list[str]:
    """Normalized shared release-group titles (exact any length; containment >=4)."""

    def norm(t: str) -> str:
        folded = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
        return "".join(ch for ch in folded.lower() if ch.isalnum())

    shared: list[str] = []
    for tb in titles_b:
        nb = norm(tb)
        if not nb:
            continue
        for ta in titles_a:
            na = norm(ta)
            if not na:
                continue
            # exact equality counts at any length ('AMA' is 3 chars and IS the
            # incident's key title); containment requires >=4 to avoid noise
            if na == nb or (len(na) >= 4 and na in nb) or (len(nb) >= 4 and nb in na):
                shared.append(tb)
                break
    return sorted(set(shared))


def _artist_entity_dupes(conn: sqlite3.Connection, client) -> list[dict]:
    """Detect one real artist split across multiple MB entities (upstream MB dupes).

    Candidates: distinct indexed artist pairs sharing a top-level folder prefix
    ('Ama/...' holds both be578aa2 'AMA' and 2d2f1395 'Ama Lou'). Confirmed only
    when their MB release-group title sets overlap. Read-only; fixing is
    mb_apply retarget's job.
    """
    if client is None:
        raise ValueError("scope='artist_entities' needs an MB client (MB release-group lookups)")
    candidates = conn.execute(
        """
        SELECT a.artist_mbid AS mbid_a, b.artist_mbid AS mbid_b,
               MAX(a.name) AS name_a, MAX(b.name) AS name_b,
               MAX(ta.folder) AS folder_a, MAX(tb.folder) AS folder_b
        FROM artists a
        JOIN artists b ON a.artist_mbid < b.artist_mbid
        JOIN albums ta ON ta.artist_mbid = a.artist_mbid
        JOIN albums tb ON tb.artist_mbid = b.artist_mbid
        WHERE substr(ta.folder, 1, instr(ta.folder || '/', '/') - 1)
            = substr(tb.folder, 1, instr(tb.folder || '/', '/') - 1)
        GROUP BY a.artist_mbid, b.artist_mbid
        """
    ).fetchall()

    findings: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    for row in candidates:
        pair = (row["mbid_a"], row["mbid_b"])
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        try:
            titles_a = [rg.get("title") or "" for rg in client.release_groups(row["mbid_a"])]
            titles_b = [rg.get("title") or "" for rg in client.release_groups(row["mbid_b"])]
        except Exception:  # noqa: BLE001 - detection must never fail the report
            continue
        shared = _rg_overlap(titles_a, titles_b)
        if not shared:
            continue
        findings.append(
            {
                "artist_mbid_a": row["mbid_a"],
                "name_a": row["name_a"],
                "artist_mbid_b": row["mbid_b"],
                "name_b": row["name_b"],
                "shared_release_groups": shared,
                "folders": sorted({row["folder_a"], row["folder_b"]}),
                "suggested_action": (
                    "same catalog under two MB entities; verify which is canonical on MB, "
                    "then mb_apply action='retarget' from the duplicate into it (cache-only)"
                ),
            }
        )
    return findings


def library_find_dupes(
    conn: sqlite3.Connection, scope: str = "all", client=None
) -> dict:
    # expose the Python normalizer to SQL for title grouping
    conn.create_function("_norm", 1, _norm_title)
    findings: dict[str, list] = defaultdict(list)

    if scope in ("release_group", "all"):
        rows = conn.execute(
            """
            SELECT album_mbid, COUNT(DISTINCT folder) AS folders, GROUP_CONCAT(folder, ' || ') AS where_
            FROM albums WHERE album_mbid IS NOT NULL
            GROUP BY album_mbid HAVING COUNT(DISTINCT folder) > 1
            """
        ).fetchall()
        findings["same_release_multiple_folders"] = [dict(r) for r in rows]

    if scope in ("title", "all"):
        # Group untagged artists by NAME, not a shared 'unknown' key — otherwise
        # different untagged artists with a common album title fake a dupe.
        conn.create_function("_norm_artist", 1, lambda v: _norm_title(v) if v else f"untagged:{v}")
        rows = conn.execute(
            """
            SELECT COALESCE(a.artist_mbid, 'untagged:' || t.artist) AS artist_key,
                   MAX(a.artist_mbid) AS artist_mbid,
                   MAX(a.title) AS title,
                   COUNT(*) AS copies,
                   GROUP_CONCAT(a.folder, ' || ') AS where_
            FROM albums a
            JOIN tracks t ON t.parent_folder = a.folder
            GROUP BY artist_key, _norm(a.title)
            HAVING COUNT(DISTINCT a.folder) > 1
            """
        ).fetchall()
        findings["same_title_same_artist"] = [dict(r) for r in rows]

    if scope == "artist_entities":
        # explicit-only scope: it queries MB release-groups per artist pair
        findings["artist_entities"] = _artist_entity_dupes(conn, client)

    if scope in ("folder", "all"):
        # file-level truth: a folder whose tracks carry >1 album identity
        rows = conn.execute(
            """
            SELECT parent_folder AS folder, COUNT(DISTINCT album_mbid) AS mbids,
                   GROUP_CONCAT(DISTINCT album_mbid) AS mbid_list
            FROM tracks WHERE album_mbid IS NOT NULL
            GROUP BY parent_folder HAVING COUNT(DISTINCT album_mbid) > 1
            """
        ).fetchall()
        findings["folder_multiple_releases"] = [dict(r) for r in rows]

    return {
        "scope": scope,
        "findings": {k: v for k, v in findings.items() if v},
        "total_findings": sum(len(v) for v in findings.values()),
    }


def library_quality_report(conn: sqlite3.Connection, min_bitrate: int = 192000) -> dict:
    totals = conn.execute(
        """
        SELECT
            COUNT(*) AS tracks,
            SUM(artist_mbid IS NULL OR artist_mbid = '') AS missing_artist_mbid,
            SUM(album_mbid IS NULL OR album_mbid = '') AS missing_album_mbid,
            SUM(release_track_mbid IS NULL OR release_track_mbid = '') AS missing_track_mbid,
            SUM(date IS NULL OR date = '') AS missing_date,
            SUM(error IS NOT NULL) AS with_errors
        FROM tracks
        """
    ).fetchone()

    low_bitrate = conn.execute(
        "SELECT COUNT(*) AS n FROM tracks WHERE bitrate IS NOT NULL AND bitrate < ?", (min_bitrate,)
    ).fetchone()["n"]

    artist_tag_variants = conn.execute(
        """
        SELECT COUNT(*) AS n FROM (
            SELECT artist_mbid FROM tracks WHERE artist_mbid IS NOT NULL
            GROUP BY artist_mbid HAVING COUNT(DISTINCT artist) > 1
        )
        """
    ).fetchone()["n"]

    formats = {
        row["format"] or "unreadable": {
            "tracks": row["n"],
            "avg_bitrate": conn.execute(
                "SELECT AVG(bitrate) FROM tracks WHERE (format = ? OR (format IS NULL AND ? IS NULL)) "
                "AND bitrate IS NOT NULL",
                (row["format"], row["format"]),
            ).fetchone()[0],
        }
        for row in conn.execute("SELECT format, COUNT(*) AS n FROM tracks GROUP BY format")
    }

    tracks = totals["tracks"] or 0

    def pct(n) -> float:
        return round(100 * (n or 0) / tracks, 1) if tracks else 0.0

    return {
        "tracks": tracks,
        "missing_pct": {
            "artist_mbid": pct(totals["missing_artist_mbid"]),
            "album_mbid": pct(totals["missing_album_mbid"]),
            "release_track_mbid": pct(totals["missing_track_mbid"]),
            "date": pct(totals["missing_date"]),
        },
        "files_with_errors": totals["with_errors"] or 0,
        "below_min_bitrate": {"threshold": min_bitrate, "tracks": low_bitrate},
        "artists_with_credit_variants": artist_tag_variants,
        "formats": formats,
    }
