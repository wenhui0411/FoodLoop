"""Shared pytest fixtures."""

from __future__ import annotations

from datetime import datetime

import pytest

from foodloop.repository import Repository
from foodloop.workflow import FoodLoopService


@pytest.fixture
def now() -> datetime:
    """A fixed reference time so tests are deterministic.

    2026-01-15 is a Thursday — relevant for collection-day checks.
    """
    return datetime(2026, 1, 15, 9, 0, 0)


@pytest.fixture
def repo(tmp_path) -> Repository:
    """A fresh, isolated repository backed by a temp-file SQLite DB."""
    return Repository(tmp_path / "test.db")


@pytest.fixture
def service(repo, now) -> FoodLoopService:
    """Service layer wired to the test repo with a frozen clock."""
    return FoodLoopService(repo, clock=lambda: now)
