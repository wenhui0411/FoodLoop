"""Wiring between Streamlit and the FoodLoop core.

Responsibilities kept here (and nowhere else in the UI):
* make the project root importable so ``foodloop`` / ``data`` resolve
  regardless of how Streamlit is launched,
* build a single shared :class:`FoodLoopService` (cached across reruns),
* seed demo data once (idempotently) and keep expiry current.

The UI never constructs a ``Repository`` or applies business rules itself;
it always goes through the service returned here.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

# --- make the project root importable (app/ is what Streamlit puts on the
#     path; the core lives one level up) -----------------------------------
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st  # noqa: E402  (after sys.path setup)

from data.seed import ensure_seeded  # noqa: E402
from foodloop.repository import Repository  # noqa: E402
from foodloop.workflow import FoodLoopService  # noqa: E402

_DEFAULT_DB = str(_ROOT / "data" / "foodloop.db")


def _db_path() -> str:
    """Resolve the DB path at call time.

    Overridable via the ``FOODLOOP_DB`` env var so the app (and tests) can run
    against an isolated database without code changes. Read lazily — not at
    import — so each test's chosen path takes effect.
    """
    return os.environ.get("FOODLOOP_DB", _DEFAULT_DB)


@st.cache_resource
def _build_service(db_path: str) -> FoodLoopService:
    """Build (and seed) a service for a specific database path.

    Cached per ``db_path`` so the real app reuses one instance while tests
    pointing at different temp databases each get an isolated service. The
    repository's connection-per-operation strategy keeps this rerun-safe.
    """
    repo = Repository(db_path)
    ensure_seeded(repo, datetime.now())
    return FoodLoopService(repo)


def get_service() -> FoodLoopService:
    """Return the shared service for the active database path."""
    return _build_service(_db_path())


def refresh(service: FoodLoopService) -> None:
    """Keep derived state current on each page load.

    Expiring overdue listings here means marketplace/dashboard queries never
    show a past-deadline donation as still available. Cheap enough to run
    every rerun at prototype scale.
    """
    service.expire_overdue()
