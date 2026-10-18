"""Reusable Streamlit UI building blocks for FoodLoop.

This package is the *only* place the UI talks to the core (via ``service.py``).
UI modules contain presentation logic only — no business rules, which all live
in the framework-agnostic ``foodloop`` package.

Importing this package also makes the **repository root** importable, so the
core ``foodloop`` (and ``data``) packages resolve no matter how or from where
Streamlit launches the app. This lives in the package ``__init__`` on purpose:
it runs before any submodule (e.g. ``components.ui``) imports ``foodloop``, so
import order in the entry point no longer matters. (Streamlit only adds
``app/`` to ``sys.path``; on hosts like Streamlit Community Cloud the repo root
is otherwise absent, which caused ``ModuleNotFoundError: No module named
'foodloop'``.)
"""

from __future__ import annotations

import sys
from pathlib import Path

# app/components/__init__.py -> parents[2] is the repository root (above app/).
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
