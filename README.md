# FoodLoop

A township **food-rescue** prototype that connects businesses with surplus
food to suitable community organisations — and verifies that the food was
actually collected, so impact is measurable rather than assumed.

> **Prototype notice.** All data in this app is **synthetic and for
> demonstration only**. It does not represent real businesses,
> organisations, partnerships, or actual food-rescue impact. "Demo"
> metrics are labelled as such throughout the UI.

## What it does (target workflow)

1. A donor lists surplus food.
2. The listing appears in the marketplace.
3. A suitable, eligible recipient discovers it.
4. The recipient **claims** it (a commitment to collect).
5. Status updates — a claim is *not* yet a success.
6. **Collection is explicitly confirmed.**
7. Dashboards and impact metrics update from confirmed collections only.

## Architecture

Business logic is kept independent of the UI so it can be tested in
isolation and reused if the frontend ever changes.

| Layer | Location | Responsibility |
|------|----------|----------------|
| UI | `app/` | Streamlit pages & components only — no business rules |
| Core | `foodloop/` | Models, validation, workflow, matching, impact |
| Persistence | `foodloop/repository.py` | SQLite (stdlib) behind a domain interface |
| Demo data | `data/` | Clearly-labelled synthetic seed data |
| Tests | `tests/` | pytest — critical behaviour |

**Persistence:** SQLite via the standard-library `sqlite3`. A `UNIQUE`
constraint on `claims.donation_id` makes duplicate-claim prevention a real
database guarantee, not best-effort app logic. SQLite serialises writes, so
this is correct but not built for high write concurrency — acceptable for a
township prototype and documented rather than hidden.

## Setup (Windows / PowerShell)

> Note: this machine's Python 3.13 shipped with a broken `ensurepip`
> (bundled pip wheel version mismatch). The `.venv` here already has pip
> bootstrapped from the bundled wheel. If you recreate the venv and `pip`
> is missing, extract the bundled wheel into `site-packages`:
> `python -c "import zipfile; zipfile.ZipFile(r'<...>\ensurepip\_bundled\pip-25.0.1-py3-none-any.whl').extractall(r'.venv\Lib\site-packages')"`

**Python 3.11 or newer is required** (the code uses `enum.StrEnum` and
`X | None` type unions).

```powershell
# from the project root
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt   # runtime deps + test tooling (dev)
```

Runtime-only install (what a deployment uses): `pip install -r requirements.txt`.

## Run

```powershell
.venv\Scripts\python.exe -m streamlit run app/Home.py
```

## Test

```powershell
.venv\Scripts\python.exe -m pytest
```

## Deployment (teammate testing)

- **Entry point:** `app/Home.py` (run from the repo root).
- **Python version:** 3.11+. On **Streamlit Community Cloud** this is set in the
  app's **Advanced settings → Python version** during deploy (Community Cloud
  does not read a `runtime.txt`/`.python-version` from the repo). Pick 3.11, 3.12
  or 3.13.
- **Dependencies:** Community Cloud installs `requirements.txt` (runtime only —
  `streamlit`, `pandas`, `altair`). Test tooling is in `requirements-dev.txt`
  and is not installed in production.
- **Data:** the app seeds disposable demo data into `data/foodloop.db` on first
  run. That file (and its WAL/SHM sidecars and migration backups) is
  git-ignored, so a **git-based** deploy ships **no** database — the deployed
  app starts empty and seeds itself. On Community Cloud the filesystem is
  **ephemeral**: data resets on restart/redeploy. This is expected for demo use;
  it needs no secrets or persistent storage. Deploy via Git (not a folder copy)
  so `.gitignore` keeps local data out.
- **No authentication:** the persona switcher is a demo stand-in, not a login
  (disclosed in the UI). If access should be limited to teammates, set the
  Community Cloud app to **private** with a viewer allowlist.

## Safety & integrity principles

- Collection **deadlines** and food-**safety** info (storage, handling,
  allergens) are separate concepts; neither is inferred from the other.
- Food safety and shelf life are **never** inferred from an image.
- Allergen info is donor-declared; "none declared" ≠ "allergen-free".
- The recipient-matching algorithm is **rule-based and explainable**, not a
  trained AI model, and is described as such.
- Points/rewards are awarded **only** for confirmed collections.
