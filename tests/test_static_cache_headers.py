"""
tests/test_static_cache_headers.py
==================================
A deploy has to reach the browser.

JS and CSS used to be served with `max-age=86400, must-revalidate`. That
header reads like "check with the server", but `must-revalidate` only
governs what a cache does once a response has gone STALE — for the first 24
hours the browser serves its copy without asking anything. A tablet in a
restaurant therefore kept running the previous release until its day
happened to roll over, which is how a fixed script went on failing in this
very app on 2026-09-23.

`no-cache` is the correct header for a file whose URL never changes: it
stays cached, and the browser revalidates before each use, so an unchanged
file costs a 304 with no body and a changed one arrives immediately.

These tests pin the distinction, because the difference between the two
headers is one word and the failure is invisible in development — where the
cache is empty and everything looks fine.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def _cache_control(resp) -> str:
    return resp.headers.get("cache-control", "").lower()


def test_scripts_are_revalidated_on_every_use(client):
    """The header that decides whether a deploy is visible today."""
    r = client.get("/static/js/mesio-utils.js")
    assert r.status_code == 200
    cc = _cache_control(r)

    assert "no-cache" in cc, f"scripts must be revalidated before use, got {cc!r}"
    # The specific regression: any positive max-age lets the browser skip
    # the revalidation entirely for that long.
    assert "max-age=0" in cc or "max-age" not in cc, (
        f"a positive max-age reintroduces the stale-tablet bug: {cc!r}"
    )


def test_stylesheets_are_revalidated_on_every_use(client):
    r = client.get("/static/css/tokens.css")
    assert r.status_code == 200
    cc = _cache_control(r)
    assert "no-cache" in cc
    assert "max-age=0" in cc or "max-age" not in cc


def test_images_are_still_cached_hard(client):
    """A stale logo is not a broken app — images keep their long cache."""
    r = client.get("/static/img/logo.png")
    assert r.status_code == 200
    assert "max-age=604800" in _cache_control(r)


def test_html_pages_do_not_leave_caching_to_the_browsers_guess(client):
    """With no Cache-Control at all, browsers invent one from Last-Modified.

    An old page keeps loading old script tags, which is the same bug one
    level up.
    """
    r = client.get("/login")
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("text/html")
    assert "no-cache" in _cache_control(r)


def test_the_service_worker_is_never_cached(client):
    """A stale service worker outlives every other kind of stale file."""
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "no-cache" in _cache_control(r)


def test_an_unchanged_script_still_answers_304(client):
    """`no-cache` must stay cheap: revalidation, not re-download.

    If this ever returns 200 with a body, every page load is paying full
    price for every asset and the header choice needs revisiting.
    """
    first = client.get("/static/js/mesio-utils.js")
    last_modified = first.headers.get("last-modified")
    assert last_modified, "StaticFiles must emit Last-Modified for revalidation to work"

    second = client.get(
        "/static/js/mesio-utils.js", headers={"If-Modified-Since": last_modified}
    )
    assert second.status_code == 304
    assert not second.content
