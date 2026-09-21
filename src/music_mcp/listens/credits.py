"""LB credit-string splitting, name normalization, and owned-name resolution.

ListenBrainz (via Pano Scrobbler) stores artist credits as display strings:
'Earl Sweatshirt, SURF GANG', 'JPEGMAFIA & Danny Brown', 'Nia Archives feat.
Jorja Smith'. Ownership tests must split these into parts and match any part
against the library's owned names — exact whole-string equality produced a
misleading gap list (Phase 2 real-data finding).

Tier 1 (this module) is script-safe string normalization: NFKD fold, casefold,
punctuation/whitespace collapse. It bridges real mismatches found in the dogfood
run (U+2010 hyphen vs ASCII 'hi-posi', 'LEE HI' vs 'LeeHi') and ensemble-suffix
subsets ('Lelio Luttazzi' vs 'Lelio Luttazzi Trio') WITHOUT transliterating —
高中正义 vs Masayoshi Takanaka is deliberately NOT bridged here (no string trick
can do that safely; mangling non-Latin names is worse than a false gap).

Tier 2 is the mb_resolutions table (music_mcp.mb.resolver): name→MBID proposals
from the alias-aware MB search. Gap analysis consults it to classify
owned-but-unresolved artists instead of listing them as purchase targets.

Known limitation (documented, accepted): splitting is naive, so a band whose
NAME contains a separator ('Simon & Garfunkel') splits into wrong parts.
Whole-string match runs first, which covers owned-band cases.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata

_CREDIT_SEPARATORS = re.compile(
    r"\s*(?:,|;|&|×|\bfeat\.?:?\b|\bft\.?:?\b|\bfeaturing\b|\bwith\b)\s*",
    re.IGNORECASE,
)


def normalize_name(name: str) -> str:
    """Script-safe name normalization for ownership matching.

    NFKD fold + casefold + keep only alphanumeric characters (unicode-aware:
    kanji, accented Latin, etc. all survive as themselves). Punctuation,
    whitespace, and hyphen variants (U+2010 vs ASCII '-') vanish, so 'hi‐posi'
    == 'hi-posi' and 'LEE HI' == 'LeeHi'. Non-Latin scripts pass through
    untranslated by design (no transliteration).
    """
    if not name:
        return ""
    folded = unicodedata.normalize("NFKD", name)
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return "".join(ch for ch in stripped if ch.isalnum()).casefold()


def split_credit(credit: str) -> list[str]:
    """Split an LB artist credit string into individual artist-name parts."""
    parts = [p.strip(" .-") for p in _CREDIT_SEPARATORS.split(credit)]
    parts = [p for p in parts if p]
    return parts or ([credit.strip()] if credit.strip() else [])


def owned_name_index(conn: sqlite3.Connection) -> set[str]:
    """Normalized set of every artist name the library knows: canonical artist
    names plus all track-level credit strings (folder-derived fallbacks included)."""
    names: set[str] = set()
    for row in conn.execute("SELECT DISTINCT name FROM artists WHERE name IS NOT NULL"):
        names.add(normalize_name(row["name"]))
    for row in conn.execute("SELECT DISTINCT artist FROM tracks WHERE artist IS NOT NULL"):
        names.add(normalize_name(row["artist"]))
    return names


def resolve_lb_credit(credit: str, owned: set[str]) -> dict:
    """Is this LB credit an owned artist?

    Whole-string match first (normalized), then any split part. As a final
    string tier, an owned name that is a strict multi-word extension of the
    credit counts as a match ('Lelio Luttazzi' ↔ 'Lelio Luttazzi Trio').
    """
    full = normalize_name(credit)
    if full in owned:
        return {"owned": True, "matched_via": [credit.strip()], "parts": [credit.strip()]}
    parts = split_credit(credit)
    matched = sorted({p for p in parts if normalize_name(p) in owned})
    if matched:
        return {"owned": True, "matched_via": matched, "parts": parts}

    # ensemble-suffix tier: an owned name that extends the credit with more
    # words ('lelioluttazzitrio' = 'lelioluttazzi' + 'trio'). Requires the owned
    # name to be longer AND the credit to be multi-word, so a bare 'Simon' never
    # claims ownership of Simon & Garfunkel.
    if " " in credit.strip():
        for owned_name in owned:
            if len(owned_name) > len(full) and owned_name.startswith(full):
                return {
                    "owned": True,
                    "matched_via": [credit.strip()],
                    "parts": parts,
                    "matched_owned_name": owned_name,
                }
    return {"owned": False, "matched_via": [], "parts": parts}


def resolver_mbid_index(conn: sqlite3.Connection) -> dict[str, str]:
    """artist name (normalized) → proposed MBID from mb_resolutions.

    Only confident proposals count: exact/case-insensitive confidence in
    'proposed' or 'applied' status.
    """
    out: dict[str, str] = {}
    for row in conn.execute(
        "SELECT folder_artist, proposed_name, proposed_mbid, confidence, status FROM mb_resolutions"
    ):
        if row["confidence"] not in ("exact", "case-insensitive"):
            continue
        if row["status"] not in ("proposed", "applied"):
            continue
        key = normalize_name(row["proposed_name"] or row["folder_artist"])
        if key:
            out[key] = row["proposed_mbid"]
    return out
