"""Presentation helpers: theme, status badges, formatting, persona sidebar.

Pure presentation — no business rules. Everything that decides *what is
true* lives in the core; this module only decides *how it looks*.
"""

from __future__ import annotations

import html
import os
from dataclasses import dataclass
from datetime import datetime

import streamlit as st

from foodloop.models import (
    Donation,
    DonationStatus,
    Donor,
    FoodCategory,
    Recipient,
    StorageRequirement,
)
from foodloop.workflow import FoodLoopService

# --------------------------------------------------------------------------
# Design tokens (the forest-green / warm-cream system; mirrors config.toml)
# --------------------------------------------------------------------------
FOREST = "#2F6B46"          # primary
FOREST_DARK = "#1C4A30"     # headings / deep accents
FOREST_LIGHT = "#4C9A6A"    # hover / soft accents
CREAM = "#FAF8F2"           # app background (a.k.a WARM_BG)
CREAM_CARD = "#FFFDF8"      # card surface
SAND = "#EFEBE0"            # secondary surface
INK = "#1C2B22"             # primary text
MUTED = "#4F5A54"           # secondary text (AA contrast on cream)
BORDER = "#E4DFD2"          # hairline borders

WARM_BG = CREAM             # backwards-compatible alias

_STATUS_STYLE: dict[DonationStatus, tuple[str, str, str]] = {
    # status: (label, background, text colour)
    DonationStatus.AVAILABLE: ("Available", "#E0EEF8", "#1C5A87"),
    DonationStatus.CLAIMED: ("Claimed", "#FCEFD6", "#8A5A00"),
    DonationStatus.RESERVED: ("Reserved", "#ECE3F5", "#5A4B8A"),
    DonationStatus.COLLECTED: ("Collected", "#DDF0E2", "#1E7A43"),
    DonationStatus.EXPIRED: ("Expired", "#F3E0E0", "#9A2A2A"),
    DonationStatus.CANCELLED: ("Cancelled", "#ECECEC", "#5F5F5F"),
}

_CATEGORY_META: dict[FoodCategory, tuple[str, str]] = {
    # category: (emoji, accent colour for the placeholder)
    FoodCategory.BAKERY: ("🥖", "#C9A66B"),
    FoodCategory.FRESH_PRODUCE: ("🥬", "#6FA86F"),
    FoodCategory.DAIRY: ("🥛", "#ABC4D6"),
    FoodCategory.MEAT: ("🍖", "#C98B8B"),
    FoodCategory.PREPARED_MEALS: ("🍲", "#D9A066"),
    FoodCategory.PANTRY: ("🥫", "#B9A98C"),
    FoodCategory.BEVERAGES: ("🧃", "#8FB8C9"),
    FoodCategory.OTHER: ("📦", "#B0B0A0"),
}

_STORAGE_LABEL = {
    StorageRequirement.AMBIENT: "Ambient",
    StorageRequirement.REFRIGERATED: "Refrigerated",
    StorageRequirement.FROZEN: "Frozen",
}


# --------------------------------------------------------------------------
# Page setup / theme
# --------------------------------------------------------------------------
def setup_page(title: str, icon: str = "🌱") -> None:
    st.set_page_config(page_title=f"FoodLoop · {title}", page_icon=icon, layout="wide")
    _inject_css()


def _inject_css() -> None:
    """Inject the FoodLoop design system.

    Favours self-owned ``fl-*`` classes over Streamlit internals. The few
    global touches target long-stable, documented hooks (``.stApp``,
    ``.block-container``/``stMainBlockContainer`` testid, ``.stButton``) and
    degrade gracefully — if a selector stops matching, the app still works,
    it just loses a little polish.
    """
    st.markdown(
        f"""
        <style>
          /* ---- Base typography & layout -------------------------------- */
          .stApp {{
              font-family: "Inter", "Segoe UI", system-ui, -apple-system,
                           "Helvetica Neue", Arial, sans-serif;
              color: {INK};
          }}
          [data-testid="stMainBlockContainer"], .block-container {{
              max-width: 1080px; padding-top: 2.2rem; padding-bottom: 5rem;
          }}
          .stApp h1 {{ font-weight: 800; letter-spacing: -0.015em; color: {INK}; }}
          .stApp h2 {{ font-weight: 700; color: {FOREST_DARK}; }}
          .stApp h3 {{ font-weight: 700; color: {FOREST_DARK}; }}

          /* ---- Buttons: larger touch target, rounded, clear focus ------ */
          .stButton > button, .stFormSubmitButton > button {{
              border-radius: 10px; min-height: 2.6rem; font-weight: 600;
          }}
          .stButton > button:focus-visible,
          .stFormSubmitButton > button:focus-visible,
          a:focus-visible {{
              outline: 3px solid {FOREST_LIGHT}; outline-offset: 2px;
          }}
          /* Soft rounding on bordered containers (cosmetic, degrades ok). */
          [data-testid="stVerticalBlockBorderWrapper"] {{ border-radius: 14px; }}

          /* ---- Hero banner --------------------------------------------- */
          .fl-hero {{
              background: linear-gradient(135deg, {FOREST}, {FOREST_DARK});
              color: #F4FBF6; padding: 1.6rem 1.7rem; border-radius: 16px;
              margin-bottom: 1.1rem; box-shadow: 0 2px 12px rgba(28,74,48,.18);
          }}
          .fl-hero .eyebrow {{
              font-size: .74rem; font-weight: 700; letter-spacing: .09em;
              text-transform: uppercase; opacity: .85;
          }}
          .fl-hero .title {{
              font-size: 1.7rem; font-weight: 800; margin: .25rem 0 .35rem;
              line-height: 1.15;
          }}
          .fl-hero p {{ color: #E6F2EA; margin: 0; font-size: 1rem; }}

          /* ---- Section headers ----------------------------------------- */
          .fl-section {{ display:flex; align-items:center; gap:.55rem;
                         margin: 1.1rem 0 .55rem; }}
          .fl-section .bar {{ width:4px; height:1.15rem; background:{FOREST};
                              border-radius:2px; }}
          .fl-section .txt {{ font-weight:700; color:{FOREST_DARK};
                              font-size:1.18rem; }}

          /* ---- Generic card -------------------------------------------- */
          .fl-card {{
              background: {CREAM_CARD}; border: 1px solid {BORDER};
              border-radius: 14px; padding: 1rem 1.1rem; margin-bottom: .8rem;
          }}

          /* ---- Badges, chips, thumbnails, meta ------------------------- */
          .fl-badge {{
              display:inline-block; padding:3px 11px; border-radius:999px;
              font-size:0.72rem; font-weight:700; letter-spacing:.02em;
          }}
          .fl-chip {{
              display:inline-block; padding:3px 10px; margin:2px 5px 2px 0;
              border-radius:7px; font-size:0.74rem; background:{SAND};
              color:#4A4A3F; border:1px solid {BORDER};
          }}
          .fl-thumb {{
              height:96px; border-radius:12px; display:flex; align-items:center;
              justify-content:center; font-size:2.7rem; margin-bottom:.55rem;
          }}
          .fl-title {{ font-weight:700; font-size:1.04rem; margin:.1rem 0;
                       color:{FOREST_DARK}; }}
          .fl-meta {{ color:{MUTED}; font-size:0.85rem; margin:.12rem 0; }}
          .fl-deadline-urgent {{ color:#9A2A2A; font-weight:700; }}
          .fl-deadline-soon {{ color:#8A5A00; font-weight:700; }}
          .fl-deadline-ok {{ color:#1E7A43; font-weight:700; }}

          /* ---- Mobile bottom navigation -------------------------------- */
          /* Scoped to the keyed container (st-key-*), a documented Streamlit
             hook — not a fragile internal selector. Desktop uses the
             st.navigation sidebar; on mobile that sidebar remains available
             as a hamburger drawer (the fallback), with this bar on top. */
          @media (min-width: 721px) {{
            .st-key-fl_bottomnav {{ display: none; }}
          }}
          @media (max-width: 720px) {{
            .st-key-fl_bottomnav {{
                position: fixed; left: 0; right: 0; bottom: 0; z-index: 9990;
                background: {CREAM_CARD}; border-top: 1px solid {BORDER};
                box-shadow: 0 -2px 10px rgba(0,0,0,.07); box-sizing: border-box;
                padding: .15rem .25rem calc(.2rem + env(safe-area-inset-bottom));
            }}
            /* st.columns stacks (and left-clusters) on mobile by default.
               A 5-track CSS grid guarantees five equal cells that always fill
               the width on any phone. Scoped to the keyed bar via stable
               data-testid hooks. */
            .st-key-fl_bottomnav [data-testid="stVerticalBlock"] {{
                gap: 0 !important; width: 100% !important;
            }}
            .st-key-fl_bottomnav [data-testid="stHorizontalBlock"] {{
                display: grid !important;
                grid-template-columns: repeat(5, minmax(0, 1fr)) !important;
                gap: .1rem !important; width: 100% !important;
            }}
            .st-key-fl_bottomnav [data-testid="stColumn"] {{
                width: 100% !important; min-width: 0 !important;
                box-sizing: border-box;
            }}
            .st-key-fl_bottomnav [data-testid="stPageLink"] {{ width: 100%; }}
            .st-key-fl_bottomnav [data-testid="stPageLink"] a {{
                display: flex; flex-direction: column; align-items: center;
                justify-content: center; gap: 2px; width: 100%;
                box-sizing: border-box; font-size: .62rem; line-height: 1.05;
                text-align: center; white-space: nowrap; overflow: hidden;
                text-overflow: ellipsis; padding: .2rem .1rem; min-height: 44px;
                border-radius: 8px;
            }}
            .st-key-fl_bottomnav [data-testid="stPageLink"] a[aria-current="page"] {{
                color: {FOREST}; font-weight: 700; background: #E7F1EA;
            }}
            /* Reserve space so the last item, form submit buttons, and toasts
               are never hidden behind the fixed bar (plus the safe-area). */
            [data-testid="stMainBlockContainer"], .block-container {{
                padding-bottom: calc(5.5rem + env(safe-area-inset-bottom)) !important;
            }}
          }}

          /* ---- Mobile: shared 2-column grid for dashboard metric cards -- */
          /* Reused by the Home 'Township pulse' and the donor/recipient
             Impact pages. st.columns stacks vertically on mobile; lay the
             metrics in a 2-col grid instead. Any metric rendered OUTSIDE the
             column row (e.g. donor 'Collection rate') stays full-width below.
             Scoped to the keyed container only. */
          @media (max-width: 720px) {{
            .st-key-fl_metricgrid [data-testid="stHorizontalBlock"] {{
                display: grid !important;
                grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
                gap: .6rem !important; width: 100% !important;
            }}
            .st-key-fl_metricgrid [data-testid="stColumn"] {{
                width: 100% !important; min-width: 0 !important;
                box-sizing: border-box;
            }}
            /* Let longer labels (e.g. "Organisations helped",
               "Confirmed collections") wrap cleanly instead of clipping. */
            .st-key-fl_metricgrid [data-testid="stMetricLabel"],
            .st-key-fl_metricgrid [data-testid="stMetricLabel"] * {{
                white-space: normal !important; overflow-wrap: break-word;
            }}
          }}
        </style>
        """,
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------
# Mobile bottom navigation
# --------------------------------------------------------------------------
def bottom_nav_enabled() -> bool:
    """Whether to render the custom mobile bottom bar.

    Defaults on; set ``FOODLOOP_BOTTOM_NAV=0`` (or false/no/off) to disable it
    and rely solely on the native ``st.navigation`` sidebar — a simple, one-
    switch fallback if the bar's CSS ever misbehaves.
    """
    return os.environ.get("FOODLOOP_BOTTOM_NAV", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def render_bottom_nav(items: list) -> None:
    """Render a mobile-only bottom tab bar from ``st.page_link``.

    ``items`` is a list of ``(page, label, icon)``. Uses real page links, so
    routing, URL/refresh behaviour, and keyboard access come for free. The bar
    is hidden on desktop (CSS); the active tab is marked via ``aria-current``.
    """
    bar = st.container(key="fl_bottomnav")
    with bar:
        cols = st.columns(len(items))
        for col, (page, label, icon) in zip(cols, items):
            with col:
                st.page_link(page, label=label, icon=icon)


def hero(eyebrow: str, title: str, subtitle: str) -> None:
    """Render the forest-gradient hero banner (dynamic text is escaped)."""
    st.markdown(
        f'<div class="fl-hero">'
        f'<div class="eyebrow">{escape(eyebrow)}</div>'
        f'<div class="title">{escape(title)}</div>'
        f"<p>{escape(subtitle)}</p></div>",
        unsafe_allow_html=True,
    )


def section_title(text: str, icon: str = "") -> None:
    """Render a consistent section header with an accent bar."""
    prefix = f"{icon} " if icon else ""
    st.markdown(
        f'<div class="fl-section"><span class="bar"></span>'
        f'<span class="txt">{prefix}{escape(text)}</span></div>',
        unsafe_allow_html=True,
    )


def flash(message: str, icon: str = "✅") -> None:
    """Queue a toast to show after the next rerun.

    Actions that end in ``st.rerun()`` lose any inline ``st.success`` they
    printed; stashing the message in session state and showing it on the
    next run gives reliable success/error feedback across reruns.
    """
    st.session_state.setdefault("_flashes", []).append((message, icon))


def render_flashes() -> None:
    for message, icon in st.session_state.pop("_flashes", []):
        st.toast(message, icon=icon)


# --------------------------------------------------------------------------
# Small HTML fragments
# --------------------------------------------------------------------------
def status_badge(status: DonationStatus) -> str:
    label, bg, fg = _STATUS_STYLE[status]
    return (
        f'<span class="fl-badge" style="background:{bg};color:{fg};">{label}</span>'
    )


def category_emoji(category: FoodCategory) -> str:
    return _CATEGORY_META[category][0]


def thumb(category: FoodCategory) -> str:
    emoji, colour = _CATEGORY_META[category]
    return (
        f'<div class="fl-thumb" style="background:{colour}22;border:1px solid {colour}55;">'
        f"{emoji}</div>"
    )


def escape(text: object) -> str:
    """Escape user-controlled text for safe interpolation into HTML rendered
    with ``unsafe_allow_html=True``.

    Use this for any donor/recipient-supplied string (titles, areas, names)
    embedded in an HTML fragment. Text rendered through ordinary Markdown
    (``st.markdown`` without ``unsafe_allow_html``) must NOT be escaped —
    Streamlit already treats it safely and escaping would show literal
    ``&amp;``/``&lt;`` entities.
    """
    return html.escape(str(text))


def chip(text: str) -> str:
    return f'<span class="fl-chip">{text}</span>'


def category_label(category: FoodCategory) -> str:
    return category.value.replace("_", " ").title()


def storage_label(storage: StorageRequirement) -> str:
    return _STORAGE_LABEL[storage]


def format_quantity(donation: Donation) -> str:
    return str(donation.quantity)


def format_deadline(deadline: datetime, now: datetime) -> tuple[str, str]:
    """Return (human text, css class) describing time left until the deadline."""
    delta = deadline - now
    secs = delta.total_seconds()
    if secs <= 0:
        return "Deadline passed", "fl-deadline-urgent"
    hours = secs / 3600
    if hours < 1:
        text = f"in {int(secs // 60)} min"
    elif hours < 48:
        text = f"in {int(round(hours))} h"
    else:
        text = f"in {int(round(hours / 24))} days"
    css = (
        "fl-deadline-urgent" if hours < 12
        else "fl-deadline-soon" if hours < 48
        else "fl-deadline-ok"
    )
    return text, css


def allergen_summary(donation: Donation) -> str:
    """Honest allergen text: distinguishes 'none declared' from 'not stated'."""
    if not donation.allergens_declared:
        return "Allergens: not stated"
    if not donation.allergens:
        return "Allergens: none declared"
    return "Allergens: " + ", ".join(donation.allergens)


# --------------------------------------------------------------------------
# Persona (demo stand-in for login — never real authentication)
# --------------------------------------------------------------------------
@dataclass
class Persona:
    role: str                        # "donor" or "recipient"
    donor: Donor | None = None
    recipient: Recipient | None = None


_ROLES = ("donor", "recipient")


def _resolve_org(entities, name):
    """Pick the entity matching ``name``, else the first (stable default)."""
    if not entities:
        return None
    for e in entities:
        if e.name == name:
            return e
    return entities[0]


def _sync_query(role: str, org_name: str | None) -> None:
    """Mirror the persona into the URL query string so it survives a browser
    refresh (which clears ``st.session_state``). Only written when changed, so
    it never causes a rerun loop."""
    qp = st.query_params
    if qp.get("role") != role:
        qp["role"] = role
    if org_name and qp.get("org") != org_name:
        qp["org"] = org_name


def current_persona(service: FoodLoopService) -> Persona:
    """Resolve the active persona, durable across navigation and refresh.

    Source of truth order: URL query params (survive a hard refresh) → plain
    session_state cache (survives navigation) → sensible defaults (donor, and
    the first organisation). These are plain keys, NOT widget keys, so they
    are never cleared by Streamlit's unused-widget cleanup.
    """
    qp = st.query_params
    role = qp.get("role") or st.session_state.get("fl_role") or "donor"
    if role not in _ROLES:
        role = "donor"

    donors = service.repo.list_donors()
    recipients = service.repo.list_recipients()

    active_name = qp.get("org") or st.session_state.get(f"fl_{role}_name")
    if role == "donor":
        donor = _resolve_org(donors, active_name)
        recipient = _resolve_org(recipients, st.session_state.get("fl_recipient_name"))
        active_org = donor
    else:
        recipient = _resolve_org(recipients, active_name)
        donor = _resolve_org(donors, st.session_state.get("fl_donor_name"))
        active_org = recipient

    # Cache to session_state + URL so navigation and refresh both hold.
    st.session_state["fl_role"] = role
    if donor:
        st.session_state["fl_donor_name"] = donor.name
    if recipient:
        st.session_state["fl_recipient_name"] = recipient.name
    _sync_query(role, active_org.name if active_org else None)

    return Persona(role=role, donor=donor, recipient=recipient)


def render_persona_switcher(service: FoodLoopService) -> None:
    """Deliberate role/organisation switcher, shown on the Profile page only.

    Writes the durable plain-key state read by :func:`current_persona` and
    reruns when the role changes so the role-based navigation reflows. This is
    a demo persona chooser — explicitly NOT a login.
    """
    current = current_persona(service)

    role_label = st.radio(
        "Act as", ["Donor", "Recipient"],
        index=0 if current.role == "donor" else 1, horizontal=True, key="ui_role",
    )
    new_role = "donor" if role_label == "Donor" else "recipient"

    if new_role == "donor":
        entities = service.repo.list_donors()
        current_name = current.donor.name if current.donor else None
    else:
        entities = service.repo.list_recipients()
        current_name = current.recipient.name if current.recipient else None

    names = [e.name for e in entities]
    choice = None
    if names:
        index = names.index(current_name) if current_name in names else 0
        choice = st.selectbox("Organisation", names, index=index, key="ui_org")

    # Commit to durable state (plain keys + URL).
    st.session_state["fl_role"] = new_role
    if choice:
        st.session_state[f"fl_{new_role}_name"] = choice
    _sync_query(new_role, choice)

    if new_role == "recipient":
        rec = _resolve_org(service.repo.list_recipients(), choice)
        if rec and not rec.verified:
            st.warning("This organisation is not yet verified and cannot claim donations.")

    # Role change must reflow the navigation on the next run.
    if new_role != current.role:
        st.rerun()


def persona_summary(persona: Persona) -> tuple[str, str]:
    """Return (organisation name, role label) for a compact indicator."""
    if persona.role == "donor":
        org = persona.donor.name if persona.donor else "—"
        return org, "Donor"
    org = persona.recipient.name if persona.recipient else "—"
    return org, "Recipient"
