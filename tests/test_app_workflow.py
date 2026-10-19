"""End-to-end UI workflow tests driven through Streamlit's AppTest harness.

These exercise the donor create flow, recipient claim flow, collection
confirmation, cancellation, and duplicate-claim prevention *through the real
pages*, verifying persisted state via an isolated temp database. Business
rules themselves are unit-tested in the core suite; here we verify the UI is
correctly wired to the service.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from foodloop import impact
from foodloop.models import (
    Donation,
    DonationStatus,
    Donor,
    DonorType,
    FoodCategory,
    Location,
    Quantity,
    Recipient,
    RecipientType,
    StorageRequirement,
    Unit,
)
from foodloop.repository import Repository

_APP = Path(__file__).resolve().parents[1] / "app"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))


@pytest.fixture
def db_path(tmp_path, monkeypatch) -> str:
    """Isolated DB path, unique per test (keys the cached service)."""
    path = str(tmp_path / "wf.db")
    monkeypatch.setenv("FOODLOOP_DB", path)
    return path


def _app_test(page: str, role: str = "donor"):
    """Build an AppTest for a view, with the demo persona role preset in
    session_state (the durable key read by ui.current_persona)."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(_APP / page), default_timeout=60)
    at.session_state["fl_role"] = role
    return at


def _button(at, key_or_label: str):
    """Find a button by key (preferred) or exact label; None if absent."""
    for b in at.button:
        if b.key == key_or_label or b.label == key_or_label:
            return b
    return None


def _metric(at, label: str):
    """Return the string value of a metric by its label, or None."""
    for m in at.metric:
        if m.label == label:
            return m.value
    return None


# --------------------------------------------------------------------------
# Deterministic fixtures inserted directly (bypasses demo seed so eligibility
# does not depend on the real-world weekday/time of the test run).
# --------------------------------------------------------------------------
def _seed_minimal(db_path: str) -> Repository:
    repo = Repository(db_path)
    repo.add_donor(Donor("d1", "Alpha Bakery", DonorType.BAKERY, Location("Central")))
    repo.add_recipient(Recipient(
        "r1", "Alpha Food Bank", RecipientType.FOOD_BANK, Location("Central"),
        accepted_categories={FoodCategory.BAKERY},
        storage_capabilities={StorageRequirement.AMBIENT},
        max_quantity_kg_equiv=None,  # no capacity gate
        collection_days={"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"},
        collection_start_hour=0, collection_end_hour=24,
        verified=True,
    ))
    return repo


def _add_available(repo: Repository, did: str = "dn1",
                   location: Location | None = None) -> Donation:
    d = Donation(
        did, "d1", "Fresh loaves", "End of day bake", FoodCategory.BAKERY,
        Quantity(20, Unit.LOAVES), location or Location("Central"),
        collection_deadline=datetime.now() + timedelta(days=5),
        created_at=datetime.now(), status=DonationStatus.AVAILABLE,
        storage=StorageRequirement.AMBIENT,
    )
    repo.add_donation(d)
    return d


# --------------------------------------------------------------------------
# Donor create flow
# --------------------------------------------------------------------------
def test_donor_can_create_listing(db_path):
    at = _app_test("views/donate.py").run()
    assert not at.exception

    at.text_input(key="f_title").set_value("Test Loaves")
    at.selectbox(key="f_cat").set_value(FoodCategory.BAKERY)
    at.number_input(key="f_qty").set_value(12)
    at.selectbox(key="f_unit").set_value(Unit.LOAVES)
    at.text_input(key="f_area").set_value("Central")
    _button(at, "List donation").click()
    at.run()

    assert not at.exception
    repo = Repository(db_path)
    created = [d for d in repo.list_donations() if d.title == "Test Loaves"]
    assert len(created) == 1
    assert created[0].status is DonationStatus.AVAILABLE


def test_invalid_donation_shows_errors_and_persists_nothing(db_path):
    at = _app_test("views/donate.py").run()
    before = len(Repository(db_path).list_donations())

    at.text_input(key="f_title").set_value("")      # required
    at.number_input(key="f_qty").set_value(0)        # must be > 0
    _button(at, "List donation").click()
    at.run()

    assert not at.exception
    assert len(at.error) >= 1                         # errors surfaced in UI
    assert len(Repository(db_path).list_donations()) == before  # nothing created


# --------------------------------------------------------------------------
# Recipient claim flow (via marketplace inline button)
# --------------------------------------------------------------------------
def test_recipient_can_claim_eligible_donation(db_path):
    repo = _seed_minimal(db_path)
    donation = _add_available(repo)

    at = _app_test("views/marketplace.py", "recipient").run()

    claim_btn = _button(at, f"claim-{donation.id}")
    assert claim_btn is not None, "eligible donation should offer a Claim button"
    claim_btn.click()
    at.run()

    assert not at.exception
    # Whole-listing claim -> donation RESERVED.
    assert repo.get_donation(donation.id).status is DonationStatus.RESERVED
    assert repo.get_claim_for_donation(donation.id).recipient_id == "r1"


def test_fully_reserved_donation_offers_no_claim_button(db_path):
    """UI-level prevention: a fully-reserved listing has no Claim button."""
    repo = _seed_minimal(db_path)
    repo.add_recipient(Recipient(
        "r2", "Beta Shelter", RecipientType.SHELTER, Location("Central"),
        accepted_categories={FoodCategory.BAKERY},
        storage_capabilities={StorageRequirement.AMBIENT},
        collection_days={"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"},
        collection_start_hour=0, collection_end_hour=24, verified=True,
    ))
    donation = _add_available(repo)
    repo.create_claim(claim_id="c1", donation_id=donation.id,
                      recipient_id="r2", claimed_at=datetime.now(), now=datetime.now())

    at = _app_test("views/marketplace.py", "recipient").run()
    # Widen the status filter so the reserved donation is visible at all.
    at.multiselect(key="mk_status").set_value(
        [DonationStatus.AVAILABLE, DonationStatus.RESERVED]
    )
    at.run()

    assert _button(at, f"claim-{donation.id}") is None
    assert repo.get_claim_for_donation(donation.id).recipient_id == "r2"


def test_detail_dialog_shows_rule_based_recommendations(db_path):
    repo = _seed_minimal(db_path)
    # Give both sides coordinates so the distance factor is available.
    repo.add_donation(Donation(
        "dn1", "d1", "Fresh loaves", "", FoodCategory.BAKERY,
        Quantity(20, Unit.LOAVES), Location("Central", -26.2041, 28.0473),
        collection_deadline=datetime.now() + timedelta(days=5),
        created_at=datetime.now(), status=DonationStatus.AVAILABLE,
        storage=StorageRequirement.AMBIENT,
    ))

    at = _app_test("views/marketplace.py").run()
    _button(at, "detail-dn1").click()
    at.run()

    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Recommended recipients" in text
    assert "Alpha Food Bank" in text
    assert "rule-based" in " ".join(c.value for c in at.caption).lower()


# --------------------------------------------------------------------------
# Collection confirmation + impact
# --------------------------------------------------------------------------
def test_recipient_sees_pickup_code_and_cannot_self_confirm(db_path):
    repo = _seed_minimal(db_path)
    donation = _add_available(repo)
    claim = repo.create_claim(claim_id="c1", donation_id=donation.id,
                              recipient_id="r1", claimed_at=datetime.now(),
                              now=datetime.now())

    at = _app_test("views/my_claims.py", "recipient").run()
    assert not at.exception
    # The code is shown to the recipient...
    codeblocks = " ".join(getattr(c, "value", "") for c in at.code)
    assert claim.pickup_code in codeblocks
    # ...and there is no recipient self-confirm button (donor confirms by code).
    assert _button(at, "confirm-c1") is None


def test_donor_confirms_handover_by_code_and_impact_counts(db_path):
    repo = _seed_minimal(db_path)
    donation = _add_available(repo)
    claim = repo.create_claim(claim_id="c1", donation_id=donation.id,
                              recipient_id="r1", claimed_at=datetime.now(),
                              now=datetime.now())
    assert impact.completed_donation_count(repo) == 0

    at = _app_test("views/my_listings.py", "donor").run()  # donor d1 persona
    at.text_input(key="handover_code").set_value(claim.pickup_code)
    _button(at, "Confirm handover").click()
    at.run()

    assert not at.exception
    assert repo.get_donation(donation.id).status is DonationStatus.COLLECTED
    assert impact.completed_donation_count(repo) == 1


def test_donor_handover_wrong_code_does_not_collect(db_path):
    repo = _seed_minimal(db_path)
    donation = _add_available(repo)
    repo.create_claim(claim_id="c1", donation_id=donation.id, recipient_id="r1",
                      claimed_at=datetime.now(), now=datetime.now())

    at = _app_test("views/my_listings.py", "donor").run()
    at.text_input(key="handover_code").set_value("ZZZZZZ")
    _button(at, "Confirm handover").click()
    at.run()

    assert not at.exception  # error surfaced as a flash, not a crash
    assert repo.get_donation(donation.id).status is DonationStatus.RESERVED
    assert impact.completed_donation_count(repo) == 0


def test_recipient_cancels_claim_releasing_donation(db_path):
    repo = _seed_minimal(db_path)
    donation = _add_available(repo)
    repo.create_claim(claim_id="c1", donation_id=donation.id, recipient_id="r1",
                      claimed_at=datetime.now(), now=datetime.now())

    at = _app_test("views/my_claims.py", "recipient").run()

    cancel = _button(at, "cancel-c1")  # keyed by claim id
    assert cancel is not None
    cancel.click()
    at.run()

    assert not at.exception
    assert repo.get_donation(donation.id).status is DonationStatus.AVAILABLE
    # The claim is kept as history, now CANCELLED, quantity freed.
    assert repo.donation_availability(donation.id).remaining == donation.quantity.value


# --------------------------------------------------------------------------
# Hardening: HTML escaping + persona persistence
# --------------------------------------------------------------------------
def test_user_title_html_is_escaped_in_marketplace(db_path):
    repo = _seed_minimal(db_path)
    repo.add_donation(Donation(
        "dn1", "d1", "<script>alert(1)</script>", "", FoodCategory.BAKERY,
        Quantity(5, Unit.LOAVES), Location("Central"),
        collection_deadline=datetime.now() + timedelta(days=2),
        created_at=datetime.now(), status=DonationStatus.AVAILABLE,
        storage=StorageRequirement.AMBIENT,
    ))
    at = _app_test("views/marketplace.py").run()
    assert not at.exception
    rendered = " ".join(m.value for m in at.markdown)
    assert "<script>alert(1)</script>" not in rendered   # not live markup
    assert "&lt;script&gt;" in rendered                   # shown escaped


def test_persona_role_persists_from_session_state(db_path):
    """A role carried in session_state (as it is across page navigation)
    activates the recipient persona without re-selecting it."""
    repo = _seed_minimal(db_path)
    _add_available(repo, "dn1")

    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(_APP / "views/marketplace.py"), default_timeout=60)
    at.session_state["fl_role"] = "recipient"   # carried as if from another page
    at.run()

    assert not at.exception
    # Acting as the single verified, eligible recipient -> inline Claim appears.
    assert _button(at, "claim-dn1") is not None


def test_persona_role_survives_refresh_via_query_params(db_path):
    """A hard refresh clears session_state; the role is restored from the URL
    query params that current_persona keeps in sync."""
    repo = _seed_minimal(db_path)
    _add_available(repo, "dn1")

    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(_APP / "views/marketplace.py"), default_timeout=60)
    at.query_params["role"] = "recipient"       # session_state intentionally empty
    at.run()

    assert not at.exception
    assert at.session_state.get("fl_role") == "recipient"   # rehydrated
    assert _button(at, "claim-dn1") is not None


# --------------------------------------------------------------------------
# Navigation: role-based nav, bottom bar, routing, fallback switch
# --------------------------------------------------------------------------
def _entry(role: str):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(_APP / "Home.py"), default_timeout=60)
    at.session_state["fl_role"] = role
    return at


def test_bottom_nav_shows_donor_tabs(db_path):
    at = _entry("donor").run()
    assert not at.exception
    labels = [p.label for p in at.page_link]
    for tab in ("Home", "Listings", "Donate", "Impact", "Profile"):
        assert tab in labels, f"missing bottom-nav tab {tab!r}"


def test_bottom_nav_shows_recipient_tabs(db_path):
    at = _entry("recipient").run()
    assert not at.exception
    labels = [p.label for p in at.page_link]
    for tab in ("Home", "Browse", "Claims", "Impact", "Profile"):
        assert tab in labels, f"missing bottom-nav tab {tab!r}"


def test_bottom_nav_can_be_disabled_leaving_native_nav(db_path, monkeypatch):
    monkeypatch.setenv("FOODLOOP_BOTTOM_NAV", "0")
    at = _entry("donor").run()
    assert not at.exception
    labels = [p.label for p in at.page_link]
    # The custom bar is gone; native st.navigation sidebar still routes.
    assert "Profile" not in labels
    assert "Impact" not in labels


def test_bottom_nav_css_uses_equal_grid(db_path):
    """Guard the mobile fix: the bar's columns must form five equal grid
    tracks that fill the width (Streamlit stacks/left-clusters columns on
    mobile by default)."""
    at = _entry("donor").run()
    css = " ".join(m.value for m in at.markdown)
    assert "st-key-fl_bottomnav" in css
    assert "grid-template-columns: repeat(5, minmax(0, 1fr))" in css
    assert "min-height: 44px" in css  # touch target preserved


def test_dashboard_metric_grid_css_shared(db_path):
    """The Home pulse and Impact pages share one 2-col mobile metric grid."""
    at = _entry("donor").run()
    assert not at.exception
    css = " ".join(m.value for m in at.markdown)
    assert "st-key-fl_metricgrid" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in css
    # Township-pulse (claim-accurate) labels present.
    labels = {m.label for m in at.metric}
    for lbl in ("Listings available", "Awaiting pickup", "Collections",
                "Organisations helped"):
        assert lbl in labels


def test_donor_routes_switch_without_error(db_path):
    at = _entry("donor").run()
    for route in ("views/home.py", "views/my_listings.py", "views/donate.py",
                  "views/donor_impact.py", "views/insights.py", "views/profile.py"):
        at.switch_page(route)
        at.run()
        assert not at.exception, f"{route}: {at.exception}"


def test_recipient_routes_switch_without_error(db_path):
    at = _entry("recipient").run()
    for route in ("views/home.py", "views/marketplace.py", "views/my_claims.py",
                  "views/recipient_impact.py", "views/insights.py", "views/profile.py"):
        at.switch_page(route)
        at.run()
        assert not at.exception, f"{route}: {at.exception}"


# --------------------------------------------------------------------------
# Donor impact page
# --------------------------------------------------------------------------
def test_donor_impact_counts_only_confirmed(db_path):
    repo = _seed_minimal(db_path)
    # One collected claim and one expired claim -> rate 1/2 = 50%.
    _add_available(repo, "dn1")
    repo.create_claim(claim_id="c1", donation_id="dn1", recipient_id="r1",
                      claimed_at=datetime.now(), now=datetime.now())
    repo.confirm_collection("c1", datetime.now(), now=datetime.now())
    repo.add_donation(Donation(
        "dn2", "d1", "Soon", "", FoodCategory.BAKERY, Quantity(5, Unit.LOAVES),
        Location("Central"), collection_deadline=datetime.now() + timedelta(minutes=1),
        created_at=datetime.now(), status=DonationStatus.AVAILABLE,
    ))
    repo.create_claim(claim_id="c2", donation_id="dn2", recipient_id="r1",
                      claimed_at=datetime.now(), now=datetime.now())
    repo.expire_overdue(datetime.now() + timedelta(minutes=5))  # c2 -> EXPIRED

    at = _app_test("views/donor_impact.py").run()  # default donor persona
    assert not at.exception
    assert _metric(at, "Confirmed collections") == "1"
    assert _metric(at, "Expired claims") == "1"
    assert _metric(at, "Collection rate") == "50%"   # 1 of 2 concluded claims


# --------------------------------------------------------------------------
# Recipient impact page
# --------------------------------------------------------------------------
def test_recipient_impact_metrics_preserved(db_path):
    repo = _seed_minimal(db_path)
    _add_available(repo, "dn1")
    repo.create_claim(claim_id="c1", donation_id="dn1", recipient_id="r1",
                      claimed_at=datetime.now(), now=datetime.now())
    repo.confirm_collection("c1", datetime.now(), now=datetime.now())
    _add_available(repo, "dn2")
    repo.create_claim(claim_id="c2", donation_id="dn2", recipient_id="r1",
                      claimed_at=datetime.now(), now=datetime.now())  # active

    at = _app_test("views/recipient_impact.py", "recipient").run()
    assert not at.exception
    assert _metric(at, "Confirmed collections") == "1"
    assert _metric(at, "In progress") == "1"
    assert _metric(at, "Donors supported") == "1"


# --------------------------------------------------------------------------
# Township insights page
# --------------------------------------------------------------------------
def test_insights_distinguishes_demo_and_user(db_path):
    from data.seed import SEED_DONATION_IDS
    from foodloop.workflow import FoodLoopService

    # Empty DB -> get_service seeds the demo data.
    at = _app_test("views/insights.py").run()
    assert not at.exception
    assert _metric(at, "Demo (seed)") == str(len(SEED_DONATION_IDS))
    assert _metric(at, "User-created") == "0"

    # A real, user-created donation must be counted as user, not demo.
    svc = FoodLoopService(Repository(db_path))
    svc.create_donation(
        donor_id="don-bakery", title="User loaves", description="",
        category=FoodCategory.BAKERY, quantity_value=5, quantity_unit=Unit.LOAVES,
        area="Central", collection_deadline=datetime.now() + timedelta(days=1),
    )
    at2 = _app_test("views/insights.py").run()
    assert _metric(at2, "User-created") == "1"
    assert _metric(at2, "Demo (seed)") == str(len(SEED_DONATION_IDS))


# --------------------------------------------------------------------------
# Donor withdraw (close to new claims)
# --------------------------------------------------------------------------
def test_donor_can_withdraw_available_listing(db_path):
    repo = _seed_minimal(db_path)
    donation = _add_available(repo)

    at = _app_test("views/my_listings.py").run()
    # Default persona is Donor; the only donor ("Alpha Bakery") is selected.
    withdraw = _button(at, f"withdraw-{donation.id}")
    assert withdraw is not None
    withdraw.click()
    at.run()

    assert not at.exception
    # Nothing was committed, so closing the listing cancels it.
    assert repo.get_donation(donation.id).status is DonationStatus.CANCELLED
    assert repo.get_donation(donation.id).closed_to_claims is True


# --------------------------------------------------------------------------
# Bug-fix regressions: partial claims in the marketplace
# --------------------------------------------------------------------------
def _add_second_recipient(repo: Repository) -> None:
    repo.add_recipient(Recipient(
        "r2", "Beta Shelter", RecipientType.SHELTER, Location("Central"),
        accepted_categories={FoodCategory.BAKERY},
        storage_capabilities={StorageRequirement.AMBIENT},
        collection_days={"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"},
        collection_start_hour=0, collection_end_hour=24, verified=True,
    ))


def _marketplace_as(recipient_name: str):
    at = _app_test("views/marketplace.py", "recipient")
    at.session_state["fl_recipient_name"] = recipient_name
    return at


def _claim_via_card(at, donation_id: str, qty) -> None:
    at.number_input(key=f"claim-qty-{donation_id}").set_value(qty)
    _button(at, f"claim-{donation_id}").click()
    at.run()


def test_recipient_can_claim_partial_quantity_from_marketplace(db_path):
    repo = _seed_minimal(db_path)
    donation = _add_available(repo)  # 20 loaves

    at = _marketplace_as("Alpha Food Bank").run()
    # Defaults to everything still available.
    assert at.number_input(key=f"claim-qty-{donation.id}").value == 20
    _claim_via_card(at, donation.id, 5)

    assert not at.exception
    claims = repo.list_claims_for_donation(donation.id)
    assert [c.quantity_value for c in claims] == [5]
    assert repo.donation_availability(donation.id).remaining == 15
    assert repo.get_donation(donation.id).status is DonationStatus.AVAILABLE
    # The refreshed card shows what's left, and the picker defaults to it.
    text = " ".join(m.value for m in at.markdown)
    assert "15 of 20 loaves available" in text
    picker = at.number_input(key=f"claim-qty-{donation.id}")
    assert picker.value == 15
    assert picker.proto.set_value   # the new default reaches the browser too


def test_stale_marketplace_claim_cannot_overreserve(db_path):
    """Another recipient reserves after this page rendered; requesting more
    than is now left is rejected by the core, shown as feedback, not a crash."""
    repo = _seed_minimal(db_path)
    _add_second_recipient(repo)
    donation = _add_available(repo)

    at = _marketplace_as("Alpha Food Bank").run()
    repo.create_claim(claim_id="c-other", donation_id=donation.id, recipient_id="r2",
                      claimed_at=datetime.now(), quantity=15, now=datetime.now())
    _claim_via_card(at, donation.id, 10)  # only 5 remain now

    assert not at.exception
    assert [c.recipient_id for c in repo.list_claims_for_donation(donation.id)] == ["r2"]
    assert repo.donation_availability(donation.id).remaining == 5
    # The picker re-defaults to what is actually left, ready for a retry.
    assert at.number_input(key=f"claim-qty-{donation.id}").value == 5


def test_multiple_partial_claims_and_cancel_keep_ui_quantities_correct(db_path):
    repo = _seed_minimal(db_path)
    _add_second_recipient(repo)
    donation = _add_available(repo)  # 20 loaves

    _claim_via_card(_marketplace_as("Alpha Food Bank").run(), donation.id, 5)
    _claim_via_card(_marketplace_as("Beta Shelter").run(), donation.id, 7)
    avail = repo.donation_availability(donation.id)
    assert (avail.reserved, avail.remaining) == (12, 8)

    # r1 cancels from My Claims: exactly its 5 loaves are released.
    r1_claim = next(c for c in repo.list_claims_for_donation(donation.id)
                    if c.recipient_id == "r1")
    mc = _app_test("views/my_claims.py", "recipient")
    mc.session_state["fl_recipient_name"] = "Alpha Food Bank"
    mc.run()
    _button(mc, f"cancel-{r1_claim.id}").click()
    mc.run()
    assert not mc.exception
    avail = repo.donation_availability(donation.id)
    assert (avail.reserved, avail.remaining) == (7, 13)

    # The marketplace reflects the released quantity on the next load.
    at = _marketplace_as("Alpha Food Bank").run()
    assert "13 of 20 loaves available" in " ".join(m.value for m in at.markdown)
    assert at.number_input(key=f"claim-qty-{donation.id}").value == 13


def test_detail_dialog_shows_quantity_breakdown_and_picker(db_path):
    """The dialog's Reserve click itself isn't driven here: AppTest reruns the
    whole script rather than the dialog fragment, so the dialog isn't
    re-entered. Both buttons share do_claim, covered via the card tests."""
    repo = _seed_minimal(db_path)
    _add_second_recipient(repo)
    donation = _add_available(repo)
    repo.create_claim(claim_id="c-other", donation_id=donation.id, recipient_id="r2",
                      claimed_at=datetime.now(), quantity=6, now=datetime.now())

    at = _marketplace_as("Alpha Food Bank").run()
    _button(at, f"detail-{donation.id}").click()
    at.run()
    text = " ".join(m.value for m in at.markdown)
    assert "Listed: 20 loaves" in text
    assert "Reserved: 6 loaves" in text
    assert "Available now: 14 loaves" in text
    assert at.number_input(key=f"claim-qty-dialog-{donation.id}").value == 14
    assert _button(at, f"claim-dialog-{donation.id}") is not None


def _my_claims_text(recipient_name: str = "Alpha Food Bank") -> str:
    at = _app_test("views/my_claims.py", "recipient")
    at.session_state["fl_recipient_name"] = recipient_name
    at.run()
    assert not at.exception
    return " ".join(m.value for m in at.markdown)


def test_my_claims_shows_partial_claim_quantity_not_listing_total(db_path):
    repo = _seed_minimal(db_path)
    donation = _add_available(repo)  # 20 loaves
    repo.create_claim(claim_id="c1", donation_id=donation.id, recipient_id="r1",
                      claimed_at=datetime.now(), quantity=5, now=datetime.now())

    text = _my_claims_text()
    assert "Reserved: <b>5 loaves</b>" in text
    assert "Reserved: <b>20 loaves</b>" not in text
    assert "(listing: 20 loaves)" in text


def test_my_claims_multiple_claims_each_show_own_quantity_and_state(db_path):
    repo = _seed_minimal(db_path)
    _add_second_recipient(repo)
    donation = _add_available(repo)
    now = datetime.now()
    repo.create_claim(claim_id="c1", donation_id=donation.id, recipient_id="r1",
                      claimed_at=now, quantity=3, now=now)
    repo.create_claim(claim_id="c2", donation_id=donation.id, recipient_id="r1",
                      claimed_at=now, quantity=4, now=now)
    repo.create_claim(claim_id="c3", donation_id=donation.id, recipient_id="r1",
                      claimed_at=now, quantity=2, now=now)
    repo.confirm_collection("c3", now, now=now)
    repo.create_claim(claim_id="c-r2", donation_id=donation.id, recipient_id="r2",
                      claimed_at=now, quantity=9, now=now)

    text = _my_claims_text()
    assert "Reserved: <b>3 loaves</b>" in text
    assert "Reserved: <b>4 loaves</b>" in text
    assert "Collected: <b>2 loaves</b>" in text
    assert "9 loaves" not in text            # another recipient's claim
    # And Beta Shelter sees only its own 9.
    other = _my_claims_text("Beta Shelter")
    assert "Reserved: <b>9 loaves</b>" in other
    assert "3 loaves" not in other


def test_capacity_limited_recipient_can_claim_part_of_kg_listing(db_path):
    """Recipient with ~10 kg capacity on a 20 kg listing: Claim offered, picker
    defaults to 10, and a 5 kg reservation goes through."""
    repo = _seed_minimal(db_path)
    repo.add_recipient(Recipient(
        "r-small", "Small Kitchen", RecipientType.SHELTER, Location("Central"),
        accepted_categories={FoodCategory.BAKERY},
        storage_capabilities={StorageRequirement.AMBIENT},
        max_quantity_kg_equiv=10,
        collection_days={"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"},
        collection_start_hour=0, collection_end_hour=24, verified=True,
    ))
    repo.add_donation(Donation(
        "dn-kg", "d1", "Flour", "", FoodCategory.BAKERY, Quantity(20, Unit.KG),
        Location("Central"), collection_deadline=datetime.now() + timedelta(days=2),
        created_at=datetime.now(), status=DonationStatus.AVAILABLE,
        storage=StorageRequirement.AMBIENT,
    ))

    at = _marketplace_as("Small Kitchen").run()
    assert _button(at, "claim-dn-kg") is not None
    assert at.number_input(key="claim-qty-dn-kg").value == 10.0
    _claim_via_card(at, "dn-kg", 5.0)
    assert not at.exception
    assert [c.quantity_value for c in repo.list_claims_for_donation("dn-kg")] == [5.0]
    assert at.number_input(key="claim-qty-dn-kg").value == 5.0   # headroom left


def test_detail_dialog_omits_recipient_with_exhausted_capacity(db_path):
    """The dialog's recommendations come from the claim-aware service."""
    repo = _seed_minimal(db_path)
    repo.add_recipient(Recipient(
        "r-small", "Small Kitchen", RecipientType.SHELTER, Location("Central"),
        accepted_categories={FoodCategory.BAKERY},
        storage_capabilities={StorageRequirement.AMBIENT},
        max_quantity_kg_equiv=10,
        collection_days={"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"},
        collection_start_hour=0, collection_end_hour=24, verified=True,
    ))
    repo.add_donation(Donation(
        "dn-kg", "d1", "Flour", "", FoodCategory.BAKERY, Quantity(20, Unit.KG),
        Location("Central"), collection_deadline=datetime.now() + timedelta(days=2),
        created_at=datetime.now(), status=DonationStatus.AVAILABLE,
        storage=StorageRequirement.AMBIENT,
    ))
    repo.create_claim(claim_id="c1", donation_id="dn-kg", recipient_id="r-small",
                      claimed_at=datetime.now(), quantity=10, now=datetime.now(),
                      recipient_capacity=10)

    at = _app_test("views/marketplace.py").run()   # donor view; dialog still ranks
    _button(at, "detail-dn-kg").click()
    at.run()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Alpha Food Bank" in text          # no capacity gate -> recommended
    assert "Small Kitchen" not in text        # used its 10 kg on this listing


# --------------------------------------------------------------------------
# Bug-fix regressions: Donate form best-before + reset
# --------------------------------------------------------------------------
def _fill_valid_donation(at, title: str = "Reset Loaves") -> None:
    at.text_input(key="f_title").set_value(title)
    at.text_area(key="f_desc").set_value("Day-old")
    at.number_input(key="f_qty").set_value(12)
    at.selectbox(key="f_unit").set_value(Unit.LOAVES)
    at.text_input(key="f_area").set_value("Central")


def test_best_before_date_is_selectable_stored_and_displayed(db_path):
    from datetime import date

    at = _app_test("views/donate.py").run()
    bb_widget = at.date_input(key="f_bb")
    assert not bb_widget.disabled          # the original bug: stuck disabled
    assert bb_widget.value is None         # optional: empty means "none"

    bb = date.today() + timedelta(days=3)
    _fill_valid_donation(at, "Best-before Loaves")
    bb_widget.set_value(bb)
    _button(at, "List donation").click()
    at.run()
    assert not at.exception

    repo = Repository(db_path)
    created = next(d for d in repo.list_donations() if d.title == "Best-before Loaves")
    assert created.best_before == datetime.combine(bb, datetime.min.time())

    # Shown in the marketplace detail view afterwards.
    mk = _app_test("views/marketplace.py").run()
    _button(mk, f"detail-{created.id}").click()
    mk.run()
    assert f"Best before:** {created.best_before:%a %d %b}" in " ".join(
        m.value for m in mk.markdown)


def test_donation_without_best_before_stores_none(db_path):
    at = _app_test("views/donate.py").run()
    _fill_valid_donation(at, "No BB Loaves")
    _button(at, "List donation").click()
    at.run()
    created = next(d for d in Repository(db_path).list_donations()
                   if d.title == "No BB Loaves")
    assert created.best_before is None


def test_donate_form_resets_after_success_and_keeps_other_state(db_path):
    from datetime import date

    at = _app_test("views/donate.py").run()
    donor_area = at.text_input(key="f_area").value   # the donor's own default
    at.session_state["unrelated_page_state"] = "keep me"
    _fill_valid_donation(at)
    at.date_input(key="f_bb").set_value(date.today() + timedelta(days=2))
    _button(at, "List donation").click()
    at.run()

    assert not at.exception
    # Fields are back to their defaults on the server...
    assert at.text_input(key="f_title").value == ""
    assert at.text_area(key="f_desc").value == ""
    assert at.number_input(key="f_qty").value == 1.0
    assert at.text_input(key="f_area").value == donor_area
    assert at.date_input(key="f_bb").value is None
    # ...and actually pushed to the browser. Streamlit only overwrites what the
    # browser shows when the element carries set_value; deleting the keys
    # (the original fix) reset the server copy but left old entries on screen.
    for widget in (at.text_input(key="f_title"), at.text_area(key="f_desc"),
                   at.number_input(key="f_qty"), at.date_input(key="f_bb"),
                   at.text_area(key="f_notes")):
        assert widget.proto.set_value, f"{widget.key} not pushed to the browser"
    # Success is the toast queued for the next page render; no inline banner.
    assert not at.success
    assert any("Reset Loaves" in msg for msg, _ in at.session_state["_flashes"])
    # Unrelated state is left alone.
    assert at.session_state["fl_role"] == "donor"
    assert at.session_state["unrelated_page_state"] == "keep me"

    # A further run (no click) neither re-submits nor resets again.
    at.run()
    titles = [d.title for d in Repository(db_path).list_donations()]
    assert titles.count("Reset Loaves") == 1


@pytest.mark.parametrize("qty, unit, fragment", [
    (2.5, Unit.LOAVES, "Loaves must be listed in whole units"),
    (1.2345, Unit.KG, "at most 3 decimal places"),
])
def test_donate_form_rejects_unsupported_precision_and_keeps_entries(
    db_path, qty, unit, fragment
):
    at = _app_test("views/donate.py").run()
    assert at.number_input(key="f_qty").proto.format == "%.3f"
    before = len(Repository(db_path).list_donations())

    _fill_valid_donation(at, "Precise Listing")
    at.number_input(key="f_qty").set_value(qty)
    at.selectbox(key="f_unit").set_value(unit)
    _button(at, "List donation").click()
    at.run()

    assert not at.exception
    assert len(at.error) >= 1
    assert any(fragment in m.value for m in at.markdown)       # useful message
    assert len(Repository(db_path).list_donations()) == before  # nothing stored
    # Every entry is kept, including the rejected amount (no silent rounding).
    assert at.text_input(key="f_title").value == "Precise Listing"
    assert at.text_area(key="f_desc").value == "Day-old"
    assert at.text_input(key="f_area").value == "Central"
    assert at.selectbox(key="f_unit").value == unit
    assert at.number_input(key="f_qty").value == qty
    assert not at.success


def test_one_tap_card_claim_on_three_decimal_kg_listing(db_path):
    repo = _seed_minimal(db_path)
    repo.add_donation(Donation(
        "dn-kg", "d1", "Flour", "", FoodCategory.BAKERY, Quantity(1.234, Unit.KG),
        Location("Central"), collection_deadline=datetime.now() + timedelta(days=2),
        created_at=datetime.now(), status=DonationStatus.AVAILABLE,
        storage=StorageRequirement.AMBIENT,
    ))
    at = _marketplace_as("Alpha Food Bank").run()
    assert at.number_input(key="claim-qty-dn-kg").value == 1.234   # default
    _button(at, "claim-dn-kg").click()                                # one tap
    at.run()

    assert not at.exception
    assert [c.quantity_value for c in repo.list_claims_for_donation("dn-kg")] == [1.234]
    assert repo.donation_availability("dn-kg").remaining == 0.0
    assert repo.get_donation("dn-kg").status is DonationStatus.RESERVED


def test_donate_form_keeps_entries_when_validation_fails(db_path):
    at = _app_test("views/donate.py").run()
    before = len(Repository(db_path).list_donations())
    _fill_valid_donation(at, "Keep My Entries")
    at.number_input(key="f_qty").set_value(0)   # invalid -> must not reset
    _button(at, "List donation").click()
    at.run()

    assert not at.exception
    assert len(at.error) >= 1
    assert len(Repository(db_path).list_donations()) == before
    assert at.text_input(key="f_title").value == "Keep My Entries"
    assert at.text_area(key="f_desc").value == "Day-old"
    assert not at.success
