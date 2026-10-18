"""FoodLoop — Recipient: manage claimed donations.

Confirm collection (the only action that counts a donation as rescued) or
release a claim. All transitions go through FoodLoopService.
"""

from __future__ import annotations

import streamlit as st

from components import ui
from components.service import get_service
from foodloop.errors import FoodLoopError
from foodloop.models import ClaimStatus, DonationStatus

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

for claim in claims:
    donation = service.repo.get_donation(claim.donation_id)
    donor = service.repo.get_donor(donation.donor_id)
    with st.container(border=True):
        top, badge = st.columns([4, 1.2])
        top.markdown(
            f"{ui.category_emoji(donation.category)} **{ui.escape(donation.title)}**  \n"
            f'<span class="fl-meta">{ui.escape(donor.name)}, '
            f"{ui.escape(donation.location.area)} · "
            f"{ui.format_quantity(donation)}</span>  \n"
            f'<span class="fl-meta">Storage: {ui.storage_label(donation.storage)} · '
            f"{ui.allergen_summary(donation)}</span>",
            unsafe_allow_html=True,
        )
        badge.markdown(ui.status_badge(donation.status), unsafe_allow_html=True)

        st.markdown(
            f'<span class="fl-meta">Reserved: {ui.format_quantity(donation)}</span>',
            unsafe_allow_html=True,
        )
        if claim.status is ClaimStatus.RESERVED:
            text, css = ui.format_deadline(donation.collection_deadline, now)
            st.markdown(
                f'<span class="fl-meta">⏰ Collect by '
                f'<span class="{css}">{text}</span></span>',
                unsafe_allow_html=True,
            )
            st.markdown("**Pickup code** — show this to the donor to confirm collection:")
            st.code(claim.pickup_code, language=None)
            if st.button("Cancel claim", key=f"cancel-{claim.id}", width="stretch"):
                try:
                    service.cancel_claim(claim.id)
                    ui.flash(f"Released claim on “{donation.title}”. The quantity is "
                             "available again.", icon="↩️")
                except FoodLoopError as exc:
                    ui.flash(str(exc), icon="⚠️")
                st.rerun()
        elif claim.status is ClaimStatus.COLLECTED:
            when = claim.collected_at.strftime("%a %d %b, %H:%M") if claim.collected_at else ""
            st.success(f"Collected{f' on {when}' if when else ''} — counted as rescued.")
        elif claim.status is ClaimStatus.EXPIRED:
            st.warning("This claim expired before it was collected, so it does not "
                       "count as rescued.")
        elif claim.status is ClaimStatus.CANCELLED:
            st.info("You released this claim; its quantity returned to the donation.")
