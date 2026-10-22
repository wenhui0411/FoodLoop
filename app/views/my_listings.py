"""FoodLoop — Donor: manage your own listings (status + withdraw), see who
claimed what, and confirm handovers by pickup code."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from components import ui
from components.service import get_service
from foodloop.errors import FoodLoopError
from foodloop.models import ClaimStatus, DonationStatus

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
# The result is shown inline (not only as a toast, which a quick rerun can
# swallow) and the field clears, so a donor always sees what just happened.
with st.form("confirm_handover", clear_on_submit=True):
    code = st.text_input("Pickup code", key="handover_code", placeholder="e.g. 7PQ2KD")
    if st.form_submit_button("Confirm handover", type="primary"):
        try:
            claim = service.confirm_handover(donor.id, code)
            done = service.repo.get_donation(claim.donation_id)
            who = service.repo.get_recipient(claim.recipient_id).name
            result = ("success", f"Handover confirmed ({claim.pickup_code}): "
                      f"{ui.format_amount(done, claim.quantity_value)} of “{done.title}” "
                      f"collected by {who}. It now counts toward impact.")
        except FoodLoopError as exc:
            result = ("error", f"Not confirmed: {exc}")
        st.session_state["_handover_result"] = (donor.id, *result)
        st.rerun()
last = st.session_state.get("_handover_result")
if last and last[0] == donor.id:
    (st.success if last[1] == "success" else st.error)(last[2])

ui.section_title("Your listings", "📋")

mine = service.repo.list_donations(donor_id=donor.id)
if not mine:
    st.info("You have not listed any food yet. Use the **Donate** page to create your "
            "first listing.")
    st.stop()

recipients = {r.id: r for r in service.repo.list_recipients()}
claims_by_donation = {d.id: service.repo.list_claims_for_donation(d.id) for d in mine}
waiting = [c for cs in claims_by_donation.values() for c in cs
           if c.status is ClaimStatus.RESERVED]
if waiting:
    n_listings = len({c.donation_id for c in waiting})
    st.info(f"**{len(waiting)} pickup{'s' if len(waiting) != 1 else ''} waiting** on "
            f"{n_listings} listing{'s' if n_listings != 1 else ''}. Ask the recipient "
            "for their pickup code and confirm it above.")

_OPEN = (DonationStatus.AVAILABLE, DonationStatus.CLAIMED, DonationStatus.RESERVED)


def render_listing(d) -> None:
    avail = service.repo.donation_availability(d.id)
    with st.container(border=True):
        a, b, c = st.columns([4, 1.2, 1.2])
        text, css = ui.format_deadline(d.collection_deadline, now)
        a.markdown(
            f"{ui.category_emoji(d.category)} **{ui.escape(d.title)}**  \n"
            f'<span class="fl-meta">{ui.format_availability(d, avail)} · '
            f"{ui.category_label(d.category)} · collect <span class='{css}'>{text}"
            "</span></span>",
            unsafe_allow_html=True,
        )
        b.markdown(ui.status_badge(d.status), unsafe_allow_html=True)
        can_close = d.status in _OPEN and not d.closed_to_claims
        if can_close:
            if c.button("Stop offering", key=f"withdraw-{d.id}"):
                try:
                    service.cancel_donation(d.id)
                    ui.flash(f"Closed “{d.title}” to new claims. Existing "
                             "reservations are kept.", icon="↩️")
                except FoodLoopError as exc:
                    ui.flash(str(exc), icon="⚠️")
                st.rerun()
        if d.closed_to_claims and d.status in _OPEN:
            st.caption("Closed to new claims. Existing pickups can still be confirmed.")
        # Who claimed what. Pickup codes are deliberately not shown to the donor:
        # the recipient reads theirs out at handover.
        for claim in claims_by_donation[d.id]:
            r = recipients.get(claim.recipient_id)
            st.markdown(
                f'<span class="fl-meta">• {ui.escape(r.name if r else claim.recipient_id)}'
                f" — {ui.format_amount(d, claim.quantity_value)} · "
                f"<b>{ui.CLAIM_LABEL[claim.status]}</b></span>",
                unsafe_allow_html=True,
            )


active = [d for d in mine if d.status in _OPEN]
past = [d for d in mine if d.status not in _OPEN]
tab_active, tab_past, tab_claims = st.tabs(
    [f"Active ({len(active)})", f"Past ({len(past)})", "Claim history"]
)
with tab_active:
    if not active:
        st.caption("No active listings. Use the **Donate** page to list surplus food.")
    for d in active:
        render_listing(d)
with tab_past:
    if not past:
        st.caption("Collected, cancelled and expired listings will appear here.")
    for d in past:
        render_listing(d)
with tab_claims:
    by_id = {d.id: d for d in mine}
    history = sorted((c for cs in claims_by_donation.values() for c in cs),
                     key=lambda c: c.claimed_at, reverse=True)
    if not history:
        st.caption("Claims on your listings will appear here.")
    else:
        st.dataframe(pd.DataFrame([
            {
                "Listing": by_id[c.donation_id].title,
                "Recipient": recipients[c.recipient_id].name
                if c.recipient_id in recipients else c.recipient_id,
                "Quantity": ui.format_amount(by_id[c.donation_id], c.quantity_value),
                "Status": ui.CLAIM_LABEL[c.status],
                "Claimed": c.claimed_at.strftime("%d %b %H:%M"),
                "Collected": c.collected_at.strftime("%d %b %H:%M") if c.collected_at else "",
            }
            for c in history
        ]), width="stretch", hide_index=True)
