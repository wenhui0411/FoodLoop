"""Domain-level exceptions shared across the core package.

Keeping these separate lets both the persistence layer and the workflow
layer raise meaningful, catchable errors without importing each other.
"""

from __future__ import annotations


class FoodLoopError(Exception):
    """Base class for all expected FoodLoop domain errors."""


class ValidationError(FoodLoopError):
    """Raised when donor-supplied input fails validation.

    Carries a mapping of ``field -> message`` so the UI can show errors
    next to the relevant inputs.
    """

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))


class NotFoundError(FoodLoopError):
    """Raised when a referenced entity does not exist."""


class DuplicateClaimError(FoodLoopError):
    """Raised when a donation already has an active claim.

    This backs the product requirement to prevent duplicate claims; it is
    triggered by the database UNIQUE constraint, so it holds even if two
    claim attempts race.
    """


class InvalidTransitionError(FoodLoopError):
    """Raised when a status change is not allowed from the current state."""


class EligibilityError(FoodLoopError):
    """Raised when a recipient is not permitted to claim a donation."""


class MigrationError(FoodLoopError):
    """Raised when a database migration (or its pre-migration backup) cannot
    be completed safely. The live database is left untouched."""


class ClaimsClosedError(FoodLoopError):
    """Raised when a new claim is attempted on a donation the donor has closed
    to further claims (existing reservations are unaffected)."""


class OverReservationError(FoodLoopError):
    """Raised when a requested quantity exceeds the donation's remaining
    (unreserved, uncollected) quantity. The reservation is rejected and the
    database is left unchanged."""
