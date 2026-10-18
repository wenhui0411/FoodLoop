"""Core domain models for FoodLoop.

These are plain dataclasses and enums with no persistence or UI concerns.
They describe *what* a donation/donor/recipient is; how they are stored
lives in ``repository.py`` and how they move between states lives in
``workflow.py``.

Design notes
------------
* A collection **deadline** (the latest time food can be collected) and
  food-**safety** information (handling, storage, allergens) are kept as
  separate concepts, as required by the product brief. We never infer
  safety from a deadline or vice versa.
* ``Quantity`` keeps a value and an explicit unit. We do not silently
  convert between units (e.g. loaves to kg) because that would require
  assumptions we cannot justify; matching logic handles units explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------
class DonationStatus(StrEnum):
    """Lifecycle state of a donation listing.

    Transitions are enforced centrally in ``workflow.py`` — not every
    state can follow every other state.
    """

    AVAILABLE = "available"      # listed, remaining quantity open to claims
    CLAIMED = "claimed"          # legacy: a recipient committed (pre-partial-claims)
    RESERVED = "reserved"        # fully reserved (remaining 0), pickups pending
    COLLECTED = "collected"      # all reserved quantity collected (success)
    CANCELLED = "cancelled"      # withdrawn by donor with nothing committed
    EXPIRED = "expired"          # deadline passed without full collection


#: Statuses that represent a finished listing (no further transitions).
TERMINAL_STATUSES: frozenset[DonationStatus] = frozenset(
    {DonationStatus.COLLECTED, DonationStatus.CANCELLED, DonationStatus.EXPIRED}
)


class ClaimStatus(StrEnum):
    """Lifecycle state of a single claim (reservation of a quantity).

    Introduced for partial-quantity claims: a donation may have several
    claims, each with its own quantity and state. ``RESERVED`` holds quantity
    pending pickup; the other three are terminal.
    """

    RESERVED = "reserved"        # quantity held, awaiting pickup
    COLLECTED = "collected"      # handover confirmed (success)
    CANCELLED = "cancelled"      # released before collection
    EXPIRED = "expired"          # deadline passed without collection


#: Claim statuses whose quantity still counts against the donation total.
CONSUMING_CLAIM_STATUSES: frozenset[ClaimStatus] = frozenset(
    {ClaimStatus.RESERVED, ClaimStatus.COLLECTED}
)


class FoodCategory(StrEnum):
    """Broad food categories. Drives recipient eligibility and placeholders.

    These are organisational categories, NOT food-safety classifications.
    """

    BAKERY = "bakery"
    FRESH_PRODUCE = "fresh_produce"
    DAIRY = "dairy"
    MEAT = "meat"
    PREPARED_MEALS = "prepared_meals"
    PANTRY = "pantry"            # shelf-stable / dry goods
    BEVERAGES = "beverages"
    OTHER = "other"


class Unit(StrEnum):
    """Units of measure for a donation quantity.

    Kept explicit; see ``Quantity`` for why we do not auto-convert.
    """

    KG = "kg"
    LITRES = "litres"
    ITEMS = "items"          # generic countable units
    LOAVES = "loaves"
    MEALS = "meals"
    TRAYS = "trays"


class DonorType(StrEnum):
    BAKERY = "bakery"
    RESTAURANT = "restaurant"
    CAFE = "cafe"
    SUPERMARKET = "supermarket"
    GROCER = "grocer"
    OTHER = "other"


class RecipientType(StrEnum):
    CHARITY = "charity"
    FOOD_BANK = "food_bank"
    COMMUNITY_ORG = "community_org"
    SHELTER = "shelter"
    SOUP_KITCHEN = "soup_kitchen"


class StorageRequirement(StrEnum):
    """How the food must be kept. Donor-provided, never inferred."""

    AMBIENT = "ambient"
    REFRIGERATED = "refrigerated"
    FROZEN = "frozen"


# Common allergens. Donor-provided; an empty list means "none declared",
# which is NOT the same as "allergen-free" (handled explicitly in the UI).
COMMON_ALLERGENS: tuple[str, ...] = (
    "gluten",
    "dairy",
    "eggs",
    "nuts",
    "peanuts",
    "soy",
    "fish",
    "shellfish",
    "sesame",
)

# Days of the week used for recipient collection availability.
WEEKDAYS: tuple[str, ...] = (
    "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun",
)


# --------------------------------------------------------------------------
# Value objects
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Quantity:
    """A quantity with an explicit unit.

    We intentionally do not convert between units. Comparing capacities
    across units (e.g. a recipient that can take "20 kg" vs a donation of
    "30 loaves") is handled explicitly and conservatively in matching.
    """

    value: float
    unit: Unit

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        # Show whole numbers without a trailing ".0".
        v = int(self.value) if float(self.value).is_integer() else self.value
        return f"{v} {self.unit.value}"


@dataclass(frozen=True, slots=True)
class Location:
    """A township-level location.

    Coordinates are optional. When absent, distance-based matching is
    skipped for that party rather than guessed — missing data is handled
    explicitly, never fabricated.
    """

    area: str
    latitude: float | None = None
    longitude: float | None = None

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None


# --------------------------------------------------------------------------
# Entities
# --------------------------------------------------------------------------
@dataclass(slots=True)
class Donor:
    """A business that lists surplus food."""

    id: str
    name: str
    donor_type: DonorType
    location: Location
    contact_email: str = ""


@dataclass(slots=True)
class Recipient:
    """A community organisation that can claim and collect donations.

    Eligibility and capability fields drive the explainable matching:
    * ``accepted_categories`` — what food the organisation can use.
    * ``storage_capabilities`` — whether it can handle refrigerated/frozen.
    * ``max_quantity_kg_equiv`` — a rough capacity ceiling (see matching).
    * ``collection_days`` / collection window — when it can collect.
    * ``verified`` — only verified orgs are eligible to claim.
    """

    id: str
    name: str
    recipient_type: RecipientType
    location: Location
    accepted_categories: set[FoodCategory] = field(default_factory=set)
    storage_capabilities: set[StorageRequirement] = field(
        default_factory=lambda: {StorageRequirement.AMBIENT}
    )
    # Rough capacity ceiling for a single donation, expressed in a kg-equivalent
    # for ambient comparison only. Documented as approximate in matching.
    max_quantity_kg_equiv: float | None = None
    collection_days: set[str] = field(default_factory=set)  # subset of WEEKDAYS
    collection_start_hour: int = 8    # 24h clock
    collection_end_hour: int = 18
    verified: bool = False
    contact_email: str = ""


@dataclass(slots=True)
class Donation:
    """A surplus-food listing.

    Safety-relevant fields (``storage``, ``allergens``, ``handling_notes``)
    are donor-provided and kept separate from the ``collection_deadline``.
    """

    id: str
    donor_id: str
    title: str
    description: str
    category: FoodCategory
    quantity: Quantity
    location: Location
    collection_deadline: datetime
    created_at: datetime
    status: DonationStatus = DonationStatus.AVAILABLE
    # Safety / handling — donor-declared, never inferred from an image.
    storage: StorageRequirement = StorageRequirement.AMBIENT
    allergens: list[str] = field(default_factory=list)
    allergens_declared: bool = False  # distinguishes "none" from "not stated"
    handling_notes: str = ""
    best_before: datetime | None = None  # optional, distinct from deadline
    # Donor "stop offering" flag: blocks NEW claims without touching existing
    # reservations or collected history (see withdrawal semantics).
    closed_to_claims: bool = False

    @property
    def is_open(self) -> bool:
        """True if the listing can still be acted upon."""
        return self.status not in TERMINAL_STATUSES


@dataclass(slots=True)
class Claim:
    """A recipient's reservation of a quantity from a specific donation.

    Carries the reserved ``quantity_value`` (in the donation's unit — never
    converted), a unique ``pickup_code`` for donor-side handover verification,
    and its own ``status``. (In M1 a donation still has at most one active
    claim; partial multi-claim reservation arrives in M2.)
    """

    id: str
    donation_id: str
    recipient_id: str
    claimed_at: datetime
    collected_at: datetime | None = None  # set only on confirmed collection
    quantity_value: float = 0.0
    pickup_code: str = ""
    status: ClaimStatus = ClaimStatus.RESERVED

    @property
    def is_collected(self) -> bool:
        return self.collected_at is not None
