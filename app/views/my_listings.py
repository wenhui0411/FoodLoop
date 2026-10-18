"""FoodLoop — Donor: manage your own listings (status + withdraw)."""

from __future__ import annotations

import streamlit as st

from components import ui
from components.service import get_service
from foodloop.errors import FoodLoopError
from foodloop.models import DonationStatus

service = get_service()
persona = ui.current_persona(service)

st.title("My listings")
st.caption("Everything you've listed and its current status. Stop offering a listing "
           "to close it to new claims — any existing reservations are kept.")

if persona.role != "donor" or persona.donor is None:
    st.info("Switch to a **Donor** persona on the Profile page to see your listings.")
    st.stop()

donor = persona.donor
now = service.now()

# --------------------------------------------------------------------------
# Confirm a handover by pickup code (the only path to a counted collection)
# --------------------------------------------------------------------------
ui.section_title("Confirm a collection", "✅")
st.caption("Enter the pickup code the recipient shows you to confirm the handover. "
           "Confirmation is single-use and counts toward impact. (Demo persona — "
           "not a real login.)")
with st.form("confirm_handover"):
    code = st.text_input("Pickup code", key="handover_code", placeholder="e.g. 7PQ2KD")
    if st.form_submit_button("Confirm handover", type="primary"):
        try:
            claim = service.confirm_handover(donor.id, code)
            ui.flash(f"Handover confirmed ({claim.pickup_code}). It now counts "
                     "toward impact.")
        except FoodLoopError as exc:
            ui.flash(str(exc), icon="⚠️")
        st.rerun()

ui.section_title("Your listings", "📋")

mine = service.repo.list_donations(donor_id=donor.id)
if not mine:
    st.info("You have not listed any food yet. Use the **Donate** page to create your "
            "first listing.")
    st.stop()

for d in mine:
    with st.container(border=True):
        a, b, c = st.columns([4, 1.2, 1.2])
        text, css = ui.format_deadline(d.collection_deadline, now)
        a.markdown(
            f"{ui.category_emoji(d.category)} **{ui.escape(d.title)}**  \n"
            f'<span class="fl-meta">{ui.format_quantity(d)} · '
            f"{ui.category_label(d.category)} · collect <span class='{css}'>{text}"
            "</span></span>",
            unsafe_allow_html=True,
        )
        b.markdown(ui.status_badge(d.status), unsafe_allow_html=True)
        can_close = (
            d.status in (DonationStatus.AVAILABLE, DonationStatus.RESERVED)
            and not d.closed_to_claims
        )
        if can_close:
            if c.button("Stop offering", key=f"withdraw-{d.id}"):
                try:
                    service.cancel_donation(d.id)
                    ui.flash(f"Closed “{d.title}” to new claims. Existing "
                             "reservations are kept.", icon="↩️")
                except FoodLoopError as exc:
                    ui.flash(str(exc), icon="⚠️")
                st.rerun()
