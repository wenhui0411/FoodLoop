"""FoodLoop — Recipient: manage claimed donations.

Pickups waiting come first — each with its pickup code (the recipient shows it;
the donor confirms it, which is the only thing that counts a donation as
rescued), the collection deadline, and a way to release the claim. Finished
claims follow as a filterable history. All transitions go through
FoodLoopService.
"""

from __future__ import annotations

import streamlit as st

from components import ui
from components.service import get_service
from foodloop.errors import FoodLoopError
from foodloop.models import ClaimStatus

service = get_service()
persona = ui.current_persona(service)

st.title("My claims")
st.caption("Donations you've committed to collect. Show the donor your pickup code at "
           "handover — they confirm it, and only confirmed collections count toward "
           "impact.")

if persona.role != "recipient" or persona.recipient is None:
    st.info("Switch to a **Recipient** persona on the Profile page to manage claims.")
    st.stop()

recipient = persona.recipient
now = service.now()

claims = service.repo.list_claims(recipient_id=recipient.id)
if not claims:
    st.info(f"**{recipient.name}** has no claims yet. Browse the **Marketplace** to "
            "find eligible donations to claim.")
    st.stop()

st.caption(f"Showing claims for **{recipient.name}**.")
donations = {c.donation_id: service.repo.get_donation(c.donation_id) for c in claims}
donors = {d.id: d for d in service.repo.list_donors()}
active = [c for c in claims if c.status is ClaimStatus.RESERVED]
history = [c for c in claims if c.status is not ClaimStatus.RESERVED]


def _summary(donation) -> str:
    donor = donors[donation.donor_id]
    return (
        f"{ui.category_emoji(donation.category)} **{ui.escape(donation.title)}**  \n"
        f'<span class="fl-meta">{ui.escape(donor.name)}, '
        f"{ui.escape(donation.location.area)}</span>  \n"
        f'<span class="fl-meta">Storage: {ui.storage_label(donation.storage)} · '
        f"{ui.allergen_summary(donation)}</span>"
    )


# --------------------------------------------------------------------------
# Ready for pickup: code, deadline, cancel
# --------------------------------------------------------------------------
ui.section_title(f"Ready for pickup ({len(active)})", "🎟️")
if not active:
    st.info("No pickups waiting. Claim food in the **Marketplace** and its pickup "
            "code will appear here.")

for claim in active:
    donation = donations[claim.donation_id]
    with st.container(border=True):
        top, badge = st.columns([4, 1.2])
        top.markdown(_summary(donation), unsafe_allow_html=True)
        badge.markdown(ui.status_badge(donation.status), unsafe_allow_html=True)

        # This claim's own amount — a donation can carry several partial claims.
        st.markdown(
            f'<span class="fl-meta">Reserved: '
            f"<b>{ui.format_amount(donation, claim.quantity_value)}</b> "
            f"(listing: {ui.format_quantity(donation)})</span>",
            unsafe_allow_html=True,
        )
        text, css = ui.format_deadline(donation.collection_deadline, now)
        st.markdown(
            f'<span class="fl-meta">⏰ Collect by '
            f"{donation.collection_deadline:%a %d %b, %H:%M} "
            f'(<span class="{css}">{text}</span>)</span>',
            unsafe_allow_html=True,
        )
        st.markdown("**Pickup code** — show this to the donor to confirm collection:")
        ui.pickup_code(claim.pickup_code, key=claim.id)
        if st.button("Cancel claim", key=f"cancel-{claim.id}", width="stretch"):
            try:
                service.cancel_claim(claim.id)
                ui.flash(f"Released claim on “{donation.title}”. The quantity is "
                         "available again.", icon="↩️")
            except FoodLoopError as exc:
                ui.flash(str(exc), icon="⚠️")
            st.rerun()

# --------------------------------------------------------------------------
# History: collected, released and expired claims
# --------------------------------------------------------------------------
ui.section_title(f"History ({len(history)})", "📜")
if not history:
    st.caption("Collected, released and expired claims will appear here.")
else:
    finished = [s for s in ui.CLAIM_LABEL if s is not ClaimStatus.RESERVED]
    shown = st.selectbox(
        "Show", [None, *finished], key="mc_status",
        format_func=lambda s: "All" if s is None else ui.CLAIM_LABEL[s],
    )
    for claim in history:
        if shown is not None and claim.status is not shown:
            continue
        donation = donations[claim.donation_id]
        with st.container(border=True):
            top, badge = st.columns([4, 1.2])
            top.markdown(_summary(donation), unsafe_allow_html=True)
            badge.markdown(
                f'<span class="fl-meta"><b>{ui.CLAIM_LABEL[claim.status]}</b></span>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<span class="fl-meta">{ui.CLAIM_LABEL[claim.status]}: '
                f"<b>{ui.format_amount(donation, claim.quantity_value)}</b> "
                f"(listing: {ui.format_quantity(donation)})</span>",
                unsafe_allow_html=True,
            )
            if claim.status is ClaimStatus.COLLECTED:
                when = claim.collected_at.strftime("%a %d %b, %H:%M") if claim.collected_at else ""
                st.success(f"Collected{f' on {when}' if when else ''} — counted as rescued.")
            elif claim.status is ClaimStatus.EXPIRED:
                st.warning("This claim expired before it was collected, so it does not "
                           "count as rescued.")
            elif claim.status is ClaimStatus.CANCELLED:
                st.info("You released this claim; its quantity returned to the donation.")
