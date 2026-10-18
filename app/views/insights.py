"""FoodLoop — Township insights (community-wide, shared).

Descriptive summaries over the donation data. Thin UI over
``foodloop.insights``; no analytics logic lives here. This is deliberately
distinct from a recipient's own 'Impact' page.
"""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from components import ui
from components.service import get_service
from data.seed import SEED_DONATION_IDS
from foodloop import insights

service = get_service()
ui.current_persona(service)  # keep persona state in sync; insights is role-neutral

st.title("Township insights")

comp = insights.dataset_composition(service.repo, SEED_DONATION_IDS)
if comp.total == 0:
    st.info("No donations in the system yet — insights will appear once there is data.")
    st.stop()

# --------------------------------------------------------------------------
# Dataset composition — demo vs user, stated plainly
# --------------------------------------------------------------------------
with st.container(border=True):
    c1, c2, c3 = st.columns(3)
    c1.metric("Donations in dataset", comp.total)
    c2.metric("Demo (seed)", comp.demo)
    c3.metric("User-created", comp.user)
period = insights.data_period(service.repo)
period_text = (f"{period[0]:%d %b %Y} – {service.now():%d %b %Y}"
               if period else "no data")
st.caption(
    f"**{comp.demo}** of these are synthetic seed records and **{comp.user}** were "
    f"created by users in this session. Insights below are computed over the whole "
    f"dataset (currently mostly demo data). Period: {period_text}."
)

st.info("These are **descriptive patterns, not causal claims**, and are easily "
        "skewed by small samples. FoodLoop does **not** estimate carbon savings or "
        "‘meals provided’ — there is no defensible methodology or data for that.")

st.divider()

# --------------------------------------------------------------------------
# Actionable observations
# --------------------------------------------------------------------------
ui.section_title("Observations", "🔎")
observations = insights.observations(service.repo, now=service.now())
if not observations:
    st.info("Not enough *concluded* donations (collected or expired) yet to draw "
            "reliable observations. Check back as more activity accumulates.")
else:
    for obs in observations:
        with st.container(border=True):
            st.markdown(f"**{obs.title}** — {obs.detail}")
            st.caption(f"Based on: {obs.counts}")

st.divider()

# --------------------------------------------------------------------------
# Activity by area
# --------------------------------------------------------------------------
ui.section_title("By area", "📍")
area_stats = insights.area_breakdown(service.repo)
area_df = pd.DataFrame([
    {
        "Area": s.area,
        "Listings": s.listed,
        "Collected": s.collected,
        "Expired": s.expired,
        "Collection rate": (f"{s.collection_rate * 100:.0f}%"
                            if s.collection_rate is not None else "—"),
    }
    for s in area_stats
])
st.dataframe(area_df, width="stretch", hide_index=True)
st.caption(f"‘Listings’ counts donations; ‘Collected’/‘Expired’ count claims "
           f"(handovers / lapsed). A collection rate appears only where at least "
           f"{insights.MIN_SAMPLE} claims have concluded; “—” means too few to "
           "report honestly.")

st.divider()

# --------------------------------------------------------------------------
# Activity by category (claim outcomes: collected vs expired)
# --------------------------------------------------------------------------
ui.section_title("By food category", "🍎")
cat_stats = insights.category_breakdown(service.repo)
long_rows = []
for s in cat_stats:
    label = s.category.replace("_", " ").title()
    long_rows.append({"Category": label, "Outcome": "Collected", "Count": s.collected})
    long_rows.append({"Category": label, "Outcome": "Expired", "Count": s.expired})
cat_df = pd.DataFrame(long_rows)
chart = (
    alt.Chart(cat_df)
    .mark_bar()
    .encode(
        x=alt.X("Count:Q", title="Claims"),
        y=alt.Y("Category:N", sort="-x", title=None),
        color=alt.Color(
            "Outcome:N",
            scale=alt.Scale(domain=["Collected", "Expired"],
                            range=[ui.FOREST, "#C98B8B"]),
            title=None,
        ),
        tooltip=["Category", "Outcome", "Count"],
    )
    .properties(height=max(160, 42 * len(cat_stats)))
)
st.altair_chart(chart, width="stretch")
st.caption("Shows concluded claims (collected vs expired) per category — not open or "
           "cancelled claims.")
