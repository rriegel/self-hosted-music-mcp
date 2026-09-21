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
import unicodedata

from music_mcp.listens.credits import split_credit

AUTO_APPLY_SCORE = 95

RESOLUTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS mb_resolutions (
    folder_artist TEXT PRIMARY KEY,     -- the untagged artist's display name in the index
    proposed_mbid TEXT,
    proposed_name TEXT,
    score INTEGER,
    confidence TEXT,                    -- 'exact' | 'case-insensitive' | 'fuzzy' | 'none'
    status TEXT NOT NULL DEFAULT 'proposed',   -- proposed | applied | rejected | ambiguous | needs_review
    proposed_at REAL NOT NULL,
    evidence TEXT                              -- why this proposal: name match + album-title overlap
);
"""


def _ensure_resolutions_table(conn) -> None:
    conn.executescript(RESOLUTIONS_SCHEMA)
    # additive migration: DBs created before the evidence column lack it
    cols = {r[1] for r in conn.execute("PRAGMA table_info(mb_resolutions)")}
    if "evidence" not in cols:
        conn.execute("ALTER TABLE mb_resolutions ADD COLUMN evidence TEXT")
        conn.commit()


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


def _norm(title: str | None) -> str:
    if not title:
        return ""
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return "".join(ch for ch in folded.lower() if ch.isalnum())


def _title_overlap(folder_titles: list[str], rg_titles: list[str]) -> list[str]:
    """MB release-group titles that evidence a folder's album titles.

    Normalized equality or containment (min 4 chars) — 'AMA' matches 'AMA',
    'I Came Home Late (Deluxe)' matches 'I Came Home Late'. Sorted for stable
    output.
    """
    hits: list[str] = []
    for rg in rg_titles:
        n_rg = _norm(rg)
        if not n_rg:
            continue
        for ft in folder_titles:
            n_ft = _norm(ft)
            if not n_ft:
                continue
            # exact equality counts at any length ('AMA' is 3 chars and IS the
            # incident's key title); containment requires >=4 to avoid noise
            if n_rg == n_ft or (len(n_ft) >= 4 and n_ft in n_rg) or (len(n_rg) >= 4 and n_rg in n_ft):
                hits.append(rg)
                break
    return sorted(set(hits))


def confidence_note(mb_name: str, folder_name: str, matched_part: str | None = None) -> str:
    """Human-readable name-match quality for the evidence column."""
    if mb_name == folder_name:
        return "exact"
    if (mb_name or "").lower() == (folder_name or "").lower():
        return "case-insensitive"
    if matched_part and (mb_name or "").lower() == matched_part.lower():
        return f"part match ('{matched_part}')"
    return "fuzzy"


def review_list(conn, status: str | None = None) -> dict:
    """All resolver proposals with their evidence (the review worklist)."""
    _ensure_resolutions_table(conn)
    where = "" if status is None else "WHERE status = ?"
    params = () if status is None else (status,)
    rows = conn.execute(
        f"SELECT folder_artist, proposed_mbid, proposed_name, score, confidence, status, "
        f"proposed_at, evidence FROM mb_resolutions {where} ORDER BY score DESC",
        params,
    ).fetchall()
    return {
        "proposals": [dict(r) for r in rows],
        "count": len(rows),
        "note": "set a corrected mbid via mb_review(action='set') after verifying "
        "against MB; reject false positives; commit applies confident 'proposed' rows only.",
    }


def review_set(conn, folder_artist: str, mbid: str, name: str | None = None) -> dict:
    """Correct a proposal after human verification (the supported fix path).

    Sets score=100/exact so commit() accepts it, resets status to 'proposed',
    and records the manual override in evidence.
    """
    _ensure_resolutions_table(conn)
    row = conn.execute(
        "SELECT proposed_name FROM mb_resolutions WHERE folder_artist = ?", (folder_artist,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no proposal for folder artist '{folder_artist}' — run mb_resolve first")
    new_name = name or row["proposed_name"]
    conn.execute(
        """
        UPDATE mb_resolutions SET proposed_mbid = ?, proposed_name = ?, score = 100,
            confidence = 'exact', status = 'proposed',
            evidence = COALESCE(evidence, '') || ' [manually verified]'
        WHERE folder_artist = ?
        """,
        (mbid, new_name, folder_artist),
    )
    conn.commit()
    return {"updated": 1, "folder_artist": folder_artist, "mbid": mbid, "name": new_name}


def review_reject(conn, folder_artist: str) -> dict:
    """Mark a proposal rejected — commit() will never apply it."""
    _ensure_resolutions_table(conn)
    cur = conn.execute(
        "UPDATE mb_resolutions SET status = 'rejected' WHERE folder_artist = ?",
        (folder_artist,),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise ValueError(f"no proposal for folder artist '{folder_artist}'")
    return {"rejected": 1, "folder_artist": folder_artist}


def retarget(conn, from_mbid: str, into_mbid: str) -> dict:
    """Merge a duplicate MB entity into the canonical one (cache-only).

    Moves albums.artist_mbid and tracks.artist_mbid from the duplicate to the
    canonical entity and removes the now-empty artists row. File tags untouched;
    scanner upserts by mtime/size and will not overwrite. Real case: MB had Ama
    Lou's catalog under two entities (be578aa2 'AMA' + 2d2f1395 'Ama Lou').
    """
    if from_mbid == into_mbid:
        raise ValueError("retarget requires two different MBIDs")
    for mbid in (from_mbid, into_mbid):
        if conn.execute("SELECT 1 FROM artists WHERE artist_mbid = ?", (mbid,)).fetchone() is None:
            raise ValueError(f"artist_mbid {mbid} not in index — nothing to retarget")
    albums_moved = conn.execute(
        "UPDATE albums SET artist_mbid = ? WHERE artist_mbid = ?", (into_mbid, from_mbid)
    ).rowcount
    tracks_moved = conn.execute(
        "UPDATE tracks SET artist_mbid = ? WHERE artist_mbid = ?", (into_mbid, from_mbid)
    ).rowcount
    conn.execute("DELETE FROM artists WHERE artist_mbid = ?", (from_mbid,))
    conn.commit()
    return {
        "from_mbid": from_mbid,
        "into_mbid": into_mbid,
        "albums_moved": albums_moved,
        "tracks_moved": tracks_moved,
        "note": "cache-only merge; file tags untouched. Re-scan is safe.",
    }


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
        score = int(best.get("score") or 0) if best else None

        # Evidence tier: compare the folder's album titles against the candidate's
        # MB release-groups. Real incident (2026-09-20): folder 'Ama' (albums
        # AMA/Life's Better/Aura) proposed the Spanish group 'Ama' at score 92
        # 'exact' — perfect name, zero music overlap. Name-only matches on
        # folders WITH album titles must not auto-apply.
        evidence = None
        rg_titles: list[str] = []
        if mbid:
            try:
                rg_titles = [r.get("title") or "" for r in client.release_groups(mbid)]
            except Exception:  # noqa: BLE001 - evidence check must never break propose
                rg_titles = []

        album_rows = conn.execute(
            "SELECT DISTINCT title FROM albums a JOIN tracks t ON t.parent_folder = a.folder "
            "WHERE t.artist = ? AND a.title IS NOT NULL",
            (name,),
        ).fetchall()
        folder_album_titles = [r["title"] for r in album_rows]

        name_quality = confidence_note(mb_name or "", name, matched_part)
        if mbid and folder_album_titles:
            overlap = _title_overlap(folder_album_titles, rg_titles)
            evidence = (
                f"name match ({name_quality}); album evidence: {', '.join(sorted(overlap))}"
                if (overlap := _title_overlap(folder_album_titles, rg_titles))
                else f"name match ({name_quality}) but no album evidence — candidate's MB "
                "release-groups share no title with the folder's albums"
            )
            status = "proposed" if overlap else "needs_review"
        elif mbid:
            evidence = (
                f"name match only ({confidence_note(mb_name or '', name)}); "
                "folder has no album titles to cross-check"
            )
            status = "proposed"
        else:
            evidence = "no MB candidate"
            status = "proposed"

        if not mbid:
            confidence, status = "none", "proposed"
            no_match += 1
        elif (mb_name or "").lower() == name.lower():
            confidence = "exact" if mb_name == name else "case-insensitive"
            proposed += 1
        elif (mb_name or "").lower() == matched_part.lower() and len(candidates_to_try) > 1:
            confidence, status = "fuzzy", ("proposed" if status == "proposed" else status)
            proposed += 1
        else:
            confidence, status = "fuzzy", ("ambiguous" if status == "proposed" else status)
            proposed += 1

        conn.execute(
            """
            INSERT INTO mb_resolutions (folder_artist, proposed_mbid, proposed_name, score,
                                        confidence, status, proposed_at, evidence)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(folder_artist) DO UPDATE SET
                proposed_mbid=excluded.proposed_mbid, proposed_name=excluded.proposed_name,
                score=excluded.score, confidence=excluded.confidence,
                status=CASE WHEN mb_resolutions.status IN ('applied','rejected')
                            THEN mb_resolutions.status ELSE excluded.status END,
                proposed_at=excluded.proposed_at,
                evidence=excluded.evidence
            """,
            (name, mbid, mb_name, score, confidence, status, time.time(), evidence),
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
