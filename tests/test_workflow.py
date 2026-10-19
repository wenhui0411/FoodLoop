"""Tests for the FoodLoopService use-case layer (the UI-facing API).

Covers the complete create -> claim -> confirm-collection slice plus the
failure paths: invalid input, ineligible recipients, expired deadlines,
duplicate claims, invalid status transitions, and the rule that only
confirmed collections count toward completed-donation metrics.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from foodloop import impact
from foodloop.errors import (
    ClaimsClosedError,
    EligibilityError,
    InvalidTransitionError,
    NotFoundError,
    OverReservationError,
    ValidationError,
)
from foodloop.models import (
    ClaimStatus,
    DonationStatus,
    Donor,
    DonorType,
    FoodCategory,
    Location,
    Recipient,
    RecipientType,
    StorageRequirement,
    Unit,
)


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------
def _add_donor(repo, donor_id="d1"):
    repo.add_donor(Donor(donor_id, "Bakery", DonorType.BAKERY, Location("Central")))


def _add_recipient(repo, rid="r1", **overrides):
    base = dict(
        id=rid, name=f"Org {rid}", recipient_type=RecipientType.FOOD_BANK,
        location=Location("Central"),
        accepted_categories={FoodCategory.BAKERY},
        storage_capabilities={StorageRequirement.AMBIENT},
        max_quantity_kg_equiv=100,
        collection_days={"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"},
        collection_start_hour=7, collection_end_hour=20,
        verified=True,
    )
    base.update(overrides)
    repo.add_recipient(Recipient(**base))


def _create_donation(service, now, **overrides):
    kwargs = dict(
        donor_id="d1", title="Bread", description="End of day",
        category=FoodCategory.BAKERY, quantity_value=10, quantity_unit=Unit.LOAVES,
        area="Central", collection_deadline=now + timedelta(days=1),
    )
    kwargs.update(overrides)
    return service.create_donation(**kwargs)


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------
def test_create_donation_persists_available_listing(service, repo, now):
    _add_donor(repo)
    donation = _create_donation(service, now)

    assert donation.status is DonationStatus.AVAILABLE
    assert donation.id.startswith("dn-")
    # It is retrievable and appears in the donor's history and marketplace.
    assert repo.get_donation(donation.id).title == "Bread"
    assert donation.id in {d.id for d in repo.list_donations(donor_id="d1")}
    assert donation.id in {
        d.id for d in repo.list_donations(status=DonationStatus.AVAILABLE)
    }


def test_create_donation_rejects_invalid_input(service, repo, now):
    _add_donor(repo)
    with pytest.raises(ValidationError):
        _create_donation(service, now, quantity_value=-1)


def test_create_donation_rejects_unknown_donor(service, now):
    with pytest.raises(NotFoundError):
        _create_donation(service, now)  # no donor added


# --------------------------------------------------------------------------
# Eligibility + claim
# --------------------------------------------------------------------------
def test_eligible_recipient_can_claim(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo)
    donation = _create_donation(service, now)

    claim = service.claim_donation(donation.id, "r1")
    assert claim.recipient_id == "r1"
    assert claim.collected_at is None
    # Whole-listing claim reserves everything -> donation RESERVED.
    assert repo.get_donation(donation.id).status is DonationStatus.RESERVED


def test_unverified_recipient_cannot_claim(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo, verified=False)
    donation = _create_donation(service, now)

    with pytest.raises(EligibilityError):
        service.claim_donation(donation.id, "r1")
    assert repo.get_donation(donation.id).status is DonationStatus.AVAILABLE


def test_ineligible_category_cannot_claim(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo, accepted_categories={FoodCategory.DAIRY})
    donation = _create_donation(service, now)

    with pytest.raises(EligibilityError):
        service.claim_donation(donation.id, "r1")


def test_recipient_without_collection_window_cannot_claim(service, repo, now):
    _add_donor(repo)
    # Only collects Sunday; deadline is tomorrow (Friday) -> no window.
    _add_recipient(repo, collection_days={"Sun"})
    donation = _create_donation(service, now, collection_deadline=now + timedelta(days=1))

    with pytest.raises(EligibilityError) as exc:
        service.claim_donation(donation.id, "r1")
    assert "deadline" in str(exc.value).lower() or "window" in str(exc.value).lower()


# --------------------------------------------------------------------------
# Duplicate claims
# --------------------------------------------------------------------------
def test_duplicate_claim_is_prevented(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo, "r1")
    _add_recipient(repo, "r2")
    donation = _create_donation(service, now)

    service.claim_donation(donation.id, "r1")  # reserves the whole listing
    # Nothing remains: the second claim is rejected (the donation is RESERVED,
    # so eligibility fails; over-reservation is the DB-level guard).
    with pytest.raises((EligibilityError, OverReservationError, InvalidTransitionError)):
        service.claim_donation(donation.id, "r2")
    assert repo.get_claim_for_donation(donation.id).recipient_id == "r1"


# --------------------------------------------------------------------------
# Collection + invalid transitions
# --------------------------------------------------------------------------
def test_full_create_claim_confirm_workflow(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo)

    donation = _create_donation(service, now)
    assert repo.get_donation(donation.id).status is DonationStatus.AVAILABLE

    claim = service.claim_donation(donation.id, "r1")
    assert repo.get_donation(donation.id).status is DonationStatus.RESERVED
    # A claim alone must NOT count as a collection.
    assert impact.completed_donation_count(repo) == 0

    confirmed = service.confirm_collection(claim.id)
    assert repo.get_donation(donation.id).status is DonationStatus.COLLECTED
    assert confirmed.is_collected
    assert impact.completed_donation_count(repo) == 1


def test_cannot_confirm_an_unknown_claim(service, repo, now):
    _add_donor(repo)
    donation = _create_donation(service, now)
    # No claim exists for this donation -> confirming by a bogus id is NotFound.
    with pytest.raises(NotFoundError):
        service.confirm_collection(donation.id)


def test_cancel_claim_allows_reclaim(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo, "r1")
    _add_recipient(repo, "r2")
    donation = _create_donation(service, now)

    claim = service.claim_donation(donation.id, "r1")
    service.cancel_claim(claim.id)
    assert repo.get_donation(donation.id).status is DonationStatus.AVAILABLE

    reclaim = service.claim_donation(donation.id, "r2")
    assert reclaim.recipient_id == "r2"


def test_withdraw_closes_donation_but_keeps_reservation(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo, "r1")
    _add_recipient(repo, "r2")
    donation = _create_donation(service, now)
    service.claim_donation(donation.id, "r1")  # reserve the whole listing

    # Withdrawal closes to NEW claims; the existing reservation is preserved.
    service.cancel_donation(donation.id)
    assert repo.get_donation(donation.id).closed_to_claims is True
    assert repo.get_donation(donation.id).status is DonationStatus.RESERVED
    # A new claim is now refused.
    with pytest.raises((ClaimsClosedError, EligibilityError, OverReservationError)):
        service.claim_donation(donation.id, "r2")


# --------------------------------------------------------------------------
# Expiry + metrics integrity
# --------------------------------------------------------------------------
def test_expire_overdue_marks_uncollected_past_deadline(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo)
    # Create with a valid future deadline, then advance the clock past it by
    # using a later service for expiry.
    donation = _create_donation(service, now, collection_deadline=now + timedelta(hours=2))

    later = FoodLoopServiceAt(repo, now + timedelta(days=1))
    expired = later.expire_overdue()
    assert donation.id in expired
    assert repo.get_donation(donation.id).status is DonationStatus.EXPIRED
    # Expired food is never counted as rescued.
    assert impact.completed_donation_count(repo) == 0


def test_claimed_but_expired_does_not_count_as_collected(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo)
    donation = _create_donation(service, now, collection_deadline=now + timedelta(hours=2))
    service.claim_donation(donation.id, "r1")

    later = FoodLoopServiceAt(repo, now + timedelta(days=1))
    later.expire_overdue()

    assert repo.get_donation(donation.id).status is DonationStatus.EXPIRED
    assert impact.completed_donation_count(repo) == 0
    # The uncollected claim must not be stamped as collected.
    claim = repo.get_claim_for_donation(donation.id)
    assert claim is None or not claim.is_collected


# Small helper to run the service at a specific "now".
def FoodLoopServiceAt(repo, at: datetime):
    from foodloop.workflow import FoodLoopService

    return FoodLoopService(repo, clock=lambda: at)


# --------------------------------------------------------------------------
# M3: donor pickup-code handover verification
# --------------------------------------------------------------------------
def _reserve(service, repo, now):
    """Set up a donor (d1), eligible recipient (r1), donation and a claim.
    Returns the claim."""
    _add_donor(repo)
    _add_recipient(repo)
    donation = _create_donation(service, now)
    return donation, service.claim_donation(donation.id, "r1")


def test_pickup_code_is_unpredictable_and_not_the_claim_id(service, repo, now):
    _, claim = _reserve(service, repo, now)
    assert claim.pickup_code
    assert claim.pickup_code != claim.id          # requirement 1
    assert len(claim.pickup_code) == 6
    assert all(ch in "ABCDEFGHJKMNPQRSTUVWXYZ23456789" for ch in claim.pickup_code)


def test_confirm_handover_by_code_marks_collected(service, repo, now):
    donation, claim = _reserve(service, repo, now)
    assert impact.completed_donation_count(repo) == 0

    confirmed = service.confirm_handover("d1", claim.pickup_code)
    assert confirmed.id == claim.id
    assert confirmed.status is ClaimStatus.COLLECTED
    assert repo.get_donation(donation.id).status is DonationStatus.COLLECTED
    assert impact.completed_donation_count(repo) == 1


def test_confirm_handover_is_case_and_space_insensitive(service, repo, now):
    donation, claim = _reserve(service, repo, now)
    service.confirm_handover("d1", f"  {claim.pickup_code.lower()}  ")
    assert repo.get_donation(donation.id).status is DonationStatus.COLLECTED


def test_confirm_handover_invalid_code_changes_nothing(service, repo, now):
    donation, _ = _reserve(service, repo, now)
    with pytest.raises(NotFoundError):
        service.confirm_handover("d1", "ZZZZZZ")
    assert repo.get_donation(donation.id).status is DonationStatus.RESERVED
    assert impact.completed_donation_count(repo) == 0


def test_confirm_handover_wrong_donor_rejected(service, repo, now):
    _add_donor(repo, "d2")  # a different donor
    donation, claim = _reserve(service, repo, now)  # donation owned by d1
    with pytest.raises(NotFoundError):
        service.confirm_handover("d2", claim.pickup_code)
    assert repo.get_donation(donation.id).status is DonationStatus.RESERVED
    assert impact.completed_donation_count(repo) == 0


def test_confirm_handover_is_single_use(service, repo, now):
    donation, claim = _reserve(service, repo, now)
    service.confirm_handover("d1", claim.pickup_code)
    with pytest.raises(InvalidTransitionError):
        service.confirm_handover("d1", claim.pickup_code)   # reused code
    assert impact.completed_donation_count(repo) == 1        # still exactly one


def test_best_before_is_stored_independently_of_deadline(service, repo, now):
    """Bug-fix regression: a donor-chosen best-before survives create/read.
    No ordering rule against the collection deadline exists in the model."""
    _add_donor(repo)
    bb = datetime(2026, 1, 20)
    donation = _create_donation(service, now, best_before=bb)
    assert repo.get_donation(donation.id).best_before == bb


# --------------------------------------------------------------------------
# Bug-fix regressions: partial-quantity claims through the service
# --------------------------------------------------------------------------
def test_service_partial_claims_cancel_and_reclaim_keep_quantities(service, repo, now):
    """Multiple partial reservations, a rejected over-reservation, then a
    cancellation that releases exactly the cancelled amount."""
    _add_donor(repo)
    for rid in ("r1", "r2", "r3"):
        _add_recipient(repo, rid)
    donation = _create_donation(service, now)  # 10 loaves

    c1 = service.claim_donation(donation.id, "r1", 3)
    c2 = service.claim_donation(donation.id, "r2", 4)
    assert (c1.quantity_value, c2.quantity_value) == (3, 4)
    avail = repo.donation_availability(donation.id)
    assert (avail.reserved, avail.remaining) == (7, 3)
    assert repo.get_donation(donation.id).status is DonationStatus.AVAILABLE

    with pytest.raises(OverReservationError):
        service.claim_donation(donation.id, "r3", 4)
    assert repo.donation_availability(donation.id).remaining == 3  # unchanged

    service.cancel_claim(c1.id)
    avail = repo.donation_availability(donation.id)
    assert (avail.reserved, avail.remaining) == (4, 6)

    service.claim_donation(donation.id, "r3", 6)
    avail = repo.donation_availability(donation.id)
    assert (avail.reserved, avail.remaining) == (10, 0)
    assert repo.get_donation(donation.id).status is DonationStatus.RESERVED


@pytest.mark.parametrize("bad", [0, -2, 2.5, "abc"])
def test_service_rejects_invalid_claim_quantity(service, repo, now, bad):
    _add_donor(repo)
    _add_recipient(repo)
    donation = _create_donation(service, now)  # loaves: whole units only
    with pytest.raises(ValidationError):
        service.claim_donation(donation.id, "r1", bad)
    assert repo.list_claims_for_donation(donation.id) == []


def test_service_allows_decimal_claim_for_continuous_unit(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo)
    donation = _create_donation(service, now, quantity_value=5, quantity_unit=Unit.KG)
    claim = service.claim_donation(donation.id, "r1", 1.25)
    assert claim.quantity_value == 1.25
    assert repo.donation_availability(donation.id).remaining == 3.75


def test_service_concurrent_partial_claims_cannot_overreserve(service, repo, now):
    """The UI path (service.claim_donation) under near-simultaneous requests:
    three recipients each ask for 4 of 10 — at most two can succeed."""
    import threading

    _add_donor(repo)
    rids = ("r1", "r2", "r3")
    for rid in rids:
        _add_recipient(repo, rid)
    donation = _create_donation(service, now)

    barrier = threading.Barrier(len(rids))
    outcomes: list[str] = []

    def attempt(rid):
        barrier.wait()
        try:
            service.claim_donation(donation.id, rid, 4)
            outcomes.append("ok")
        except (OverReservationError, EligibilityError):
            outcomes.append("rejected")

    threads = [threading.Thread(target=attempt, args=(r,)) for r in rids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 2
    avail = repo.donation_availability(donation.id)
    assert avail.reserved == 8 and avail.remaining == 2


# --------------------------------------------------------------------------
# Capacity: a per-donation ceiling on what one recipient takes
# --------------------------------------------------------------------------
def _kg_listing(service, repo, now, *, cap=10, total=20):
    _add_donor(repo)
    _add_recipient(repo, "r1", max_quantity_kg_equiv=cap)
    _add_recipient(repo, "r2", max_quantity_kg_equiv=100)
    return _create_donation(service, now, quantity_value=total, quantity_unit=Unit.KG)


def test_capacity_10kg_recipient_can_reserve_5kg_of_20kg(service, repo, now):
    donation = _kg_listing(service, repo, now)
    assert service.check_eligibility(donation.id, "r1").eligible
    claim = service.claim_donation(donation.id, "r1", 5)
    assert claim.quantity_value == 5
    assert repo.donation_availability(donation.id).remaining == 15
    assert service.claimable_quantity(donation.id, "r1") == 5   # 10 cap − 5 held


def test_capacity_request_over_capacity_rejected_without_write(service, repo, now):
    donation = _kg_listing(service, repo, now)
    with pytest.raises(EligibilityError, match="exceeds capacity"):
        service.claim_donation(donation.id, "r1", 12)
    assert repo.list_claims_for_donation(donation.id) == []


def test_capacity_counts_own_reservations_on_same_donation(service, repo, now):
    donation = _kg_listing(service, repo, now)
    service.claim_donation(donation.id, "r1", 6)
    with pytest.raises(EligibilityError):
        service.claim_donation(donation.id, "r1", 5)              # 6 + 5 > 10
    service.claim_donation(donation.id, "r1", 4)                   # 6 + 4 == 10
    assert service.claimable_quantity(donation.id, "r1") == 0
    assert not service.check_eligibility(donation.id, "r1").eligible
    # The listing still has 10 kg left for others.
    assert service.check_eligibility(donation.id, "r2").eligible


def test_capacity_ignores_other_recipients_and_released_claims(service, repo, now):
    donation = _kg_listing(service, repo, now)
    service.claim_donation(donation.id, "r2", 10)   # someone else's reservation
    assert service.claimable_quantity(donation.id, "r1") == 10
    mine = service.claim_donation(donation.id, "r1", 7)
    service.cancel_claim(mine.id)                    # cancelled releases capacity
    assert service.claimable_quantity(donation.id, "r1") == 10


def test_capacity_counts_collected_quantity(service, repo, now):
    donation = _kg_listing(service, repo, now)
    claim = service.claim_donation(donation.id, "r1", 6)
    service.confirm_collection(claim.id)
    assert service.claimable_quantity(donation.id, "r1") == 4
    with pytest.raises(EligibilityError):
        service.claim_donation(donation.id, "r1", 5)


def test_claim_without_quantity_reserves_up_to_capacity(service, repo, now):
    donation = _kg_listing(service, repo, now)
    claim = service.claim_donation(donation.id, "r1")   # one-tap claim
    assert claim.quantity_value == 10                    # min(remaining 20, cap 10)
    assert repo.get_donation(donation.id).status is DonationStatus.AVAILABLE


def test_capacity_does_not_limit_countable_units(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo, "r1", max_quantity_kg_equiv=10)
    donation = _create_donation(service, now, quantity_value=20)  # loaves
    assert service.claimable_quantity(donation.id, "r1") == 20
    assert service.claim_donation(donation.id, "r1", 15).quantity_value == 15


def test_capacity_guard_is_atomic_for_concurrent_same_recipient(service, repo, now):
    """Two tabs as the same org: three 4 kg requests against a 10 kg ceiling
    on a 20 kg listing — the in-transaction re-check lets only two through."""
    import threading

    donation = _kg_listing(service, repo, now)
    barrier = threading.Barrier(3)
    outcomes: list[str] = []

    def attempt():
        barrier.wait()
        try:
            service.claim_donation(donation.id, "r1", 4)
            outcomes.append("ok")
        except EligibilityError:
            outcomes.append("rejected")

    threads = [threading.Thread(target=attempt) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 2
    assert repo.donation_availability(donation.id).reserved == 8


def test_repository_capacity_guard_rejects_without_write(repo, now):
    """Direct check of the transactional guard (bypassing the service)."""
    _add_donor(repo)
    _add_recipient(repo, "r1")
    from foodloop.workflow import FoodLoopService
    donation = _create_donation(FoodLoopService(repo, clock=lambda: now), now,
                                quantity_value=20, quantity_unit=Unit.KG)
    repo.create_claim(claim_id="c1", donation_id=donation.id, recipient_id="r1",
                      claimed_at=now, quantity=8, now=now, recipient_capacity=10)
    with pytest.raises(EligibilityError):
        repo.create_claim(claim_id="c2", donation_id=donation.id, recipient_id="r1",
                          claimed_at=now, quantity=3, now=now, recipient_capacity=10)
    assert [c.id for c in repo.list_claims_for_donation(donation.id)] == ["c1"]


# --------------------------------------------------------------------------
# Audit F1: exact-decimal sums in the transactional reservation check
# --------------------------------------------------------------------------
def test_decimal_remainder_is_claimable_exactly(service, repo, now):
    """0.6 kg listing: 0.1 + 0.2 reserved, then the exact 0.3 remainder."""
    _add_donor(repo)
    for rid in ("r1", "r2", "r3"):
        _add_recipient(repo, rid)
    donation = _create_donation(service, now, quantity_value=0.6, quantity_unit=Unit.KG)
    service.claim_donation(donation.id, "r1", 0.1)
    service.claim_donation(donation.id, "r2", 0.2)
    assert service.claimable_quantity(donation.id, "r3") == 0.3

    # An over-request reports the exact remainder — no float artefact.
    with pytest.raises(OverReservationError) as exc:
        service.claim_donation(donation.id, "r3", 0.4)
    assert "remaining 0.3." in str(exc.value)
    assert "0.2999" not in str(exc.value) and "0.3000" not in str(exc.value)

    claim = service.claim_donation(donation.id, "r3", 0.3)
    assert claim.quantity_value == 0.3
    avail = repo.donation_availability(donation.id)
    assert avail.remaining == 0.0
    assert repo.get_donation(donation.id).status is DonationStatus.RESERVED


def test_decimal_capacity_sum_allows_exact_fill(service, repo, now):
    """Same drift in the per-recipient capacity sum: 0.1 + 0.2 held, then
    0.3 more against a 0.6 kg ceiling must fit exactly."""
    _add_donor(repo)
    _add_recipient(repo, "r1", max_quantity_kg_equiv=0.6)
    donation = _create_donation(service, now, quantity_value=1, quantity_unit=Unit.KG)
    service.claim_donation(donation.id, "r1", 0.1)
    service.claim_donation(donation.id, "r1", 0.2)
    service.claim_donation(donation.id, "r1", 0.3)
    assert service.claimable_quantity(donation.id, "r1") == 0.0
    with pytest.raises(EligibilityError):
        service.claim_donation(donation.id, "r1", 0.1)


def test_one_tap_claim_on_three_decimal_listing_leaves_no_remainder(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo)
    donation = _create_donation(service, now, quantity_value=1.234, quantity_unit=Unit.KG)
    assert service.claimable_quantity(donation.id, "r1") == 1.234
    claim = service.claim_donation(donation.id, "r1")      # quantity=None: one tap
    assert claim.quantity_value == 1.234
    assert repo.donation_availability(donation.id).remaining == 0.0
    assert repo.get_donation(donation.id).status is DonationStatus.RESERVED


def test_service_rejects_unclaimable_listing_precision(service, repo, now):
    _add_donor(repo)
    for qty, unit in ((1.2345, Unit.KG), (2.5, Unit.LOAVES)):
        with pytest.raises(ValidationError) as exc:
            _create_donation(service, now, quantity_value=qty, quantity_unit=unit)
        assert "quantity_value" in exc.value.errors
    assert repo.list_donations() == []


# --------------------------------------------------------------------------
# Audit F2: recommendations account for capacity already held
# --------------------------------------------------------------------------
def test_recommendations_exclude_recipient_with_exhausted_capacity(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo, "r1", max_quantity_kg_equiv=10)   # will be exhausted
    _add_recipient(repo, "r2", max_quantity_kg_equiv=10)   # partly used
    _add_recipient(repo, "r3", max_quantity_kg_equiv=100)  # untouched
    donation = _create_donation(service, now, quantity_value=40, quantity_unit=Unit.KG)
    before = {r.recipient.id: r.score for r in service.recommend_recipients(donation.id)}
    assert set(before) == {"r1", "r2", "r3"}

    service.claim_donation(donation.id, "r1", 10)
    service.claim_donation(donation.id, "r2", 4)
    after = {r.recipient.id: r.score for r in service.recommend_recipients(donation.id)}

    assert "r1" not in after                    # no capacity left on this donation
    assert {"r2", "r3"} <= set(after)           # remaining capacity: still recommended
    assert after["r2"] == before["r2"] and after["r3"] == before["r3"]  # scores unchanged


def test_recommendations_release_capacity_after_cancel(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo, "r1", max_quantity_kg_equiv=10)
    donation = _create_donation(service, now, quantity_value=20, quantity_unit=Unit.KG)
    claim = service.claim_donation(donation.id, "r1", 10)
    assert service.recommend_recipients(donation.id) == []
    service.cancel_claim(claim.id)
    assert [r.recipient.id for r in service.recommend_recipients(donation.id)] == ["r1"]


def test_confirm_handover_after_deadline_rejected(service, repo, now):
    _add_donor(repo)
    _add_recipient(repo)
    donation = _create_donation(service, now, collection_deadline=now + timedelta(hours=2))
    claim = service.claim_donation(donation.id, "r1")

    later = FoodLoopServiceAt(repo, now + timedelta(days=1))
    with pytest.raises(InvalidTransitionError):
        later.confirm_handover("d1", claim.pickup_code)
    # Unchanged (still reserved; impact zero).
    assert repo.get_claim_for_donation(donation.id).status is ClaimStatus.RESERVED
    assert impact.completed_donation_count(repo) == 0
