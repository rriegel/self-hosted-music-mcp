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

Part-match policy (2026-09-21 dogfood): multi-artist folder names are split and
each part is searched separately; a candidate is only trusted when its stored
name EQUALS the query (whole string) or one of the split parts — MB's score
ranks alias-substring matches first, which pinned 'Mos Def;MF DOOM' onto MF
DOOM alone. Part-matches are stored as 'fuzzy' with status 'needs_review', so
they never auto-apply; confirm or correct them via mb_review.
"""

from __future__ import annotations

import time

from music_mcp.listens.credits import normalize_name, split_credit

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


def _title_overlap(folder_titles: list[str], rg_titles: list[str]) -> list[str]:
    """MB release-group titles that evidence a folder's album titles.

    Normalized equality or containment (min 2 chars) — 'AMA' matches 'AMA',
    'I Came Home Late (Deluxe)' matches 'I Came Home Late'. The containment
    floor is 2 chars (was 4) because CJK album titles are typically 2-4
    characters; '平原綾香' evidence must not be lost to an ASCII-sized floor.
    Sorted for stable output.
    """
    hits: list[str] = []
    for rg in rg_titles:
        n_rg = normalize_name(rg)
        if not n_rg:
            continue
        for ft in folder_titles:
            n_ft = normalize_name(ft)
            if not n_ft:
                continue
            # exact equality counts at any length ('AMA' is 3 chars and IS the
            # incident's key title); containment requires >=2 to avoid noise
            if n_rg == n_ft or (len(n_ft) >= 2 and n_ft in n_rg) or (len(n_rg) >= 2 and n_rg in n_ft):
                hits.append(rg)
                break
    return sorted(set(hits))


def confidence_note(mb_name: str, folder_name: str, matched_part: str | None = None) -> str:
    """Human-readable name-match quality for the evidence column.

    Comparisons use normalize_name (script-safe: casefold + punctuation
    collapse, non-Latin scripts pass through), so kanji/kana names classify
    correctly instead of degrading to 'fuzzy'.
    """
    if mb_name == folder_name:
        return "exact"
    if mb_name and folder_name and normalize_name(mb_name) == normalize_name(folder_name):
        return "case-insensitive"
    if matched_part and mb_name and normalize_name(mb_name) == normalize_name(matched_part):
        return f"part match ('{matched_part}')"
    n_mb, n_folder = normalize_name(mb_name), normalize_name(folder_name)
    if n_mb and n_folder and (n_mb.startswith(n_folder) or n_folder.startswith(n_mb)):
        return "prefix"
    return "fuzzy"


def _match_tier(candidate: dict, query: str) -> int:
    """How directly does this MB candidate answer the query?

    2 = query equals the candidate's stored name (normalize_name equality),
    1 = query equals one of its aliases, 0 = only a fuzzy/substring hit.
    Tier beats MB's score: 'The YMD' scored 100 for 'Mos Def' via the 'Yah
    Mos Def' substring while the real artist sat at 76 with the exact alias.
    """
    target = normalize_name(query)
    if not target:
        return 0
    if normalize_name(candidate.get("name") or "") == target:
        return 2
    for al in candidate.get("aliases") or []:
        if normalize_name(al.get("name") or "") == target:
            return 1
    return 0


def _pick_best_candidate(client, attempts: list[str]) -> tuple[dict | None, int, str]:
    """Search each attempt; return the best candidate by (tier, score).

    Tier 2 (name equality) beats tier 1 (alias equality) beats everything;
    within a tier, higher MB score wins. Attempts are searched in order
    (whole credit first, then split parts) — a part-exact candidate always
    outranks a whole-credit substring hit, which is what pinned collab
    folders onto one member.
    """
    best: dict | None = None
    best_tier = 0
    matched_part = attempts[0]
    for attempt in attempts:
        data = client.search_artist(attempt, limit=3)
        for cand in data.get("artists", []):
            tier = _match_tier(cand, attempt)
            score = int(cand.get("score") or 0)
            if tier == 0:
                continue  # substring/fuzzy hits are not candidates
            if best is None or tier > best_tier or (tier == best_tier and score > int(best.get("score") or 0)):
                best, best_tier, matched_part = cand, tier, attempt
        if best_tier == 2 and normalize_name((best or {}).get("name") or "") == normalize_name(attempt):
            break  # stored-name exact for this attempt — as good as it gets
    return best, best_tier, matched_part


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
    separators and each part searched. The whole credit string is only a seed:
    a candidate wins by name/alias EQUALITY (normalize_name, tiered in
    _match_tier) against some attempt, never by MB score alone. Collab folders
    therefore resolve to one of their real members (or stay unresolved for
    review) instead of latching onto whichever member MB's score liked.
    """
    _ensure_resolutions_table(conn)
    artists = untagged_artists(conn, limit=max_artists)
    proposed, no_match = 0, 0
    re_proposed = 0
    decisions_preserved: dict[str, int] = {}
    unresolved: list[str] = []
    processed: list[str] = []
    for artist in artists:
        name = artist["name"] or ""
        if not name:
            continue
        processed.append(name)

        existing = conn.execute(
            "SELECT status FROM mb_resolutions WHERE folder_artist = ?", (name,)
        ).fetchone()
        if existing is not None:
            re_proposed += 1
            key = existing["status"]
            decisions_preserved[key] = decisions_preserved.get(key, 0) + 1

        parts = [p for p in split_credit(name) if normalize_name(p) != normalize_name(name)]
        best, best_tier, matched_part = _pick_best_candidate(client, [name] + parts)

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

        if not mbid:
            name_quality = "no MB candidate"
        elif best_tier == 2 and normalize_name(mb_name or "") == normalize_name(name):
            name_quality = confidence_note(mb_name or "", name)
        elif best_tier == 2:
            # equality against one split part (collab folder member)
            name_quality = confidence_note(mb_name or "", name, matched_part)
        else:  # tier 1: the query (whole or a part) equals one of the candidate's aliases
            alias_hit = next(
                (
                    al.get("name")
                    for al in ((best or {}).get("aliases") or [])
                    if normalize_name(al.get("name") or "") == normalize_name(matched_part)
                ),
                None,
            )
            if alias_hit is None:
                name_quality = "fuzzy"
            else:
                kind = "alias" if normalize_name(matched_part) == normalize_name(name) else "part"
                name_quality = f"{kind} match ('{alias_hit}')"

        if mbid and folder_album_titles:
            overlap = _title_overlap(folder_album_titles, rg_titles)
            evidence = (
                f"name match ({name_quality}); album evidence: {', '.join(sorted(overlap))}"
                if overlap
                else f"name match ({name_quality}) but no album evidence — candidate's MB "
                "release-groups share no title with the folder's albums"
            )
            status = "proposed" if overlap else "needs_review"
        elif mbid:
            evidence = (
                f"name match only ({name_quality}); "
                "folder has no album titles to cross-check"
            )
            status = "proposed"
        else:
            evidence = "no MB candidate"
            status = "proposed"

        if not mbid:
            confidence = "none"
            no_match += 1
            unresolved.append(name)
        elif best_tier == 2 and normalize_name(mb_name or "") == normalize_name(name):
            confidence = "exact" if mb_name == name else "case-insensitive"
            proposed += 1
        else:
            # alias-equality on the whole query, or equality against one split
            # part: real match, but the folder is a collab/renamed credit —
            # human review before any apply
            confidence = "fuzzy"
            status = "needs_review"
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

    rows = conn.execute(
        f"SELECT * FROM mb_resolutions WHERE folder_artist IN "
        f"({','.join('?' for _ in processed)}) ORDER BY score DESC",
        processed or ["__none__"],
    ).fetchall() if processed else []
    return {
        "artists_considered": len(artists),
        "proposals_stored": proposed,
        "re_proposed_existing": re_proposed,
        "decisions_preserved": decisions_preserved,
        "no_mb_match": no_match,
        "unresolved_artists": unresolved,
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
            for r in rows
        ],
    }


def commit(client, conn, batch_size: int = 10) -> dict:
    """Apply confident proposals into the index (artists + tracks tables).

    client: unused at apply time (proposals are already stored); accepted for
    interface symmetry with propose().

    One MBID binds to one folder-artist: a candidate MBID that is already
    applied to (or being applied to) a different folder-artist is skipped with
    a retarget hint instead of silently merging two library artists into one
    entity (2026-09-21: 'Larry June;The Alchemist' and 'Armand Hammer;The
    Alchemist' both latching onto The Alchemist). Deliberate merges go through
    mb_apply(action='retarget').
    """
    _ensure_resolutions_table(conn)
    rows = conn.execute(
        "SELECT folder_artist, proposed_mbid, proposed_name, confidence, score "
        "FROM mb_resolutions WHERE status = 'proposed' ORDER BY score DESC"
    ).fetchall()

    applied: list[dict] = []
    skipped: list[dict] = []
    taken: dict[str, str] = {}
    for row in rows:
        name = row["folder_artist"]
        if row["confidence"] not in ("exact", "case-insensitive") or (row["score"] or 0) < AUTO_APPLY_SCORE:
            skipped.append({"artist": name, "reason": "low confidence"})
            continue
        mbid = row["proposed_mbid"]
        bound_elsewhere = conn.execute(
            "SELECT artist FROM tracks WHERE artist_mbid = ? AND artist IS NOT NULL AND artist != ? LIMIT 1",
            (mbid, name),
        ).fetchone()
        if name in taken.values() or mbid in taken:
            skipped.append(
                {
                    "artist": name,
                    "reason": f"mbid {mbid} already bound to another folder-artist this run "
                    f"('{taken.get(mbid, '?')}') — retarget to merge deliberately",
                }
            )
            continue
        if bound_elsewhere:
            skipped.append(
                {
                    "artist": name,
                    "reason": f"mbid {mbid} already applied to tracks of "
                    f"'{bound_elsewhere['artist']}' — retarget to merge deliberately",
                }
            )
            continue
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
        taken[mbid] = name
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
