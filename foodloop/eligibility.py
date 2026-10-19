"""Recipient eligibility for a donation.

These are the HARD constraints that must hold before a recipient may claim
a donation (and, later, before a recipient is even considered by the
matching algorithm). Keeping them here — pure, framework-free, and separate
from both the repository and the scoring logic — means the claim path and
the matching path apply exactly the same rules, with no duplication.

Each check records a human-readable result so the reasoning can be shown to
users (supporting the "explainable matching" requirement in M4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal

from .models import (
    WEEKDAYS,
    Donation,
    DonationStatus,
    Recipient,
)


@dataclass(frozen=True, slots=True)
class Check:
    """One eligibility criterion's outcome."""

    name: str
    passed: bool
    detail: str


@dataclass(slots=True)
class EligibilityResult:
    """Aggregated outcome of all eligibility checks."""

    eligible: bool
    checks: list[Check] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        """Human-readable reasons the recipient is NOT eligible."""
        return [c.detail for c in self.checks if not c.passed]


def can_collect_before(
    recipient: Recipient, deadline: datetime, now: datetime
) -> bool:
    """True if the recipient has a collection window between ``now`` and the
    deadline.

    Walks each calendar day in the window and checks whether the recipient's
    operating hours on one of its collection days overlap the remaining time
    before the deadline. A recipient with no declared collection days cannot
    collect (missing availability is treated as "cannot", never guessed).
    """
    if deadline <= now:
        return False
    if not recipient.collection_days:
        return False

    day = now.date()
    end_day = deadline.date()
    while day <= end_day:
        if WEEKDAYS[day.weekday()] in recipient.collection_days:
            midnight = datetime.combine(day, time(0, 0))
            win_start = midnight + timedelta(hours=recipient.collection_start_hour)
            win_end = midnight + timedelta(hours=recipient.collection_end_hour)
            # Overlap of the operating window with the remaining [now, deadline].
            lo = max(win_start, now)
            hi = min(win_end, deadline)
            if lo < hi:
                return True
        day += timedelta(days=1)
    return False


def capacity_fits(
    recipient: Recipient, donation: Donation
) -> tuple[bool, bool]:
    """Return ``(fits, comparable)`` for taking the *whole listing*.

    Capacity is a rough kg-equivalent ceiling. We only compare it when the
    donation is itself measured in kg; for countable units (loaves, items,
    trays, ...) a kg comparison would require assumptions we cannot justify,
    so we report ``comparable=False`` and do not treat it as a hard failure.
    Used by matching as a soft signal; the hard claim gate is
    :func:`capacity_check`, which judges the requested quantity.
    """
    from .models import Unit

    if recipient.max_quantity_kg_equiv is None:
        return True, False
    if donation.quantity.unit is Unit.KG:
        return donation.quantity.value <= recipient.max_quantity_kg_equiv, True
    return True, False


def capacity_headroom(
    recipient: Recipient, donation: Donation, already_held: float = 0.0
) -> float | None:
    """How much more (kg) the recipient may take from this donation, or None
    when capacity does not apply (no ceiling, or a non-kg unit).

    Capacity is a ceiling *per donation* (see ``Recipient``): what this
    recipient already holds from the same donation (reserved or collected)
    counts against it; other recipients' claims and other donations do not.
    """
    _, comparable = capacity_fits(recipient, donation)
    if not comparable:
        return None
    left = Decimal(str(recipient.max_quantity_kg_equiv)) - Decimal(str(already_held))
    return float(max(left, Decimal(0)))


def capacity_check(
    recipient: Recipient, donation: Donation, *,
    quantity: float | None = None, already_held: float = 0.0,
) -> Check:
    """The capacity criterion. With ``quantity=None`` it asks whether the
    recipient can take *any* of the listing (partial claims allowed); with a
    quantity it asks whether that request fits the remaining headroom."""
    headroom = capacity_headroom(recipient, donation, already_held)
    if headroom is None:
        return Check("capacity", True,
                     "Capacity not directly comparable for this unit (not a blocker).")
    cap = recipient.max_quantity_kg_equiv
    if quantity is None:
        fits = headroom > 0
        return Check(
            "capacity", fits,
            f"Can take up to ~{headroom:g} kg of this listing "
            f"(capacity ~{cap:g} kg per donation)." if fits
            else f"Already holds its ~{cap:g} kg capacity from this listing.",
        )
    fits = Decimal(str(quantity)) <= Decimal(str(headroom))
    return Check(
        "capacity", fits,
        f"Request of {quantity:g} kg is within capacity (~{cap:g} kg per donation)."
        if fits
        else f"Request of {quantity:g} kg exceeds capacity: up to ~{headroom:g} kg "
             f"more of ~{cap:g} kg per donation.",
    )


def check_eligibility(
    recipient: Recipient, donation: Donation, *, now: datetime,
    quantity: float | None = None, already_held: float = 0.0,
) -> EligibilityResult:
    """Run every hard eligibility check and aggregate the result.

    ``quantity`` / ``already_held`` only affect the capacity criterion (see
    :func:`capacity_check`); omitted, it asks whether any amount can be taken.
    """
    checks: list[Check] = []

    # 1. Donation must be open to claims.
    is_available = donation.status is DonationStatus.AVAILABLE
    checks.append(Check(
        "availability", is_available,
        "Donation is available for claiming." if is_available
        else f"Donation is '{donation.status.value}', not available to claim.",
    ))

    # 2. Recipient must be verified.
    checks.append(Check(
        "verified", recipient.verified,
        "Recipient organisation is verified." if recipient.verified
        else "Recipient organisation is not yet verified.",
    ))

    # 3. Recipient must accept this food category.
    accepts = donation.category in recipient.accepted_categories
    checks.append(Check(
        "category", accepts,
        f"Accepts {donation.category.value.replace('_', ' ')}." if accepts
        else f"Does not accept {donation.category.value.replace('_', ' ')}.",
    ))

    # 4. Recipient must be able to store the food as required.
    can_store = donation.storage in recipient.storage_capabilities
    checks.append(Check(
        "storage", can_store,
        f"Can store {donation.storage.value} food." if can_store
        else f"Cannot store {donation.storage.value} food.",
    ))

    # 5. Recipient must be able to collect before the deadline.
    can_collect = can_collect_before(recipient, donation.collection_deadline, now)
    checks.append(Check(
        "deadline", can_collect,
        "Can collect before the deadline." if can_collect
        else "Has no collection window before the deadline.",
    ))

    # 6. Capacity — only enforced when comparable (see capacity_check).
    checks.append(capacity_check(
        recipient, donation, quantity=quantity, already_held=already_held,
    ))

    eligible = all(c.passed for c in checks)
    return EligibilityResult(eligible=eligible, checks=checks)
