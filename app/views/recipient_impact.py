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

if ri.confirmed_collections == 0 and ri.active_claims == 0:
    st.info("No claims yet. Browse the **Marketplace** to find eligible donations to "
            "collect, and your impact will appear here.")
    st.stop()

with st.container(border=True, key="fl_metricgrid"):
    c1, c2, c3 = st.columns(3)
    c1.metric("Confirmed collections", ri.confirmed_collections,
              help="Only food you have explicitly confirmed as collected.")
    c2.metric("In progress", ri.active_claims, help="Claimed, not yet collected.")
    c3.metric("Donors supported", ri.donors_supported,
              help="Distinct donors whose food you have collected.")

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
