"""Township-level insights — descriptive summaries over the donation data.

Framework-free and read-only. Everything here is **descriptive**: counts and
simple rates over the data that exists. Nothing here claims causation, and
nothing estimates environmental benefit (carbon) or "meals provided" — there
is no defensible methodology or data for those, so they are deliberately
absent.

Small samples are handled honestly: per-area/category collection rates are
only produced once enough donations have *concluded* (collected or expired);
below that threshold the rate is ``None`` and the caller shows the raw counts
with an "insufficient data" note rather than a misleading percentage.

The demo-vs-user split is injected (``demo_ids``) so this module stays
decoupled from ``data/seed.py``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from .models import ClaimStatus, DonationStatus
from .repository import Repository

#: Minimum number of *concluded claims* before a collection rate is reported.
MIN_SAMPLE = 3


@dataclass(slots=True)
class DatasetComposition:
    total: int
    demo: int
    user: int


@dataclass(slots=True)
class AreaStat:
    """Per-area figures. ``listed`` counts donation *listings*; ``collected``
    and ``expired`` count *claims* (confirmed handovers / lapsed claims), so
    partial collections are reflected accurately."""

    area: str
    listed: int             # donation listings in the area
    collected: int          # COLLECTED claims (handovers)
    expired: int            # EXPIRED claims
    concluded: int          # collected + expired claims
    collection_rate: float | None   # None when concluded < MIN_SAMPLE


@dataclass(slots=True)
class CategoryStat:
    category: str
    listed: int             # donation listings
    collected: int          # COLLECTED claims
    expired: int            # EXPIRED claims


@dataclass(slots=True)
class TownshipOverview:
    """Claim-accurate headline figures for the Home 'township pulse'."""

    available_listings: int             # donations still open to claims
    reserved_claims: int                # awaiting pickup
    collected_claims: int               # confirmed handovers
    organisations_helped: int           # distinct recipients with a collection
    listed_per_day: dict[str, int] = field(default_factory=dict)
    collected_per_day: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class Observation:
    title: str
    detail: str
    counts: str      # the supporting counts, stated plainly


def dataset_composition(
    repo: Repository, demo_ids: frozenset[str] | set[str]
) -> DatasetComposition:
    """Split the dataset into synthetic (seed) vs user-created donations."""
    donations = repo.list_donations()
    demo = sum(1 for d in donations if d.id in demo_ids)
    return DatasetComposition(total=len(donations), demo=demo, user=len(donations) - demo)


def data_period(repo: Repository) -> tuple[datetime, datetime] | None:
    """Earliest and latest donation ``created_at``, or None if no data."""
    donations = repo.list_donations()
    if not donations:
        return None
    times = [d.created_at for d in donations]
    return min(times), max(times)


def _grouped_counts(repo: Repository, key):
    """Bucket listings (donations) and claim outcomes by ``key(donation)``.

    Returns ``{group: {"listed": int, "collected": int, "expired": int}}``,
    where ``collected``/``expired`` count *claims* (joined to their donation's
    group) — so partial collections are counted at the handover level.
    """
    donations = {d.id: d for d in repo.list_donations()}
    buckets: dict[str, dict[str, int]] = defaultdict(
        lambda: {"listed": 0, "collected": 0, "expired": 0}
    )
    for d in donations.values():
        buckets[key(d)]["listed"] += 1
    for claim in repo.list_claims():
        donation = donations.get(claim.donation_id)
        if donation is None:
            continue
        if claim.status is ClaimStatus.COLLECTED:
            buckets[key(donation)]["collected"] += 1
        elif claim.status is ClaimStatus.EXPIRED:
            buckets[key(donation)]["expired"] += 1
    return buckets


def area_breakdown(repo: Repository, *, min_sample: int = MIN_SAMPLE) -> list[AreaStat]:
    """Per-area listings + claim-level handovers, with a claim-based collection
    rate where the sample supports it. Ordered by listings desc, then area."""
    buckets = _grouped_counts(repo, lambda d: d.location.area)
    stats: list[AreaStat] = []
    for area, b in buckets.items():
        concluded = b["collected"] + b["expired"]
        rate = (b["collected"] / concluded) if concluded >= min_sample else None
        stats.append(AreaStat(area, b["listed"], b["collected"], b["expired"],
                              concluded, rate))
    stats.sort(key=lambda s: (-s.listed, s.area))
    return stats


def category_breakdown(repo: Repository) -> list[CategoryStat]:
    """Per-category listings + claim-level handover counts. Deterministic."""
    buckets = _grouped_counts(repo, lambda d: d.category.value)
    stats = [
        CategoryStat(cat, b["listed"], b["collected"], b["expired"])
        for cat, b in buckets.items()
    ]
    stats.sort(key=lambda s: (-s.listed, s.category))
    return stats


def township_overview(repo: Repository) -> TownshipOverview:
    """Claim-accurate headline figures + daily trend for the Home pulse."""
    donations = repo.list_donations()
    available = sum(1 for d in donations if d.status is DonationStatus.AVAILABLE)
    listed_per_day: dict[str, int] = defaultdict(int)
    for d in donations:
        listed_per_day[d.created_at.date().isoformat()] += 1

    reserved = collected = 0
    orgs: set[str] = set()
    collected_per_day: dict[str, int] = defaultdict(int)
    for claim in repo.list_claims():
        if claim.status is ClaimStatus.RESERVED:
            reserved += 1
        elif claim.status is ClaimStatus.COLLECTED:
            collected += 1
            orgs.add(claim.recipient_id)
            if claim.collected_at is not None:
                collected_per_day[claim.collected_at.date().isoformat()] += 1

    return TownshipOverview(
        available_listings=available,
        reserved_claims=reserved,
        collected_claims=collected,
        organisations_helped=len(orgs),
        listed_per_day=dict(listed_per_day),
        collected_per_day=dict(collected_per_day),
    )


def _period_text(repo: Repository, now: datetime) -> str:
    period = data_period(repo)
    if period is None:
        return "no data yet"
    start, _ = period
    return f"{start:%d %b %Y} – {now:%d %b %Y}"


def observations(
    repo: Repository, *, now: datetime, min_sample: int = MIN_SAMPLE
) -> list[Observation]:
    """A few transparent, descriptive observations with supporting counts.

    Returns an empty list when too little has concluded to say anything
    meaningful — the caller shows a graceful "insufficient data" message
    rather than inventing an insight.
    """
    donations = {d.id: d for d in repo.list_donations()}
    claims = repo.list_claims()
    collected = [c for c in claims if c.status is ClaimStatus.COLLECTED]
    expired = [c for c in claims if c.status is ClaimStatus.EXPIRED]
    concluded = len(collected) + len(expired)
    if concluded < min_sample:
        return []

    period = _period_text(repo, now)
    obs: list[Observation] = []

    # 1. Overall missed-collection rate (claim-based).
    if expired:
        pct = len(expired) / concluded * 100
        obs.append(Observation(
            "Missed collections",
            f"{len(expired)} of {concluded} concluded claims expired before "
            f"collection ({pct:.0f}%). Reducing these is the clearest "
            "redistribution win.",
            f"{len(expired)} expired / {concluded} concluded claims · {period}",
        ))
    else:
        obs.append(Observation(
            "No missed collections",
            f"All {concluded} concluded claims were collected — no expiries "
            "in this period.",
            f"0 expired / {concluded} concluded claims · {period}",
        ))

    # 2. Area with the most expired claims (deterministic tie-break by name).
    if expired:
        area_counts = Counter(
            donations[c.donation_id].location.area
            for c in expired if c.donation_id in donations
        )
        area, count = sorted(area_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        obs.append(Observation(
            "Where collections are missed",
            f"Most missed collections were in {area}. This is a descriptive "
            "pattern, not a cause — worth a closer look at collection logistics "
            "there.",
            f"{count} of {len(expired)} expiries in {area} · {period}",
        ))

    # 3. Category with the highest expiry rate among sufficiently-sampled ones.
    cat_stats = [
        c for c in category_breakdown(repo)
        if (c.collected + c.expired) >= min_sample and c.expired > 0
    ]
    if cat_stats:
        # Highest expiry rate, deterministic tie-break by category name.
        worst = sorted(
            cat_stats,
            key=lambda c: (-(c.expired / (c.collected + c.expired)), c.category),
        )[0]
        concluded_c = worst.collected + worst.expired
        rate_c = worst.expired / concluded_c * 100
        obs.append(Observation(
            "Category to watch",
            f"‘{worst.category.replace('_', ' ')}’ has the highest expiry rate "
            f"({rate_c:.0f}%) among categories with enough data. Descriptive only.",
            f"{worst.expired} expired / {concluded_c} concluded · {period}",
        ))

    return obs[:3]
