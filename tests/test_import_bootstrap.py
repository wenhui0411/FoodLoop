"""Regression test for the deployment import-order bug.

On Streamlit Community Cloud only ``app/`` is on ``sys.path`` — the repository
root is not. ``app/Home.py`` imports ``components.ui`` (which imports
``foodloop``) before ``components.service``, so the root-path insertion must
happen in ``components/__init__`` (package import), not in ``service``.

This runs a subprocess with ONLY ``app/`` on ``PYTHONPATH`` and the cwd set
away from the repo root, reproducing that environment: ``from components import
ui`` must still succeed. Previously this raised
``ModuleNotFoundError: No module named 'foodloop'``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_APP = _ROOT / "app"


def test_components_import_bootstraps_foodloop_without_root_on_path(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_APP)       # only app/, NOT the repo root
    env.pop("FOODLOOP_DB", None)

    result = subprocess.run(
        [sys.executable, "-c",
         "from components import ui; "
         "import foodloop.models; "
         "print('ok', bool(ui.FOREST))"],
        cwd=str(tmp_path),              # cwd is not the repo root
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"import failed (deployment-style path):\n{result.stderr}"
    )
    assert "ok" in result.stdout
