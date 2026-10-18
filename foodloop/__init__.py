"""FoodLoop core — framework-agnostic domain logic.

This package deliberately contains NO Streamlit imports so the business
rules (validation, workflow, matching, impact) can be unit-tested in
isolation and reused if the UI layer ever changes.
"""

__all__ = [
    "models",
    "repository",
]
