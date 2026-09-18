"""ListenBrainz client tests via respx (no network)."""

from __future__ import annotations

import pytest
import respx
from httpx import Response

from music_mcp.listens.client import UA, LBClient

LB = "https://api.listenbrainz.org/1"


def _listen(ts: int, artist: str, track: str) -> dict:
    return {"listened_at": ts, "track_metadata": {"artist_name": artist, "track_name": track}}


@respx.mock
def test_iter_all_listens_paginates_backward():
    respx.get(f"{LB}/user/tester/listens").side_effect = [
        Response(200, json={"payload": {"listens": [_listen(300, "A", "3"), _listen(200, "A", "2")]}}),
        Response(200, json={"payload": {"listens": [_listen(100, "B", "1")]}}),
        Response(200, json={"payload": {"listens": []}}),
    ]
    client = LBClient(user="tester")
    got = [
        (ln["listened_at"], ln["track_metadata"]["artist_name"]) for ln in client.iter_all_listens()
    ]
    assert got == [(300, "A"), (200, "A"), (100, "B")]


@respx.mock
def test_iter_all_listens_stops_at_since_ts():
    respx.get(f"{LB}/user/tester/listens").mock(
        return_value=Response(
            200, json={"payload": {"listens": [_listen(300, "A", "3"), _listen(50, "B", "old")]}}
        )
    )
    client = LBClient(user="tester")
    got = list(client.iter_all_listens(since_ts=100))
    assert got == [_listen(300, "A", "3")]  # 50 is below the since boundary: stop


@respx.mock
def test_retry_on_5xx_then_success():
    route = respx.get(f"{LB}/user/tester/listens").mock(
        return_value=Response(200, json={"payload": {"listens": [_listen(1, "A", "t")]}})
    )
    client = LBClient(user="tester")
    # first response 503, then real one
    route.side_effect = [Response(503), route.return_value]
    got = list(client.iter_all_listens(stop_after_pages=1))
    assert len(got) == 1


@respx.mock
def test_user_agent_sent():
    respx.get(f"{LB}/user/tester/listens").mock(
        return_value=Response(200, json={"payload": {"listens": []}})
    )
    LBClient(user="tester").iter_all_listens()
    assert UA.startswith("self-hosted-music-mcp/")


@respx.mock
def test_top_artists_shape():
    respx.get(f"{LB}/stats/user/tester/artists").mock(
        return_value=Response(
            200, json={"payload": {"artists": [{"artist_name": "A", "listen_count": 10}]}}
        )
    )
    artists = LBClient(user="tester").top_artists()
    assert artists[0]["listen_count"] == 10


def test_missing_user_raises():
    with pytest.raises(ValueError):
        LBClient()
