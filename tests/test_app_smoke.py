"""Smoke tests that the Streamlit app runs without raising.

Uses Streamlit's AppTest harness to execute the navigation entry and each
view headlessly against an isolated temp database. Guards the "keep the app
runnable" requirement as the UI evolves — it does not assert visual layout.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parents[1] / "app"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    # Point the app at a throwaway DB so smoke runs never touch data/.
    monkeypatch.setenv("FOODLOOP_DB", str(tmp_path / "smoke.db"))


@pytest.mark.parametrize("role", ["donor", "recipient"])
def test_entry_navigation_runs(role):
    """The st.navigation entry builds a role-based nav and renders Home."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(_APP / "Home.py"), default_timeout=60)
    at.session_state["fl_role"] = role
    at.run()
    assert not at.exception, at.exception


# Views addressable standalone (no in-page st.page_link). Home is covered via
# the entry above because it uses page_link to role-specific pages.
_DONOR_VIEWS = ["donate", "my_listings", "donor_impact"]
_RECIPIENT_VIEWS = ["marketplace", "my_claims", "recipient_impact"]
_SHARED_VIEWS = ["insights", "profile"]


@pytest.mark.parametrize("view,role", (
    [(v, "donor") for v in _DONOR_VIEWS]
    + [(v, "recipient") for v in _RECIPIENT_VIEWS]
    + [(v, "donor") for v in _SHARED_VIEWS]
))
def test_view_runs_without_exception(view, role):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(_APP / "views" / f"{view}.py"), default_timeout=60)
    at.session_state["fl_role"] = role
    at.run()
    assert not at.exception, at.exception


def test_marketplace_lists_seeded_available_donations():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(_APP / "views/marketplace.py"), default_timeout=60)
    at.session_state["fl_role"] = "recipient"
    at.run()
    assert not at.exception, at.exception
    captions = " ".join(c.value for c in at.caption)
    assert "listings" in captions
