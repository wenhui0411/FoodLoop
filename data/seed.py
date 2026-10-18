"""Synthetic seed data for the FoodLoop demo.

IMPORTANT: every donor, recipient, donation, and outcome below is made up
for demonstration. None of it represents real businesses, organisations,
partnerships, or actual food-rescue impact. The UI surfaces this clearly
(a "Demo data" label) so demo metrics are never mistaken for real impact.

The data is intentionally seeded into several lifecycle states (available,
claimed, collected, expired) so dashboards, impact, and insights have
realistic shapes to display.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from foodloop.models import (
    Donation,
    DonationStatus,
    Donor,
    DonorType,
    FoodCategory,
    Location,
    Quantity,
    Recipient,
    RecipientType,
    StorageRequirement,
    Unit,
)
from foodloop.repository import Repository

# A small, fictional township with approximate relative coordinates so
# distance-based matching has something to work with. Coordinates are
# illustrative, not surveyed.
_AREAS: dict[str, tuple[float, float]] = {
    "Central": (-26.2041, 28.0473),
    "Riverside": (-26.1950, 28.0600),
    "Hillview": (-26.2200, 28.0350),
    "Market Square": (-26.2000, 28.0450),
    "Eastgate": (-26.2100, 28.0700),
}


def _loc(area: str) -> Location:
    lat, lon = _AREAS[area]
    return Location(area=area, latitude=lat, longitude=lon)


def build_donors() -> list[Donor]:
    return [
        Donor("don-bakery", "Sunrise Bakery", DonorType.BAKERY, _loc("Central"),
              "surplus@sunrisebakery.example"),
        Donor("don-cafe", "Corner Cafe", DonorType.CAFE, _loc("Riverside"),
              "hello@cornercafe.example"),
        Donor("don-super", "FreshMart Supermarket", DonorType.SUPERMARKET,
              _loc("Market Square"), "donations@freshmart.example"),
        Donor("don-rest", "Thandi's Kitchen", DonorType.RESTAURANT, _loc("Eastgate"),
              "info@thandiskitchen.example"),
        Donor("don-grocer", "Green Grocer", DonorType.GROCER, _loc("Hillview"),
              "produce@greengrocer.example"),
    ]


def build_recipients() -> list[Recipient]:
    return [
        Recipient(
            "rec-hope", "Hope Food Bank", RecipientType.FOOD_BANK, _loc("Central"),
            accepted_categories={
                FoodCategory.BAKERY, FoodCategory.FRESH_PRODUCE, FoodCategory.DAIRY,
                FoodCategory.PANTRY, FoodCategory.BEVERAGES,
            },
            storage_capabilities={
                StorageRequirement.AMBIENT, StorageRequirement.REFRIGERATED,
            },
            max_quantity_kg_equiv=120,
            collection_days={"Mon", "Tue", "Wed", "Thu", "Fri"},
            collection_start_hour=8, collection_end_hour=17,
            verified=True, contact_email="collect@hopefoodbank.example",
        ),
        Recipient(
            "rec-ubuntu", "Ubuntu Community Kitchen", RecipientType.SOUP_KITCHEN,
            _loc("Riverside"),
            accepted_categories={
                FoodCategory.PREPARED_MEALS, FoodCategory.FRESH_PRODUCE,
                FoodCategory.BAKERY, FoodCategory.MEAT,
            },
            storage_capabilities={
                StorageRequirement.AMBIENT, StorageRequirement.REFRIGERATED,
                StorageRequirement.FROZEN,
            },
            max_quantity_kg_equiv=40,
            collection_days={"Mon", "Wed", "Fri", "Sat"},
            collection_start_hour=9, collection_end_hour=19,
            verified=True, contact_email="kitchen@ubuntu.example",
        ),
        Recipient(
            "rec-shelter", "Safe Haven Shelter", RecipientType.SHELTER,
            _loc("Hillview"),
            accepted_categories={
                FoodCategory.BAKERY, FoodCategory.PANTRY, FoodCategory.BEVERAGES,
                FoodCategory.FRESH_PRODUCE,
            },
            storage_capabilities={StorageRequirement.AMBIENT},
            max_quantity_kg_equiv=60,
            collection_days={"Tue", "Thu", "Sat", "Sun"},
            collection_start_hour=10, collection_end_hour=20,
            verified=True, contact_email="help@safehaven.example",
        ),
        Recipient(
            "rec-little", "Little Ones Creche", RecipientType.COMMUNITY_ORG,
            _loc("Eastgate"),
            accepted_categories={
                FoodCategory.DAIRY, FoodCategory.FRESH_PRODUCE, FoodCategory.BAKERY,
            },
            storage_capabilities={
                StorageRequirement.AMBIENT, StorageRequirement.REFRIGERATED,
            },
            max_quantity_kg_equiv=25,
            collection_days={"Mon", "Tue", "Wed", "Thu", "Fri"},
            collection_start_hour=7, collection_end_hour=16,
            verified=True, contact_email="care@littleones.example",
        ),
        # Intentionally UNVERIFIED — used to demonstrate eligibility gating.
        Recipient(
            "rec-pending", "New Horizons (pending verification)",
            RecipientType.CHARITY, _loc("Market Square"),
            accepted_categories={FoodCategory.BAKERY, FoodCategory.PANTRY},
            storage_capabilities={StorageRequirement.AMBIENT},
            max_quantity_kg_equiv=30,
            collection_days={"Mon", "Fri"},
            verified=False, contact_email="contact@newhorizons.example",
        ),
    ]


def build_donations(now: datetime) -> list[tuple[Donation, str]]:
    """Return (donation, outcome) pairs.

    ``outcome`` is one of "available", "claimed:<recipient_id>",
    "collected:<recipient_id>", or "expired" and tells the seeder how to
    drive each donation through the real workflow so invariants hold.
    """

    def deadline(days: float, hour: int = 16) -> datetime:
        d = (now + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
        return d

    rows: list[tuple[Donation, str]] = [
        # --- Available listings -----------------------------------------
        (Donation(
            "dn-bread", "don-bakery", "Assorted bread & rolls",
            "End-of-day loaves, rolls and buns. Best eaten within a day.",
            FoodCategory.BAKERY, Quantity(30, Unit.LOAVES), _loc("Central"),
            deadline(1), now, storage=StorageRequirement.AMBIENT,
            allergens=["gluten"], allergens_declared=True,
            handling_notes="Keep in paper bags, away from direct sun.",
        ), "available"),
        (Donation(
            "dn-veg", "don-grocer", "Mixed vegetables",
            "Carrots, spinach, tomatoes and onions — cosmetically imperfect but good.",
            FoodCategory.FRESH_PRODUCE, Quantity(18, Unit.KG), _loc("Hillview"),
            deadline(2), now, storage=StorageRequirement.AMBIENT,
            allergens=[], allergens_declared=True,
            handling_notes="Store cool and dry.",
        ), "available"),
        (Donation(
            "dn-milk", "don-super", "Short-dated milk",
            "Fresh milk approaching best-before. Must stay refrigerated.",
            FoodCategory.DAIRY, Quantity(24, Unit.LITRES), _loc("Market Square"),
            deadline(1, hour=12), now, storage=StorageRequirement.REFRIGERATED,
            allergens=["dairy"], allergens_declared=True,
            best_before=now + timedelta(days=2),
            handling_notes="Maintain cold chain; do not leave at room temperature.",
        ), "available"),
        (Donation(
            "dn-meals", "don-rest", "Prepared meal trays",
            "Cooked rice-and-vegetable trays, chilled after service.",
            FoodCategory.PREPARED_MEALS, Quantity(5, Unit.TRAYS), _loc("Eastgate"),
            deadline(1, hour=11), now, storage=StorageRequirement.REFRIGERATED,
            allergens=["soy"], allergens_declared=True,
            handling_notes="Reheat thoroughly before serving.",
        ), "available"),

        # --- Claimed (committed, not yet collected) ---------------------
        (Donation(
            "dn-pastry", "don-cafe", "Pastries & muffins",
            "Croissants, muffins and scones from the morning bake.",
            FoodCategory.BAKERY, Quantity(40, Unit.ITEMS), _loc("Riverside"),
            deadline(1, hour=15), now, storage=StorageRequirement.AMBIENT,
            allergens=["gluten", "dairy", "eggs"], allergens_declared=True,
            handling_notes="Best same day.",
        ), "claimed:rec-ubuntu"),
        (Donation(
            "dn-canned", "don-super", "Canned goods assortment",
            "Beans, tinned tomatoes and lentils — shelf stable.",
            FoodCategory.PANTRY, Quantity(45, Unit.ITEMS), _loc("Market Square"),
            deadline(5), now, storage=StorageRequirement.AMBIENT,
            allergens=[], allergens_declared=True,
        ), "claimed:rec-shelter"),

        # --- Collected (confirmed successes) ----------------------------
        (Donation(
            "dn-fruit", "don-grocer", "Seasonal fruit",
            "Apples, bananas and oranges.",
            FoodCategory.FRESH_PRODUCE, Quantity(22, Unit.KG), _loc("Hillview"),
            deadline(-1), now - timedelta(days=2), storage=StorageRequirement.AMBIENT,
            allergens=[], allergens_declared=True,
        ), "collected:rec-hope"),
        (Donation(
            "dn-rolls", "don-bakery", "Bread rolls",
            "Soft rolls from yesterday's over-bake.",
            FoodCategory.BAKERY, Quantity(50, Unit.ITEMS), _loc("Central"),
            deadline(-1), now - timedelta(days=3), storage=StorageRequirement.AMBIENT,
            allergens=["gluten"], allergens_declared=True,
        ), "collected:rec-shelter"),
        (Donation(
            "dn-yoghurt", "don-super", "Yoghurt tubs",
            "Dairy yoghurt, short dated.",
            FoodCategory.DAIRY, Quantity(30, Unit.ITEMS), _loc("Market Square"),
            deadline(-2), now - timedelta(days=4), storage=StorageRequirement.REFRIGERATED,
            allergens=["dairy"], allergens_declared=True,
        ), "collected:rec-hope"),

        # --- Expired (listed but never collected — NOT a success) -------
        (Donation(
            "dn-salad", "don-cafe", "Prepared salads",
            "Mixed salads that were not collected in time.",
            FoodCategory.PREPARED_MEALS, Quantity(8, Unit.ITEMS), _loc("Riverside"),
            deadline(-1, hour=10), now - timedelta(days=1, hours=6),
            storage=StorageRequirement.REFRIGERATED,
            allergens=["eggs"], allergens_declared=True,
        ), "expired"),
    ]
    return rows


# Canonical ids of the synthetic demo donations. Lets the insights view
# distinguish seed data from real, user-created listings. User listings get
# uuid-based ids (see FoodLoopService.create_donation) that cannot collide
# with these fixed slugs. Derived from build_donations so the two can never
# drift — the ids do not depend on the reference time passed in.
SEED_DONATION_IDS: frozenset[str] = frozenset(
    donation.id for donation, _ in build_donations(datetime(2000, 1, 1))
)


def is_seeded(repo: Repository) -> bool:
    """True if the repository already contains data.

    Used to avoid re-seeding (which would clash with the fixed demo IDs)
    and, crucially, to avoid overwriting anything a user created during a
    session on a persistent database.
    """
    return bool(repo.list_donors())


def ensure_seeded(repo: Repository, now: datetime) -> bool:
    """Seed the demo data only if the repository is empty.

    Idempotent: safe to call on every app start / Streamlit rerun. Returns
    True if seeding ran, False if the repository was already populated (in
    which case existing — possibly user-created — data is left untouched).
    """
    if is_seeded(repo):
        return False
    seed_repository(repo, now)
    return True


def seed_repository(repo: Repository, now: datetime) -> None:
    """Populate ``repo`` with the synthetic demo data in realistic states.

    Donations are driven through the genuine repository workflow
    (create -> claim -> confirm) so that persisted invariants (e.g. a
    collected donation has a stamped claim) always hold.
    """
    for donor in build_donors():
        repo.add_donor(donor)
    for recipient in build_recipients():
        repo.add_recipient(recipient)

    for i, (donation, outcome) in enumerate(build_donations(now)):
        repo.add_donation(donation)
        if outcome == "available":
            continue
        if outcome == "expired":
            repo.set_donation_status(donation.id, DonationStatus.EXPIRED)
            continue

        action, _, recipient_id = outcome.partition(":")
        claim_time = donation.created_at + timedelta(hours=2)
        # Whole-listing reservation (quantity=None -> all remaining). ``now`` is
        # the historical claim moment so the deadline check passes for the
        # back-dated collected/claimed demo rows.
        repo.create_claim(
            claim_id=f"clm-{i}", donation_id=donation.id,
            recipient_id=recipient_id, claimed_at=claim_time,
            quantity=None, now=claim_time,
        )
        if action == "collected":
            collected_at = claim_time + timedelta(hours=3)
            repo.confirm_collection(f"clm-{i}", collected_at, now=collected_at)
