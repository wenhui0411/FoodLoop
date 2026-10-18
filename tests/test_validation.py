"""Tests for donation input validation."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from foodloop.errors import ValidationError
from foodloop.models import FoodCategory, Quantity, StorageRequirement, Unit
from foodloop.validation import validate_donation_input


def _valid_kwargs(now: datetime) -> dict:
    return dict(
        donor_id="d1",
        title="Bread rolls",
        description="End of day",
        category=FoodCategory.BAKERY,
        quantity_value=12,
        quantity_unit=Unit.LOAVES,
        area="Central",
        collection_deadline=now + timedelta(days=1),
    )


def test_valid_input_returns_typed_fields(now):
    clean = validate_donation_input(now=now, **_valid_kwargs(now))
    assert clean["title"] == "Bread rolls"
    assert clean["category"] is FoodCategory.BAKERY
    assert clean["quantity"] == Quantity(12.0, Unit.LOAVES)
    assert clean["location"].area == "Central"
    assert clean["storage"] is StorageRequirement.AMBIENT


def test_accepts_enum_values_as_strings(now):
    kwargs = _valid_kwargs(now)
    kwargs.update(category="bakery", quantity_unit="loaves", storage="ambient")
    clean = validate_donation_input(now=now, **kwargs)
    assert clean["category"] is FoodCategory.BAKERY
    assert clean["quantity"].unit is Unit.LOAVES


def test_missing_title_is_rejected(now):
    kwargs = _valid_kwargs(now)
    kwargs["title"] = "   "
    with pytest.raises(ValidationError) as exc:
        validate_donation_input(now=now, **kwargs)
    assert "title" in exc.value.errors


@pytest.mark.parametrize("bad_qty", [0, -5, "abc", None])
def test_non_positive_or_nonnumeric_quantity_rejected(now, bad_qty):
    kwargs = _valid_kwargs(now)
    kwargs["quantity_value"] = bad_qty
    with pytest.raises(ValidationError) as exc:
        validate_donation_input(now=now, **kwargs)
    assert "quantity_value" in exc.value.errors


def test_past_deadline_rejected(now):
    kwargs = _valid_kwargs(now)
    kwargs["collection_deadline"] = now - timedelta(hours=1)
    with pytest.raises(ValidationError) as exc:
        validate_donation_input(now=now, **kwargs)
    assert "collection_deadline" in exc.value.errors


def test_deadline_equal_to_now_rejected(now):
    kwargs = _valid_kwargs(now)
    kwargs["collection_deadline"] = now
    with pytest.raises(ValidationError) as exc:
        validate_donation_input(now=now, **kwargs)
    assert "collection_deadline" in exc.value.errors


def test_missing_deadline_rejected(now):
    kwargs = _valid_kwargs(now)
    kwargs["collection_deadline"] = None
    with pytest.raises(ValidationError) as exc:
        validate_donation_input(now=now, **kwargs)
    assert "collection_deadline" in exc.value.errors


def test_invalid_category_rejected(now):
    kwargs = _valid_kwargs(now)
    kwargs["category"] = "nonsense"
    with pytest.raises(ValidationError) as exc:
        validate_donation_input(now=now, **kwargs)
    assert "category" in exc.value.errors


def test_allergens_listed_without_declaration_rejected(now):
    kwargs = _valid_kwargs(now)
    kwargs.update(allergens=["gluten"], allergens_declared=False)
    with pytest.raises(ValidationError) as exc:
        validate_donation_input(now=now, **kwargs)
    assert "allergens" in exc.value.errors


def test_declared_empty_allergens_is_valid(now):
    kwargs = _valid_kwargs(now)
    kwargs.update(allergens=[], allergens_declared=True)
    clean = validate_donation_input(now=now, **kwargs)
    assert clean["allergens"] == []
    assert clean["allergens_declared"] is True


def test_multiple_errors_reported_together(now):
    with pytest.raises(ValidationError) as exc:
        validate_donation_input(
            now=now,
            donor_id="d1",
            title="",
            category="bogus",
            quantity_value=-1,
            quantity_unit=Unit.KG,
            area="",
            collection_deadline=now - timedelta(days=1),
        )
    # All four problems surfaced at once, not just the first.
    assert set(exc.value.errors) >= {
        "title", "category", "quantity_value", "area", "collection_deadline"
    }
