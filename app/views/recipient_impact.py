"""FoodLoop — Recipient impact.

Thin UI over ``foodloop.impact.recipient_impact``. Counts confirmed
collections only, quantities stay in original units, and no environmental or
"meals" figures are produced. Distinct from the township-wide Insights page.
"""

from __future__ import annotations

import streamlit as st

from components import ui
from components.service import get_service
from foodloop import impact

service = get_service()
persona = ui.current_persona(service)

st.title("Your impact")

if persona.role != "recipient" or persona.recipient is None:
    st.info("Switch to a **Recipient** persona on the Profile page to see your impact.")
    st.stop()

recipient = persona.recipient
ri = impact.recipient_impact(service.repo, recipient.id)
st.caption(f"Impact for **{recipient.name}**.")

if not (ri.confirmed_collections or ri.active_claims
        or ri.expired_claims or ri.cancelled_claims):
    st.info("No claims yet. Browse the **Marketplace** to find eligible donations to "
            "collect, and your impact will appear here.")
    st.stop()

with st.container(border=True, key="fl_metricgrid"):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Confirmed collections", ri.confirmed_collections,
              help="Only food you have explicitly confirmed as collected.")
    c2.metric("In progress", ri.active_claims, help="Claimed, not yet collected.")
    c3.metric("Donors supported", ri.donors_supported,
              help="Distinct donors whose food you have collected.")
    c4.metric("Pickup rate",
              "—" if ri.pickup_rate is None else f"{ri.pickup_rate * 100:.0f}%",
              help="Collected ÷ (collected + expired) claims. Cancelling releases "
                   "the food in time, so cancelled claims don't lower it.")
    if ri.pickup_rate is None:
        st.caption("Pickup rate needs at least one *concluded* claim "
                   "(collected or expired).")
    else:
        st.caption(
            f"Pickup rate = {ri.confirmed_collections} collected ÷ "
            f"{ri.rate_denominator} concluded claims · {ri.expired_claims} expired "
            f"before pickup · {ri.cancelled_claims} cancelled (not counted)."
        )

ui.section_title("Food rescued (confirmed)", "🍽️")
if ri.collected_by_unit:
    cols = st.columns(min(len(ri.collected_by_unit), 3))
    for i, (unit, total) in enumerate(sorted(ri.collected_by_unit.items())):
        shown = int(total) if float(total).is_integer() else round(total, 1)
        cols[i % len(cols)].metric(unit.title(), shown)
    st.caption("Collected-claim quantities, grouped by original unit — never "
               "converted between units (e.g. loaves to kg).")
else:
    st.info("No confirmed collections yet.")

if ri.reserved_by_unit:
    st.caption("Reserved but **not yet collected** (not counted as rescued): "
               + ", ".join(
                   f"{int(q) if float(q).is_integer() else round(q, 1)} {u}"
                   for u, q in sorted(ri.reserved_by_unit.items())))

st.caption("Counts **confirmed collections only**. FoodLoop does not estimate carbon "
           "savings or ‘meals provided’.")
