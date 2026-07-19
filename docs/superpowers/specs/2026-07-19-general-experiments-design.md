# General Experiments — Design

**Date:** 2026-07-19
**Branch:** `feat/general-experiments`
**Status:** Approved (single-approval workflow)

## Problem

The Experiments module is hard-wired to one shape: weekly minutes of sport,
optionally auto-imported from Oura. Three concrete failures:

1. **Behavioral protocols are unrepresentable.** A "14-day PMO gate" experiment
   (log each impulse, rate craving 0-10 before/after, mark whether the 10-minute
   gate was executed, success = 8/10 gates) has no home: entries only store
   `duration_minutes`, targets only mean minutes/week.
2. **No editing after creation.** Title, description, dates, and activity types
   are frozen; only week targets are editable via a hidden double-click.
   Completed/abandoned experiments can be deleted or reopened but never edited.
3. **No fast daily check-in.** Logging means scrolling to a 4-field workout form.
   A yes/no-per-day experiment needs one tap.

Additionally (user requirement, scoped in): **dictionary tables** — DB-backed
lookup data such as `exercise_library` — have no CRUD UI. Migration 009 made the
DB the source of truth, but rows are only editable via SQL.

## Approach

Chosen: **metric kinds on the existing tables** (over a JSON check-in schema —
too heavy — and a separate Protocols module — duplication).

An experiment tracks 1..N **metrics** (renamed concept for
`experiment_activity_types`). Each metric has a `kind`:

| kind       | value meaning            | logging UX            | multiple/day |
|------------|--------------------------|-----------------------|--------------|
| `duration` | minutes                  | form (as today), Oura | yes          |
| `count`    | event increment (≥1)     | one-tap `+1` + note   | yes          |
| `boolean`  | 1 = yes, 0 = no          | one-tap ✓/✗ toggle    | no (upsert)  |
| `scale`    | 0–10 rating              | quick number input    | yes          |

Existing rows become `kind='duration'` — zero behavior change for current data.

### Targets

Two complementary target systems, each shown only where meaningful:

- **Weekly minutes window** (existing `experiment_weeks.target_min/max` +
  labels like DELOAD): applies to the sum of `duration` entries. Only shown when
  the experiment has ≥1 duration metric.
- **Per-metric target** (new): `target_value INTEGER` + `target_period`
  (`day` | `week` | `total`) on the metric row. Examples: boolean "every day"
  → value 1 / day; "Bramka ≥8 total" → value 8 / total; "3 sessions/week"
  → value 3 / week (count). 0 = no target.

### Example mapping — "Eksperyment 14 dni"

2-week experiment, metrics: *Impuls* (count, no target), *Bramka wykonana*
(count, target 8 total), *Głód przed* (scale), *Głód po* (scale). Function
(NAPIĘCIE/PUSTKA/…) and details go in the entry note. Simple habit experiments:
one boolean metric, target 1/day.

## Schema (migration 015_general_experiments)

```sql
ALTER TABLE experiment_activity_types ADD COLUMN kind TEXT NOT NULL DEFAULT 'duration';
  -- app-level CHECK: kind IN ('duration','count','boolean','scale')
ALTER TABLE experiment_activity_types ADD COLUMN target_value INTEGER NOT NULL DEFAULT 0;
ALTER TABLE experiment_activity_types ADD COLUMN target_period TEXT NOT NULL DEFAULT 'week';
ALTER TABLE experiment_entries ADD COLUMN value INTEGER NOT NULL DEFAULT 0;
UPDATE experiment_entries SET value = duration_minutes;
ALTER TABLE experiment_entries DROP COLUMN duration_minutes;
```

`app/db.py` CREATE TABLE statements updated to the final shape (fresh DBs).
SQLite in the deploy image supports DROP COLUMN (≥3.35); the partial unique
index `uq_entry_source` does not reference the dropped column.

Boolean one-per-day is enforced at the app level (delete-then-insert on the
metric+date), not by index — kind lives on a different table.

### Dictionary tables (App Configuration)

New `builtin` flag distinguishes developer-seeded rows from user rows:

```sql
ALTER TABLE exercise_library ADD COLUMN builtin INTEGER NOT NULL DEFAULT 0;
ALTER TABLE exercise_library ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;
UPDATE exercise_library SET builtin = 1;  -- everything present at migration time was seeded
```

Rules: users **add** their own entries (builtin=0), **edit/delete** only their
own, and may **archive/unarchive** builtin rows (hides from pickers,
non-destructive). Builtin rows are never deletable or editable.

## UX

### New/Edit experiment form (`/experiments/new`, `/experiments/{id}/edit` — new)

- Per-metric row: name, kind select, color, conditional fields (Alpine):
  Oura `source_match` only for duration; target value + period for
  count/boolean (boolean fixed to value 1 when period=day).
- Weekly minutes targets + week labels section only visible when ≥1 duration
  metric exists.
- Edit page works for **any status** (active/completed/abandoned): title,
  description, start date, num_weeks, status, metrics (add, rename, recolor,
  retarget; delete cascades the metric's entries — the confirm dialog states
  the entry count being deleted).
- `num_weeks` change re-syncs `experiment_weeks` rows (insert missing, delete
  beyond, preserving edited targets/labels of kept weeks).

### Detail page

- **Quick-log bar ("Today")** pinned above the grid for active experiments:
  boolean → ✓/✗ toggle; count → `+1` button with optional note field; scale →
  0-10 number + log button. Duration keeps the existing form (and Oura import).
- **Grid day cells:** duration → colored fill + `45m` (as today); non-duration
  metrics → small colored dot row inside the cell (✓-dot for boolean yes,
  dot+count for count, dot+value for scale). Cell background stays
  duration-driven; boolean-only experiments get a full ✓ cell fill.
- **Week progress column:** duration total vs weekly window (as today) when
  duration metrics exist; below it one line per targeted metric:
  `Bramka 5/8 (total)`, `Medytacja 6/7 dni`.
- **Stats bar:** per metric, kind-aware units (`m` / `×` / `dni` / `avg`).

### Experiments list

Card "Logged" stat: minutes when duration metrics exist, else entry count.

### Settings → App Configuration (new tab)

Table per dictionary (initially: Exercise Library): list grouped by category,
add-entry form, edit/delete for user rows, archive toggle for builtin rows.
Server routes: `POST /settings/dictionary/exercise/{add|update|delete|archive}`.
Designed so future dictionaries (blood markers, goal areas) reuse the pattern.

## API + MCP

- `GET /api/experiments/active` — response gains per-metric array:
  `{name, kind, color, target_value, target_period, logged_today, logged_week,
  logged_total}`. Existing `week_target`/`week_logged` minutes stay (duration).
- **`POST /api/experiments/{id}/entries`** (new; **breaks the "API never
  mutates" doctrine — approved**): body `{date?, metric (name or id), value?,
  notes?}`; validates active experiment, metric kind rules (boolean upsert,
  scale 0-10); same `X-API-Key` auth. Update module docstring accordingly.
- MCP `get_experiments` description updated; new MCP tool
  `log_experiment_entry(experiment, metric, value, notes, date)` → POST.

## Ripple updates

| Surface | Change |
|---|---|
| `oura_api._auto_populate_experiments` | match only `kind='duration'` metrics; write `value` |
| `experiment_summary` service | kind-aware prompt sections (counts, ✓-days, scale averages) |
| `markdown_export._section_experiments` | kind-aware units |
| `dashboard.py` active experiments card | kind-aware "logged" figure |
| `onboarding.create_suggested_experiment` | sets kind='duration' explicitly |
| `models/experiments.py` | update pydantic models to match (kept as documentation of shapes) |
| `scripts/seed_demo.py` | second demo experiment (boolean + count metrics) for screenshots |

## Testing

- Migration test: legacy rows → kind='duration', value backfilled, column dropped.
- Create/edit experiments with each kind; num_weeks resync; edit on
  completed status.
- Quick-log: boolean upsert (no dupes per day), count accumulation, scale bounds.
- Targets: per-metric progress math (day/week/total).
- API: new response shape; POST entry auth (401/403), validation, boolean upsert.
- Dictionary CRUD: add user row; edit/delete denied for builtin; archive builtin.
- Full pytest suite + runtime smoke test (app boot, all experiment pages render,
  demo seed).

## Delivery

Implement on `feat/general-experiments` → pytest + smoke → pair-programmer
review → README (Experiments section, migration table, App Configuration) +
regenerated `docs/screenshots/experiments.png` from seed_demo → PR.
