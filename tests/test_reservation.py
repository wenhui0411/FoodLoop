"""Tests for reservation maths and the donation-status precedence (M1).

Covers availability (reserved/collected/remaining kept separate), the
float-safe comparison, and the full status truth-table including the four
donor-withdrawal cases and the mixed-state example.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from foodloop.models import ClaimStatus, Claim, DonationStatus
from foodloop.reservation import available_quantity, derive_donation_status

NOW = datetime(2026, 1, 15, 9, 0, 0)
FUTURE = NOW + timedelta(days=1)
PAST = NOW - timedelta(hours=1)


def _claim(status: ClaimStatus, qty: float, i: int = 0) -> Claim:
    return Claim(
        id=f"c{i}", donation_id="dn1", recipient_id="r",
        claimed_at=NOW, quantity_value=qty, status=status,
    )


def _status(total, claims, *, now=NOW, deadline=FUTURE, closed=False):
    return derive_donation_status(
        total, claims, now=now, deadline=deadline, closed_to_claims=closed
    )


# --------------------------------------------------------------------------
# Availability: reserved and collected are summed separately
# --------------------------------------------------------------------------
def test_availability_separates_reserved_and_collected():
    claims = [
        _claim(ClaimStatus.COLLECTED, 3, 1),
        _claim(ClaimStatus.RESERVED, 2, 2),
        _claim(ClaimStatus.CANCELLED, 4, 3),   # released
        _claim(ClaimStatus.EXPIRED, 1, 4),      # released
    ]
    a = available_quantity(10, claims)
    assert (a.reserved, a.collected, a.remaining) == (2, 3, 5)
    # reserved is NOT folded into collected
    assert a.collected == 3


def test_availability_float_safe_no_overclaim_drift():
    # 0.1 + 0.2 must equal exactly 0.3 of reserved -> remaining 0, not -epsilon.
    claims = [_claim(ClaimStatus.RESERVED, 0.1, 1), _claim(ClaimStatus.RESERVED, 0.2, 2)]
    a = available_quantity(0.3, claims)
    assert a.remaining == 0.0
    assert a.reserved == pytest.approx(0.3)


# --------------------------------------------------------------------------
# Withdrawal cases (Detail 1: a, b, c, d)
# --------------------------------------------------------------------------
def test_withdraw_a_no_claims_is_cancelled():
    assert _status(10, [], closed=True) is DonationStatus.CANCELLED


def test_withdraw_b_pending_reservations_is_reserved():
    claims = [_claim(ClaimStatus.RESERVED, 4, 1)]
    assert _status(10, claims, closed=True) is DonationStatus.RESERVED


def test_withdraw_c_collected_no_pending_is_collected():
    claims = [_claim(ClaimStatus.COLLECTED, 6, 1)]
    assert _status(10, claims, closed=True) is DonationStatus.COLLECTED


def test_withdraw_d_collected_and_pending_is_reserved():
    claims = [_claim(ClaimStatus.COLLECTED, 3, 1), _claim(ClaimStatus.RESERVED, 2, 2)]
    assert _status(10, claims, closed=True) is DonationStatus.RESERVED


def test_withdraw_never_cancels_when_collected_history_exists():
    # Any collected claim means NOT cancelled, even if nothing is pending.
    claims = [_claim(ClaimStatus.COLLECTED, 1, 1)]
    assert _status(10, claims, closed=True) is not DonationStatus.CANCELLED


def test_withdraw_pending_past_deadline_expires_but_keeps_collected():
    claims = [_claim(ClaimStatus.COLLECTED, 3, 1), _claim(ClaimStatus.RESERVED, 2, 2)]
    # Listing lapses, but the collected quantity is untouched by the derivation.
    assert _status(10, claims, closed=True, now=NOW, deadline=PAST) is DonationStatus.EXPIRED
    assert available_quantity(10, claims).collected == 3


# --------------------------------------------------------------------------
# Open-listing precedence
# --------------------------------------------------------------------------
def test_open_remaining_is_available():
    assert _status(10, [_claim(ClaimStatus.RESERVED, 4, 1)]) is DonationStatus.AVAILABLE


def test_open_fully_reserved_is_reserved():
    assert _status(10, [_claim(ClaimStatus.RESERVED, 10, 1)]) is DonationStatus.RESERVED


def test_open_fully_collected_is_collected():
    assert _status(10, [_claim(ClaimStatus.COLLECTED, 10, 1)]) is DonationStatus.COLLECTED


def test_open_past_deadline_with_remaining_is_expired():
    assert _status(10, [], deadline=PAST) is DonationStatus.EXPIRED


def test_fully_collected_outranks_expiry():
    # A finished donation stays COLLECTED even after its deadline.
    claims = [_claim(ClaimStatus.COLLECTED, 10, 1)]
    assert _status(10, claims, deadline=PAST) is DonationStatus.COLLECTED


def test_fully_reserved_past_deadline_expires():
    claims = [_claim(ClaimStatus.RESERVED, 10, 1)]
    assert _status(10, claims, deadline=PAST) is DonationStatus.EXPIRED


def test_mixed_state_example_is_available_and_counts_only_collected():
    # total 10: collected 3, reserved 2, cancelled 4 -> remaining 5, open.
    claims = [
        _claim(ClaimStatus.COLLECTED, 3, 1),
        _claim(ClaimStatus.RESERVED, 2, 2),
        _claim(ClaimStatus.CANCELLED, 4, 3),
    ]
    assert _status(10, claims) is DonationStatus.AVAILABLE
    a = available_quantity(10, claims)
    assert (a.collected, a.reserved, a.remaining) == (3, 2, 5)
