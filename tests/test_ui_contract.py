"""Guards the public API that the pages import from ``components.ui``.

The flash helpers are referenced by every page; if one were accidentally
removed or renamed, the pages would fail at runtime with an AttributeError.
This pins their presence so such a regression is caught in the test suite.

(Note: this cannot detect a *stale running Streamlit server* holding an old
copy of the module in sys.modules — that is resolved by restarting the
server, not by code.)
"""

from __future__ import annotations

import sys
from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "app"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))


def test_ui_exposes_flash_api():
    from components import ui

    assert callable(ui.flash)
    assert callable(ui.render_flashes)


def test_escape_neutralises_html():
    from components import ui

    out = ui.escape('<script>alert("x")</script>')
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    # Quotes and ampersands are escaped too.
    assert ui.escape("a & b") == "a &amp; b"


def test_no_deprecated_use_container_width_in_app():
    """Guard the width-API migration: the deprecated arg must not reappear."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = [
        str(p.relative_to(app_dir))
        for p in app_dir.rglob("*.py")
        if "use_container_width" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"deprecated use_container_width still present in: {offenders}"


def test_design_system_api_present():
    """The shared design-system helpers and colour tokens the views rely on."""
    from components import ui

    assert callable(ui.hero)
    assert callable(ui.section_title)
    for token in ("FOREST", "FOREST_DARK", "FOREST_LIGHT", "CREAM",
                  "CREAM_CARD", "SAND", "INK", "MUTED", "BORDER"):
        value = getattr(ui, token)
        assert isinstance(value, str) and value.startswith("#")
