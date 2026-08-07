"""The home page at `/` -- the front door, mounted with the debug pages.

Same off-switch as every debug surface: `debug=False` removes it, writes are
rejected, and the document carries the zero-external-request property the
brain page's tests already pin for `/debug`.
"""

from __future__ import annotations

import httpx
from synapse_providers import FakeProvider

from synapse_service.api import build_app


def _client(provider, **kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(provider, **kw)),
                             base_url="http://svc")


async def test_home_page_is_served_at_the_root() -> None:
    async with _client(FakeProvider(scripts=[])) as client:
        r = await client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert 'id="sessions"' in r.text
        assert 'href="/debug"' in r.text
        assert 'href="/debug/log"' in r.text


async def test_home_page_is_absent_when_debug_is_disabled() -> None:
    async with _client(FakeProvider(scripts=[]), debug=False) as client:
        assert (await client.get("/")).status_code == 404


async def test_home_page_rejects_writes() -> None:
    async with _client(FakeProvider(scripts=[])) as client:
        assert (await client.post("/", json={})).status_code == 405


async def test_home_page_makes_zero_external_requests() -> None:
    # No CDN, no font, no image: the page can be opened on a demo machine
    # with no network and still be itself.
    async with _client(FakeProvider(scripts=[])) as client:
        body = (await client.get("/")).text
    assert "<script src" not in body
    assert "http://" not in body
    assert "https://" not in body
    assert 'href="data:,"' in body
