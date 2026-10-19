"""Reservation arithmetic and donation-status derivation.

Pure, framework-free, and dependency-free. Quantity maths uses
``decimal.Decimal`` so the reservation invariant (Σ held ≤ total) cannot be
broken by binary floating-point drift (e.g. ``0.1 + 0.2``). Everything here
operates on a single donation's claims; units are never converted.

This module is the single source of truth for:
* how much of a donation is reserved / collected / remaining, and
* the donation-status precedence over mixed claim states.

In M1 these functions are available and fully tested, but are not yet wired
into the live claim/collection flow (that happens in M2).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .models import Claim, ClaimStatus, DonationStatus


def _dec(value: object) -> Decimal:
    """Convert via str so float inputs don't carry binary drift into maths."""
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class DonationAvailability:
    """Quantity breakdown for one donation (all in the donation's unit)."""

    total: float
    reserved: float      # held by RESERVED claims (awaiting pickup)
    collected: float     # confirmed by COLLECTED claims
    remaining: float     # total − reserved − collected (never negative)


def available_quantity(total: float, claims: Iterable[Claim]) -> DonationAvailability:
    """Compute reserved / collected / remaining from a donation's claims.

    Only RESERVED and COLLECTED claims consume quantity; CANCELLED and EXPIRED
    release it. Reserved and collected are summed **separately** and never
    merged, so reserved food is never reported as collected.
    """
    reserved = Decimal(0)
    collected = Decimal(0)
    for c in claims:
        if c.status is ClaimStatus.RESERVED:
            reserved += _dec(c.quantity_value)
        elif c.status is ClaimStatus.COLLECTED:
            collected += _dec(c.quantity_value)
    total_d = _dec(total)
    remaining = total_d - reserved - collected
    if remaining < 0:
        remaining = Decimal(0)  # defensive clamp; the invariant forbids this
    return DonationAvailability(
        total=float(total_d), reserved=float(reserved),
        collected=float(collected), remaining=float(remaining),
    )


def held_by_recipient(claims: Iterable[Claim], recipient_id: str) -> float:
    """Quantity one recipient holds from a donation: its RESERVED plus
    COLLECTED claims (cancelled/expired release theirs). Counts toward that
    recipient's per-donation capacity ceiling."""
    held = Decimal(0)
    for c in claims:
        if c.recipient_id == recipient_id and c.status in (
            ClaimStatus.RESERVED, ClaimStatus.COLLECTED
        ):
            held += _dec(c.quantity_value)
    return float(held)


def derive_donation_status(
    total: float,
    claims: Iterable[Claim],
    *,
    now: datetime,
    deadline: datetime,
    closed_to_claims: bool,
) -> DonationStatus:
    """Derive the donation's listing status from its claims.

    Precedence (CANCELLED > COLLECTED > EXPIRED > RESERVED > AVAILABLE),
    split by whether the donor has closed the listing to new claims:

    * Closed: nothing committed → CANCELLED; else fully collected → COLLECTED;
      else pending past deadline → EXPIRED; else RESERVED.
    * Open: fully collected → COLLECTED (outranks expiry); else past deadline
      → EXPIRED; else fully reserved → RESERVED; else AVAILABLE.

    A donation with any collected quantity is never labelled CANCELLED, and a
    fully-collected donation stays COLLECTED even after its deadline.
    """
    claims = list(claims)
    avail = available_quantity(total, claims)
    reserved = _dec(avail.reserved)
    collected = _dec(avail.collected)
    remaining = _dec(avail.remaining)

    if closed_to_claims:
        if reserved == 0 and collected == 0:
            return DonationStatus.CANCELLED          # (a) nothing committed
        if reserved == 0:                            # (c) collected, none pending
            return DonationStatus.COLLECTED
        if now > deadline:                           # (b)/(d) pending past deadline
            return DonationStatus.EXPIRED
        return DonationStatus.RESERVED               # (b)/(d) pending, in time

    if remaining == 0 and reserved == 0:             # fully collected
        return DonationStatus.COLLECTED
    if now > deadline:                               # listing lapsed
        return DonationStatus.EXPIRED
    if remaining == 0 and reserved > 0:              # fully reserved
        return DonationStatus.RESERVED
    return DonationStatus.AVAILABLE                  # remaining > 0, claimable
