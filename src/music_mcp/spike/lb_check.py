"""Spike: verify ListenBrainz API access — listens, top stats, similar artists.

Public endpoints work unauthenticated for public data; the auth token is only
needed for private data. This probe tries public first and reports what works.

Usage: uv run python -m music_mcp.spike.lb_check [--user rriegel]
Writes findings JSON to stdout (plus a machine-readable file via --out).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from urllib.request import Request, urlopen

LB_BASE = "https://api.listenbrainz.org/1"
UA = "self-hosted-music-mcp-spike/0.0.1 (https://github.com/rriegel; hermes@riegelmedia.com)"


def lb_get(path: str, token: str | None = None) -> dict | int:
    """GET a LB endpoint. Returns parsed JSON, or the HTTP status code on failure."""
    headers = {"User-Agent": UA}
    if token:
        headers["Authorization"] = f"Token {token}"
    try:
        req = Request(f"{LB_BASE}{path}", headers=headers)
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001 - spike: any failure is a finding
        code = getattr(exc, "code", None)
        return int(code) if code else -1


def summarize(payload: dict) -> dict:
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default="rriegel")
    parser.add_argument("--out", default=None, help="also write JSON to this path")
    args = parser.parse_args()

    token = os.environ.get("LB_TOKEN")

    findings: dict = {"user": args.user, "checked_at": datetime.now(UTC).isoformat()}

    # 1. Public listens (no auth) — how many MBIDs ride along?
    listens = lb_get(f"/user/{args.user}/listens?count=100")
    if isinstance(listens, int):
        findings["public_listens"] = {"status": listens}
    else:
        events = listens.get("payload", {}).get("listens", [])
        with_mbid = sum(
            1 for e in events if e.get("track_metadata", {}).get("mbids")
        )
        findings["public_listens"] = {
            "status": 200,
            "count": len(events),
            "newest_ts": listens.get("payload", {}).get("latest_listen_ts"),
            "with_mbids": with_mbid,
            "sample": [
                {
                    "track": e.get("track_metadata", {}).get("track_name"),
                    "artist": e.get("track_metadata", {}).get("artist_name"),
                    "mbids": e.get("track_metadata", {}).get("mbids", [])[:3],
                }
                for e in events[:5]
            ],
        }

    # 2. Top artists, all-time (public stat)
    top = lb_get(f"/stats/user/{args.user}/artists?range=all_time&count=20")
    if isinstance(top, int):
        findings["top_artists"] = {"status": top}
    else:
        artists = top.get("payload", {}).get("artists", [])
        findings["top_artists"] = {
            "status": 200,
            "count": len(artists),
            "with_mbid": sum(1 for a in artists if a.get("mbid")),
            "sample": [
                {"name": a.get("artist_name"), "mbid": a.get("mbid"), "listens": a.get("listen_count")}
                for a in artists[:5]
            ],
        }

    # 3. Similar artists (public) — feeds gap analysis later
    top_artists = findings.get("top_artists")
    sample = top_artists.get("sample") if isinstance(top_artists, dict) else None  # type: ignore[union-attr]
    seed_mbid = sample[0].get("mbid") if sample else None
    if seed_mbid:
        sim = lb_get(f"/popularity/similar-artists?artist_mbids={seed_mbid}")
        findings["similar_artists"] = (
            {"status": sim} if isinstance(sim, int) else {"status": 200, "payload_keys": list(sim)[:6]}
        )
    else:
        findings["similar_artists"] = {"status": "skipped: no seed mbid"}

    # 4. Auth check — only if a token is present (validates private-data access)
    if token:
        validate = lb_get("/validate-token", token=token)
        findings["token_validate"] = (
            {"status": validate} if isinstance(validate, int) else validate.get("valid", None)
        )
    else:
        findings["token_validate"] = "no LB_TOKEN env var — skipped"

    print(json.dumps(findings, indent=1))
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(findings, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
