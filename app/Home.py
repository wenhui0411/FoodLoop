"""FoodLoop — application entry point and navigation controller.

Run with:  streamlit run app/Home.py  (from the repository root)

This script runs on every interaction: it sets up the page, resolves the
active demo persona, builds a **role-based** navigation with ``st.navigation``
(donor vs recipient journeys), and dispatches to the selected view. All view
content lives in ``app/views/``; all business logic lives in ``foodloop/``.
"""

from __future__ import annotations

import streamlit as st

from components import ui
from components.service import get_service, refresh

ui.setup_page("FoodLoop")
service = get_service()
refresh(service)
persona = ui.current_persona(service)

# Pages shared across both journeys.
home_page = st.Page("views/home.py", title="Home", icon="🏠",
                    url_path="home", default=True)
profile_page = st.Page("views/profile.py", title="Profile", icon="👤",
                       url_path="profile")
insights_page = st.Page("views/insights.py", title="Township insights", icon="🏘️",
                        url_path="insights")

if persona.role == "donor":
    section = "Donor"
    listings_page = st.Page("views/my_listings.py", title="My Listings", icon="📋",
                            url_path="my-listings")
    donate_page = st.Page("views/donate.py", title="Donate", icon="➕",
                          url_path="donate")
    impact_page = st.Page("views/donor_impact.py", title="Impact", icon="📊",
                          url_path="impact")
    role_pages = [home_page, listings_page, donate_page, impact_page, profile_page]
    bottom_items = [
        (home_page, "Home", "🏠"),
        (listings_page, "Listings", "📋"),
        (donate_page, "Donate", "➕"),
        (impact_page, "Impact", "📊"),
        (profile_page, "Profile", "👤"),
    ]
else:
    section = "Recipient"
    marketplace_page = st.Page("views/marketplace.py", title="Marketplace", icon="🛒",
                               url_path="marketplace")
    claims_page = st.Page("views/my_claims.py", title="My Claims", icon="🤝",
                          url_path="my-claims")
    impact_page = st.Page("views/recipient_impact.py", title="Impact", icon="📊",
                          url_path="impact")
    role_pages = [home_page, marketplace_page, claims_page, impact_page, profile_page]
    bottom_items = [
        (home_page, "Home", "🏠"),
        (marketplace_page, "Browse", "🛒"),
        (claims_page, "Claims", "🤝"),
        (impact_page, "Impact", "📊"),
        (profile_page, "Profile", "👤"),
    ]

nav = {section: role_pages, "Community": [insights_page]}
pg = st.navigation(nav, position="sidebar")

# Compact, read-only persona indicator beneath the nav (switching is a
# deliberate action on the Profile page).
org_name, role_label = ui.persona_summary(persona)
with st.sidebar:
    st.divider()
    st.markdown("**Acting as**")
    st.markdown(ui.escape(org_name))
    st.caption(f"{role_label} mode · demo persona, not a real login")
    st.page_link(profile_page, label="Switch role / profile", icon="🔁")

ui.render_flashes()
pg.run()

# Mobile-only bottom navigation (hidden on desktop via CSS; the sidebar is the
# desktop nav and the mobile fallback). Toggle off with FOODLOOP_BOTTOM_NAV=0.
if ui.bottom_nav_enabled():
    ui.render_bottom_nav(bottom_items)
