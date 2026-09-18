"""mb_* report primitives over the MB client (cache-backed)."""

from __future__ import annotations

from music_mcp.mb.client import MBClient


def mb_artist(client: MBClient, artist_mbid: str) -> dict:
    data = client.lookup_artist(artist_mbid, inc="tags")
    return {
        "artist_mbid": data.get("id"),
        "name": data.get("name"),
        "disambiguation": data.get("disambiguation"),
        "type": data.get("type"),
        "country": data.get("country"),
        "begin": data.get("life-span", {}).get("begin"),
        "end": data.get("life-span", {}).get("end"),
        "tags": [t.get("name") for t in (data.get("tags") or [])[:10]],
    }


def mb_artist_releases(client: MBClient, artist_mbid: str) -> dict:
    groups = client.release_groups(artist_mbid)
    releases = [
        {
            "release_group_mbid": rg.get("id"),
            "title": rg.get("title"),
            "first_release_date": rg.get("first-release-date"),
            "primary_type": rg.get("primary-type"),
            "secondary_types": rg.get("secondary-types") or [],
        }
        for rg in groups
    ]
    releases.sort(key=lambda r: r["first_release_date"] or "")
    return {"artist_mbid": artist_mbid, "release_groups": releases, "count": len(releases)}


def mb_search(client: MBClient, query: str, limit: int = 5) -> dict:
    data = client.search_artist(query, limit=limit)
    return {
        "query": query,
        "results": [
            {
                "artist_mbid": a.get("id"),
                "name": a.get("name"),
                "score": a.get("score"),
                "disambiguation": a.get("disambiguation"),
            }
            for a in data.get("artists", [])
        ],
        "count": len(data.get("artists", [])),
    }


def mb_lookup(client: MBClient, mbid: str, entity: str = "artist") -> dict:
    data = client.get(f"/{entity}/{mbid}")
    return {"entity": entity, "mbid": data.get("id"), "payload": data}
