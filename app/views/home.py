"""FoodLoop — role-aware Home.

A polished landing: a forest hero with a role-specific greeting and primary
call-to-action, then a community "township pulse". All data is read-only; the
visuals use the shared design system in ``components.ui``.
"""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from components import ui
from components.service import get_service
from foodloop import insights

service = get_service()
persona = ui.current_persona(service)
now = service.now()

org_name, role_label = ui.persona_summary(persona)

# --------------------------------------------------------------------------
# Role-aware hero + primary action
# --------------------------------------------------------------------------
if persona.role == "donor":
    ui.hero(
        "FoodLoop · Donor",
        f"Welcome, {org_name}",
        "List surplus food so a nearby community organisation can collect it "
        "before it goes to waste.",
    )
    st.page_link("views/donate.py", label="List surplus food", icon="➕")
else:
    ui.hero(
        "FoodLoop · Recipient",
        f"Welcome, {org_name}",
        "Discover surplus food you’re eligible to collect and put it to good "
        "use in your community.",
    )
    st.page_link("views/marketplace.py", label="Browse the marketplace", icon="🛒")

# --------------------------------------------------------------------------
# Township pulse (community-wide, demo data)
# --------------------------------------------------------------------------
ui.section_title("Township pulse", "📊")
st.caption("Community-wide activity across the township. Handovers are counted per "
           "claim, so partial collections are reflected accurately.")

overview = insights.township_overview(service.repo)

with st.container(border=True, key="fl_metricgrid"):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Listings available", overview.available_listings)
    c2.metric("Awaiting pickup", overview.reserved_claims)
    c3.metric("Collections", overview.collected_claims)
    c4.metric("Organisations helped", overview.organisations_helped)

# Activity trend: listings created vs confirmed handovers over time.
listed_per_day = overview.listed_per_day
collected_per_day = overview.collected_per_day
all_days = sorted(set(listed_per_day) | set(collected_per_day))
if all_days:
    trend = pd.DataFrame(
        [{"Date": day, "Metric": "Listed", "Count": listed_per_day.get(day, 0)}
         for day in all_days]
        + [{"Date": day, "Metric": "Collected", "Count": collected_per_day.get(day, 0)}
           for day in all_days]
    )
    line = (
        alt.Chart(trend)
        .mark_line(point=True, strokeWidth=3)
        .encode(
            x=alt.X("Date:T", title=None),
            y=alt.Y("Count:Q", title=None),
            color=alt.Color(
                "Metric:N",
                scale=alt.Scale(domain=["Listed", "Collected"],
                                range=["#9BB7A6", ui.FOREST]),
                title=None,
            ),
            tooltip=["Date", "Metric", "Count"],
        )
        .properties(height=240)
    )
    st.altair_chart(line, width="stretch")

ui.section_title("Go deeper", "🧭")
st.page_link("views/insights.py", label="See township insights", icon="🏘️")
