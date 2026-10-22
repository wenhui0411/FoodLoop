"""Local HTTP API for the React (Vite) demo frontend.

A thin Starlette adapter over :class:`~foodloop.workflow.FoodLoopService`.
Every route calls the existing service/repository and translates domain
objects into the JSON shapes the React client already uses (camelCase,
epoch-millisecond timestamps). No business rules live here: eligibility,
atomic partial-quantity claims and single-use pickup codes stay in the core.

The acting organisation is a demo persona sent by the client — NOT
authentication. Intended for local demonstration only.

Run from the repository root, against a demo database copy::

    $env:FOODLOOP_DB = "<path to a demo copy>\\foodloop_demo.db"
    .venv\\Scripts\\python -m uvicorn api.server:app --host 127.0.0.1 --port 8000

Two opt-in settings, both off unless set, exist for a hosted demo:

* ``FOODLOOP_WEB_DIR`` — also serve the built React app from this folder, from
  the same origin as ``/api``, with deep links (e.g. ``/listings/x``) falling
  back to ``index.html``.
* ``FOODLOOP_DEMO_BOOTSTRAP=1`` — if ``FOODLOOP_DB`` does not exist, create and
  seed a new synthetic demo database there (``api/demo_bootstrap.py``).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from api.demo_bootstrap import bootstrap_demo_db
from data.seed import SEED_DONATION_IDS
from foodloop import impact, insights
from foodloop.errors import FoodLoopError, NotFoundError, ValidationError
from foodloop.matching import haversine_km
from foodloop.models import (
    COMMON_ALLERGENS,
    Claim,
    ClaimStatus,
    Donation,
    DonationStatus,
    Donor,
    DonorType,
    FoodCategory,
    Location,
    Recipient,
    RecipientType,
    StorageRequirement,
    Unit,
    WEEKDAYS,
)
from foodloop.repository import Repository
from foodloop.reservation import DonationAvailability
from foodloop.validation import is_whole_unit
from foodloop.workflow import FoodLoopService

log = logging.getLogger(__name__)

_REPO_DB = Path(__file__).resolve().parents[1] / "data" / "foodloop.db"

# --------------------------------------------------------------------------
# Vocabulary mapping: FoodLoop enums -> the React app's ids (display only).
# React crashes on an org type it does not know, so every type maps to one.
# --------------------------------------------------------------------------
_DONOR_TYPE = {
    DonorType.BAKERY: "restaurant",
    DonorType.RESTAURANT: "restaurant",
    DonorType.CAFE: "restaurant",
    DonorType.OTHER: "restaurant",
    DonorType.SUPERMARKET: "supermarket",
    DonorType.GROCER: "supermarket",
}
_CATEGORY = {
    FoodCategory.BAKERY: "baked",
    FoodCategory.FRESH_PRODUCE: "produce",
    FoodCategory.DAIRY: "dairy",
    FoodCategory.MEAT: "raw",
    FoodCategory.PANTRY: "packaged",
    FoodCategory.BEVERAGES: "packaged",
    FoodCategory.OTHER: "packaged",
}
_DONATION_STATUS = {
    DonationStatus.AVAILABLE: "available",
    DonationStatus.CLAIMED: "reserved",
    DonationStatus.RESERVED: "reserved",
    DonationStatus.COLLECTED: "completed",
    DonationStatus.CANCELLED: "withdrawn",
    DonationStatus.EXPIRED: "expired",
}
_CLAIM_STATUS = {
    ClaimStatus.RESERVED: "confirmed",
    ClaimStatus.COLLECTED: "collected",
    ClaimStatus.CANCELLED: "cancelled",
    ClaimStatus.EXPIRED: "expired",
}


def _ms(dt: datetime | None) -> int | None:
    return None if dt is None else int(dt.timestamp() * 1000)


def _label(value: str) -> str:
    return value.replace("_", " ").title()   # as the Streamlit UI labels enums


def _camel(data: dict) -> dict:
    """Top-level snake_case keys -> camelCase (nested keys such as unit names
    or dates are data, not field names, and are left alone)."""
    return {re.sub(r"_([a-z])", lambda m: m.group(1).upper(), k): v for k, v in data.items()}


def _category_id(d: Donation) -> str:
    if d.category is FoodCategory.PREPARED_MEALS:
        return "cooked_hot" if d.storage is StorageRequirement.AMBIENT else "cooked_chilled"
    return _CATEGORY[d.category]


def _storage_note(d: Donation) -> str:
    """Donor-declared storage and handling, always non-empty, so React never
    falls back to its own per-category handling text. Allergen status is sent
    separately (``allergensDeclared``) so "not stated" is never shown as "none"."""
    parts = [f"Storage: {d.storage.value} (declared by the donor)."]
    if d.best_before is not None:
        parts.append(f"Best before {d.best_before:%d %b %Y}.")
    if d.handling_notes:
        parts.append(d.handling_notes)
    return " ".join(parts)


def _distance(loc: Location, viewer: Location | None) -> float | None:
    km = haversine_km(loc, viewer) if viewer is not None else None
    return None if km is None else round(km, 1)


# --------------------------------------------------------------------------
# JSON views
# --------------------------------------------------------------------------
def _donor_json(d: Donor, viewer: Location | None = None) -> dict:
    return {
        "id": d.id, "name": d.name, "type": _DONOR_TYPE[d.donor_type], "role": "donor",
        "area": d.location.area, "address": d.location.area,
        "distanceKm": _distance(d.location, viewer), "accepts": None,
        "hours": "Collect before the listing deadline",
        "about": f"{d.donor_type.value.title()} in {d.location.area}.",
        "kind": _label(d.donor_type.value), "contactEmail": d.contact_email,
    }


def _recipient_json(r: Recipient, viewer: Location | None = None) -> dict:
    cats = ", ".join(sorted(c.value.replace("_", " ") for c in r.accepted_categories))
    return {
        "id": r.id, "name": r.name,
        "type": "food_bank" if r.recipient_type is RecipientType.FOOD_BANK else "charity",
        "role": "recipient", "area": r.location.area, "address": r.location.area,
        "distanceKm": _distance(r.location, viewer), "accepts": "human",
        "hours": f"Collects {', '.join(d for d in WEEKDAYS if d in r.collection_days) or 'no days set'}, "
                 f"{r.collection_start_hour}:00 to {r.collection_end_hour}:00",
        "about": f"Accepts {cats or 'no categories yet'}."
                 + ("" if r.verified else " Verification pending."),
        "kind": _label(r.recipient_type.value), "contactEmail": r.contact_email,
        "verified": r.verified,
        "acceptedCategories": sorted(_label(c.value) for c in r.accepted_categories),
        "storageCapabilities": sorted(s.value for s in r.storage_capabilities),
        "capacityKg": r.max_quantity_kg_equiv,
    }


def _listing_json(
    d: Donation, avail: DonationAvailability, donor: Donor, viewer: Location | None = None
) -> dict:
    return {
        "id": d.id, "donorId": d.donor_id, "categoryId": _category_id(d),
        "category": d.category.value, "title": d.title, "description": d.description,
        "quantityTotal": d.quantity.value, "quantityAvailable": avail.remaining,
        "unit": d.quantity.unit.value, "wholeUnits": is_whole_unit(d.quantity.unit),
        "quantityKg": d.quantity.value if d.quantity.unit is Unit.KG else None,
        "allergens": list(d.allergens), "allergensDeclared": d.allergens_declared,
        "intendedUse": "human", "storageNote": _storage_note(d),
        "categoryLabel": _label(d.category.value), "area": d.location.area,
        "readyAt": _ms(d.created_at), "expiresAt": _ms(d.collection_deadline),
        "createdAt": _ms(d.created_at), "withdrawn": d.closed_to_claims, "photo": None,
        "status": _DONATION_STATUS[d.status],
        "activeQty": avail.reserved, "collectedQty": avail.collected,
        "donor": _donor_json(donor, viewer),
    }


class _Api:
    """Route handlers. Each takes plain dicts and returns JSON-ready data."""

    def __init__(self, service: FoodLoopService):
        self.svc = service
        self.repo = service.repo

    # -- helpers ----------------------------------------------------------
    def _org(self, org_id: str) -> Donor | Recipient:
        try:
            return self.repo.get_donor(org_id)
        except NotFoundError:
            pass
        try:
            return self.repo.get_recipient(org_id)
        except NotFoundError:
            raise NotFoundError("Organization not found.") from None

    def _viewer(self, org_id: str | None) -> Location | None:
        if not org_id:
            return None
        try:
            return self._org(org_id).location
        except NotFoundError:
            return None

    def _listing(self, d: Donation, viewer: Location | None = None) -> dict:
        return _listing_json(
            d, self.repo.donation_availability(d.id), self.repo.get_donor(d.donor_id), viewer
        )

    def _claim(self, c: Claim, *, show_code: bool) -> dict:
        d = self.repo.get_donation(c.donation_id)
        listing = self._listing(d)
        return {
            "id": c.id, "listingId": c.donation_id, "recipientId": c.recipient_id,
            "quantity": c.quantity_value,
            "quantityKg": c.quantity_value if d.quantity.unit is Unit.KG else None,
            "status": _CLAIM_STATUS[c.status],
            # The pickup code is the recipient's proof at handover: never sent
            # to the donor, who must type it in to confirm.
            "code": c.pickup_code if show_code else None,
            "claimedAt": _ms(c.claimed_at), "deadline": _ms(d.collection_deadline),
            "collectedAt": _ms(c.collected_at), "impact": None,
            "listing": listing, "donor": listing["donor"],
            "recipient": _recipient_json(self.repo.get_recipient(c.recipient_id)),
        }

    def _refresh(self) -> None:
        self.svc.expire_overdue()  # as Streamlit's refresh() does on each load

    # -- organisations ----------------------------------------------------
    def list_orgs(self, q: dict) -> list[dict]:
        self._refresh()
        viewer = self._viewer(q.get("viewer"))
        role, text = q.get("role", ""), q.get("query", "").strip().lower()
        open_by_donor: dict[str, int] = {}
        for d in self.repo.list_donations(status=DonationStatus.AVAILABLE):
            open_by_donor[d.donor_id] = open_by_donor.get(d.donor_id, 0) + 1
        orgs = [_donor_json(d, viewer) for d in self.repo.list_donors()]
        orgs += [_recipient_json(r, viewer) for r in self.repo.list_recipients()]
        for o in orgs:
            o["activeCount"] = open_by_donor.get(o["id"], 0)
        return [
            o for o in orgs
            if (not role or o["role"] == role)
            and (not text or text in f"{o['name']} {o['area']} {o['type']}".lower())
        ]

    def get_org(self, org_id: str, q: dict) -> dict:
        self._refresh()
        viewer = self._viewer(q.get("viewer"))
        org = self._org(org_id)
        if isinstance(org, Recipient):
            return {"org": _recipient_json(org, viewer), "listings": []}
        rows = self.repo.list_donations(status=DonationStatus.AVAILABLE, donor_id=org_id)
        listings = sorted((self._listing(d, viewer) for d in rows), key=lambda r: r["expiresAt"])
        return {"org": _donor_json(org, viewer), "listings": listings}

    # -- listings ---------------------------------------------------------
    def list_listings(self, q: dict) -> list[dict]:
        self._refresh()
        viewer = self._viewer(q.get("viewer"))
        rows = [
            self._listing(d, viewer)
            for d in self.repo.list_donations(donor_id=q.get("donorId") or None)
        ]
        scope = q.get("scope", "available")
        live = {"available", "reserved"}
        if scope == "available":
            rows = [r for r in rows if r["status"] == "available"]
        elif scope == "live":
            rows = [r for r in rows if r["status"] in live]
        elif scope == "past":
            rows = [r for r in rows if r["status"] not in live]
        if q.get("categoryId"):
            rows = [r for r in rows if r["categoryId"] == q["categoryId"]]
        if q.get("eligibleFor"):  # "Only food we can receive": the core's own checks
            rid = self.repo.get_recipient(q["eligibleFor"]).id   # NotFoundError if unknown
            rows = [r for r in rows if self.svc.check_eligibility(r["id"], rid).eligible]
        text = q.get("query", "").strip().lower()
        if text:
            rows = [r for r in rows
                    if text in f"{r['title']} {r['description']} {r['donor']['name']} {r['category']}".lower()]
        sort = "newest" if scope == "past" else q.get("sort", "soonest")
        if sort == "newest":
            rows.sort(key=lambda r: r["createdAt"], reverse=True)
        elif sort == "nearest":
            rows.sort(key=lambda r: (r["donor"]["distanceKm"] is None, r["donor"]["distanceKm"] or 0))
        else:
            rows.sort(key=lambda r: r["expiresAt"])
        return rows

    def get_listing(self, listing_id: str, q: dict) -> dict:
        self._refresh()
        viewer_id = q.get("viewer", "")
        d = self.repo.get_donation(listing_id)
        owner = d.donor_id == viewer_id
        claims = [
            self._claim(c, show_code=c.recipient_id == viewer_id)
            for c in self.repo.list_claims_for_donation(listing_id)
            if owner or c.recipient_id == viewer_id
        ]
        claims.sort(key=lambda c: c["claimedAt"], reverse=True)
        out = {"listing": self._listing(d, self._viewer(viewer_id)), "claims": claims}
        if viewer_id and any(r.id == viewer_id for r in self.repo.list_recipients()):
            # The same checks claim_donation enforces, shown before the claim.
            result = self.svc.check_eligibility(listing_id, viewer_id)
            out["eligibility"] = {
                "eligible": result.eligible,
                "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail}
                           for c in result.checks],
            }
            out["claimable"] = (
                self.svc.claimable_quantity(listing_id, viewer_id) if result.eligible else 0
            )
        return out

    def withdraw_listing(self, listing_id: str, body: dict) -> dict:
        d = self.repo.get_donation(listing_id)
        if d.donor_id != str(body.get("donorId", "")):
            raise NotFoundError("Listing not found.")
        self.svc.cancel_donation(listing_id)  # closes to new claims; keeps reservations
        return self._listing(self.repo.get_donation(listing_id))

    def create_listing(self, body: dict) -> dict:
        errors: dict[str, str] = {}

        def when(key: str, field: str, label: str) -> datetime | None:
            raw = str(body.get(key) or "").strip()
            if not raw:
                return None
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                errors[field] = f"{label} is not a valid date."
                return None

        deadline = when("collectionDeadline", "collection_deadline", "The collection deadline")
        best_before = when("bestBefore", "best_before", "Best before")
        allergens = body.get("allergens") or []
        if not isinstance(allergens, list) or any(a not in COMMON_ALLERGENS for a in allergens):
            errors["allergens"] = "Choose allergens from the list."
        if errors:
            raise ValidationError(errors)
        d = self.svc.create_donation(   # all other rules live in validation.py
            donor_id=str(body.get("donorId", "")), title=body.get("title", ""),
            description=body.get("description", ""), category=body.get("category"),
            quantity_value=body.get("quantityValue"), quantity_unit=body.get("quantityUnit"),
            area=body.get("area", ""), collection_deadline=deadline,
            storage=body.get("storage") or StorageRequirement.AMBIENT,
            allergens=allergens, allergens_declared=bool(body.get("allergensDeclared")),
            handling_notes=body.get("handlingNotes", ""), best_before=best_before,
        )
        return self._listing(d)

    def meta(self, q: dict) -> dict:
        """The backend's own vocabulary, so forms never use React's mock lists."""
        return {
            "categories": [{"id": c.value, "label": _label(c.value)} for c in FoodCategory],
            "units": [{"id": u.value, "label": u.value, "whole": is_whole_unit(u)} for u in Unit],
            "storage": [{"id": s.value, "label": _label(s.value)} for s in StorageRequirement],
            "allergens": list(COMMON_ALLERGENS),
        }

    # -- claims -----------------------------------------------------------
    def claim_listing(self, listing_id: str, body: dict) -> dict:
        if "quantity" not in body:
            raise ValidationError({"quantity": "Enter a quantity to claim."})
        claim = self.svc.claim_donation(
            listing_id, str(body.get("recipientId", "")), body["quantity"]
        )
        return self._claim(claim, show_code=True)

    def cancel_claim(self, claim_id: str, body: dict) -> dict:
        recipient_id = str(body.get("recipientId", ""))
        mine = {c.id for c in self.repo.list_claims(recipient_id=recipient_id)}
        if claim_id not in mine:
            raise NotFoundError("Claim not found.")
        self.svc.cancel_claim(claim_id)
        claim = next(c for c in self.repo.list_claims(recipient_id=recipient_id) if c.id == claim_id)
        return self._claim(claim, show_code=True)

    def collect_claim(self, body: dict) -> dict:
        claim = self.svc.confirm_handover(str(body.get("donorId", "")), str(body.get("code", "")))
        return self._claim(claim, show_code=False)

    def list_claims(self, q: dict) -> list[dict]:
        self._refresh()
        org_id, role = q.get("orgId", ""), q.get("role", "")
        # Without both, the repository would return every claim (and code).
        if not org_id or role not in ("donor", "recipient"):
            raise ValidationError({"orgId": "orgId and role (donor or recipient) are required."})
        if role == "donor":
            own = {d.id for d in self.repo.list_donations(donor_id=org_id)}
            claims = [c for c in self.repo.list_claims() if c.donation_id in own]
        else:
            claims = self.repo.list_claims(recipient_id=org_id)
        return [self._claim(c, show_code=role != "donor") for c in claims]

    # -- impact and insights (figures from foodloop.impact / .insights only) --
    def get_impact(self, q: dict) -> dict:
        self._refresh()
        org_id, role = q.get("orgId", ""), q.get("role", "")
        if role == "donor":
            self.repo.get_donor(org_id)
            return {"role": role, **_camel(asdict(impact.donor_impact(self.repo, org_id)))}
        if role == "recipient":
            self.repo.get_recipient(org_id)
            return {"role": role, **_camel(asdict(impact.recipient_impact(self.repo, org_id)))}
        raise ValidationError({"role": "role must be donor or recipient."})

    def get_insights(self, q: dict) -> dict:
        self._refresh()
        now = self.svc.now()
        period = insights.data_period(self.repo)
        return {
            "composition": _camel(asdict(insights.dataset_composition(self.repo, SEED_DONATION_IDS))),
            "period": None if period is None else [_ms(period[0]), _ms(now)],
            "overview": _camel(asdict(insights.township_overview(self.repo))),
            "observations": [asdict(o) for o in insights.observations(self.repo, now=now)],
            "areas": [_camel(asdict(a)) for a in insights.area_breakdown(self.repo)],
            "categories": [
                {**_camel(asdict(c)), "label": _label(c.category)}
                for c in insights.category_breakdown(self.repo)
            ],
            "minSample": insights.MIN_SAMPLE,
        }


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------
def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _message(exc: FoodLoopError) -> str:
    if isinstance(exc, ValidationError):
        return " ".join(exc.errors.values())
    return str(exc)


def _endpoint(handler: Callable[..., object]):
    """Wrap a handler: parse JSON, run it off the event loop (SQLite is
    blocking), and turn domain errors into ``{"error": message}`` responses."""

    async def endpoint(request: Request) -> JSONResponse:
        args: list[object] = list(request.path_params.values())
        if request.method == "POST":
            try:
                body = await request.json()
            except ValueError:
                return _error(400, "The request body must be JSON.")
            if not isinstance(body, dict):
                return _error(400, "The request body must be a JSON object.")
            args.append(body)
        else:
            args.append(dict(request.query_params))
        api: _Api = request.app.state.api
        try:
            return JSONResponse(await run_in_threadpool(getattr(api, handler.__name__), *args))
        except NotFoundError as exc:
            return _error(404, _message(exc))
        except ValidationError as exc:  # per-field messages for forms
            return JSONResponse({"error": _message(exc), "fields": exc.errors}, status_code=400)
        except FoodLoopError as exc:
            return _error(400, _message(exc))
        except Exception:  # pragma: no cover - logged, never leaked to the client
            log.exception("Unhandled API error")
            return _error(500, "Something went wrong on the server.")

    return endpoint


async def _health(request: Request) -> JSONResponse:
    # Server time and zone: collection windows are wall-clock hours, so a host
    # left on UTC would shift them (set TZ there). This makes that visible.
    now = datetime.now().astimezone()
    return JSONResponse({
        "ok": True, "serverTime": now.isoformat(timespec="seconds"),
        "timezone": now.tzname(), "utcOffsetMinutes": int(now.utcoffset().total_seconds() // 60),
    })


def service_from_env() -> FoodLoopService:
    """Build the service from ``FOODLOOP_DB``. Refuses to fall back to (or
    open) the repository's real database. A missing file is an error unless
    ``FOODLOOP_DEMO_BOOTSTRAP=1``, which creates a new seeded demo database."""
    raw = os.environ.get("FOODLOOP_DB", "").strip()
    if not raw:
        raise RuntimeError("Set FOODLOOP_DB to a demo database copy before starting the API.")
    path = Path(raw).resolve()
    if path == _REPO_DB.resolve():
        raise RuntimeError("FOODLOOP_DB points at data/foodloop.db; use a demo copy instead.")
    if not path.is_file():
        if os.environ.get("FOODLOOP_DEMO_BOOTSTRAP", "").strip() != "1":
            raise RuntimeError(f"FOODLOOP_DB does not exist: {path}")
        bootstrap_demo_db(path)
        log.warning("Created a new synthetic demo database at %s", path)
    return FoodLoopService(Repository(path))


def _web_routes(web_dir: Path) -> list[Route]:
    """Serve the built React app: real files as-is, any other non-/api path as
    index.html (client-side routes), and a 404 for missing files or /api paths."""
    index = web_dir / "index.html"

    async def serve(request: Request) -> Response:
        rel = request.path_params["path"]
        if rel == "api" or rel.startswith("api/"):
            return _error(404, "Not found.")
        target = (web_dir / rel).resolve()
        if rel and target.is_relative_to(web_dir) and target.is_file():
            # Vite fingerprints assets/, so they can be cached for good.
            cache = "public, max-age=31536000, immutable" if rel.startswith("assets/") else "no-cache"
            return FileResponse(target, headers={"Cache-Control": cache})
        if "." in rel.rsplit("/", 1)[-1]:  # a missing file, not an app route
            return PlainTextResponse("Not found.", status_code=404)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return [Route("/{path:path}", serve, methods=["GET", "HEAD"])]


def _web_dir_from_env() -> Path | None:
    raw = os.environ.get("FOODLOOP_WEB_DIR", "").strip()
    if not raw:
        return None
    web_dir = Path(raw).resolve()
    if not (web_dir / "index.html").is_file():
        raise RuntimeError(f"FOODLOOP_WEB_DIR has no index.html: {web_dir}")
    return web_dir


def create_app(service: FoodLoopService | None = None, web_dir: Path | None = None) -> Starlette:
    """Build the ASGI app. Without ``service`` it is built from the
    environment at startup, so a misconfiguration fails before serving.
    ``web_dir`` (default: ``FOODLOOP_WEB_DIR``) also serves the React build."""
    web_dir = web_dir.resolve() if web_dir is not None else _web_dir_from_env()

    @asynccontextmanager
    async def lifespan(app: Starlette):
        if not hasattr(app.state, "api"):
            app.state.api = _Api(service_from_env())
        yield

    a = _Api
    app = Starlette(
        routes=[
            Route("/api/health", _health),
            Route("/api/meta", _endpoint(a.meta)),
            Route("/api/orgs", _endpoint(a.list_orgs)),
            Route("/api/orgs/{org_id}", _endpoint(a.get_org)),
            Route("/api/listings", _endpoint(a.list_listings)),
            Route("/api/listings", _endpoint(a.create_listing), methods=["POST"]),
            Route("/api/listings/{listing_id}", _endpoint(a.get_listing)),
            Route("/api/listings/{listing_id}/claims", _endpoint(a.claim_listing), methods=["POST"]),
            Route("/api/listings/{listing_id}/withdraw", _endpoint(a.withdraw_listing), methods=["POST"]),
            Route("/api/impact", _endpoint(a.get_impact)),
            Route("/api/insights", _endpoint(a.get_insights)),
            Route("/api/claims", _endpoint(a.list_claims)),
            Route("/api/claims/collect", _endpoint(a.collect_claim), methods=["POST"]),
            Route("/api/claims/{claim_id}/cancel", _endpoint(a.cancel_claim), methods=["POST"]),
            *(_web_routes(web_dir) if web_dir is not None else []),  # last: catch-all
        ],
        lifespan=lifespan,
    )
    if service is not None:
        app.state.api = _Api(service)
    return app


app = create_app()
