"""FoodLoop — Profile: the deliberate demo-persona switcher + org details.

This is where role/organisation switching happens, clearly labelled as a demo
persona rather than a real account. The sidebar only *indicates* the current
persona and links here.
"""

from __future__ import annotations

import streamlit as st

from components import ui
from components.service import get_service
from foodloop.models import WEEKDAYS

service = get_service()
persona = ui.current_persona(service)

st.title("Profile")

# --------------------------------------------------------------------------
# Current persona at a glance
# --------------------------------------------------------------------------
org_name, role_label = ui.persona_summary(persona)
st.markdown(
    f'<div class="fl-card">'
    f'<div class="fl-meta">Currently acting as</div>'
    f'<div style="font-size:1.3rem;font-weight:800;color:{ui.FOREST_DARK};'
    f'margin:.15rem 0 .5rem;">{ui.escape(org_name)}</div>'
    f'<span class="fl-badge" style="background:#DDF0E2;color:#1E7A43;">'
    f'{role_label} mode</span> '
    f'<span class="fl-badge" style="background:#FFF6E6;color:#7A5A12;">'
    f'Demo persona</span>'
    f"</div>",
    unsafe_allow_html=True,
)

st.info("**Demo persona — not a real account.** FoodLoop has no login or "
        "authentication. This selector lets you explore the app as different "
        "organisations; nothing here signs you in.")

# --------------------------------------------------------------------------
# Deliberate role / organisation switch
# --------------------------------------------------------------------------
ui.section_title("Switch demo persona", "🔁")
ui.render_persona_switcher(service)

st.divider()

# --------------------------------------------------------------------------
# Details of the current organisation (read-only)
# --------------------------------------------------------------------------
persona = ui.current_persona(service)  # re-read after any switch
ui.section_title("Organisation details", "🏢")

with st.container(border=True):
    if persona.role == "donor" and persona.donor is not None:
        d = persona.donor
        st.markdown(f"**{ui.escape(d.name)}**")
        st.markdown(f"<span class='fl-meta'>{d.donor_type.value.title()} · "
                    f"{ui.escape(d.location.area)}</span>", unsafe_allow_html=True)
        if d.contact_email:
            st.markdown(f"<span class='fl-meta'>Contact: "
                        f"{ui.escape(d.contact_email)}</span>", unsafe_allow_html=True)
    elif persona.role == "recipient" and persona.recipient is not None:
        r = persona.recipient
        st.markdown(f"**{ui.escape(r.name)}**")
        verified = "✅ Verified" if r.verified else "⏳ Not yet verified"
        st.markdown(
            f"<span class='fl-meta'>"
            f"{r.recipient_type.value.replace('_', ' ').title()} · "
            f"{ui.escape(r.location.area)} · {verified}</span>",
            unsafe_allow_html=True,
        )
        cats = ", ".join(ui.category_label(c) for c in sorted(
            r.accepted_categories, key=lambda c: c.value)) or "—"
        stores = ", ".join(ui.storage_label(s) for s in sorted(
            r.storage_capabilities, key=lambda s: s.value)) or "—"
        days = ", ".join(d for d in WEEKDAYS if d in r.collection_days) or "—"
        st.markdown(f"**Accepts:** {cats}")
        st.markdown(f"**Storage:** {stores}")
        st.markdown(f"**Collection days:** {days} "
                    f"({r.collection_start_hour:02d}:00–{r.collection_end_hour:02d}:00)")
        if r.max_quantity_kg_equiv is not None:
            st.markdown(f"**Approx. capacity:** ~{r.max_quantity_kg_equiv:g} kg "
                        "per donation")
        if r.contact_email:
            st.markdown(f"<span class='fl-meta'>Contact: "
                        f"{ui.escape(r.contact_email)}</span>", unsafe_allow_html=True)
    else:
        st.info("No organisation selected.")
