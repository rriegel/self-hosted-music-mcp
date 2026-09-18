"""Name→MBID resolver for untagged library artists (propose→apply).

Two-phase, index-level only:
- propose (read-only): for each untagged artist (no artist_mbid on any of its
  tracks), search MB by name and store candidates with a confidence heuristic.
  Writes only to the mb_resolutions table.
- apply: write accepted resolutions into the index (artists.artist_mbid +
  tracks.artist_mbid rows) so discography lookups light up. Never touches file
  tags — tag writes stay out of scope for the resolver (explicit, separate
  concern if ever built).

Confidence: MB search score (0-100) combined with exact/case-insensitive name
equality. Auto-apply only proposals >= AUTO_APPLY_SCORE with exact-caseless name
match; everything else waits for explicit approval.
"""

from __future__ import annotations

import time

from music_mcp.listens.credits import split_credit

AUTO_APPLY_SCORE = 95

RESOLUTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS mb_resolutions (
    folder_artist TEXT PRIMARY KEY,     -- the untagged artist's display name in the index
    proposed_mbid TEXT,
    proposed_name TEXT,
    score INTEGER,
    confidence TEXT,                    -- 'exact' | 'case-insensitive' | 'fuzzy' | 'none'
    status TEXT NOT NULL DEFAULT 'proposed',   -- proposed | applied | rejected | ambiguous
    proposed_at REAL NOT NULL
);
"""


def _ensure_resolutions_table(conn) -> None:
    conn.executescript(RESOLUTIONS_SCHEMA)


def untagged_artists(conn, limit: int | None = None) -> list[dict]:
    """Index artists with no artist_mbid, with their owned track/folder counts."""
    sql = """
        SELECT t.artist AS name, COUNT(*) AS tracks, COUNT(DISTINCT t.parent_folder) AS albums
        FROM tracks t
        WHERE t.artist_mbid IS NULL OR t.artist_mbid = ''
        GROUP BY t.artist
        ORDER BY tracks DESC
    """
    sql += " LIMIT ?" if limit else ""
    return [dict(r) for r in (conn.execute(sql, (limit,)) if limit else conn.execute(sql)).fetchall()]


def propose(client, conn, max_artists: int = 25) -> dict:
    """Search MB for each untagged artist; store best candidate (or none).

    client: MBClient or a test double with search_artist().

    Multi-artist folder names ('Armand Hammer;The Alchemist') are split on
    separators and each part searched (whole string first — MB indexes some
    compound names directly). Best candidate across all attempts wins; the
    confidence compares the candidate name against the *matched* part, and a
    part-match caps auto-apply (multi-artist folders are ambiguous by nature).
    """
    _ensure_resolutions_table(conn)
    artists = untagged_artists(conn, limit=max_artists)
    proposed, no_match = 0, 0
    for artist in artists:
        name = artist["name"] or ""
        if not name:
            continue
        candidates_to_try = [name] + [p for p in split_credit(name) if p.lower() != name.lower()]
        best = None
        matched_part = name
        for attempt in candidates_to_try:
            data = client.search_artist(attempt, limit=3)
            candidates = data.get("artists", [])
            exact = next((a for a in candidates if (a.get("name") or "").lower() == attempt.lower()), None)
            pick = exact or (candidates[0] if candidates else None)
            if pick and (best is None or int(pick.get("score") or 0) > int(best.get("score") or 0)):
                best = pick
                matched_part = attempt
            if exact:
                break  # exact match for this attempt is as good as it gets

        mbid = best.get("id") if best else None
        mb_name = best.get("name") if best else None
        score = int(best.get("score") or 0) if best else 0

        if not mbid:
            confidence, status = "none", "proposed"
            no_match += 1
        elif (mb_name or "").lower() == name.lower():
            confidence, status = ("exact" if mb_name == name else "case-insensitive"), "proposed"
            proposed += 1
        elif (mb_name or "").lower() == matched_part.lower() and len(candidates_to_try) > 1:
            # matched a split part, not the whole string: resolvable but ambiguous
            confidence, status = "fuzzy", "proposed"
            proposed += 1
        else:
            confidence, status = "fuzzy", "ambiguous"
            proposed += 1

        conn.execute(
            """
            INSERT INTO mb_resolutions (folder_artist, proposed_mbid, proposed_name, score,
                                        confidence, status, proposed_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(folder_artist) DO UPDATE SET
                proposed_mbid=excluded.proposed_mbid, proposed_name=excluded.proposed_name,
                score=excluded.score, confidence=excluded.confidence,
                status=CASE WHEN mb_resolutions.status IN ('applied','rejected')
                            THEN mb_resolutions.status ELSE excluded.status END,
                proposed_at=excluded.proposed_at
            """,
            (name, mbid, mb_name, score, confidence, status, time.time()),
        )
        conn.commit()

    return {
        "artists_considered": len(artists),
        "proposals_stored": proposed,
        "no_mb_match": no_match,
        "note": "read-only: nothing applied. Review with "
        "'python -m music_mcp.mb resolve --db <db>'; apply with the apply command.",
        "proposals": [
            {
                "artist": r["folder_artist"],
                "mbid": r["proposed_mbid"],
                "mb_name": r["proposed_name"],
                "score": r["score"],
                "confidence": r["confidence"],
                "status": r["status"],
            }
            for r in conn.execute(
                "SELECT * FROM mb_resolutions ORDER BY score DESC LIMIT 50"
            ).fetchall()
        ],
    }


def commit(client, conn, batch_size: int = 10) -> dict:
    """Apply confident proposals into the index (artists + tracks tables).

    client: unused at apply time (proposals are already stored); accepted for
    interface symmetry with propose().
    """
    _ensure_resolutions_table(conn)
    rows = conn.execute(
        "SELECT folder_artist, proposed_mbid, proposed_name, confidence, score "
        "FROM mb_resolutions WHERE status = 'proposed'"
    ).fetchall()

    applied: list[dict] = []
    skipped: list[dict] = []
    for row in rows:
        if row["confidence"] not in ("exact", "case-insensitive") or (row["score"] or 0) < AUTO_APPLY_SCORE:
            skipped.append({"artist": row["folder_artist"], "reason": "low confidence"})
            continue
        mbid = row["proposed_mbid"]
        name = row["folder_artist"]
        conn.execute(
            """
            INSERT INTO artists (artist_mbid, name, first_seen, last_seen)
            VALUES (?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(artist_mbid) DO UPDATE SET
                name = COALESCE(artists.name, excluded.name),
                last_seen = datetime('now')
            """,
            (mbid, row["proposed_name"]),
        )
        conn.execute(
            "UPDATE tracks SET artist_mbid = ? WHERE artist = ? AND (artist_mbid IS NULL OR artist_mbid = '')",
            (mbid, name),
        )
        conn.execute("UPDATE mb_resolutions SET status = 'applied' WHERE folder_artist = ?", (name,))
        applied.append({"artist": name, "mbid": mbid})
        if len(applied) >= batch_size:
            break
    conn.commit()

    remaining = conn.execute("SELECT COUNT(*) FROM mb_resolutions WHERE status='proposed'").fetchone()[0]
    return {
        "applied": applied,
        "applied_count": len(applied),
        "skipped": skipped,
        "remaining_proposed": remaining,
        "note": "index-level only; file tags untouched. Re-scan is safe: the scanner "
        "upserts by mtime/size and will not overwrite these MBIDs.",
    }
