"""FoodLoop — Donation marketplace (recipient mode).

Browse, search, filter and sort surplus-food listings, open a detail view
(with rule-based recipient recommendations), and — when acting as an eligible
recipient — claim an available donation. Rendered inside the role-based
navigation defined in ``app/Home.py``.
"""

from __future__ import annotations

import streamlit as st

from components import ui
from components.service import get_service
from foodloop.errors import (
    ClaimsClosedError,
    DuplicateClaimError,
    EligibilityError,
    InvalidTransitionError,
    OverReservationError,
    ValidationError,
)
from foodloop.matching import haversine_km
from foodloop.models import DonationStatus, FoodCategory, StorageRequirement
from foodloop.validation import is_whole_unit

service = get_service()
persona = ui.current_persona(service)

st.title("Donation marketplace")
st.caption("Browse surplus food available for collection. Use the filters to narrow "
           "by category, status or storage.")

donors = {d.id: d for d in service.repo.list_donors()}
all_donations = service.repo.list_donations()
now = service.now()

_acting_recipient = persona.recipient if persona.role == "recipient" else None


def do_claim(donation_id: str, quantity: float) -> None:
    """Reserve ``quantity`` of a donation for the active recipient persona.

    Unit precision and the ``<= remaining`` limit are enforced by the service
    and repository (atomically), so a stale page or a concurrent reserver
    gets a clear message here rather than an over-reservation.
    """
    if _acting_recipient is None:
        return
    try:
        claim = service.claim_donation(donation_id, _acting_recipient.id, quantity)
        donation = service.repo.get_donation(donation_id)
        ui.flash(f"Reserved {ui.format_amount(donation, claim.quantity_value)}. "
                 "Show your pickup code on the ‘My Claims’ page.")
    except ValidationError as exc:
        ui.flash(" ".join(exc.errors.values()), icon="⚠️")
    except (EligibilityError, DuplicateClaimError, InvalidTransitionError,
            OverReservationError, ClaimsClosedError) as exc:
        ui.flash(str(exc), icon="⚠️")
    # Keyed widgets keep their last value across reruns; drop it (the widget
    # already exists this run, so it can't be assigned) and the picker
    # re-seeds the fresh claimable quantity on the next run.
    for key in (f"claim-qty-{donation_id}", f"claim-qty-dialog-{donation_id}"):
        st.session_state.pop(key, None)
    st.rerun()


def claim_quantity_input(d, key: str) -> float:
    """Quantity picker for a claim, defaulting to the most this recipient may
    take (remaining, capped by its capacity — computed by the service).

    No ``max_value`` on purpose: if someone else reserves in the meantime the
    request must still reach the service and be rejected with a clear
    message, not trip a widget bounds error on rerun.
    """
    default = service.claimable_quantity(d.id, _acting_recipient.id)
    unit = d.quantity.unit
    label = f"Quantity to reserve ({unit.value})"
    whole = is_whole_unit(unit) and float(default).is_integer()
    # Seed via session state (assigned, not passed as value=) so Streamlit
    # pushes the fresh default to the browser after do_claim drops the key.
    if key not in st.session_state:
        st.session_state[key] = int(default) if whole else float(default)
    if whole:
        return st.number_input(label, min_value=1, step=1, key=key)
    return st.number_input(label, min_value=0.0, step=0.1, format="%.3f", key=key)


# --------------------------------------------------------------------------
# Detail dialog
# --------------------------------------------------------------------------
@st.dialog("Donation details", width="large")
def show_detail(donation_id: str) -> None:
    d = service.repo.get_donation(donation_id)
    avail = service.repo.donation_availability(d.id)
    donor = donors.get(d.donor_id) or service.repo.get_donor(d.donor_id)

    head, badge = st.columns([4, 1])
    head.markdown(f"### {ui.category_emoji(d.category)} {d.title}")
    badge.markdown(ui.status_badge(d.status), unsafe_allow_html=True)

    st.markdown(d.description or "_No description provided._")

    a, b = st.columns(2)
    with a:
        st.markdown("**Donor**")
        st.markdown(
            f"{ui.escape(donor.name)}  \n"
            f'<span class="fl-meta">{donor.donor_type.value.title()} · '
            f"{ui.escape(d.location.area)}</span>",
            unsafe_allow_html=True,
        )
        st.markdown("**Quantity**")
        st.markdown(
            f"Listed: {ui.format_quantity(d)}  \n"
            f"Reserved: {ui.format_amount(d, avail.reserved)} · "
            f"Collected: {ui.format_amount(d, avail.collected)}  \n"
            f"**Available now: {ui.format_amount(d, avail.remaining)}**"
        )
        st.markdown("**Category**")
        st.markdown(ui.category_label(d.category))
    with b:
        text, css = ui.format_deadline(d.collection_deadline, now)
        st.markdown("**Collection deadline**")
        st.markdown(
            f"{d.collection_deadline:%a %d %b, %H:%M}  \n"
            f'<span class="{css}">{text}</span>',
            unsafe_allow_html=True,
        )
        st.caption("The deadline is about *when* food can be collected — "
                   "it is not a food-safety judgement.")

    st.divider()
    ui.section_title("Food safety & handling", "🧊")
    st.caption("Donor-provided. Safety and shelf life are never inferred from an image.")
    s1, s2 = st.columns(2)
    s1.markdown(f"**Storage:** {ui.storage_label(d.storage)}")
    s1.markdown(f"**{ui.allergen_summary(d)}**")
    if d.best_before is not None:
        s2.markdown(f"**Best before:** {d.best_before:%a %d %b}")
        s2.caption("Best-before is distinct from the collection deadline.")
    if d.handling_notes:
        st.markdown(f"**Handling notes:** {d.handling_notes}")

    # Recommended recipients (only meaningful while the food is available).
    if d.status is DonationStatus.AVAILABLE:
        st.divider()
        ui.section_title("Recommended recipients", "🧭")
        st.caption(
            "A **rule-based** ranking of *eligible* organisations, computed from the "
            "data they provided. It is not an AI prediction, not a food-safety "
            "judgement, and not a guarantee that collection will happen."
        )
        recs = service.recommend_recipients(d.id)
        if not recs:
            st.info("No eligible recipients to recommend for this donation right now.")
        else:
            for rank, rec in enumerate(recs[:5], start=1):
                with st.container(border=True):
                    st.markdown(
                        f"**{rank}. {rec.recipient.name}** — "
                        f"match score {rec.score * 100:.0f}%"
                    )
                    for factor in rec.factors:
                        marker = "✔" if factor.available else "–"
                        st.markdown(
                            f'<span class="fl-meta">{marker} {factor.detail}</span>',
                            unsafe_allow_html=True,
                        )
            st.caption(
                "“–” marks a factor with no data, which is left out of the score "
                "rather than guessed. Weights: timing 40%, distance 35%, capacity 25% "
                "(renormalised over available factors)."
            )

    # Recipient view: explain eligibility transparently and allow claiming.
    if _acting_recipient is not None:
        st.divider()
        ui.section_title("Eligibility for your organisation", "🤝")
        st.caption(
            f"Transparent, rule-based checks for **{_acting_recipient.name}** — "
            "not an AI judgement, and nothing here assesses food safety."
        )
        result = service.check_eligibility(d.id, _acting_recipient.id)
        for check in result.checks:
            st.markdown(f"{'✅' if check.passed else '❌'} {check.detail}")
        if d.status is not DonationStatus.AVAILABLE:
            st.info(f"This donation is '{d.status.value}' and cannot be claimed.")
        elif result.eligible:
            qty = claim_quantity_input(d, key=f"claim-qty-dialog-{d.id}")
            if st.button("Reserve this quantity", key=f"claim-dialog-{d.id}",
                         type="primary", width="stretch"):
                do_claim(d.id, qty)
        else:
            st.warning("Your organisation is not eligible to claim this donation "
                       "(see the ❌ items above).")


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------
with st.container(border=True):
    f1, f2, f3 = st.columns([2, 2, 1.3])
    query = f1.text_input("Search", placeholder="Food, donor or description…",
                          key="mk_query")
    categories = f2.multiselect(
        "Category",
        list(FoodCategory),
        format_func=ui.category_label,
        key="mk_category",
    )
    sort_by = f3.selectbox(
        "Sort by", ["Deadline (soonest)", "Nearest", "Newest", "Largest quantity"]
        if _acting_recipient is not None else
        ["Deadline (soonest)", "Newest", "Largest quantity"],
        key="mk_sort",
    )
    g1, g2 = st.columns([2, 2])
    statuses = g1.multiselect(
        "Status",
        list(DonationStatus),
        default=[DonationStatus.AVAILABLE],
        format_func=lambda s: ui._STATUS_STYLE[s][0],
        key="mk_status",
    )
    storages = g2.multiselect(
        "Storage",
        list(StorageRequirement),
        format_func=ui.storage_label,
        key="mk_storage",
    )
    eligible_only = False
    if _acting_recipient is not None:
        eligible_only = st.toggle(
            f"Only food {_acting_recipient.name} can claim", value=True,
            key="mk_eligible_only",
            help="Uses the same rule-based checks as claiming: verification, "
                 "category, storage, collection window and capacity.",
        )

# Eligibility for the acting recipient, computed once per listing by the
# service (the same checks claim_donation enforces).
eligibility = (
    {d.id: service.check_eligibility(d.id, _acting_recipient.id) for d in all_donations}
    if _acting_recipient is not None else {}
)


def _distance(d) -> float | None:
    if _acting_recipient is None:
        return None
    return haversine_km(_acting_recipient.location, d.location)


# --------------------------------------------------------------------------
# Apply filters + sorting
# --------------------------------------------------------------------------
def _matches(d) -> bool:
    if eligible_only and not eligibility[d.id].eligible:
        return False
    if statuses and d.status not in statuses:
        return False
    if categories and d.category not in categories:
        return False
    if storages and d.storage not in storages:
        return False
    if query:
        donor = donors.get(d.donor_id)
        haystack = " ".join(
            [d.title, d.description, donor.name if donor else "", d.location.area]
        ).lower()
        if query.lower() not in haystack:
            return False
    return True


results = [d for d in all_donations if _matches(d)]

if sort_by == "Deadline (soonest)":
    results.sort(key=lambda d: d.collection_deadline)
elif sort_by == "Nearest":  # listings without coordinates go last, never guessed
    results.sort(key=lambda d: (_distance(d) is None, _distance(d) or 0.0))
elif sort_by == "Newest":
    results.sort(key=lambda d: d.created_at, reverse=True)
else:  # Largest quantity (within-unit only; see caption)
    results.sort(key=lambda d: d.quantity.value, reverse=True)

st.caption(f"Showing **{len(results)}** of {len(all_donations)} listings"
           + (" that your organisation can claim." if eligible_only else "."))
if sort_by == "Largest quantity":
    st.caption("Quantity sort compares raw values; units are not converted.")
elif sort_by == "Nearest":
    st.caption("Distance is shown only where a listing and your organisation both have "
               "coordinates; listings without them are listed last, never guessed.")


# --------------------------------------------------------------------------
# Results grid / empty state
# --------------------------------------------------------------------------
if not results:
    st.info("No donations match your filters. Try clearing the search or widening "
            "the status/category selection"
            + (", or turn off “Only food … can claim” to see everything." if eligible_only
               else "."))
else:
    per_row = 3
    for start in range(0, len(results), per_row):
        cols = st.columns(per_row)
        for col, d in zip(cols, results[start:start + per_row]):
            donor = donors.get(d.donor_id)
            avail = service.repo.donation_availability(d.id)
            text, css = ui.format_deadline(d.collection_deadline, now)
            km = _distance(d)
            where = ui.escape(d.location.area) + (f" · 📍 {km:.1f} km" if km is not None else "")
            with col:
                card = st.container(border=True)
                with card:
                    st.markdown(ui.thumb(d.category), unsafe_allow_html=True)
                    tcol, scol = st.columns([3, 1.4])
                    tcol.markdown(
                        f'<div class="fl-title">{ui.escape(d.title)}</div>',
                        unsafe_allow_html=True,
                    )
                    scol.markdown(ui.status_badge(d.status), unsafe_allow_html=True)
                    st.markdown(
                        f'<div class="fl-meta">'
                        f'{ui.escape(donor.name) if donor else "—"} · '
                        f"{where}</div>"
                        f'<div class="fl-meta">{ui.format_availability(d, avail)} · '
                        f"{ui.category_label(d.category)}</div>"
                        f'<div class="fl-meta">⏰ Collect '
                        f'<span class="{css}">{text}</span></div>',
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        ui.chip(ui.storage_label(d.storage))
                        + ui.chip(ui.allergen_summary(d).replace("Allergens: ", "⚠ ")),
                        unsafe_allow_html=True,
                    )
                    if st.button("View details", key=f"detail-{d.id}",
                                 width="stretch"):
                        show_detail(d.id)

                    # Inline claim for a recipient persona on available listings.
                    if _acting_recipient is not None and d.status is DonationStatus.AVAILABLE:
                        result = eligibility[d.id]
                        if result.eligible:
                            qty = claim_quantity_input(d, key=f"claim-qty-{d.id}")
                            if st.button("Claim", key=f"claim-{d.id}",
                                         type="primary", width="stretch"):
                                do_claim(d.id, qty)
                        else:
                            more = len(result.reasons) - 1
                            st.caption(f"❌ Not eligible: {result.reasons[0]}"
                                       + (f" (+{more} more)" if more > 0 else "")
                                       + " — see details")
