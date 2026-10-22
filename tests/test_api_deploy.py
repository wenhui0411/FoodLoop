"""Opt-in hosted-demo features of the API: demo database bootstrap, serving the
React build from the same origin (with SPA deep-link fallback), and startup.

Everything here is off by default; these tests also pin that the default
local workflow is unchanged (no static routes, no database creation).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

import anyio
import pytest

from api import server
from api.demo_bootstrap import REPO_DB, bootstrap_demo_db, demo_listings
from foodloop.models import DonationStatus
from foodloop.repository import Repository
from foodloop.workflow import FoodLoopService
from tests.test_api import call

INDEX = "<!doctype html><title>FoodLoop</title><div id=root></div>"


@pytest.fixture
def web_dir(tmp_path):
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text(INDEX, encoding="utf-8")
    (web / "assets" / "index-abc123.js").write_text("console.log('app')", encoding="utf-8")
    (web / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside the web folder", encoding="utf-8")
    return web


def raw_call(app, path: str, method: str = "GET"):
    """Like tests.test_api.call, but returns (status, headers, body bytes)."""
    path, _, query = path.partition("?")
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": query.encode(), "root_path": "", "headers": [],
        "client": ("127.0.0.1", 1), "server": ("testserver", 80),
    }
    sent: list[dict] = []
    requested = False

    async def receive():
        # Like a real server: the request once, then block (no disconnect) —
        # FileResponse listens here while it streams the file.
        nonlocal requested
        if requested:
            await anyio.sleep_forever()
        requested = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    anyio.run(app, scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    return start["status"], headers, b"".join(m.get("body", b"") for m in sent
                                             if m["type"] == "http.response.body")


def run_lifespan_startup(app) -> dict:
    """Drive the ASGI lifespan startup the way uvicorn does; return the reply."""
    replies: list[dict] = []

    async def go():
        queue = [{"type": "lifespan.startup"}]

        async def receive():
            if queue:
                return queue.pop(0)
            await anyio.sleep_forever()

        async def send(message):
            replies.append(message)
            if message["type"].startswith("lifespan.startup"):
                tg.cancel_scope.cancel()

        async def run_app():
            try:
                await app({"type": "lifespan", "asgi": {"version": "2.0"}}, receive, send)
            except Exception:
                pass  # after lifespan.startup.failed Starlette re-raises; uvicorn logs it

        async with anyio.create_task_group() as tg:
            tg.start_soon(run_app)

    anyio.run(go)
    return replies[0]


def sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# Demo database bootstrap
# --------------------------------------------------------------------------
def test_bootstrap_creates_and_seeds_a_new_database(tmp_path):
    target = bootstrap_demo_db(tmp_path / "deep" / "demo.db", now=datetime(2026, 10, 23, 20, 0))
    repo = Repository(target)
    titles = {d.title for d in repo.list_donations(status=DonationStatus.AVAILABLE)}
    assert {"Day-old bread loaves", "Bananas and papaya", "Yoghurt cups",
            "Vegetable curry meal trays"} <= titles
    assert repo.list_donors() and repo.list_recipients()


@pytest.mark.parametrize("start", [datetime(2026, 10, 23 + d, 21, 30) for d in range(7)])
def test_demo_walkthrough_listing_is_claimable_whatever_the_start_day(tmp_path, start):
    # The demo starts with Hope Food Bank claiming the bread; that must work on
    # any weekday the free host happens to (re)start, late in the evening too.
    target = bootstrap_demo_db(tmp_path / "demo.db", now=start)
    service = FoodLoopService(Repository(target), clock=lambda: start)
    bread = next(d for d in service.repo.list_donations() if d.title == "Day-old bread loaves")
    assert service.check_eligibility(bread.id, "rec-hope").eligible, start.strftime("%a")


def test_bootstrap_refuses_the_repository_database():
    before = sha256(REPO_DB) if REPO_DB.exists() else None
    with pytest.raises(RuntimeError, match="data/foodloop.db"):
        bootstrap_demo_db(REPO_DB)
    assert (sha256(REPO_DB) if REPO_DB.exists() else None) == before


def test_bootstrap_never_touches_an_existing_file(tmp_path):
    existing = tmp_path / "demo.db"
    existing.write_bytes(b"not a database, and must stay exactly like this")
    before = sha256(existing)
    with pytest.raises(RuntimeError, match="existing database"):
        bootstrap_demo_db(existing)
    assert sha256(existing) == before


def test_bootstrap_refuses_leftover_sqlite_sidecars(tmp_path):
    (tmp_path / "demo.db-wal").write_bytes(b"x")
    with pytest.raises(RuntimeError, match="existing database"):
        bootstrap_demo_db(tmp_path / "demo.db")
    assert not (tmp_path / "demo.db").exists()


def test_failed_bootstrap_leaves_no_partial_file(tmp_path, monkeypatch):
    import api.demo_bootstrap as boot

    monkeypatch.setattr(boot, "demo_listings", lambda now: [{"donor_id": "nobody"}])
    with pytest.raises(Exception):
        boot.bootstrap_demo_db(tmp_path / "demo.db")
    assert list(tmp_path.iterdir()) == []


def test_demo_listing_deadlines_are_relative_to_now():
    now = datetime(2026, 10, 20, 9, 0)
    assert {f["collection_deadline"] for f in demo_listings(now)} == {datetime(2026, 10, 23, 16, 0)}


# --------------------------------------------------------------------------
# service_from_env: bootstrap is opt-in and still guarded
# --------------------------------------------------------------------------
def test_missing_database_is_still_an_error_without_the_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("FOODLOOP_DB", str(tmp_path / "demo.db"))
    monkeypatch.delenv("FOODLOOP_DEMO_BOOTSTRAP", raising=False)
    with pytest.raises(RuntimeError, match="does not exist"):
        server.service_from_env()
    assert not (tmp_path / "demo.db").exists()


def test_flag_bootstraps_a_missing_database(monkeypatch, tmp_path):
    monkeypatch.setenv("FOODLOOP_DB", str(tmp_path / "demo.db"))
    monkeypatch.setenv("FOODLOOP_DEMO_BOOTSTRAP", "1")
    service = server.service_from_env()
    assert service.repo.list_donations(status=DonationStatus.AVAILABLE)


def test_flag_reuses_an_existing_database_without_reseeding(monkeypatch, tmp_path):
    target = bootstrap_demo_db(tmp_path / "demo.db")
    before = sha256(target)
    monkeypatch.setenv("FOODLOOP_DB", str(target))
    monkeypatch.setenv("FOODLOOP_DEMO_BOOTSTRAP", "1")
    server.service_from_env()
    assert sha256(target) == before


def test_flag_never_unlocks_the_repository_database(monkeypatch):
    monkeypatch.setenv("FOODLOOP_DB", str(REPO_DB))
    monkeypatch.setenv("FOODLOOP_DEMO_BOOTSTRAP", "1")
    with pytest.raises(RuntimeError, match="demo copy"):
        server.service_from_env()


# --------------------------------------------------------------------------
# Startup (ASGI lifespan, as uvicorn runs it)
# --------------------------------------------------------------------------
def test_startup_bootstraps_and_serves_api_and_app(monkeypatch, tmp_path, web_dir):
    monkeypatch.setenv("FOODLOOP_DB", str(tmp_path / "demo.db"))
    monkeypatch.setenv("FOODLOOP_DEMO_BOOTSTRAP", "1")
    monkeypatch.setenv("FOODLOOP_WEB_DIR", str(web_dir))
    app = server.create_app()
    assert run_lifespan_startup(app)["type"] == "lifespan.startup.complete"
    status, rows = call(app, "GET", "/api/listings?viewer=rec-hope")
    assert status == 200 and "Day-old bread loaves" in {r["title"] for r in rows}
    assert raw_call(app, "/listings/whatever")[2].decode() == INDEX


def test_startup_fails_fast_on_a_bad_web_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("FOODLOOP_WEB_DIR", str(tmp_path))          # no index.html
    with pytest.raises(RuntimeError, match="index.html"):
        server.create_app()


def test_startup_fails_without_database_config(monkeypatch):
    monkeypatch.delenv("FOODLOOP_DB", raising=False)
    monkeypatch.delenv("FOODLOOP_WEB_DIR", raising=False)
    reply = run_lifespan_startup(server.create_app())
    assert reply["type"] == "lifespan.startup.failed" and "FOODLOOP_DB" in reply["message"]


# --------------------------------------------------------------------------
# Static files and SPA deep-link fallback
# --------------------------------------------------------------------------
@pytest.fixture
def web_app(repo, service, now, web_dir):
    from data.seed import seed_repository

    seed_repository(repo, now)
    return server.create_app(service, web_dir=web_dir)


@pytest.mark.parametrize("path", ["/", "/login", "/browse", "/claims", "/my-listings",
                                  "/listings/dn-bread", "/orgs/rec-hope", "/insights"])
def test_app_routes_fall_back_to_index(web_app, path):
    status, headers, body = raw_call(web_app, path)
    assert status == 200 and body.decode() == INDEX
    assert headers["content-type"].startswith("text/html")
    assert headers["cache-control"] == "no-cache"


def test_built_assets_are_served_with_long_cache(web_app):
    status, headers, body = raw_call(web_app, "/assets/index-abc123.js")
    assert status == 200 and body == b"console.log('app')"
    assert "javascript" in headers["content-type"]
    assert "immutable" in headers["cache-control"]
    assert raw_call(web_app, "/favicon.svg")[0] == 200


@pytest.mark.parametrize("path", ["/assets/missing.js", "/missing.css", "/robots.txt"])
def test_missing_files_are_404_not_the_app(web_app, path):
    status, _, body = raw_call(web_app, path)
    assert status == 404 and body != INDEX.encode()


@pytest.mark.parametrize("path", ["/../secret.txt", "/assets/../../secret.txt",
                                  "/%2e%2e/secret.txt", "/..%2fsecret.txt"])
def test_no_files_outside_the_web_folder(web_app, path):
    _, _, body = raw_call(web_app, path)
    assert b"outside the web folder" not in body


def test_api_routes_win_over_the_app_and_unknown_api_paths_are_json_404(web_app):
    status, data = call(web_app, "GET", "/api/listings")
    assert status == 200 and isinstance(data, list)
    status, headers, body = raw_call(web_app, "/api/nope")
    assert status == 404 and json.loads(body) == {"error": "Not found."}


def test_without_web_dir_the_api_serves_no_pages(monkeypatch, repo, service):
    monkeypatch.delenv("FOODLOOP_WEB_DIR", raising=False)
    app = server.create_app(service)
    assert raw_call(app, "/")[0] == 404 and raw_call(app, "/listings/x")[0] == 404


def test_health_reports_server_time_and_zone(web_app):
    status, data = call(web_app, "GET", "/api/health")
    assert status == 200 and data["ok"] is True
    assert {"serverTime", "timezone", "utcOffsetMinutes"} <= set(data)
