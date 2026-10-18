"""FoodLoop — Donor impact & donation history.

Thin UI over ``foodloop.impact.donor_impact``; no accounting logic here.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from components import ui
from components.service import get_service
from foodloop import impact

service = get_service()
persona = ui.current_persona(service)

st.title("Your impact")

if persona.role != "donor" or persona.donor is None:
    st.info("Switch to a **Donor** persona on the Profile page to see donor impact.")
    st.stop()

donor = persona.donor
di = impact.donor_impact(service.repo, donor.id)
st.caption(f"Impact for **{donor.name}**.")

if di.total_listed == 0:
    st.info("You have not listed any food yet. Create a listing on the **Donate** "
            "page and your impact will appear here.")
    st.stop()

# --------------------------------------------------------------------------
# Headline metrics
# --------------------------------------------------------------------------
with st.container(border=True, key="fl_metricgrid"):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Listings", di.total_listed, help="Donations you have listed.")
    c2.metric("Confirmed collections", di.confirmed_collections,
              help="Confirmed handovers (collected claims). A listing can have "
                   "several.")
    c3.metric("Awaiting pickup", di.reserved_claims,
              help="Reserved claims not yet collected.")
    c4.metric("Expired claims", di.expired_claims,
              help="Reserved but not collected before the deadline.")

    # Collection rate with an explicit numerator and denominator (claim-based).
    if di.collection_rate is None:
        st.metric("Collection rate", "—")
        st.caption("Collection rate needs at least one *concluded* claim "
                   "(collected or expired). Nothing has concluded yet.")
    else:
        st.metric("Collection rate", f"{di.collection_rate * 100:.0f}%")
        st.caption(
            f"Collection rate = collected ÷ concluded claims = "
            f"{di.confirmed_collections} ÷ {di.rate_denominator}, where concluded "
            "= collected + expired claims. Reserved and cancelled claims are "
            "excluded."
        )

# --------------------------------------------------------------------------
# Rescued vs reserved quantities + organisations helped
# --------------------------------------------------------------------------
left, right = st.columns([2, 1])
with left:
    ui.section_title("Food rescued (confirmed)", "🍽️")
    if di.collected_by_unit:
        cols = st.columns(min(len(di.collected_by_unit), 3))
        for i, (unit, total) in enumerate(sorted(di.collected_by_unit.items())):
            shown = int(total) if float(total).is_integer() else round(total, 1)
            cols[i % len(cols)].metric(unit.title(), shown)
        st.caption("Collected-claim quantities, grouped by original unit — never "
                   "converted between units (e.g. loaves to kg).")
    else:
        st.info("No confirmed collections yet.")

    if di.reserved_by_unit:
        st.caption("Reserved but **not yet collected** (not counted as rescued): "
                   + ", ".join(
                       f"{int(q) if float(q).is_integer() else round(q, 1)} {u}"
                       for u, q in sorted(di.reserved_by_unit.items())))

with right:
    ui.section_title("Reach", "🤝")
    st.metric("Organisations helped", di.organisations_helped,
              help="Distinct recipient organisations that confirmed a collection.")

st.divider()

# --------------------------------------------------------------------------
# Achievements (strictly from confirmed collections)
# --------------------------------------------------------------------------
ui.section_title("Achievements", "🏅")
points = di.confirmed_collections
st.caption("Impact points — **1 point per confirmed collection** (handover). "
           "Reserved, cancelled or expired claims earn nothing.")
milestones = [1, 5, 10, 25, 50, 100]
next_milestone = next((m for m in milestones if m > points), None)
if next_milestone is None:
    st.success(f"🏆 {points} points — top milestone reached.")
else:
    st.progress(points / next_milestone,
                text=f"{points} / {next_milestone} points to the next milestone")

st.divider()

# --------------------------------------------------------------------------
# Donation history
# --------------------------------------------------------------------------
ui.section_title("Donation history", "📜")
rows = [
    {
        "Title": d.title,
        "Category": ui.category_label(d.category),
        "Quantity": ui.format_quantity(d),
        "Status": ui._STATUS_STYLE[d.status][0],
        "Deadline": d.collection_deadline.strftime("%d %b %H:%M"),
        "Listed": d.created_at.strftime("%d %b"),
    }
    for d in service.repo.list_donations(donor_id=donor.id)
]
st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
