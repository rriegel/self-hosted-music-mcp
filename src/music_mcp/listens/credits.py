"""LB credit-string splitting and owned-name resolution.

ListenBrainz (via Pano Scrobbler) stores artist credits as display strings:
'Earl Sweatshirt, SURF GANG', 'JPEGMAFIA & Danny Brown', 'Nia Archives feat.
Jorja Smith'. Ownership tests must split these into parts and match any part
against the library's owned names (case-insensitive) — exact whole-string
equality produced a misleading gap list (Phase 2 real-data finding).

Known limitation (documented, accepted for Phase 2): splitting is naive, so a
band whose NAME contains a separator ('Simon & Garfunkel') splits into wrong
parts. Whole-string match runs first, which covers owned-band cases; a
not-owned 'Simon & Garfunkel' paired with owned 'Simon' would misclassify.
The Phase 3 MB resolver (authoritative artist credits/relations) replaces
this heuristic.
"""

from __future__ import annotations

import re
import sqlite3

_CREDIT_SEPARATORS = re.compile(
    r"\s*(?:,|;|&|×|\bfeat\.?:?\b|\bft\.?:?\b|\bfeaturing\b|\bwith\b)\s*",
    re.IGNORECASE,
)


def split_credit(credit: str) -> list[str]:
    """Split an LB artist credit string into individual artist-name parts."""
    parts = [p.strip(" .-") for p in _CREDIT_SEPARATORS.split(credit)]
    parts = [p for p in parts if p]
    return parts or ([credit.strip()] if credit.strip() else [])


def owned_name_index(conn: sqlite3.Connection) -> set[str]:
    """Lowercased set of every artist name the library knows: canonical artist
    names plus all track-level credit strings (folder-derived fallbacks included)."""
    names: set[str] = set()
    for row in conn.execute("SELECT DISTINCT name FROM artists WHERE name IS NOT NULL"):
        names.add(row["name"].lower())
    for row in conn.execute("SELECT DISTINCT artist FROM tracks WHERE artist IS NOT NULL"):
        names.add(row["artist"].lower())
    return names


def resolve_lb_credit(credit: str, owned: set[str]) -> dict:
    """Is this LB credit an owned artist? Whole-string match first, then any part."""
    full = credit.strip().lower()
    if full in owned:
        return {"owned": True, "matched_via": [credit.strip()], "parts": [credit.strip()]}
    parts = split_credit(credit)
    matched = sorted({p for p in parts if p.lower() in owned})
    return {"owned": bool(matched), "matched_via": matched, "parts": parts}
