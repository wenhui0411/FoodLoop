"""Validation of donor-supplied donation input.

This is the single source of truth for *what makes a donation listing
valid*. Both the service layer and (later) the UI rely on it, so the rules
are not duplicated anywhere.

The public entry point :func:`validate_donation_input` accepts loosely-typed
input (strings/enums, as a UI would supply) and either returns a dict of
clean, typed fields ready to build a :class:`~foodloop.models.Donation`, or
raises :class:`~foodloop.errors.ValidationError` carrying a
``field -> message`` map so the UI can show errors in context.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TypeVar

from .errors import ValidationError
from .models import (
    FoodCategory,
    Location,
    Quantity,
    StorageRequirement,
    Unit,
)

_TITLE_MAX = 120
_DESC_MAX = 1000
_NOTES_MAX = 1000

#: Units that are counted in whole pieces (no fractional claims).
_INTEGER_UNITS: frozenset[Unit] = frozenset(
    {Unit.ITEMS, Unit.LOAVES, Unit.MEALS, Unit.TRAYS}
)
#: Decimal places retained for continuous units (grams / millilitres).
_DECIMAL_PLACES = 3

_E = TypeVar("_E", bound=StrEnum)


def is_whole_unit(unit: Unit) -> bool:
    """Whether ``unit`` is claimed in whole pieces only (lets a UI choose an
    integer input without duplicating the rule in :func:`validate_claim_quantity`)."""
    return unit in _INTEGER_UNITS


def validate_claim_quantity(unit: Unit, value: object) -> float:
    """Validate a requested claim quantity for its unit.

    * Must be a positive number.
    * Integer-only units (items/loaves/meals/trays) must be whole numbers.
    * Continuous units (kg/litres) may be decimal, quantised to
      ``_DECIMAL_PLACES`` so stored values don't carry float drift.

    Returns the cleaned value or raises
    :class:`~foodloop.errors.ValidationError`. The ``<= remaining`` check is
    enforced atomically in the repository, not here.
    """
    try:
        qty = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValidationError({"quantity": "Quantity must be a number."})
    if qty != qty or qty in (float("inf"), float("-inf")):  # NaN / inf
        raise ValidationError({"quantity": "Quantity must be a finite number."})
    if qty <= 0:
        raise ValidationError({"quantity": "Quantity must be greater than zero."})
    if unit in _INTEGER_UNITS and not float(qty).is_integer():
        raise ValidationError(
            {"quantity": f"{unit.value.title()} must be claimed in whole units."}
        )
    return round(qty, _DECIMAL_PLACES)


def _listing_precision_error(unit: Unit, qty: float) -> str | None:
    """Why a positive listing quantity has unsupported precision, or None.

    Uses ``Decimal(str(qty))`` — the shortest round-trip text of the float —
    so a typed ``1.234`` is checked as exactly 1.234, free of binary noise.
    """
    value = Decimal(str(qty))
    if not value.is_finite():
        return "Quantity must be a finite number."
    if is_whole_unit(unit):
        if value != value.to_integral_value():
            return f"{unit.value.title()} must be listed in whole units."
        return None
    if value.normalize().as_tuple().exponent < -_DECIMAL_PLACES:
        return f"Quantity can have at most {_DECIMAL_PLACES} decimal places."
    return None


def _coerce_enum(
    enum_cls: type[_E], value: object, field: str, errors: dict[str, str]
) -> _E | None:
    """Return ``value`` as a member of ``enum_cls`` or record an error."""
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)  # type: ignore[arg-type]
    except (ValueError, KeyError):
        allowed = ", ".join(m.value for m in enum_cls)
        errors[field] = f"Must be one of: {allowed}."
        return None


def _clean_text(
    value: object, field: str, errors: dict[str, str], *, required: bool, max_len: int
) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        errors[field] = "This field is required."
    elif len(text) > max_len:
        errors[field] = f"Must be at most {max_len} characters."
    return text


def validate_donation_input(
    *,
    donor_id: str,
    title: object,
    description: object = "",
    category: object,
    quantity_value: object,
    quantity_unit: object,
    area: object,
    collection_deadline: object,
    now: datetime,
    latitude: float | None = None,
    longitude: float | None = None,
    storage: object = StorageRequirement.AMBIENT,
    allergens: list[str] | None = None,
    allergens_declared: bool = False,
    handling_notes: object = "",
    best_before: datetime | None = None,
) -> dict[str, object]:
    """Validate and normalise donation input.

    Returns clean, typed fields (keys match the :class:`Donation`
    constructor, minus ``id``/``created_at``/``status``). Raises
    :class:`ValidationError` if anything is wrong.
    """
    errors: dict[str, str] = {}

    if not str(donor_id or "").strip():
        errors["donor_id"] = "A donor is required."

    title_text = _clean_text(title, "title", errors, required=True, max_len=_TITLE_MAX)
    desc_text = _clean_text(
        description, "description", errors, required=False, max_len=_DESC_MAX
    )
    notes_text = _clean_text(
        handling_notes, "handling_notes", errors, required=False, max_len=_NOTES_MAX
    )
    area_text = _clean_text(area, "area", errors, required=True, max_len=_TITLE_MAX)

    category_val = _coerce_enum(FoodCategory, category, "category", errors)
    unit_val = _coerce_enum(Unit, quantity_unit, "quantity_unit", errors)
    storage_val = _coerce_enum(StorageRequirement, storage, "storage", errors)

    # Quantity must be a positive number.
    qty: float | None = None
    try:
        qty = float(quantity_value)  # type: ignore[arg-type]
        if qty <= 0:
            errors["quantity_value"] = "Quantity must be greater than zero."
    except (TypeError, ValueError):
        errors["quantity_value"] = "Quantity must be a number."

    # Precision must match what can later be claimed (validate_claim_quantity),
    # or part of the listing could never be reserved. Rejected, never rounded:
    # the donor's stated amount is not changed silently.
    if qty is not None and unit_val is not None and "quantity_value" not in errors:
        message = _listing_precision_error(unit_val, qty)
        if message:
            errors["quantity_value"] = message

    # Collection deadline must be a datetime strictly in the future. The
    # deadline is purely about *when food can be collected* — it is not a
    # food-safety judgement (storage/allergens cover that, separately).
    if not isinstance(collection_deadline, datetime):
        errors["collection_deadline"] = "A collection deadline is required."
    elif collection_deadline <= now:
        errors["collection_deadline"] = "The collection deadline must be in the future."

    # Allergen consistency: you cannot list specific allergens while also
    # saying none were declared. "Declared with an empty list" is valid and
    # means "donor states there are none" — distinct from "not stated".
    allergen_list = list(allergens or [])
    if allergen_list and not allergens_declared:
        errors["allergens"] = (
            "Mark allergen information as declared to list specific allergens."
        )

    if best_before is not None and not isinstance(best_before, datetime):
        errors["best_before"] = "Best-before must be a valid date/time if provided."

    if errors:
        raise ValidationError(errors)

    return {
        "donor_id": str(donor_id).strip(),
        "title": title_text,
        "description": desc_text,
        "category": category_val,
        "quantity": Quantity(qty, unit_val),  # type: ignore[arg-type]
        "location": Location(area_text, latitude, longitude),
        "collection_deadline": collection_deadline,
        "storage": storage_val,
        "allergens": allergen_list,
        "allergens_declared": bool(allergens_declared),
        "handling_notes": notes_text,
        "best_before": best_before,
    }
