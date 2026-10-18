"""Wiring between Streamlit and the FoodLoop core.

Responsibilities kept here (and nowhere else in the UI):
* build a single shared :class:`FoodLoopService` (cached across reruns),
* seed demo data once (idempotently) and keep expiry current.

(Making the repository root importable lives in ``components/__init__.py`` so
it runs before any ``foodloop`` import, independent of import order.)

The UI never constructs a ``Repository`` or applies business rules itself;
it always goes through the service returned here.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import streamlit as st

from data.seed import ensure_seeded
from foodloop.repository import Repository
from foodloop.workflow import FoodLoopService

# Repo root (…/FoodLoop, one level above app/) — used only for the default DB
# path. ``foodloop``/``data`` are made importable in ``components/__init__``.
_ROOT = Path(__file__).resolve().parents[2]
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
