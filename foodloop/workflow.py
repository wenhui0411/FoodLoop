"""The FoodLoop service layer — the application's use-case API.

This is the single entry point the UI (or any other frontend) should call to
*do* things: create a donation, check eligibility, claim, confirm collection.
It orchestrates the lower layers — :mod:`validation`, :mod:`eligibility`,
and the :class:`~foodloop.repository.Repository` — and owns nothing that
belongs to them, so there is no duplicated business logic.

It is deliberately free of any Streamlit import. Time is injected via a
``clock`` callable so behaviour is deterministic under test.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from . import validation
from .eligibility import (
    EligibilityResult,
    capacity_check,
    capacity_fits,
    capacity_headroom,
    check_eligibility,
)
from .errors import EligibilityError, NotFoundError
from .matching import Recommendation, rank_recipients
from .models import Claim, Donation, DonationStatus
from .repository import Repository
from .reservation import held_by_recipient


def _default_clock() -> datetime:
    return datetime.now()


class FoodLoopService:
    """Use-case orchestration over the repository and domain rules."""

    def __init__(self, repo: Repository, clock: Callable[[], datetime] = _default_clock):
        self._repo = repo
        self._clock = clock

    @property
    def repo(self) -> Repository:
        """Read access to persistence for queries the UI needs (lists, lookups)."""
        return self._repo

    def now(self) -> datetime:
        return self._clock()

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------
    def create_donation(self, **fields: object) -> Donation:
        """Validate input, confirm the donor exists, and persist a new listing.

        Accepts loosely-typed input (as a form would supply) and delegates
        all rule-checking to :func:`validation.validate_donation_input`,
        which raises :class:`ValidationError` on bad input. Returns the
        created :class:`Donation` (status AVAILABLE).
        """
        now = self._clock()
        clean = validation.validate_donation_input(now=now, **fields)  # may raise

        # Donor existence is a persistence concern, checked here (not in the
        # pure validator) because it needs the repository.
        self._repo.get_donor(str(clean["donor_id"]))  # raises NotFoundError

        donation = Donation(
            id=f"dn-{uuid.uuid4().hex[:10]}",
            created_at=now,
            status=DonationStatus.AVAILABLE,
            **clean,  # type: ignore[arg-type]
        )
        self._repo.add_donation(donation)
        return donation

    # ------------------------------------------------------------------
    # Eligibility
    # ------------------------------------------------------------------
    def check_eligibility(
        self, donation_id: str, recipient_id: str, quantity: float | None = None
    ) -> EligibilityResult:
        """Evaluate whether a recipient may claim a donation (no side effects).

        Without ``quantity`` it asks whether the recipient can take any of it;
        with one, whether that request fits its per-donation capacity, counting
        what it already holds from this donation.
        """
        donation = self._repo.get_donation(donation_id)
        recipient = self._repo.get_recipient(recipient_id)
        return check_eligibility(
            recipient, donation, now=self._clock(), quantity=quantity,
            already_held=self._held_by(donation_id, recipient_id),
        )

    def claimable_quantity(self, donation_id: str, recipient_id: str) -> float:
        """The most this recipient may reserve now: the donation's remaining
        quantity, capped by its per-donation capacity headroom when that
        applies. A UI uses it as the default/limit hint for a claim."""
        donation = self._repo.get_donation(donation_id)
        recipient = self._repo.get_recipient(recipient_id)
        remaining = self._repo.donation_availability(donation_id).remaining
        headroom = capacity_headroom(
            recipient, donation, self._held_by(donation_id, recipient_id)
        )
        return remaining if headroom is None else min(remaining, headroom)

    def recommend_recipients(self, donation_id: str) -> list[Recommendation]:
        """Rule-based recipient ranking for a donation, aware of current
        claims: a recipient that already holds its per-donation capacity
        from this donation is not recommended."""
        donation = self._repo.get_donation(donation_id)
        claims = self._repo.list_claims_for_donation(donation_id)
        held = {
            rid: held_by_recipient(claims, rid)
            for rid in {c.recipient_id for c in claims}
        }
        return rank_recipients(
            donation, self._repo.list_recipients(), now=self._clock(), held=held
        )

    def _held_by(self, donation_id: str, recipient_id: str) -> float:
        return held_by_recipient(
            self._repo.list_claims_for_donation(donation_id), recipient_id
        )

    # ------------------------------------------------------------------
    # Claim (partial-quantity reservation)
    # ------------------------------------------------------------------
    def claim_donation(
        self, donation_id: str, recipient_id: str, quantity: float | None = None
    ) -> Claim:
        """Reserve a quantity of a donation for a recipient.

        Eligibility (verified, category, storage, deadline window, capacity)
        is enforced first; the requested quantity is unit-validated and then
        checked against the recipient's per-donation capacity; finally the
        repository reserves it atomically (availability check, capacity
        re-check, insert and status recompute in one transaction), so
        concurrent reservers cannot over-reserve. ``quantity=None`` reserves
        as much as is claimable (all remaining unless capacity caps it),
        preserving the existing one-tap claim UX.
        """
        result = self.check_eligibility(donation_id, recipient_id)
        if not result.eligible:
            raise EligibilityError(
                "Recipient is not eligible to claim this donation: "
                + "; ".join(result.reasons)
            )
        donation = self._repo.get_donation(donation_id)
        recipient = self._repo.get_recipient(recipient_id)
        if quantity is None:
            quantity = self.claimable_quantity(donation_id, recipient_id)
        quantity = validation.validate_claim_quantity(donation.quantity.unit, quantity)
        check = capacity_check(
            recipient, donation, quantity=quantity,
            already_held=self._held_by(donation_id, recipient_id),
        )
        if not check.passed:
            raise EligibilityError(check.detail)
        _, comparable = capacity_fits(recipient, donation)
        return self._repo.create_claim(
            claim_id=f"clm-{uuid.uuid4().hex[:10]}",
            donation_id=donation_id,
            recipient_id=recipient_id,
            claimed_at=self._clock(),
            quantity=quantity,
            now=self._clock(),
            recipient_capacity=recipient.max_quantity_kg_equiv if comparable else None,
        )

    # ------------------------------------------------------------------
    # Collection (the only path to a counted success)
    # ------------------------------------------------------------------
    def confirm_collection(self, claim_id: str) -> Claim:
        """Confirm a specific reserved claim was collected, by claim id — the
        low-level path used internally/for tests. The donor-facing workflow is
        :meth:`confirm_handover` (by pickup code). The repository enforces the
        claim is RESERVED and the deadline has not passed."""
        now = self._clock()
        return self._repo.confirm_collection(claim_id, now, now=now)

    def confirm_handover(self, donor_id: str, code: str) -> Claim:
        """Donor confirms a handover by entering the recipient's pickup code.

        Verifies (atomically, in the repository) that the code exists, belongs
        to one of this donor's donations, is still RESERVED, and is within the
        deadline; then marks it collected. Single-use. ``donor_id`` is the
        acting demo persona — NOT real authentication.
        """
        now = self._clock()
        return self._repo.confirm_handover(donor_id, code, now, now=now)

    def cancel_claim(self, claim_id: str) -> None:
        """Release a reserved claim, freeing its quantity back to the donation."""
        self._repo.cancel_claim(claim_id, now=self._clock())

    def cancel_donation(self, donation_id: str) -> None:
        """Donor withdrawal: close the donation to NEW claims.

        Existing reservations and collected history are preserved. If nothing
        is committed the donation becomes CANCELLED; otherwise it stays
        RESERVED/COLLECTED until its claims resolve.
        """
        self._repo.close_to_claims(donation_id, now=self._clock())

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    def expire_overdue(self) -> list[str]:
        """Lapse past-deadline, uncollected donations (and their reserved
        claims) to EXPIRED. Collected claims are never affected. Returns the
        expired donation ids."""
        return self._repo.expire_overdue(self._clock())
