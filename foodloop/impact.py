"""Impact accounting.

The single rule enforced here: **only confirmed collections count as rescued
food.** Listed, claimed, cancelled, and expired donations are explicitly not
counted. ``donor_impact`` builds the donor-facing figures on these primitives,
and ``foodloop.insights`` builds township-level summaries; keeping the "only
collected counts" guarantee in one place means it is defined and tested once.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .models import ClaimStatus, Donation, DonationStatus
from .repository import Repository


def collected_donations(
    repo: Repository, *, donor_id: str | None = None
) -> list[Donation]:
    """Donation *listings* that are fully collected (status COLLECTED).

    Listing-level helper; semantically safe. For counting rescued food use
    :func:`completed_donation_count` (confirmed handovers = COLLECTED claims).
    """
    return repo.list_donations(status=DonationStatus.COLLECTED, donor_id=donor_id)


def completed_donation_count(repo: Repository, *, donor_id: str | None = None) -> int:
    """Count of confirmed handovers — i.e. COLLECTED **claims**, not donations.

    A donation may yield several collected claims; each is one rescued
    handover. Restricted to the donor's donations when ``donor_id`` is given.
    """
    if donor_id is None:
        return sum(1 for c in repo.list_claims() if c.status is ClaimStatus.COLLECTED)
    donor_donation_ids = {d.id for d in repo.list_donations(donor_id=donor_id)}
    return sum(
        1 for c in repo.list_claims()
        if c.status is ClaimStatus.COLLECTED and c.donation_id in donor_donation_ids
    )


@dataclass(slots=True)
class DonorImpact:
    """A donor's claim-accurate impact figures.

    All handover/quantity figures come from **claims**, so partial and
    multiple claims per donation are counted correctly:

    * ``confirmed_collections`` — COLLECTED claims (confirmed handovers).
    * ``reserved_claims`` — active RESERVED claims (awaiting pickup).
    * ``expired_claims`` / ``cancelled_claims`` — lapsed / released claims.
    * ``collected_by_unit`` — Σ COLLECTED claim quantities, grouped by the
      donation's original unit (never converted).
    * ``reserved_by_unit`` — Σ RESERVED claim quantities (kept separate from
      collected; reserved food is never reported as collected).

    ``collection_rate`` = ``confirmed_collections / rate_denominator`` where
    ``rate_denominator = confirmed_collections + expired_claims`` (concluded
    claims only; RESERVED and CANCELLED excluded). ``None`` when the
    denominator is zero.
    """

    donor_id: str
    total_listed: int                 # donation listings
    confirmed_collections: int        # COLLECTED claims (rate numerator)
    reserved_claims: int              # RESERVED claims (awaiting pickup)
    expired_claims: int
    cancelled_claims: int
    rate_denominator: int             # confirmed + expired claims (concluded)
    collection_rate: float | None
    collected_by_unit: dict[str, float] = field(default_factory=dict)
    reserved_by_unit: dict[str, float] = field(default_factory=dict)
    organisations_helped: int = 0


@dataclass(slots=True)
class RecipientImpact:
    """A recipient organisation's own, claim-accurate impact.

    Only COLLECTED claims count as confirmed; RESERVED claims are "in
    progress"; CANCELLED/EXPIRED count as neither. Quantities stay in their
    original units — never converted — with reserved and collected kept
    separate. No environmental/"meals" figures (no defensible methodology).
    """

    recipient_id: str
    confirmed_collections: int                      # COLLECTED claims
    active_claims: int                              # RESERVED claims
    donors_supported: int
    collected_by_unit: dict[str, float] = field(default_factory=dict)
    reserved_by_unit: dict[str, float] = field(default_factory=dict)


def recipient_impact(repo: Repository, recipient_id: str) -> RecipientImpact:
    """Compute a recipient's claim-accurate impact from persisted data."""
    confirmed = 0
    active = 0
    collected_by_unit: dict[str, float] = defaultdict(float)
    reserved_by_unit: dict[str, float] = defaultdict(float)
    donors: set[str] = set()
    for claim in repo.list_claims(recipient_id=recipient_id):
        donation = repo.get_donation(claim.donation_id)
        unit = donation.quantity.unit.value
        if claim.status is ClaimStatus.COLLECTED:
            confirmed += 1
            collected_by_unit[unit] += claim.quantity_value
            donors.add(donation.donor_id)
        elif claim.status is ClaimStatus.RESERVED:
            active += 1
            reserved_by_unit[unit] += claim.quantity_value

    return RecipientImpact(
        recipient_id=recipient_id,
        confirmed_collections=confirmed,
        active_claims=active,
        donors_supported=len(donors),
        collected_by_unit=dict(collected_by_unit),
        reserved_by_unit=dict(reserved_by_unit),
    )


def donor_impact(repo: Repository, donor_id: str) -> DonorImpact:
    """Compute a donor's claim-accurate impact from persisted data only."""
    donations = repo.list_donations(donor_id=donor_id)
    unit_by_donation = {d.id: d.quantity.unit.value for d in donations}

    confirmed = reserved = expired = cancelled = 0
    collected_by_unit: dict[str, float] = defaultdict(float)
    reserved_by_unit: dict[str, float] = defaultdict(float)
    orgs: set[str] = set()

    for claim in repo.list_claims():
        unit = unit_by_donation.get(claim.donation_id)
        if unit is None:                    # claim on another donor's donation
            continue
        if claim.status is ClaimStatus.COLLECTED:
            confirmed += 1
            collected_by_unit[unit] += claim.quantity_value
            orgs.add(claim.recipient_id)
        elif claim.status is ClaimStatus.RESERVED:
            reserved += 1
            reserved_by_unit[unit] += claim.quantity_value
        elif claim.status is ClaimStatus.EXPIRED:
            expired += 1
        elif claim.status is ClaimStatus.CANCELLED:
            cancelled += 1

    denominator = confirmed + expired
    rate = (confirmed / denominator) if denominator > 0 else None

    return DonorImpact(
        donor_id=donor_id,
        total_listed=len(donations),
        confirmed_collections=confirmed,
        reserved_claims=reserved,
        expired_claims=expired,
        cancelled_claims=cancelled,
        rate_denominator=denominator,
        collection_rate=rate,
        collected_by_unit=dict(collected_by_unit),
        reserved_by_unit=dict(reserved_by_unit),
        organisations_helped=len(orgs),
    )
