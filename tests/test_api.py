"""HTTP API adapter (api/server.py) for the React demo frontend.

Drives the ASGI app in-process (no httpx/TestClient dependency) against an
isolated, seeded temp database with a frozen clock. Covers the live demo
workflow — browse, claim, pickup-code handover — and that the adapter only
relays the core's rules (eligibility, over-reservation, single-use codes).
"""

from __future__ import annotations

import json

import anyio
import pytest

from api.server import create_app, service_from_env
from data.seed import seed_repository

# The ids React's config/rules.js knows; anything else breaks or blanks the UI.
REACT_ORG_TYPES = {"restaurant", "hotel", "supermarket", "charity", "food_bank", "animal_farm"}
REACT_CATEGORIES = {"cooked_hot", "cooked_chilled", "baked", "produce", "dairy", "packaged", "raw"}


def call(app, method: str, path: str, body: object = None, raw: bytes | None = None):
    """Send one request through the ASGI app; return (status, parsed JSON)."""
    path, _, query = path.partition("?")
    payload = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": query.encode(), "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1), "server": ("testserver", 80),
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        sent.append(message)

    anyio.run(app, scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(data)


@pytest.fixture
def app(repo, service, now):
    seed_repository(repo, now)
    return create_app(service)


def _claim(app, recipient="rec-hope", qty=5, listing="dn-bread"):
    return call(app, "POST", f"/api/listings/{listing}/claims",
                {"recipientId": recipient, "quantity": qty})


def test_orgs_and_listings_use_react_vocabulary(app):
    status, orgs = call(app, "GET", "/api/orgs?viewer=rec-hope")
    assert status == 200
    assert {o["type"] for o in orgs} <= REACT_ORG_TYPES
    assert {o["role"] for o in orgs} == {"donor", "recipient"}
    status, rows = call(app, "GET", "/api/listings?viewer=rec-hope")
    assert status == 200 and rows
    assert {r["categoryId"] for r in rows} <= REACT_CATEGORIES
    assert all(r["status"] == "available" for r in rows)
    bread = next(r for r in rows if r["id"] == "dn-bread")
    assert bread["quantityTotal"] == 30 and bread["quantityAvailable"] == 30
    assert isinstance(bread["donor"]["distanceKm"], float)
    assert bread["expiresAt"] > bread["readyAt"]


def test_storage_and_allergen_declarations_are_passed_through(app):
    _, rows = call(app, "GET", "/api/listings")
    by_id = {r["id"]: r for r in rows}
    veg, bread = by_id["dn-veg"], by_id["dn-bread"]
    assert veg["allergensDeclared"] is True and veg["allergens"] == []   # declared none
    assert bread["allergens"] == ["gluten"]
    assert bread["storageNote"].startswith("Storage: ambient")
    assert bread["wholeUnits"] is True and veg["wholeUnits"] is False


def test_browse_claim_handover_end_to_end(app):
    status, claim = _claim(app, qty=5)
    assert status == 200
    assert claim["status"] == "confirmed" and claim["quantity"] == 5
    code = claim["code"]
    assert len(code) == 6

    _, detail = call(app, "GET", "/api/listings/dn-bread?viewer=rec-hope")
    assert detail["listing"]["quantityAvailable"] == 25          # partial claim
    assert detail["claims"][0]["code"] == code                  # recipient sees it
    _, as_donor = call(app, "GET", "/api/listings/dn-bread?viewer=don-bakery")
    assert as_donor["claims"][0]["code"] is None                # donor must type it

    status, done = call(app, "POST", "/api/claims/collect", {"code": code.lower(), "donorId": "don-bakery"})
    assert status == 200 and done["status"] == "collected" and done["collectedAt"]

    status, again = call(app, "POST", "/api/claims/collect", {"code": code, "donorId": "don-bakery"})
    assert status == 400 and "already been used" in again["error"]   # single-use

    _, history = call(app, "GET", "/api/claims?orgId=rec-hope&role=recipient")
    assert {c["id"]: c["status"] for c in history}[claim["id"]] == "collected"


def test_invalid_pickup_codes_are_rejected_without_changes(app):
    _, claim = _claim(app)
    status, err = call(app, "POST", "/api/claims/collect", {"code": "ZZZZZZ", "donorId": "don-bakery"})
    assert status == 404 and err["error"] == "No reserved pickup matches that code."
    # Another donor's valid code is reported identically.
    status, err = call(app, "POST", "/api/claims/collect", {"code": claim["code"], "donorId": "don-cafe"})
    assert status == 404 and err["error"] == "No reserved pickup matches that code."
    _, detail = call(app, "GET", "/api/listings/dn-bread?viewer=rec-hope")
    assert detail["claims"][0]["status"] == "confirmed"


@pytest.mark.parametrize("recipient, qty, message", [
    ("rec-hope", 999, "exceeds the remaining"),
    ("rec-hope", 1.5, "whole units"),
    ("rec-hope", 0, "greater than zero"),
    ("rec-pending", 2, "not yet verified"),
    ("nobody", 2, "not found"),
])
def test_invalid_claims_are_rejected_and_nothing_is_reserved(app, recipient, qty, message):
    status, err = _claim(app, recipient=recipient, qty=qty)
    assert status in (400, 404) and message in err["error"]
    _, detail = call(app, "GET", "/api/listings/dn-bread?viewer=don-bakery")
    assert detail["listing"]["quantityAvailable"] == 30 and detail["claims"] == []


def test_claim_requires_a_quantity_and_a_json_body(app):
    status, err = call(app, "POST", "/api/listings/dn-bread/claims", {"recipientId": "rec-hope"})
    assert status == 400 and "quantity" in err["error"].lower()
    status, err = call(app, "POST", "/api/listings/dn-bread/claims", raw=b"not json")
    assert status == 400 and "JSON" in err["error"]


def test_cancel_only_by_the_claiming_recipient_and_releases_quantity(app):
    _, claim = _claim(app, qty=10)
    status, _ = call(app, "POST", f"/api/claims/{claim['id']}/cancel", {"recipientId": "rec-shelter"})
    assert status == 404
    status, cancelled = call(app, "POST", f"/api/claims/{claim['id']}/cancel", {"recipientId": "rec-hope"})
    assert status == 200 and cancelled["status"] == "cancelled"
    _, detail = call(app, "GET", "/api/listings/dn-bread?viewer=rec-hope")
    assert detail["listing"]["quantityAvailable"] == 30


def test_donor_sees_claims_on_own_listings_without_codes(app):
    _, claim = _claim(app)
    _, rows = call(app, "GET", "/api/claims?orgId=don-bakery&role=donor")
    assert claim["id"] in {r["id"] for r in rows}               # plus seeded history
    assert all(r["code"] is None for r in rows)
    assert {r["listing"]["donorId"] for r in rows} == {"don-bakery"}
    _, live = call(app, "GET", "/api/listings?donorId=don-bakery&scope=live")
    assert [r["id"] for r in live] == ["dn-bread"]


def test_service_from_env_refuses_unsafe_databases(monkeypatch, tmp_path):
    from api.server import _REPO_DB

    monkeypatch.delenv("FOODLOOP_DB", raising=False)
    with pytest.raises(RuntimeError, match="Set FOODLOOP_DB"):
        service_from_env()
    monkeypatch.setenv("FOODLOOP_DB", str(_REPO_DB))
    with pytest.raises(RuntimeError, match="demo copy"):
        service_from_env()
    missing = tmp_path / "missing.db"
    monkeypatch.setenv("FOODLOOP_DB", str(missing))
    with pytest.raises(RuntimeError, match="does not exist"):
        service_from_env()
    assert not missing.exists()                                 # never created


# --------------------------------------------------------------------------
# Claim history scoping, eligibility preview, withdrawal, create, impact,
# insights — each relays an existing service/impact/insights function.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("query", ["", "?orgId=rec-hope", "?orgId=&role=recipient", "?orgId=rec-hope&role=x"])
def test_claim_history_requires_org_and_role(app, query):
    _claim(app)
    status, err = call(app, "GET", f"/api/claims{query}")
    assert status == 400 and "required" in err["error"]       # never every code


def test_listing_detail_shows_the_cores_eligibility_to_recipients(app):
    _, ok = call(app, "GET", "/api/listings/dn-bread?viewer=rec-hope")
    assert ok["eligibility"]["eligible"] is True and ok["claimable"] == 30
    _, no = call(app, "GET", "/api/listings/dn-bread?viewer=rec-pending")
    failed = [c["name"] for c in no["eligibility"]["checks"] if not c["passed"]]
    assert no["eligibility"]["eligible"] is False and "verified" in failed and no["claimable"] == 0
    _, donor = call(app, "GET", "/api/listings/dn-bread?viewer=don-bakery")
    assert "eligibility" not in donor


def test_only_food_we_can_receive_uses_eligibility(app):
    _, hope = call(app, "GET", "/api/listings?eligibleFor=rec-hope")
    _, pending = call(app, "GET", "/api/listings?eligibleFor=rec-pending")
    assert "dn-bread" in {r["id"] for r in hope}
    assert "dn-bread" not in {r["id"] for r in pending}
    status, _ = call(app, "GET", "/api/listings?eligibleFor=nobody")
    assert status == 404


def test_withdraw_closes_to_new_claims_and_keeps_reservations(app):
    _, claim = _claim(app, qty=5)
    status, _ = call(app, "POST", "/api/listings/dn-bread/withdraw", {"donorId": "don-cafe"})
    assert status == 404                                         # not the owner
    status, listing = call(app, "POST", "/api/listings/dn-bread/withdraw", {"donorId": "don-bakery"})
    assert status == 200 and listing["withdrawn"] is True and listing["status"] != "available"
    _, detail = call(app, "GET", "/api/listings/dn-bread?viewer=rec-hope")
    assert detail["claims"][0]["status"] == "confirmed"          # reservation kept
    status, _ = _claim(app, recipient="rec-shelter", qty=1)
    assert status == 400                                         # closed to new claims
    status, done = call(app, "POST", "/api/claims/collect", {"code": claim["code"], "donorId": "don-bakery"})
    assert status == 200 and done["status"] == "collected"


def _new_listing(**over):
    body = {
        "donorId": "don-bakery", "title": "Fresh scones", "description": "", "category": "bakery",
        "quantityValue": "12", "quantityUnit": "items", "area": "Central",
        "collectionDeadline": "2026-01-16T15:00", "storage": "ambient",
        "allergens": ["gluten", "dairy"], "allergensDeclared": True,
        "bestBefore": "", "handlingNotes": "Boxed.",
    }
    return {**body, **over}


def test_create_listing_goes_through_core_validation(app):
    status, listing = call(app, "POST", "/api/listings", _new_listing())
    assert status == 200 and listing["status"] == "available" and listing["quantityTotal"] == 12
    assert listing["allergens"] == ["gluten", "dairy"] and listing["allergensDeclared"] is True
    _, rows = call(app, "GET", "/api/listings")
    assert listing["id"] in {r["id"] for r in rows}


@pytest.mark.parametrize("over, field", [
    ({"title": ""}, "title"),
    ({"collectionDeadline": "2026-01-14T10:00"}, "collection_deadline"),   # in the past
    ({"collectionDeadline": ""}, "collection_deadline"),
    ({"collectionDeadline": "soon"}, "collection_deadline"),
    ({"quantityValue": "2.5"}, "quantity_value"),                        # whole units
    ({"category": "snacks"}, "category"),
    ({"allergens": ["gluten"], "allergensDeclared": False}, "allergens"),
    ({"allergens": ["glitter"]}, "allergens"),
])
def test_invalid_listing_reports_fields_and_persists_nothing(app, over, field):
    _, before = call(app, "GET", "/api/listings?scope=all")
    status, err = call(app, "POST", "/api/listings", _new_listing(**over))
    assert status == 400 and field in err["fields"]
    _, after = call(app, "GET", "/api/listings?scope=all")
    assert len(after) == len(before)


def test_meta_exposes_backend_vocabulary(app):
    _, meta = call(app, "GET", "/api/meta")
    assert {"id": "prepared_meals", "label": "Prepared Meals"} in meta["categories"]
    assert {u["id"]: u["whole"] for u in meta["units"]}["loaves"] is True
    assert "gluten" in meta["allergens"]


def test_impact_counts_confirmed_collections_in_original_units(app):
    _, before = call(app, "GET", "/api/impact?orgId=rec-hope&role=recipient")
    _, claim = _claim(app, qty=5)
    _, mid = call(app, "GET", "/api/impact?orgId=rec-hope&role=recipient")
    assert mid["activeClaims"] == before["activeClaims"] + 1
    assert mid["confirmedCollections"] == before["confirmedCollections"]   # reserved != rescued
    call(app, "POST", "/api/claims/collect", {"code": claim["code"], "donorId": "don-bakery"})
    _, after = call(app, "GET", "/api/impact?orgId=rec-hope&role=recipient")
    assert after["confirmedCollections"] == before["confirmedCollections"] + 1
    assert after["collectedByUnit"]["loaves"] == before["collectedByUnit"].get("loaves", 0) + 5
    assert after["rateDenominator"] == before["rateDenominator"] + 1
    assert after["pickupRate"] == after["confirmedCollections"] / after["rateDenominator"]
    assert {"expiredClaims", "cancelledClaims"} <= set(after)
    _, donor = call(app, "GET", "/api/impact?orgId=don-bakery&role=donor")
    assert {"collectionRate", "rateDenominator", "organisationsHelped"} <= set(donor)
    assert not {"meals", "co2e", "kg"} & (set(after) | set(donor))        # nothing invented
    status, _ = call(app, "GET", "/api/impact?orgId=rec-hope")
    assert status == 400


def test_insights_relays_township_summaries(app):
    status, data = call(app, "GET", "/api/insights")
    assert status == 200
    _, rows = call(app, "GET", "/api/listings?scope=all")
    assert data["composition"]["total"] == len(rows)
    assert {"availableListings", "reservedClaims", "collectedClaims"} <= set(data["overview"])
    assert all({"title", "detail", "counts"} <= set(o) for o in data["observations"])
    assert data["minSample"] >= 1
