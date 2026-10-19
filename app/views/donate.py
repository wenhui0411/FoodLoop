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

#: Widget keys owned by the create form — the only state cleared on success.
_FORM_KEYS = (
    "f_title", "f_desc", "f_cat", "f_qty", "f_unit", "f_area", "f_date", "f_time",
    "f_storage", "f_declared", "f_allergens", "f_bb", "f_notes",
)

# Reset after a successful create. Done here, at the top of the run that
# follows st.rerun() and *before* the widgets are instantiated, because
# Streamlit forbids changing a widget's state once it exists in this run.
# A failed submission never sets the flag, so the donor's entries are kept.
created_title = st.session_state.pop("_donate_created", None)
if created_title is not None:
    for key in _FORM_KEYS:
        st.session_state.pop(key, None)
    st.success(f"Listed “{created_title}”. It is now available in the marketplace. "
               "The form has been cleared for your next listing.")

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
    # Display/entry precision only; the core rejects >3 decimals and
    # fractional whole units (loaves, items, ...) whatever the browser sends.
    quantity_value = c2.number_input("Quantity", min_value=0.0, value=1.0, step=1.0,
                                     format="%.3f", key="f_qty",
                                     help="Up to 3 decimals for kg/litres; whole "
                                          "numbers for items, loaves, meals and trays.")
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

    # Optional date: empty (None) means "not provided". Deliberately not tied to
    # a checkbox via `disabled=` — widgets inside st.form don't rerun the
    # script, so a checkbox could never re-enable it before submit.
    b1, b2 = st.columns(2)
    best_before_date = b1.date_input("Best before (optional)", value=None, key="f_bb",
                                     help="Leave empty if there is no best-before date.")
    b2.caption("Best-before is a quality date set by the donor. It is separate from "
               "the collection deadline and is not checked by FoodLoop.")
    handling_notes = st.text_area("Handling notes", key="f_notes",
                                  placeholder="Storage, reheating, packaging…")

    submitted = st.form_submit_button("List donation", type="primary", width="stretch")

if submitted:
    deadline = datetime.combine(deadline_date, deadline_time)
    best_before = (datetime.combine(best_before_date, time(0, 0))
                   if best_before_date is not None else None)
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
        st.session_state["_donate_created"] = donation.title  # triggers the reset above
        st.rerun()
    except ValidationError as exc:
        st.error("Please fix the following before listing:")
        for field, message in exc.errors.items():
            st.markdown(f"- **{field.replace('_', ' ').title()}**: {message}")
