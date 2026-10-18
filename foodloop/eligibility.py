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
    """Return ``(fits, comparable)`` for the recipient's capacity.

    Capacity is a rough kg-equivalent ceiling. We only compare it when the
    donation is itself measured in kg; for countable units (loaves, items,
    trays, ...) a kg comparison would require assumptions we cannot justify,
    so we report ``comparable=False`` and do not treat it as a hard failure.
    Matching (M4) can still use this as a soft signal.
    """
    from .models import Unit

    if recipient.max_quantity_kg_equiv is None:
        return True, False
    if donation.quantity.unit is Unit.KG:
        return donation.quantity.value <= recipient.max_quantity_kg_equiv, True
    return True, False


def check_eligibility(
    recipient: Recipient, donation: Donation, *, now: datetime
) -> EligibilityResult:
    """Run every hard eligibility check and aggregate the result."""
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

    # 6. Capacity — only enforced when comparable (see capacity_fits).
    fits, comparable = capacity_fits(recipient, donation)
    if comparable:
        checks.append(Check(
            "capacity", fits,
            f"Within capacity (~{recipient.max_quantity_kg_equiv:g} kg)." if fits
            else f"Exceeds capacity (~{recipient.max_quantity_kg_equiv:g} kg).",
        ))
    else:
        checks.append(Check(
            "capacity", True,
            "Capacity not directly comparable for this unit (not a blocker).",
        ))

    eligible = all(c.passed for c in checks)
    return EligibilityResult(eligible=eligible, checks=checks)
