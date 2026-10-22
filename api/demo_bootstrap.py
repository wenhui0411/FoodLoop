"""Opt-in creation of a fresh, synthetic demo database for hosted demos.

Used only when ``FOODLOOP_DEMO_BOOTSTRAP=1`` (see ``api/server.py``). It creates
a NEW SQLite file at the requested path, seeds the synthetic demo data from
``data/seed.py`` and adds a few listings with deadlines relative to now, so a
free host with an ephemeral disk always starts with claimable food.

Safety: it never opens, overwrites or seeds an existing file, and never the
repository's ``data/foodloop.db``. Everything goes through the normal
Repository / FoodLoopService paths, so all validation still applies.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from data.seed import ensure_seeded
from foodloop.repository import Repository
from foodloop.workflow import FoodLoopService

REPO_DB = Path(__file__).resolve().parents[1] / "data" / "foodloop.db"

_SIDECARS = ("-wal", "-shm", "-journal")


def demo_listings(now: datetime) -> list[dict]:
    """Listings for the demo walkthrough. Deadlines are three days out at
    16:00, so every seeded recipient has a collection window before them
    whatever weekday the server starts on."""
    deadline = (now + timedelta(days=3)).replace(hour=16, minute=0, second=0, microsecond=0)
    return [
        dict(donor_id="don-bakery", title="Day-old bread loaves", category="bakery",
             description="Unsold sourdough and wholemeal loaves from today.",
             quantity_value=20, quantity_unit="loaves", area="Central", storage="ambient",
             allergens=["gluten"], allergens_declared=True, collection_deadline=deadline),
        dict(donor_id="don-super", title="Bananas and papaya", category="fresh_produce",
             description="Ripe fruit with cosmetic blemishes.",
             quantity_value=15, quantity_unit="kg", area="Market Square", storage="ambient",
             allergens=[], allergens_declared=True, collection_deadline=deadline),
        dict(donor_id="don-super", title="Yoghurt cups", category="dairy",
             description="Short-dated plain yoghurt cups.",
             quantity_value=24, quantity_unit="items", area="Market Square",
             storage="refrigerated", allergens=["dairy"], allergens_declared=True,
             best_before=now + timedelta(days=5), collection_deadline=deadline),
        dict(donor_id="don-rest", title="Vegetable curry meal trays", category="prepared_meals",
             description="Trays from today's lunch service, chilled after cooking.",
             quantity_value=6, quantity_unit="trays", area="Eastgate", storage="refrigerated",
             allergens_declared=False, collection_deadline=deadline),
    ]


def bootstrap_demo_db(path: str | Path, now: datetime | None = None) -> Path:
    """Create and seed a new demo database at ``path``; return its resolved path.

    Raises ``RuntimeError`` (and writes nothing) if ``path`` is the
    repository's real database or already exists. If seeding fails part-way,
    the partial file is removed so the next start begins clean.
    """
    target = Path(path).resolve()
    if target == REPO_DB.resolve():
        raise RuntimeError("Refusing to bootstrap data/foodloop.db; use a separate demo path.")
    if target.exists() or any(Path(f"{target}{s}").exists() for s in _SIDECARS):
        raise RuntimeError(f"Refusing to bootstrap over an existing database: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now()
    try:
        service = FoodLoopService(Repository(target), clock=lambda: now)
        ensure_seeded(service.repo, now)
        for fields in demo_listings(now):
            service.create_donation(**fields)
    except BaseException:
        for leftover in (target, *(Path(f"{target}{s}") for s in _SIDECARS)):
            leftover.unlink(missing_ok=True)
        raise
    return target
