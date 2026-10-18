"""FoodLoop — Donor: create a surplus-food listing.

Validation and persistence go through FoodLoopService; this view only
collects input and renders feedback. Managing existing listings lives on the
separate 'My Listings' page.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

import streamlit as st

from components import ui
from components.service import get_service
from foodloop.errors import ValidationError
from foodloop.models import (
    COMMON_ALLERGENS,
    FoodCategory,
    StorageRequirement,
    Unit,
)

service = get_service()
persona = ui.current_persona(service)

st.title("List surplus food")
st.caption("Share surplus food with a suitable community organisation. It appears in "
           "the marketplace as soon as you list it.")

if persona.role != "donor" or persona.donor is None:
    st.info("Switch to a **Donor** persona on the Profile page to create a listing.")
    st.stop()

donor = persona.donor
now = service.now()

with st.form("create_donation"):
    st.markdown(f"Listing as **{donor.name}** ({donor.donor_type.value.title()}).")

    ui.section_title("Food details", "🍞")
    title = st.text_input("Food title", key="f_title",
                          placeholder="e.g. Assorted bread & rolls")
    description = st.text_area("Description", key="f_desc",
                               placeholder="What is it, and anything useful to know?")

    c1, c2, c3 = st.columns([2, 1, 1])
    category = c1.selectbox("Category", list(FoodCategory),
                            format_func=ui.category_label, key="f_cat")
    quantity_value = c2.number_input("Quantity", min_value=0.0, value=1.0, step=1.0,
                                     key="f_qty")
    quantity_unit = c3.selectbox("Unit", list(Unit),
                                 format_func=lambda u: u.value, key="f_unit")

    area = st.text_input("Collection area", value=donor.location.area, key="f_area")

    ui.section_title("Collection deadline", "⏰")
    st.caption("The latest time the food can be collected. This is about timing — "
               "it is **not** a food-safety or shelf-life judgement.")
    d1, d2 = st.columns(2)
    deadline_date = d1.date_input("Date", value=(now + timedelta(days=1)).date(),
                                  min_value=now.date(), key="f_date")
    deadline_time = d2.time_input("Time", value=time(16, 0), key="f_time")

    ui.section_title("Food safety & handling", "🧊")
    st.caption("Donor-provided information. FoodLoop does **not** determine food "
               "safety or remaining shelf life, and never infers it from images.")
    s1, s2 = st.columns(2)
    storage = s1.selectbox("Storage requirement", list(StorageRequirement),
                           format_func=ui.storage_label, key="f_storage")
    allergens_declared = s2.checkbox("I am declaring allergen information",
                                     key="f_declared")
    allergens = st.multiselect("Allergens present", COMMON_ALLERGENS, key="f_allergens",
                               help="Leave empty with the box ticked to state 'none'.")

    b1, b2 = st.columns(2)
    include_bb = b1.checkbox("Add a best-before date", key="f_bb_include")
    best_before_date = b2.date_input("Best before", value=now.date(),
                                     key="f_bb", disabled=not include_bb)
    handling_notes = st.text_area("Handling notes", key="f_notes",
                                  placeholder="Storage, reheating, packaging…")

    submitted = st.form_submit_button("List donation", type="primary", width="stretch")

if submitted:
    deadline = datetime.combine(deadline_date, deadline_time)
    best_before = datetime.combine(best_before_date, time(0, 0)) if include_bb else None
    try:
        donation = service.create_donation(
            donor_id=donor.id,
            title=title,
            description=description,
            category=category,
            quantity_value=quantity_value,
            quantity_unit=quantity_unit,
            area=area,
            collection_deadline=deadline,
            storage=storage,
            allergens=allergens,
            allergens_declared=allergens_declared,
            handling_notes=handling_notes,
            best_before=best_before,
        )
        ui.flash(f"Listed “{donation.title}”. It is now available in the marketplace.")
        st.rerun()
    except ValidationError as exc:
        st.error("Please fix the following before listing:")
        for field, message in exc.errors.items():
            st.markdown(f"- **{field.replace('_', ' ').title()}**: {message}")
