# General Experiments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generalize Virgil Experiments from weekly-minutes-of-sport to metric kinds (duration/count/boolean/scale) with per-metric targets, one-tap logging, full edit lifecycle, an API write endpoint + MCP tool, and a Settings "App Configuration" tab for dictionary tables.

**Architecture:** Extend existing tables (`experiment_activity_types` → metrics with `kind`; `experiment_entries` → generic `value`) via guarded migration 015. Weekly-minutes targets stay for duration metrics; count/boolean metrics get `target_value` + `target_period`. Dictionary tables gain `builtin`/`archived` flags with CRUD in Settings.

**Tech Stack:** FastAPI + aiosqlite + Jinja2 + Alpine.js, pytest with `TestClient`, FastMCP stdio server.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-19-general-experiments-design.md`
- Metric kinds: exactly `('duration','count','boolean','scale')`; target periods `('day','week','total')`.
- Boolean entries: max one per metric per date (app-level delete-then-insert upsert).
- Value bounds: duration 1–1440, count 1–1000, boolean 0/1, scale 0–10.
- Builtin dictionary rows: never editable, never deletable; archive/unarchive only.
- All migrations PRAGMA-guarded and idempotent (pattern of 003/013).
- Blank numeric form fields: follow the existing pattern in the repo (see `tests/test_forms_blank_numbers.py`) — accept `str = Form("")` and coerce.
- Commits: conventional style, no Co-Authored-By lines.
- Per-metric targets only for count/boolean kinds (UI + progress math). Scale/duration ignore `target_value`.
- API entry writes: `source='api'`, `source_ref=str(uuid4())` (partial unique index `uq_entry_source` forbids duplicate `(experiment_id, source, source_ref)` for non-manual sources).

---

### Task 1: Migration 015 + db.py final schema

**Files:**
- Create: `app/migrations/015_general_experiments.py`
- Modify: `app/db.py:195-223` (experiment_activity_types, experiment_entries final shape)
- Test: `tests/test_migration_015.py`

**Interfaces:**
- Produces: columns `experiment_activity_types.kind/target_value/target_period`, `experiment_entries.value` (no `duration_minutes`), `exercise_library.builtin/archived`.

- [ ] **Step 1: failing test** — build a legacy-shaped SQLite db in tmp_path, run `up()`, assert new shape:

```python
"""Migration 015: legacy → general experiments shape."""
import importlib

import aiosqlite
import pytest


@pytest.mark.anyio
async def test_migration_015_backfills_and_drops(tmp_path):
    db_path = tmp_path / "legacy.db"
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("""CREATE TABLE experiment_activity_types (
            id INTEGER PRIMARY KEY, experiment_id INTEGER, name TEXT, color TEXT,
            display_order INTEGER, source_match TEXT NOT NULL DEFAULT '')""")
        await db.execute("""CREATE TABLE experiment_entries (
            id INTEGER PRIMARY KEY, experiment_id INTEGER, date TEXT,
            activity_type_id INTEGER, duration_minutes INTEGER NOT NULL DEFAULT 0,
            notes TEXT, source TEXT NOT NULL DEFAULT 'manual',
            source_ref TEXT NOT NULL DEFAULT '')""")
        await db.execute("""CREATE TABLE exercise_library (
            id INTEGER PRIMARY KEY, category TEXT, section TEXT, name TEXT,
            sets INTEGER, reps TEXT, notes TEXT, display_order INTEGER)""")
        await db.execute("INSERT INTO experiment_entries (experiment_id, date, activity_type_id, duration_minutes) VALUES (1,'2026-07-01',1,45)")
        await db.execute("INSERT INTO exercise_library (category, section, name) VALUES ('Cardio','Cardio','Jump Rope')")
        await db.commit()

        mod = importlib.import_module("app.migrations.015_general_experiments")
        await mod.up(db)
        await db.commit()

        cols = {r["name"] for r in await db.execute_fetchall("PRAGMA table_info(experiment_entries)")}
        assert "value" in cols and "duration_minutes" not in cols
        row = (await db.execute_fetchall("SELECT value FROM experiment_entries"))[0]
        assert row["value"] == 45
        at_cols = {r["name"] for r in await db.execute_fetchall("PRAGMA table_info(experiment_activity_types)")}
        assert {"kind", "target_value", "target_period"} <= at_cols
        lib = (await db.execute_fetchall("SELECT builtin, archived FROM exercise_library"))[0]
        assert lib["builtin"] == 1 and lib["archived"] == 0

        # Idempotent
        await mod.up(db)
```

Note: module name starts with a digit → import via `importlib.import_module("app.migrations.015_general_experiments")` exactly as `runner.py` does. Check whether the repo's test suite uses `pytest.mark.anyio` or sync sqlite3 — mirror the prevailing pattern (`test_migration_upgrade.py`).

- [ ] **Step 2: run test — expect FAIL** (module not found).
- [ ] **Step 3: implement migration:**

```python
"""Generalize experiments + dictionary flags.

- experiment_activity_types: kind / target_value / target_period (per-metric targets).
- experiment_entries: generic `value` replaces duration_minutes (backfilled, dropped).
- exercise_library: builtin (seeded rows, protected) + archived (hidden from pickers).
"""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cols = await db.execute_fetchall("PRAGMA table_info(experiment_activity_types)")
    names = {c[1] for c in cols}
    if "kind" not in names:
        await db.execute("ALTER TABLE experiment_activity_types ADD COLUMN kind TEXT NOT NULL DEFAULT 'duration'")
    if "target_value" not in names:
        await db.execute("ALTER TABLE experiment_activity_types ADD COLUMN target_value INTEGER NOT NULL DEFAULT 0")
    if "target_period" not in names:
        await db.execute("ALTER TABLE experiment_activity_types ADD COLUMN target_period TEXT NOT NULL DEFAULT 'week'")

    cols = await db.execute_fetchall("PRAGMA table_info(experiment_entries)")
    names = {c[1] for c in cols}
    if "value" not in names:
        await db.execute("ALTER TABLE experiment_entries ADD COLUMN value INTEGER NOT NULL DEFAULT 0")
        if "duration_minutes" in names:
            await db.execute("UPDATE experiment_entries SET value = duration_minutes")
    if "duration_minutes" in names:
        await db.execute("ALTER TABLE experiment_entries DROP COLUMN duration_minutes")

    cols = await db.execute_fetchall("PRAGMA table_info(exercise_library)")
    names = {c[1] for c in cols}
    if "builtin" not in names:
        await db.execute("ALTER TABLE exercise_library ADD COLUMN builtin INTEGER NOT NULL DEFAULT 0")
        await db.execute("UPDATE exercise_library SET builtin = 1")
    if "archived" not in names:
        await db.execute("ALTER TABLE exercise_library ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
```

- [ ] **Step 4: update `app/db.py`** — final shape for fresh DBs (migration guards keep legacy DBs converging to the same shape):

```sql
CREATE TABLE IF NOT EXISTS experiment_activity_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT '#3b82f6',
    display_order INTEGER DEFAULT 0,
    source_match TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'duration' CHECK(kind IN ('duration','count','boolean','scale')),
    target_value INTEGER NOT NULL DEFAULT 0,
    target_period TEXT NOT NULL DEFAULT 'week' CHECK(target_period IN ('day','week','total'))
);

CREATE TABLE IF NOT EXISTS experiment_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    activity_type_id INTEGER NOT NULL REFERENCES experiment_activity_types(id) ON DELETE CASCADE,
    value INTEGER NOT NULL DEFAULT 0,
    notes TEXT DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    source_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);
```

(`exercise_library` stays migration-owned — 009 creates + seeds, 015 flags builtin.)

- [ ] **Step 5: run migration test + full suite; commit** `feat(experiments): migration 015 — metric kinds, generic entry value, dictionary flags`

---

### Task 2: Metric constants, create route, new/edit forms

**Files:**
- Modify: `app/routers/experiments.py` (constants, create, new edit routes, metric CRUD)
- Modify: `app/models/experiments.py` (shapes documentation)
- Modify: `app/templates/experiment_new.html`
- Create: `app/templates/experiment_edit.html`
- Modify: `app/templates/experiment_detail.html` (Edit link in header)
- Test: `tests/test_general_experiments.py`, update `tests/test_experiments_lifecycle.py`

**Interfaces:**
- Produces: `METRIC_KINDS`, `TARGET_PERIODS`, `_clamp_value(kind, value) -> int | None`, form fields `metric_names/metric_colors/metric_kinds/metric_targets/metric_periods/source_matches`, routes `GET|POST /experiments/{id}/edit`, `POST /experiments/{id}/metric/add`, `POST /experiments/{id}/metric/{metric_id}/update`, `POST /experiments/{id}/metric/{metric_id}/delete`.

- [ ] **Step 1: failing tests** — create with mixed kinds; edit works on completed experiment; num_weeks resync:

```python
def _create_general(auth_client, **overrides):
    token = csrf_token(auth_client, "/experiments/new")
    data = {
        "title": "Eksperyment 14 dni", "start_date": "2026-07-13", "num_weeks": "2",
        "target_min": "0", "target_max": "0",
        "metric_names": ["Impuls", "Bramka", "Glod przed"],
        "metric_colors": ["#ef4444", "#22c55e", "#eab308"],
        "metric_kinds": ["count", "count", "scale"],
        "metric_targets": ["0", "8", "0"],
        "metric_periods": ["week", "total", "week"],
        "source_matches": ["", "", ""],
        "_csrf_token": token,
    }
    data.update(overrides)
    return auth_client.post("/experiments/create", data=data, follow_redirects=False)


def test_create_general_experiment_kinds_stored(auth_client): ...
    # assert 303; read user_db: kinds == count/count/scale, Bramka target_value 8 period 'total'

def test_edit_completed_experiment(auth_client): ...
    # create, POST /complete completed, GET /{id}/edit → 200,
    # POST /{id}/edit new title + num_weeks 4 → weeks rows == 4, title updated, status still completed

def test_num_weeks_shrink_preserves_kept_weeks(auth_client): ...
    # create 4 weeks, edit week 2 targets via /week/2/targets, shrink to 2 → weeks 1-2 remain w/ targets, 3-4 gone
```

- [ ] **Step 2: run — FAIL** (unknown form fields ignored → kinds default 'duration'; /edit 404s).
- [ ] **Step 3: implement router changes.** Constants at module top:

```python
METRIC_KINDS = ("duration", "count", "boolean", "scale")
TARGET_PERIODS = ("day", "week", "total")

_VALUE_BOUNDS = {"duration": (1, 1440), "count": (1, 1000), "boolean": (0, 1), "scale": (0, 10)}


def _clamp_value(kind: str, value: int) -> int | None:
    """None = reject the entry (out of the kind's bounds)."""
    lo, hi = _VALUE_BOUNDS.get(kind, (1, 1440))
    return value if lo <= value <= hi else None


def _normalize_metric(name, color, kind, target, period, source_match) -> dict | None:
    if not name.strip():
        return None
    kind = kind if kind in METRIC_KINDS else "duration"
    period = period if period in TARGET_PERIODS else "week"
    try:
        target = max(0, int(target))
    except (TypeError, ValueError):
        target = 0
    if kind not in ("count", "boolean"):
        target = 0                      # targets only defined for count/boolean
    if kind == "boolean" and period == "day":
        target = min(target, 1)
    return {
        "name": truncate(name.strip(), 100), "color": color.strip() or "#3b82f6",
        "kind": kind, "target_value": target, "target_period": period,
        "source_match": source_match.strip() if kind == "duration" else "",
    }
```

`create_experiment`: swap `activity_names/activity_colors` for the metric_* fields, run `_normalize_metric` per row, insert with new columns. Weeks creation unchanged.

Edit routes:

```python
@router.get("/{experiment_id}/edit", response_class=HTMLResponse)
async def edit_experiment_form(request: Request, experiment_id: int):
    # load experiment + metrics (with per-metric entry counts for delete confirm), render experiment_edit.html


@router.post("/{experiment_id}/edit")
async def edit_experiment(request, experiment_id, title=Form(...), description=Form(""),
                          start_date=Form(...), num_weeks=Form(...), status=Form("active")):
    # validate as create; UPDATE experiments SET title/description/start_date/num_weeks/status
    # resync weeks: DELETE WHERE week_number > num_weeks;
    # template targets = last surviving week's (or 0/0):
    # INSERT OR IGNORE INTO experiment_weeks (experiment_id, week_number, label, target_min, target_max)
    # for wn in 1..num_weeks
```

Metric CRUD (`/metric/add`, `/metric/{mid}/update`, `/metric/{mid}/delete`): add uses `_normalize_metric` (kind chosen once at add time); update edits name/color/target_value/target_period/source_match but never kind; delete removes metric + its entries (FK cascade) after ownership check `WHERE experiment_id = ?`.

- [ ] **Step 4: templates.** `experiment_new.html`: metric row gains kind `<select>` + Alpine conditionals:

```html
<select x-model="m.kind" name="metric_kinds">
  <option value="duration">Minutes</option><option value="count">Count</option>
  <option value="boolean">Yes / No</option><option value="scale">Rating 0-10</option>
</select>
<template x-if="m.kind === 'duration'"> <!-- source_match input as today --> </template>
<template x-if="m.kind === 'count' || m.kind === 'boolean'">
  <!-- target: <input type=number name=metric_targets> per <select name=metric_periods> day/week/total -->
</template>
```

All rows always submit hidden inputs for every field so index alignment holds (`x-show` styling, not `x-if`, for inputs that must always post — Alpine `x-if` removes nodes from the DOM and desynchronizes the parallel arrays).
Weekly minutes card wrapped in `x-show="metrics.some(m => m.kind === 'duration')"`.
`experiment_edit.html`: Basics form (title/description/start_date/num_weeks/status select) + metrics table (per-row update form, delete form with `onsubmit="return confirm('Delete metric and its N entries?')"`) + add-metric form. Detail header gains `<a href="/experiments/{{ experiment.id }}/edit">Edit</a>`.

- [ ] **Step 5: update `app/models/experiments.py`** to the new shapes (`MetricIn(name, color, kind, target_value, target_period, source_match)`, `EntryIn(date, metric_id, value, notes)`).
- [ ] **Step 6: fix `tests/test_experiments_lifecycle.py`** field names (`metric_names` etc.).
- [ ] **Step 7: run new tests + suite; commit** `feat(experiments): metric kinds on create + full edit lifecycle`

---

### Task 3: Generalized logging + quick-log bar + kind-aware detail page

**Files:**
- Modify: `app/routers/experiments.py` (`add_entry`, `_build_week_grid`, detail stats, `_metric_progress`)
- Modify: `app/templates/experiment_detail.html`
- Modify: `app/templates/experiments.html` (list card logged stat)
- Test: `tests/test_general_experiments.py`

**Interfaces:**
- Consumes: `_clamp_value`, `METRIC_KINDS`.
- Produces: `POST /experiments/{id}/entry` fields `metric_id`, `value`, `notes`, `date`; `_metric_progress(metric, entries, exp_start, num_weeks, today) -> dict | None` returning `{name, color, label, pct, met}`; grid day dict gains `duration_mins`, `dots: [{color, text, title}]`; week dict gains `metric_lines: [{name, color, text, met}]`, `has_duration` flag on stats.

- [ ] **Step 1: failing tests:**

```python
def test_boolean_upsert_one_per_day(auth_client): ...
    # boolean metric; POST /entry value=1 twice + value=0 once for same date
    # → exactly ONE row for that metric+date, value == 0 (last write wins)

def test_count_accumulates(auth_client): ...
    # POST value=1 three times same date → 3 rows, sum 3

def test_scale_rejects_out_of_bounds(auth_client): ...
    # POST value=11 → 303 redirect, no row inserted

def test_metric_progress_total_and_day(auth_client): ...
    # unit-test _metric_progress directly: count/total 5 of 8 → label "5/8", pct 62, met False;
    # boolean/day with 3 yes-days over 5 elapsed days → label "3/5 days", met False
```

- [ ] **Step 2: run — FAIL.**
- [ ] **Step 3: implement `add_entry`:**

```python
@router.post("/{experiment_id}/entry")
async def add_entry(request, experiment_id: int, date: str = Form(...),
                    metric_id: int = Form(...), value: str = Form("1"), notes: str = Form("")):
    # valid_date; metric = SELECT ... WHERE id=? AND experiment_id=? (else redirect)
    # v = int(value) (ValueError → redirect); v = _clamp_value(metric["kind"], v) (None → redirect)
    # boolean: DELETE FROM experiment_entries WHERE experiment_id=? AND activity_type_id=? AND date=?
    # INSERT (experiment_id, date, activity_type_id, value, notes)
```

- [ ] **Step 4: `_metric_progress`** (module-level, pure — unit-testable):

```python
def _metric_progress(metric: dict, entries: list[dict], exp_start: date,
                     num_weeks: int, today: date) -> dict | None:
    """Progress vs the metric's own target; only count/boolean metrics have targets."""
    if metric["kind"] not in ("count", "boolean") or not metric["target_value"]:
        return None
    tv, period = metric["target_value"], metric["target_period"]
    exp_end = exp_start + timedelta(weeks=num_weeks) - timedelta(days=1)
    week_start = today - timedelta(days=today.weekday())
    windows = {
        "total": (exp_start, exp_end),
        "week": (week_start, week_start + timedelta(days=6)),
        "day": (today, today),
    }
    lo, hi = windows[period]
    mine = [e for e in entries
            if e["activity_type_id"] == metric["id"] and lo.isoformat() <= e["date"] <= hi.isoformat()]
    if metric["kind"] == "boolean":
        logged = len({e["date"] for e in mine if e["value"] == 1})
    else:
        logged = sum(e["value"] for e in mine)
    if metric["kind"] == "boolean" and period == "day":
        # "every day": denominator = elapsed experiment days
        elapsed = max(1, min((today - exp_start).days + 1, num_weeks * 7))
        mine_all = [e for e in entries if e["activity_type_id"] == metric["id"] and e["value"] == 1]
        logged = len({e["date"] for e in mine_all if e["date"] <= today.isoformat()})
        return {"name": metric["name"], "color": metric["color"],
                "label": f"{logged}/{elapsed} days", "pct": round(logged / elapsed * 100),
                "met": logged >= elapsed}
    suffix = {"total": "", "week": " this week", "day": " today"}[period]
    unit = " days" if metric["kind"] == "boolean" else ""
    return {"name": metric["name"], "color": metric["color"],
            "label": f"{logged}/{tv}{unit}{suffix}",
            "pct": min(100, round(logged / tv * 100)), "met": logged >= tv}
```

- [ ] **Step 5: `_build_week_grid` generalization.** Metric kind map; day cells:

```python
duration_mins = sum(e["value"] for e in day_entries if type_kinds[e["activity_type_id"]] == "duration")
# per-metric aggregation for dots (boolean ✓/✗, count sum, scale avg):
dots = []
for at in activity_types:
    if at["kind"] == "duration":
        continue
    es = [e for e in day_entries if e["activity_type_id"] == at["id"]]
    if not es:
        continue
    if at["kind"] == "boolean":
        text = "✓" if es[-1]["value"] == 1 else "✗"
    elif at["kind"] == "count":
        text = str(sum(e["value"] for e in es))
    else:
        text = str(round(sum(e["value"] for e in es) / len(es)))
    dots.append({"color": at["color"], "text": text, "title": at["name"]})
```

Cell fill: duration color + `{mins}m` label as today; special case single-boolean-metric experiment → yes-day fills cell with metric color + "✓". Week totals/status only count duration entries; "need X" hints only for duration metrics. Week dict gains `metric_lines`: for each count metric with `target_period='week'` → `f"{sum}/{tv}"`, each boolean metric → `f"{yes_days}/{tv or 7}"` for that grid week.

- [ ] **Step 6: detail template.** Quick-log bar (active experiments, above grid):

```html
<div class="exp-quicklog">
  <strong>Today</strong>
  {% for m in activity_types if m.kind != 'duration' %}
  <form action="/experiments/{{ experiment.id }}/entry" method="post" class="exp-quicklog-item">
    <input type="hidden" name="date" value="{{ today }}">
    <input type="hidden" name="metric_id" value="{{ m.id }}">
    {% if m.kind == 'boolean' %}
      <button name="value" value="1" class="btn btn-sm" style="border-color: {{ m.color }};">✓ {{ m.name }}</button>
      <button name="value" value="0" class="btn btn-sm btn-secondary">✗</button>
    {% elif m.kind == 'count' %}
      <input type="hidden" name="value" value="1">
      <input type="text" name="notes" placeholder="note (optional)" class="exp-quicklog-note">
      <button class="btn btn-sm" style="border-color: {{ m.color }};">+1 {{ m.name }}</button>
    {% else %}
      <input type="number" name="value" min="0" max="10" placeholder="0-10" class="exp-quicklog-scale">
      <button class="btn btn-sm" style="border-color: {{ m.color }};">{{ m.name }}</button>
    {% endif %}
  </form>
  {% endfor %}
</div>
```

Stats bar: duration `{{ total }}m`, count `{{ total }}×`, boolean `{{ yes_days }} days`, scale `avg {{ avg }}`; target chips from `_metric_progress` results (`stats.metric_progress` list). Log Activity form: metric select shows all metrics; Minutes input relabeled Value with kind hint (Alpine map metric_id→kind for placeholder). Entries table prints kind-aware value (`45m` / `+1` / `✓`,`✗` / `7/10`). Day cells render `dots`.
List card: `{{ exp.total_minutes }}m` → router provides `logged_label` (minutes if any duration metric else `N entries`).

- [ ] **Step 7: run tests + suite; commit** `feat(experiments): kind-aware logging, quick-log bar, per-metric progress`

---

### Task 4: Ripple — Oura import, AI summary, export, dashboard, onboarding, seed

**Files:**
- Modify: `app/services/oura_api.py:318-345`, `app/services/experiment_summary.py`, `app/services/markdown_export.py:271-300`, `app/routers/dashboard.py:198-220`, `app/services/onboarding.py:246-284`, `scripts/seed_demo.py`
- Test: existing suites (grep `duration_minutes` under `tests/` and fix), extend `tests/test_onboarding_experiment.py` assertion with `kind == 'duration'`

- [ ] **Step 1:** `grep -rn "duration_minutes" app/ scripts/ tests/ --include="*.py"` — every hit on experiment tables (NOT `oura_workouts.duration_minutes`, which keeps its name) switches to `value`.
- [ ] **Step 2:** `_auto_populate_experiments`: activity-type query adds `AND kind = 'duration'`; INSERT uses `value`.
- [ ] **Step 3:** `experiment_summary.generate_week_summary`: entries query selects `ee.value, eat.kind, eat.name`; line format kind-aware (`45m` / `+3` / `yes/no` / `7/10`); `total_mins` sums duration only; prompt lists metrics with kind + target.
- [ ] **Step 4:** `markdown_export._section_experiments`: same kind-aware line rendering.
- [ ] **Step 5:** dashboard card: minutes total → join kind='duration'; `logged_label` fallback to entry count (mirror list page).
- [ ] **Step 6:** `create_suggested_experiment`: INSERT metric with explicit `kind='duration'`.
- [ ] **Step 7:** `seed_demo.py`: second experiment "Daily reset protocol", 2 weeks, metrics: `Meditation` (boolean, target 1/day, #22c55e), `Urge logged` (count, no target, #ef4444), `Gate executed` (count, target 8 total, #3b82f6); seed ~10 days of entries (mix of ✓ days, a few urges with 6/10→3/10 notes).
- [ ] **Step 8: run suite; commit** `feat(experiments): kind-aware Oura import, summaries, export, dashboard, seed`

---

### Task 5: API metrics + POST entries + MCP tool

**Files:**
- Modify: `app/routers/api.py` (docstring, GET /experiments/active, new POST)
- Modify: `mcp_server/virgil_mcp.py`
- Test: `tests/test_api.py`
- Check: `app/csrf.py` — confirm `/api/` POSTs bypass session CSRF (webhook already does; reuse the same exemption).

**Interfaces:**
- Produces: `POST /api/experiments/{id}/entries` body `{metric: str|int, value?: int=1, date?: str, notes?: str}` → 200 `{ok, entry_id, metric_id, kind, date, value}`; errors 404 (experiment/metric), 409 (not active), 422 (value bounds).

- [ ] **Step 1: failing tests:**

```python
def test_api_experiments_active_has_metrics(auth_client): ...
    # seed general experiment via form; GET /api/experiments/active
    # → each exp has "metrics": [{name, kind, color, target_value, target_period,
    #    logged_today, logged_week, logged_total}]

def test_api_post_entry_boolean_upsert(auth_client): ...
    # POST twice value 1 then 0 → 200 both; GET shows logged_today == 0; one row in DB

def test_api_post_entry_requires_key(auth_client): ...   # 401 without key
def test_api_post_entry_unknown_metric_404(auth_client): ...
def test_api_post_entry_inactive_409(auth_client): ...   # after /complete
def test_api_post_entry_value_bounds_422(auth_client): ...  # scale metric, value=11
```

- [ ] **Step 2: run — FAIL.**
- [ ] **Step 3: implement.** GET: per metric compute `logged_today/logged_week/logged_total` (boolean → distinct yes-days in window; scale → avg rounded 1; else sum). POST:

```python
from uuid import uuid4
from pydantic import BaseModel

from app.routers.experiments import METRIC_KINDS, _clamp_value  # reuse, do not duplicate
# If this import creates a cycle (experiments.py imports app.main for templates),
# move METRIC_KINDS/TARGET_PERIODS/_clamp_value into app/validation.py and import from there
# in BOTH routers instead.


class ApiEntryIn(BaseModel):
    metric: str | int
    value: int = 1
    date: str | None = None
    notes: str = ""


@router.post("/experiments/{experiment_id}/entries")
async def api_log_entry(experiment_id: int, payload: ApiEntryIn, db: ApiDb):
    # experiment lookup → 404; status != active → 409
    # metric: int → by id; str → LOWER(name) match within experiment → 404 if missing
    # date: default today; valid_date else 422
    # v = _clamp_value(kind, payload.value) → None ⇒ 422
    # boolean → DELETE same metric+date first
    # INSERT ... source='api', source_ref=str(uuid4())
    # return {"ok": True, "entry_id": cursor.lastrowid, "metric_id": mid, "kind": kind,
    #         "date": d, "value": v}
```

Module docstring: replace "All endpoints are GET — this API never mutates data." with "GET endpoints are read-only. One write exists: POST /api/experiments/{id}/entries (experiment logging via MCP)."

- [ ] **Step 4: MCP.** Add `_post` helper (mirror `_get`, `httpx.post(json=...)`); update `get_experiments` docstring (per-metric kinds + progress); add:

```python
@mcp.tool()
def log_experiment_entry(experiment_id: int, metric: str, value: int = 1, notes: str = "", date: str = "") -> dict:
    """Log one entry into an active experiment. metric = metric name (e.g. 'Gate executed') or id.
    value by kind: duration=minutes, count=events (default 1), boolean=1/0 (one per day, upserts),
    scale=0-10 rating. date YYYY-MM-DD, empty = today."""
    payload = {"metric": metric, "value": value, "notes": notes}
    if date:
        payload["date"] = date
    return _post(f"/api/experiments/{experiment_id}/entries", payload)
```

- [ ] **Step 5: run tests + suite; commit** `feat(api): per-metric experiment progress + entry write endpoint + MCP log tool`

---

### Task 6: Settings → App Configuration (dictionary CRUD)

**Files:**
- Modify: `app/routers/settings.py` (`SETTINGS_TABS`, context branch, 4 routes)
- Modify: `app/templates/settings.html` (tab label + tab block)
- Modify: `app/routers/training.py:138` (picker: `WHERE archived = 0`)
- Test: `tests/test_settings_library.py`

**Interfaces:**
- Produces: routes `POST /settings/library/add|update|delete|archive`; tab `configuration`.

- [ ] **Step 1: failing tests:**

```python
def test_add_user_exercise(auth_client): ...          # add → row builtin=0 in DB, visible on tab
def test_builtin_not_editable_or_deletable(auth_client): ...
    # update/delete a builtin id → 303, row unchanged / still present
def test_archive_builtin_hides_from_training_picker(auth_client): ...
    # archive builtin row → archived=1; GET /training page: name absent from library JSON;
    # unarchive → present again
def test_delete_user_row(auth_client): ...
```

- [ ] **Step 2: run — FAIL.**
- [ ] **Step 3: implement routes:**

```python
from app.routers.training import SECTION_ORDER


@router.post("/settings/library/add")
async def library_add(request, name=Form(...), category=Form(...), section=Form("Core"),
                      sets=Form(""), reps=Form(""), notes=Form("")):
    # section not in SECTION_ORDER → "Core"; truncate name/category 100, reps/notes 200
    # INSERT (builtin=0, display_order=1000+MAX(id)); UNIQUE(category,name) conflict → ignore
    # redirect /settings?tab=configuration


@router.post("/settings/library/update")   # WHERE id=? AND builtin=0
@router.post("/settings/library/delete")   # DELETE WHERE id=? AND builtin=0
@router.post("/settings/library/archive")  # UPDATE archived=? WHERE id=? (any row)
```

`SETTINGS_TABS = [..., "configuration"]`; settings_page branch loads `SELECT * FROM exercise_library ORDER BY category, display_order, name` grouped by category + `SECTION_ORDER` for the select.

- [ ] **Step 4: template.** Tab label `'configuration': 'App Config'`. Block: intro line ("Dictionary data used by pickers. Built-in rows can be archived, not edited."), add-form (name, category w/ `<datalist>` of existing, section select, sets, reps, notes), per-category tables: user rows → inline edit form + Delete; builtin rows → "built-in" badge + Archive/Restore toggle; archived rows dimmed.
- [ ] **Step 5: training picker filter** `WHERE archived = 0` (line 138 query).
- [ ] **Step 6: run tests + suite; commit** `feat(settings): App Configuration tab — exercise library CRUD with builtin protection`

---

### Task 7: Full verification, README, screenshots

**Files:**
- Modify: `README.md` (Experiments section ~line 231, migrations table ~317, tables list ~352, MCP tools, Settings, API doc)
- Modify: `docs/screenshots/experiments.png`
- Also: `CHANGELOG.md` entry

- [ ] **Step 1:** `uv run pytest -q` — full suite green.
- [ ] **Step 2: runtime smoke** — boot the app against a fresh temp DB, run `scripts/seed_demo.py`, curl through `/experiments`, `/experiments/new`, detail, edit, `/settings?tab=configuration`, `POST /api/experiments/{id}/entries`; verify a legacy-shaped copy of a real DB migrates (015) cleanly.
- [ ] **Step 3: README:** Experiments section rewrite (metric kinds table, quick-log, edit, per-metric targets, PMO-gate example), migration 015 row, `App Configuration` under Settings docs, API write endpoint + `log_experiment_entry` MCP tool.
- [ ] **Step 4: screenshots:** re-seed demo, capture `/experiments` (or detail) at the README's existing viewport; replace `docs/screenshots/experiments.png`.
- [ ] **Step 5: commit** `docs: general experiments — README + screenshot`; then pair-programmer review of the whole branch diff; apply confirmed findings; push + PR.
