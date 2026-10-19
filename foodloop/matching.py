"""Explainable, rule-based recipient matching.

Given a donation, rank the recipient organisations that could receive it.

This is **not** machine learning and **not** a prediction of food safety or a
guarantee that collection will happen. It is a deterministic, transparent
score computed from data the organisations provided, and every number comes
with a plain-language reason.

Design principles
-----------------
* **Eligibility is a hard gate, applied first.** Ranking only ever reorders
  recipients that already pass :func:`foodloop.eligibility.check_eligibility`.
  A high score can never resurrect an ineligible recipient — the two concerns
  are kept strictly separate.
* **Score only what the data supports.** Each factor reports whether its data
  is available. Missing data (e.g. no coordinates, non-comparable units) is
  reported honestly and simply left out of the weighted average — it is never
  invented and never counted as a zero that would unfairly punish a recipient.
* **Deterministic.** Same inputs → same ordering, every time. Ties are broken
  by recipient name so the order is stable and reproducible.

Factors and weights
--------------------
Weights apply to whichever factors are *available* for a given recipient, and
are renormalised over those. Defaults (documented, not tuned to look clever):

* ``timing`` (0.40) — how soon the recipient could collect, relative to the
  deadline. Most operationally important: it reflects whether the food is
  likely to actually move in time. Always available for eligible recipients.
* ``distance`` (0.35) — proximity of recipient to the donation. Available only
  when both have coordinates.
* ``capacity`` (0.25) — spare capacity after taking the donation. A softer
  comfort signal; available only when the units are directly comparable (kg).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from .eligibility import can_collect_before, capacity_fits, check_eligibility
from .models import WEEKDAYS, Donation, Location, Recipient, Unit

# Maximum distance (km) at which proximity still scores above zero. Chosen for
# a township-scale catchment; documented, not fitted to the demo data.
MAX_DISTANCE_KM = 15.0

DEFAULT_WEIGHTS: dict[str, float] = {
    "timing": 0.40,
    "distance": 0.35,
    "capacity": 0.25,
}


@dataclass(frozen=True, slots=True)
class RankingFactor:
    """One contribution to a recipient's ranking score."""

    name: str
    available: bool
    score: float | None   # normalised [0, 1] when available, else None
    weight: float
    detail: str           # human-readable explanation (always set)


@dataclass(slots=True)
class Recommendation:
    """A ranked, explained recipient recommendation for a donation."""

    recipient: Recipient
    score: float                       # overall, in [0, 1]
    factors: list[RankingFactor] = field(default_factory=list)

    @property
    def available_factors(self) -> list[RankingFactor]:
        return [f for f in self.factors if f.available]

    @property
    def reasons(self) -> list[str]:
        """Plain-language reasons, available factors first."""
        return [f.detail for f in self.factors if f.available] + [
            f.detail for f in self.factors if not f.available
        ]


# --------------------------------------------------------------------------
# Factor helpers (pure)
# --------------------------------------------------------------------------
def haversine_km(a: Location, b: Location) -> float | None:
    """Great-circle distance in km, or None if either side lacks coordinates."""
    if not (a.has_coordinates and b.has_coordinates):
        return None
    r = 6371.0
    lat1, lon1, lat2, lon2 = map(
        math.radians, [a.latitude, a.longitude, b.latitude, b.longitude]
    )
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def earliest_collection(
    recipient: Recipient, deadline: datetime, now: datetime
) -> datetime | None:
    """First moment the recipient could collect within ``[now, deadline]``.

    Mirrors the window logic in :func:`eligibility.can_collect_before` but
    returns *when* rather than *whether*. None if there is no window (which,
    for an eligible recipient, cannot happen).
    """
    if deadline <= now or not recipient.collection_days:
        return None
    day = now.date()
    end_day = deadline.date()
    while day <= end_day:
        if WEEKDAYS[day.weekday()] in recipient.collection_days:
            midnight = datetime.combine(day, time(0, 0))
            win_start = midnight + timedelta(hours=recipient.collection_start_hour)
            win_end = midnight + timedelta(hours=recipient.collection_end_hour)
            lo = max(win_start, now)
            hi = min(win_end, deadline)
            if lo < hi:
                return lo
        day += timedelta(days=1)
    return None


def _timing_factor(
    recipient: Recipient, donation: Donation, now: datetime
) -> RankingFactor:
    weight = DEFAULT_WEIGHTS["timing"]
    earliest = earliest_collection(recipient, donation.collection_deadline, now)
    total = (donation.collection_deadline - now).total_seconds()
    if earliest is None or total <= 0:
        # Should not occur for eligible recipients; handled defensively.
        return RankingFactor("timing", False, None, weight,
                             "Collection timing could not be determined.")
    margin = (donation.collection_deadline - earliest).total_seconds() / total
    margin = max(0.0, min(1.0, margin))
    return RankingFactor(
        "timing", True, margin, weight,
        f"Could collect from {earliest:%a %H:%M} "
        f"({margin * 100:.0f}% of the time before the deadline still spare).",
    )


def _distance_factor(recipient: Recipient, donation: Donation) -> RankingFactor:
    weight = DEFAULT_WEIGHTS["distance"]
    dist = haversine_km(donation.location, recipient.location)
    if dist is None:
        return RankingFactor(
            "distance", False, None, weight,
            "Distance unavailable — donation or recipient has no coordinates.",
        )
    score = max(0.0, 1.0 - dist / MAX_DISTANCE_KM)
    return RankingFactor(
        "distance", True, score, weight, f"About {dist:.1f} km from the food.",
    )


def _capacity_factor(recipient: Recipient, donation: Donation) -> RankingFactor:
    weight = DEFAULT_WEIGHTS["capacity"]
    fits, comparable = capacity_fits(recipient, donation)
    if not comparable or recipient.max_quantity_kg_equiv is None:
        return RankingFactor(
            "capacity", False, None, weight,
            "Capacity fit not comparable for this unit — not scored.",
        )
    cap = recipient.max_quantity_kg_equiv
    qty = donation.quantity.value
    # Reward spare headroom for taking the whole listing. A smaller recipient
    # stays eligible (partial claims) but scores 0 here.
    headroom = max(0.0, min(1.0, 1.0 - qty / cap)) if cap > 0 else 0.0
    if not fits:
        return RankingFactor(
            "capacity", True, 0.0, weight,
            f"Could take ~{cap:g} of ~{qty:g} kg (partial collection; no headroom).",
        )
    return RankingFactor(
        "capacity", True, headroom, weight,
        f"Needs ~{qty:g} of ~{cap:g} kg capacity "
        f"({headroom * 100:.0f}% headroom to spare).",
    )


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def score_recipient(
    recipient: Recipient, donation: Donation, now: datetime
) -> Recommendation:
    """Compute the (already-eligible) recipient's factors and overall score.

    The overall score is the weighted mean over *available* factors only, so a
    recipient is judged on what is actually known about it.
    """
    factors = [
        _timing_factor(recipient, donation, now),
        _distance_factor(recipient, donation),
        _capacity_factor(recipient, donation),
    ]
    available = [f for f in factors if f.available and f.score is not None]
    total_weight = sum(f.weight for f in available)
    if total_weight > 0:
        overall = sum(f.weight * f.score for f in available) / total_weight
    else:
        overall = 0.0
    return Recommendation(recipient=recipient, score=overall, factors=factors)


def rank_recipients(
    donation: Donation, recipients: list[Recipient], *, now: datetime,
    held: Mapping[str, float] | None = None,
) -> list[Recommendation]:
    """Return eligible recipients, ranked best-first, with explanations.

    Eligibility (the hard gate in :mod:`foodloop.eligibility`) is applied
    first; only recipients that pass are scored. ``held`` maps recipient id
    to the quantity it already holds from this donation, so one that has
    used up its per-donation capacity is excluded (omitted: nothing held).
    Ordering is deterministic: by score descending, then recipient name
    ascending to break ties. Scores themselves do not depend on ``held``.
    """
    held = held or {}
    eligible = [
        r for r in recipients
        if check_eligibility(
            r, donation, now=now, already_held=held.get(r.id, 0.0)
        ).eligible
    ]
    recs = [score_recipient(r, donation, now) for r in eligible]
    recs.sort(key=lambda rec: (-rec.score, rec.recipient.name))
    return recs
