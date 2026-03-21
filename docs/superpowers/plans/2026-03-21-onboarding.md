# Onboarding Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a 6-step onboarding wizard that collects user profile, ideal day, goals, habits, and medical records — then uses LLM to generate personalized content (realistic day, goal levels, experiment).

**Architecture:** Multi-page wizard at `/onboarding` with progress bar. Each step saves to DB immediately. After confirmation, LLM enrichment pass generates personalized content. All steps optional. Auth middleware redirects to onboarding until completed.

**Tech Stack:** FastAPI, Jinja2, HTMX, Alpine.js, LiteLLM, aiosqlite

**Spec:** `docs/superpowers/specs/2026-03-21-onboarding-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `app/migrations/008_onboarding.py` | Create | New `user_profiles` table + `onboarding_completed` setting |
| `app/models/user_profile.py` | Create | CRUD queries for `user_profiles` table |
| `app/auth.py` | Modify | Add onboarding redirect after session check |
| `app/routers/onboarding.py` | Create | GET/POST handlers for 6-step wizard |
| `app/templates/onboarding.html` | Create | Wizard template with progress bar + step blocks |
| `app/services/onboarding.py` | Create | LLM enrichment — profile summary, realistic day, goal expansion, habit analysis |
| `app/main.py` | Modify | Register onboarding router |

---

### Task 1: Migration 008 — user_profiles table

**Files:**
- Create: `app/migrations/008_onboarding.py`

- [ ] **Step 1: Create migration file**

Create `/Users/krzysztofbury/PRIV/virgil/app/migrations/008_onboarding.py`:

```python
"""Create user_profiles table and onboarding_completed setting."""


async def up(db):
    await db.execute("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sex TEXT,
            age INTEGER,
            height_cm REAL,
            weight_kg REAL,
            family TEXT,
            habits_good TEXT,
            habits_bad TEXT,
            ideal_day TEXT,
            realistic_day TEXT,
            training_routine TEXT,
            equipment TEXT,
            habits_build TEXT,
            habits_break TEXT,
            llm_summary TEXT,
            onboarding_step INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Seed onboarding_completed=0 so auth middleware knows to redirect.
    await db.execute(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('onboarding_completed', '0')"
    )
```

- [ ] **Step 2: Lint**

Run: `cd /Users/krzysztofbury/PRIV/virgil && uv run ruff check app/migrations/008_onboarding.py`

- [ ] **Step 3: Commit**

```bash
git add app/migrations/008_onboarding.py
git commit -m "migration: add user_profiles table and onboarding_completed setting"
```

---

### Task 2: User profile model

**Files:**
- Create: `app/models/user_profile.py`

- [ ] **Step 1: Create model file**

Create `/Users/krzysztofbury/PRIV/virgil/app/models/user_profile.py`:

```python
"""DB queries for user_profiles table."""


async def get_profile(db) -> dict | None:
    """Return the user profile row as a dict, or None if not yet created."""
    rows = await db.execute_fetchall("SELECT * FROM user_profiles WHERE id = 1")
    return dict(rows[0]) if rows else None


async def ensure_profile(db) -> dict:
    """Return existing profile or create an empty one."""
    profile = await get_profile(db)
    if profile:
        return profile
    await db.execute("INSERT INTO user_profiles (id) VALUES (1)")
    await db.commit()
    return await get_profile(db)


async def update_step1(db, sex: str, age: int | None, height_cm: float | None,
                        weight_kg: float | None, family: str, habits_good: str, habits_bad: str) -> None:
    """Save Step 1 (About You) data."""
    await ensure_profile(db)
    await db.execute(
        """UPDATE user_profiles SET
            sex = ?, age = ?, height_cm = ?, weight_kg = ?,
            family = ?, habits_good = ?, habits_bad = ?,
            onboarding_step = MAX(onboarding_step, 1), updated_at = datetime('now')
        WHERE id = 1""",
        (sex or None, age, height_cm, weight_kg, family, habits_good, habits_bad),
    )
    await db.commit()


async def update_step2(db, ideal_day: str) -> None:
    """Save Step 2 (Ideal Day) data."""
    await ensure_profile(db)
    await db.execute(
        """UPDATE user_profiles SET
            ideal_day = ?, onboarding_step = MAX(onboarding_step, 2), updated_at = datetime('now')
        WHERE id = 1""",
        (ideal_day,),
    )
    await db.commit()


async def update_step3(db) -> None:
    """Mark Step 3 complete (goals saved directly to goals table)."""
    await ensure_profile(db)
    await db.execute(
        "UPDATE user_profiles SET onboarding_step = MAX(onboarding_step, 3), updated_at = datetime('now') WHERE id = 1"
    )
    await db.commit()


async def update_step4(db, training_routine: str, equipment: str,
                        habits_build: str, habits_break: str) -> None:
    """Save Step 4 (Habits & Training) data."""
    await ensure_profile(db)
    await db.execute(
        """UPDATE user_profiles SET
            training_routine = ?, equipment = ?, habits_build = ?, habits_break = ?,
            onboarding_step = MAX(onboarding_step, 4), updated_at = datetime('now')
        WHERE id = 1""",
        (training_routine, equipment, habits_build, habits_break),
    )
    await db.commit()


async def update_step5(db) -> None:
    """Mark Step 5 complete (medical records saved to blood_markers/blood_results)."""
    await ensure_profile(db)
    await db.execute(
        "UPDATE user_profiles SET onboarding_step = MAX(onboarding_step, 5), updated_at = datetime('now') WHERE id = 1"
    )
    await db.commit()


async def save_enrichment(db, llm_summary: str | None, realistic_day: str | None) -> None:
    """Save LLM-generated enrichment data."""
    await db.execute(
        """UPDATE user_profiles SET
            llm_summary = COALESCE(?, llm_summary),
            realistic_day = COALESCE(?, realistic_day),
            onboarding_step = 6, updated_at = datetime('now')
        WHERE id = 1""",
        (llm_summary, realistic_day),
    )
    await db.commit()
```

- [ ] **Step 2: Lint**

Run: `cd /Users/krzysztofbury/PRIV/virgil && uv run ruff check app/models/user_profile.py && uv run ruff format app/models/user_profile.py`

- [ ] **Step 3: Commit**

```bash
git add app/models/user_profile.py
git commit -m "feat: add user_profile model with per-step save functions"
```

---

### Task 3: Auth middleware — onboarding redirect

**Files:**
- Modify: `app/auth.py:16-17` (PUBLIC_PATHS), `app/auth.py:75-107` (AuthMiddleware.__call__)

- [ ] **Step 1: Read current auth.py**

Read `/Users/krzysztofbury/PRIV/virgil/app/auth.py` to see current state.

- [ ] **Step 2: Add /onboarding to PUBLIC_PATHS**

In `app/auth.py`, update `PUBLIC_PATHS` to include `/onboarding`:

```python
PUBLIC_PATHS = frozenset({"/login", "/setup", "/mfa/verify", "/offline", "/service-worker.js", "/api/oura/webhook"})
PUBLIC_PREFIXES = ("/static/", "/onboarding")
```

- [ ] **Step 3: Add onboarding redirect logic**

In the `AuthMiddleware.__call__` method, after the session validation block (after line 107 `scope["state"] = ...`), add the onboarding redirect check BEFORE `await self.app(...)`:

```python
        # Store username in state for downstream use
        scope["state"] = {**scope.get("state", {}), "username": username}

        # Check if onboarding is completed — redirect to wizard if not
        global _onboarding_done
        if _onboarding_done is not True:
            from app.db import get_db, get_setting

            db = await get_db()
            done = await get_setting(db, "onboarding_completed", "1")
            if done == "1":
                _onboarding_done = True
            else:
                # Allow API, static, logout, onboarding paths through
                if not path.startswith(("/onboarding", "/static/", "/api/", "/logout", "/service-worker")):
                    response = RedirectResponse("/onboarding", status_code=303)
                    await response(scope, receive, send)
                    return

        await self.app(scope, receive, send)
```

Also add `_onboarding_done: bool | None = None` as a module-level global near `_user_exists`:

```python
_user_exists: bool | None = None
_onboarding_done: bool | None = None
```

Add a function to reset the cache (called after onboarding completes):

```python
def mark_onboarding_complete():
    """Called after onboarding finishes to update the cached state."""
    global _onboarding_done
    _onboarding_done = True
```

- [ ] **Step 4: Lint**

Run: `cd /Users/krzysztofbury/PRIV/virgil && uv run ruff check app/auth.py && uv run ruff format app/auth.py`

- [ ] **Step 5: Commit**

```bash
git add app/auth.py
git commit -m "feat: add onboarding redirect in auth middleware"
```

---

### Task 4: Onboarding router — Steps 1-4 (forms)

**Files:**
- Create: `app/routers/onboarding.py`
- Modify: `app/main.py:132-158` (register router)

- [ ] **Step 1: Create onboarding router**

Create `/Users/krzysztofbury/PRIV/virgil/app/routers/onboarding.py`:

```python
import logging

from fastapi import APIRouter, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import get_db, get_setting, set_setting
from app.main import templates
from app.models.user_profile import (
    ensure_profile,
    get_profile,
    update_step1,
    update_step2,
    update_step4,
    update_step5,
)
from app.validation import truncate

logger = logging.getLogger(__name__)

router = APIRouter()

GOAL_CATEGORY_MAP = {
    "mind": ["Duchowość", "Rozwój"],
    "body": ["Zdrowie"],
    "finance": ["Planowanie Życia", "Praca"],
    "relations": ["Rodzina", "Życie Towarzyskie", "Relaks"],
}


@router.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request, step: int = 0):
    db = await get_db()
    profile = await ensure_profile(db)

    # If no step specified, resume from where user left off.
    if step == 0:
        step = min(profile["onboarding_step"] + 1, 6)

    # Load goals for step 3 display and step 6 summary.
    goals = {}
    if step in (3, 6):
        rows = await db.execute_fetchall(
            """SELECT ga.name as area_name, g.content
               FROM goals g JOIN goal_areas ga ON g.area_id = ga.id
               WHERE g.horizon = '10yr' ORDER BY ga.display_order"""
        )
        for row in rows:
            goals[row["area_name"]] = row["content"]

    # Check if internal LLM is available (for step 5 info).
    from app.config import INTERNAL_LLM_KEY
    has_internal_llm = bool(INTERNAL_LLM_KEY)

    return templates.TemplateResponse("onboarding.html", {
        "request": request,
        "step": step,
        "profile": profile,
        "goals": goals,
        "has_internal_llm": has_internal_llm,
    })


@router.post("/onboarding/step/1")
async def save_step1(
    request: Request,
    sex: str = Form(""),
    age: str = Form(""),
    height_cm: str = Form(""),
    weight_kg: str = Form(""),
    family: str = Form(""),
    habits_good: str = Form(""),
    habits_bad: str = Form(""),
):
    db = await get_db()
    await update_step1(
        db,
        sex=truncate(sex, 20),
        age=int(age) if age.strip().isdigit() else None,
        height_cm=float(height_cm) if height_cm.strip() else None,
        weight_kg=float(weight_kg) if weight_kg.strip() else None,
        family=truncate(family, 500),
        habits_good=truncate(habits_good, 2000),
        habits_bad=truncate(habits_bad, 2000),
    )
    return RedirectResponse("/onboarding?step=2", status_code=303)


@router.post("/onboarding/step/2")
async def save_step2(request: Request, ideal_day: str = Form("")):
    db = await get_db()
    await update_step2(db, ideal_day=truncate(ideal_day, 5000))
    return RedirectResponse("/onboarding?step=3", status_code=303)


@router.post("/onboarding/step/3")
async def save_step3(
    request: Request,
    goal_mind: str = Form(""),
    goal_body: str = Form(""),
    goal_finance: str = Form(""),
    goal_relations: str = Form(""),
):
    db = await get_db()

    # Save end goals (Level 3 / 10yr) to goals table.
    category_goals = {
        "mind": truncate(goal_mind, 1000),
        "body": truncate(goal_body, 1000),
        "finance": truncate(goal_finance, 1000),
        "relations": truncate(goal_relations, 1000),
    }

    for category, content in category_goals.items():
        if not content.strip():
            continue
        # Map to the first goal area in the category.
        area_name = GOAL_CATEGORY_MAP[category][0]
        area_row = await db.execute_fetchall(
            "SELECT id FROM goal_areas WHERE name = ?", (area_name,)
        )
        if area_row:
            area_id = area_row[0]["id"]
            await db.execute(
                """INSERT INTO goals (area_id, horizon, content, display_order)
                   VALUES (?, '10yr', ?, 1)
                   ON CONFLICT DO NOTHING""",
                (area_id, content),
            )

    await db.commit()

    from app.models.user_profile import update_step3
    await update_step3(db)

    return RedirectResponse("/onboarding?step=4", status_code=303)


@router.post("/onboarding/step/4")
async def save_step4(
    request: Request,
    training_routine: str = Form(""),
    equipment: list[str] = Form([]),
    habits_build: str = Form(""),
    habits_break: str = Form(""),
):
    db = await get_db()
    await update_step4(
        db,
        training_routine=truncate(training_routine, 3000),
        equipment=",".join(equipment),
        habits_build=truncate(habits_build, 2000),
        habits_break=truncate(habits_break, 2000),
    )
    return RedirectResponse("/onboarding?step=5", status_code=303)


@router.post("/onboarding/step/5")
async def save_step5(
    request: Request,
    medical_text: str = Form(""),
    medical_file: UploadFile | None = File(None),
):
    db = await get_db()
    from app.config import INTERNAL_LLM_KEY, INTERNAL_LLM_MODEL

    raw_text = truncate(medical_text, 10000)

    # Process PDF via multimodal LLM if uploaded.
    if medical_file and medical_file.size and medical_file.size > 0 and INTERNAL_LLM_KEY:
        import base64
        import litellm

        pdf_bytes = await medical_file.read()
        pdf_b64 = base64.b64encode(pdf_bytes).decode()

        try:
            response = await litellm.acompletion(
                model=INTERNAL_LLM_MODEL,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": (
                            "Extract all blood test markers from this PDF. "
                            "Return ONLY a markdown list, one marker per line, format: "
                            "'### Marker Name\\n* YYYY-MM-DD: value unit (flag)'. "
                            "Include all dates found. Use (H) for high, (L) for low flags."
                        )},
                        {"type": "image_url", "image_url": {"url": f"data:application/pdf;base64,{pdf_b64}"}},
                    ],
                }],
                api_key=INTERNAL_LLM_KEY,
                max_tokens=4096,
                timeout=120.0,
            )
            raw_text = response.choices[0].message.content
        except Exception:
            logger.exception("Failed to process medical PDF")

    # Parse extracted text into blood_markers + blood_results.
    if raw_text.strip() and INTERNAL_LLM_KEY:
        try:
            await _parse_medical_text(db, raw_text)
        except Exception:
            logger.exception("Failed to parse medical records")

    await update_step5(db)
    return RedirectResponse("/onboarding?step=6", status_code=303)


async def _parse_medical_text(db, text: str) -> None:
    """Use LLM to extract structured markers from free text, then save to DB."""
    import json
    import litellm
    from app.config import INTERNAL_LLM_KEY, INTERNAL_LLM_MODEL

    response = await litellm.acompletion(
        model=INTERNAL_LLM_MODEL,
        messages=[
            {"role": "system", "content": (
                "Extract blood test markers from the text. Return JSON array of objects: "
                '{"marker": "name", "unit": "unit", "ref_low": number_or_null, "ref_high": number_or_null, '
                '"results": [{"date": "YYYY-MM-DD", "value": number, "flag": "H"/"L"/null}]}. '
                "Return ONLY valid JSON, no markdown fences."
            )},
            {"role": "user", "content": text},
        ],
        api_key=INTERNAL_LLM_KEY,
        max_tokens=4096,
        timeout=90.0,
    )

    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines)

    markers = json.loads(raw)
    if not isinstance(markers, list):
        return

    for m in markers:
        name = m.get("marker", "")
        if not name:
            continue
        unit = m.get("unit", "")
        ref_low = m.get("ref_low")
        ref_high = m.get("ref_high")

        # Upsert marker.
        await db.execute(
            """INSERT INTO blood_markers (name, unit, ref_low, ref_high, category)
               VALUES (?, ?, ?, ?, 'Imported')
               ON CONFLICT(name) DO UPDATE SET
                   unit = COALESCE(excluded.unit, unit),
                   ref_low = COALESCE(excluded.ref_low, ref_low),
                   ref_high = COALESCE(excluded.ref_high, ref_high)""",
            (name, unit, ref_low, ref_high),
        )

        marker_row = await db.execute_fetchall(
            "SELECT id FROM blood_markers WHERE name = ?", (name,)
        )
        if not marker_row:
            continue
        marker_id = marker_row[0]["id"]

        for r in m.get("results", []):
            date_val = r.get("date", "")
            value = r.get("value")
            flag = r.get("flag", "")
            if date_val and value is not None:
                await db.execute(
                    """INSERT INTO blood_results (marker_id, date, value, flag)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT DO NOTHING""",
                    (marker_id, date_val, value, flag or ""),
                )

    await db.commit()


@router.post("/onboarding/confirm")
async def confirm_onboarding(request: Request):
    db = await get_db()
    profile = await get_profile(db)

    # Run LLM enrichment (each part independent, only if data provided).
    from app.services.onboarding import run_enrichment
    await run_enrichment(db, profile)

    # Mark onboarding complete.
    await set_setting(db, "onboarding_completed", "1")

    from app.auth import mark_onboarding_complete
    mark_onboarding_complete()

    return RedirectResponse("/", status_code=303)


@router.post("/onboarding/skip")
async def skip_onboarding(request: Request):
    db = await get_db()
    await set_setting(db, "onboarding_completed", "1")

    from app.auth import mark_onboarding_complete
    mark_onboarding_complete()

    return RedirectResponse("/", status_code=303)
```

- [ ] **Step 2: Register router in main.py**

In `/Users/krzysztofbury/PRIV/virgil/app/main.py`, add `onboarding` to the router imports (line 132) and include it (after line 158):

Add to the import block:
```python
from app.routers import (  # noqa: E402
    auth,
    bloodwork,
    daily,
    dashboard,
    experiments,
    feniks,
    goals,
    life_scores,
    onboarding,
    oura,
    oura_webhook,
    settings,
    training,
)
```

Add after `app.include_router(settings.router)`:
```python
app.include_router(onboarding.router)
```

- [ ] **Step 3: Lint**

Run: `cd /Users/krzysztofbury/PRIV/virgil && uv run ruff check app/routers/onboarding.py app/main.py && uv run ruff format app/routers/onboarding.py app/main.py`

- [ ] **Step 4: Commit**

```bash
git add app/routers/onboarding.py app/main.py
git commit -m "feat: add onboarding router with 6-step wizard handlers"
```

---

### Task 5: Onboarding template

**Files:**
- Create: `app/templates/onboarding.html`

- [ ] **Step 1: Create the template**

Create `/Users/krzysztofbury/PRIV/virgil/app/templates/onboarding.html`. This is a large file — the full Jinja2 template with progress bar and all 6 step blocks.

```html
{% extends "base.html" %}
{% block title %}Virgil - Onboarding{% endblock %}

{% block content %}
<!-- Progress Bar -->
<div style="margin-bottom:2rem;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.75rem;">
        <span style="font-size:0.8rem;color:var(--text-secondary);">Step {{ step }} of 6</span>
        <form method="POST" action="/onboarding/skip" style="margin:0;">
            <button type="submit" class="btn btn-sm btn-outline" style="font-size:0.7rem;">Skip onboarding</button>
        </form>
    </div>
    <div style="display:flex;gap:4px;">
        {% for i in range(1, 7) %}
        <div style="flex:1;height:4px;border-radius:2px;background:{% if i < step %}var(--accent){% elif i == step %}var(--accent){% else %}var(--bg-surface){% endif %};"></div>
        {% endfor %}
    </div>
</div>

{% if step == 1 %}
<!-- ═══ STEP 1: About You ═══ -->
<div class="card">
    <h2 style="margin-bottom:0.25rem;">About You</h2>
    <p class="text-muted" style="margin-bottom:1.5rem;font-size:0.85rem;">
        This helps Virgil recommend realistic training loads and goals. A 25-year-old single person and a 40-year-old parent with 2 kids need very different daily plans.
    </p>

    <form method="POST" action="/onboarding/step/1">
        <div class="grid" style="margin-bottom:1rem;">
            <div>
                <label>Sex
                    <select name="sex">
                        <option value="">— prefer not to say —</option>
                        <option value="male" {% if profile.sex == 'male' %}selected{% endif %}>Male</option>
                        <option value="female" {% if profile.sex == 'female' %}selected{% endif %}>Female</option>
                    </select>
                </label>
            </div>
            <div>
                <label>Age
                    <input type="number" name="age" min="10" max="120" value="{{ profile.age or '' }}" placeholder="e.g. 35">
                </label>
            </div>
        </div>
        <div class="grid" style="margin-bottom:1rem;">
            <div>
                <label>Height (cm)
                    <input type="number" name="height_cm" step="0.1" value="{{ profile.height_cm or '' }}" placeholder="e.g. 180">
                </label>
            </div>
            <div>
                <label>Weight (kg)
                    <input type="number" name="weight_kg" step="0.1" value="{{ profile.weight_kg or '' }}" placeholder="e.g. 85">
                </label>
            </div>
        </div>

        <div style="margin-bottom:1rem;">
            <label style="display:flex;align-items:center;gap:0.5rem;">
                Family
                <span class="text-muted" title="e.g. married, 2 kids (ages 6, 9)" style="cursor:help;font-size:0.75rem;">&#9432;</span>
            </label>
            <input type="text" name="family" value="{{ profile.family or '' }}" placeholder="e.g. married, 2 kids (ages 6, 9)">
        </div>

        <div style="margin-bottom:1rem;">
            <label style="display:flex;align-items:center;gap:0.5rem;">
                Good habits
                <span class="text-muted" title="e.g. morning walks, reading before bed, 2L water daily" style="cursor:help;font-size:0.75rem;">&#9432;</span>
            </label>
            <textarea name="habits_good" rows="3" placeholder="e.g. morning walks, reading before bed, 2L water daily">{{ profile.habits_good or '' }}</textarea>
        </div>

        <div style="margin-bottom:1rem;">
            <label style="display:flex;align-items:center;gap:0.5rem;">
                Habits you struggle with
                <span class="text-muted" title="e.g. doom scrolling, late-night snacking, skipping workouts" style="cursor:help;font-size:0.75rem;">&#9432;</span>
            </label>
            <textarea name="habits_bad" rows="3" placeholder="e.g. doom scrolling, late-night snacking, skipping workouts">{{ profile.habits_bad or '' }}</textarea>
        </div>

        <div style="display:flex;justify-content:space-between;margin-top:1.5rem;">
            <span></span>
            <button type="submit" class="btn btn-primary">Next</button>
        </div>
        <div style="text-align:center;margin-top:0.75rem;">
            <a href="/onboarding?step=2" class="text-muted" style="font-size:0.75rem;">Skip this step</a>
        </div>
    </form>
</div>

{% elif step == 2 %}
<!-- ═══ STEP 2: Your Ideal Day ═══ -->
<div class="card">
    <h2 style="margin-bottom:0.25rem;">Your Ideal Day</h2>
    <p class="text-muted" style="margin-bottom:1rem;font-size:0.85rem;">
        Virgil will create a realistic version of your ideal day, adjusted for your real obligations. Without this, daily task suggestions will be generic.
    </p>

    <details style="margin-bottom:1rem;">
        <summary style="font-size:0.8rem;"><span style="font-size:0.75rem;">&#9432;</span> See example</summary>
        <div class="card" style="margin-top:0.5rem;font-size:0.8rem;line-height:1.6;">
            <strong>~ 06:00 START</strong> — Water + light exposure + cold shower + meditation<br>
            <strong>~ 07:30 PREP</strong> — Protein breakfast + plan the day + zero social media<br>
            <strong>~ 09:00 DEEP WORK</strong> — 4 blocks of 50 min, hardest tasks<br>
            <strong>~ 13:00 BREAK</strong> — Lunch + relax + social media reward<br>
            <strong>~ 14:00 SHALLOW WORK</strong> — Admin, calls, lighter tasks<br>
            <strong>~ 17:00 FAMILY & BODY</strong> — Training + dinner + family (100% presence)<br>
            <strong>~ 21:00 WIND DOWN</strong> — Journal + reading + no electronics
        </div>
    </details>

    <form method="POST" action="/onboarding/step/2">
        <textarea name="ideal_day" rows="12" placeholder="Describe your perfect day from morning to night...&#10;&#10;Use time blocks like:&#10;06:00 - Morning routine...&#10;09:00 - Deep work...&#10;17:00 - Family time...">{{ profile.ideal_day or '' }}</textarea>

        <div style="display:flex;justify-content:space-between;margin-top:1.5rem;">
            <a href="/onboarding?step=1" class="btn btn-outline">Back</a>
            <button type="submit" class="btn btn-primary">Next</button>
        </div>
        <div style="text-align:center;margin-top:0.75rem;">
            <a href="/onboarding?step=3" class="text-muted" style="font-size:0.75rem;">Skip this step</a>
        </div>
    </form>
</div>

{% elif step == 3 %}
<!-- ═══ STEP 3: Goals ═══ -->
<div class="card">
    <h2 style="margin-bottom:0.25rem;">Your End Goals</h2>
    <p class="text-muted" style="margin-bottom:1rem;font-size:0.85rem;">
        Virgil breaks your end goals into 3 milestone levels and tracks your progress. Without goals, the dashboard life scores section will be empty.
    </p>

    <form method="POST" action="/onboarding/step/3">
        {% for cat, label, icon, example in [
            ('mind', 'Mind', '🧠', 'e.g. Stoic calm as my nature, daily meditation practice'),
            ('body', 'Body', '💪', 'e.g. Athletic physique, biologically 10 years younger'),
            ('finance', 'Finance', '💰', 'e.g. Full financial independence, work by choice'),
            ('relations', 'Relations', '❤️', 'e.g. Strong, loving family, deep lifelong friendships')
        ] %}
        <div style="margin-bottom:1.25rem;">
            <label style="display:flex;align-items:center;gap:0.5rem;font-size:1rem;font-weight:600;">
                {{ icon }} {{ label }}
                <span class="text-muted" title="{{ example }}" style="cursor:help;font-size:0.75rem;font-weight:400;">&#9432;</span>
            </label>
            <input type="text" name="goal_{{ cat }}" placeholder="{{ example }}"
                   value="{{ goals.get(cat, '') }}">
            <small class="text-muted">Your dream — Level 3 (10-year vision)</small>
        </div>
        {% endfor %}

        <div style="display:flex;justify-content:space-between;margin-top:1.5rem;">
            <a href="/onboarding?step=2" class="btn btn-outline">Back</a>
            <button type="submit" class="btn btn-primary">Next</button>
        </div>
        <div style="text-align:center;margin-top:0.75rem;">
            <a href="/onboarding?step=4" class="text-muted" style="font-size:0.75rem;">Skip this step</a>
        </div>
    </form>
</div>

{% elif step == 4 %}
<!-- ═══ STEP 4: Habits & Training ═══ -->
<div class="card">
    <h2 style="margin-bottom:0.25rem;">Habits & Training</h2>
    <p class="text-muted" style="margin-bottom:1rem;font-size:0.85rem;">
        Virgil will set up your training protocol and suggest one experiment to replace a bad habit. Without this, you'll need to configure training manually.
    </p>

    <form method="POST" action="/onboarding/step/4">
        <div style="margin-bottom:1rem;">
            <label>Current training routine</label>
            <textarea name="training_routine" rows="4" placeholder="e.g. Home gym 3x/week (Mon/Wed/Fri). Kettlebells, resistance bands, pull-up bar. Focus on compound movements.">{{ profile.training_routine or '' }}</textarea>
        </div>

        <div style="margin-bottom:1rem;">
            <label>Equipment available</label>
            <div style="display:flex;flex-wrap:wrap;gap:0.5rem;margin-top:0.25rem;">
                {% set equip_list = (profile.equipment or '').split(',') %}
                {% for val, label in [
                    ('home_gym', 'Home gym'),
                    ('gym_membership', 'Gym membership'),
                    ('resistance_bands', 'Resistance bands'),
                    ('kettlebells', 'Kettlebells'),
                    ('pullup_bar', 'Pull-up bar'),
                    ('bodyweight', 'Bodyweight only'),
                ] %}
                <label style="display:flex;align-items:center;gap:0.35rem;font-size:0.85rem;cursor:pointer;padding:0.4rem 0.75rem;border:1px solid var(--border);border-radius:var(--radius-full);">
                    <input type="checkbox" name="equipment" value="{{ val }}" {% if val in equip_list %}checked{% endif %}>
                    {{ label }}
                </label>
                {% endfor %}
            </div>
        </div>

        <div style="margin-bottom:1rem;">
            <label>Habits you want to build</label>
            <textarea name="habits_build" rows="3" placeholder="e.g. Morning cold shower, daily reading, consistent sleep schedule">{{ profile.habits_build or '' }}</textarea>
        </div>

        <div style="margin-bottom:1rem;">
            <label>Habits you want to break</label>
            <textarea name="habits_break" rows="3" placeholder="e.g. Doom scrolling, late-night snacking, procrastination">{{ profile.habits_break or '' }}</textarea>
        </div>

        <div style="display:flex;justify-content:space-between;margin-top:1.5rem;">
            <a href="/onboarding?step=3" class="btn btn-outline">Back</a>
            <button type="submit" class="btn btn-primary">Next</button>
        </div>
        <div style="text-align:center;margin-top:0.75rem;">
            <a href="/onboarding?step=5" class="text-muted" style="font-size:0.75rem;">Skip this step</a>
        </div>
    </form>
</div>

{% elif step == 5 %}
<!-- ═══ STEP 5: Medical Records (Optional) ═══ -->
<div class="card">
    <h2 style="margin-bottom:0.25rem;">Medical Records</h2>
    <p class="text-muted" style="margin-bottom:1rem;font-size:0.85rem;">
        Virgil will track your blood markers over time and flag trends. This is completely optional — you can add results later in the Bloodwork section.
    </p>

    <div style="text-align:center;margin-bottom:1.5rem;">
        <a href="/onboarding?step=6" class="btn btn-outline">Skip this step</a>
    </div>

    {% if not has_internal_llm %}
    <div style="background:rgba(229,169,64,0.1);border:1px solid rgba(229,169,64,0.3);border-radius:var(--radius-sm);padding:1rem;margin-bottom:1rem;">
        <p style="color:var(--yellow);font-size:0.85rem;margin:0;">
            Medical record import requires an internal LLM key (<code>VIRGIL_INTERNAL_LLM_KEY</code>).
            You can add blood work results manually later in the Bloodwork section.
        </p>
    </div>
    {% endif %}

    <form method="POST" action="/onboarding/step/5" enctype="multipart/form-data">
        <div style="margin-bottom:1rem;" x-data="{ mode: 'file' }">
            <div style="display:flex;gap:0.5rem;margin-bottom:1rem;">
                <button type="button" class="btn btn-sm" :class="mode === 'file' ? 'btn-primary' : 'btn-outline'" @click="mode = 'file'">Upload PDF</button>
                <button type="button" class="btn btn-sm" :class="mode === 'text' ? 'btn-primary' : 'btn-outline'" @click="mode = 'text'">Paste text</button>
            </div>

            <div x-show="mode === 'file'">
                <label>Blood test PDF
                    <input type="file" name="medical_file" accept=".pdf" style="padding:0.5rem;">
                </label>
            </div>

            <div x-show="mode === 'text'" x-cloak>
                <label>Paste lab results</label>
                <textarea name="medical_text" rows="8" placeholder="Paste your lab results here...&#10;&#10;e.g.&#10;WBC: 5.63 tys/µl&#10;RBC: 5.21 mln/µl&#10;HGB: 15.8 g/dl&#10;TSH: 1.898 µIU/ml"></textarea>
            </div>
        </div>

        <div style="display:flex;justify-content:space-between;margin-top:1.5rem;">
            <a href="/onboarding?step=4" class="btn btn-outline">Back</a>
            <button type="submit" class="btn btn-primary" {% if not has_internal_llm %}disabled{% endif %}>Process & Next</button>
        </div>
    </form>
</div>

{% elif step == 6 %}
<!-- ═══ STEP 6: Summary ═══ -->
<div class="card" style="margin-bottom:1rem;">
    <h2 style="margin-bottom:0.25rem;">Summary</h2>
    <p class="text-muted" style="margin-bottom:1.5rem;font-size:0.85rem;">
        Review what you've shared. Edit any section, then confirm to let Virgil personalize your experience.
    </p>

    {% if profile.sex or profile.age or profile.family %}
    <div style="margin-bottom:1.25rem;padding-bottom:1.25rem;border-bottom:1px solid var(--border);">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.5rem;">
            <h3 style="margin:0;">About You</h3>
            <a href="/onboarding?step=1" class="btn btn-sm btn-outline">Edit</a>
        </div>
        <div style="font-size:0.85rem;color:var(--text-secondary);">
            {% if profile.sex %}<span>{{ profile.sex | capitalize }}</span>{% endif %}
            {% if profile.age %}<span> &middot; {{ profile.age }} years</span>{% endif %}
            {% if profile.height_cm %}<span> &middot; {{ profile.height_cm }}cm</span>{% endif %}
            {% if profile.weight_kg %}<span> &middot; {{ profile.weight_kg }}kg</span>{% endif %}
            {% if profile.family %}<div style="margin-top:0.25rem;">Family: {{ profile.family }}</div>{% endif %}
        </div>
    </div>
    {% endif %}

    {% if profile.ideal_day %}
    <div style="margin-bottom:1.25rem;padding-bottom:1.25rem;border-bottom:1px solid var(--border);">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.5rem;">
            <h3 style="margin:0;">Your Ideal Day</h3>
            <a href="/onboarding?step=2" class="btn btn-sm btn-outline">Edit</a>
        </div>
        <div style="font-size:0.85rem;color:var(--text-secondary);white-space:pre-line;">{{ profile.ideal_day }}</div>
    </div>
    {% endif %}

    {% if goals %}
    <div style="margin-bottom:1.25rem;padding-bottom:1.25rem;border-bottom:1px solid var(--border);">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.5rem;">
            <h3 style="margin:0;">End Goals</h3>
            <a href="/onboarding?step=3" class="btn btn-sm btn-outline">Edit</a>
        </div>
        {% for area, content in goals.items() %}
        <div style="font-size:0.85rem;margin-bottom:0.35rem;">
            <strong>{{ area }}:</strong> <span class="text-muted">{{ content }}</span>
        </div>
        {% endfor %}
    </div>
    {% endif %}

    {% if profile.training_routine or profile.equipment %}
    <div style="margin-bottom:1.25rem;padding-bottom:1.25rem;border-bottom:1px solid var(--border);">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.5rem;">
            <h3 style="margin:0;">Habits & Training</h3>
            <a href="/onboarding?step=4" class="btn btn-sm btn-outline">Edit</a>
        </div>
        {% if profile.training_routine %}
        <div style="font-size:0.85rem;color:var(--text-secondary);white-space:pre-line;margin-bottom:0.5rem;">{{ profile.training_routine }}</div>
        {% endif %}
        {% if profile.equipment %}
        <div style="font-size:0.8rem;color:var(--text-tertiary);">Equipment: {{ profile.equipment | replace(',', ', ') | replace('_', ' ') }}</div>
        {% endif %}
    </div>
    {% endif %}
</div>

<div style="display:flex;justify-content:space-between;gap:1rem;">
    <a href="/onboarding?step=5" class="btn btn-outline">Back</a>
    <div style="display:flex;gap:0.5rem;">
        <form method="POST" action="/onboarding/skip" style="margin:0;">
            <button type="submit" class="btn btn-outline">Skip for now</button>
        </form>
        <form method="POST" action="/onboarding/confirm" style="margin:0;">
            <button type="submit" class="btn btn-primary">Confirm & Start</button>
        </form>
    </div>
</div>

{% endif %}
{% endblock %}
```

- [ ] **Step 2: Commit**

```bash
git add app/templates/onboarding.html
git commit -m "feat: add onboarding wizard template with 6-step flow"
```

---

### Task 6: LLM enrichment service

**Files:**
- Create: `app/services/onboarding.py`

- [ ] **Step 1: Create enrichment service**

Create `/Users/krzysztofbury/PRIV/virgil/app/services/onboarding.py`:

```python
"""LLM enrichment logic for onboarding — runs after user confirms Step 6."""

import json
import logging
from datetime import date

import litellm

from app.config import INTERNAL_LLM_KEY, INTERNAL_LLM_MODEL
from app.db import get_setting, set_setting
from app.models.user_profile import save_enrichment

logger = logging.getLogger(__name__)


async def run_enrichment(db, profile: dict) -> None:
    """Run all applicable LLM enrichment steps. Each is independent and optional."""
    if not INTERNAL_LLM_KEY:
        logger.warning("No VIRGIL_INTERNAL_LLM_KEY set — skipping LLM enrichment")
        return

    llm_summary = None
    realistic_day = None

    # 1. Profile summary (if Step 1 data exists).
    if profile.get("sex") or profile.get("age") or profile.get("family"):
        try:
            llm_summary = await _generate_profile_summary(profile)
        except Exception:
            logger.exception("Failed to generate profile summary")

    # 2. Realistic day (if Step 2 data exists).
    if profile.get("ideal_day"):
        try:
            realistic_day = await _generate_realistic_day(profile, llm_summary)
        except Exception:
            logger.exception("Failed to generate realistic day")

    # Save profile enrichment.
    await save_enrichment(db, llm_summary, realistic_day)

    # 3. Goal expansion (if goals exist in DB).
    try:
        await _expand_goals(db, llm_summary)
    except Exception:
        logger.exception("Failed to expand goals")

    # 4. Habit analysis (if Step 4 data exists).
    if profile.get("training_routine") or profile.get("habits_break"):
        try:
            await _analyze_habits(db, profile, llm_summary)
        except Exception:
            logger.exception("Failed to analyze habits")


async def _llm_call(system_prompt: str, user_prompt: str, max_tokens: int = 2048) -> str:
    """Internal LLM call using env-var provider."""
    response = await litellm.acompletion(
        model=INTERNAL_LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        api_key=INTERNAL_LLM_KEY,
        max_tokens=max_tokens,
        timeout=90.0,
    )
    return response.choices[0].message.content


async def _generate_profile_summary(profile: dict) -> str:
    """Generate a concise profile paragraph for use as LLM context."""
    parts = []
    if profile.get("sex"):
        parts.append(f"Sex: {profile['sex']}")
    if profile.get("age"):
        parts.append(f"Age: {profile['age']}")
    if profile.get("height_cm"):
        parts.append(f"Height: {profile['height_cm']}cm")
    if profile.get("weight_kg"):
        parts.append(f"Weight: {profile['weight_kg']}kg")
    if profile.get("family"):
        parts.append(f"Family: {profile['family']}")
    if profile.get("habits_good"):
        parts.append(f"Good habits: {profile['habits_good']}")
    if profile.get("habits_bad"):
        parts.append(f"Struggles with: {profile['habits_bad']}")

    return await _llm_call(
        "You are a personal development assistant. Write a concise profile summary (2-3 sentences) "
        "that captures the key facts about this person. This will be used as context for future AI interactions. "
        "Write in the same language the user used in their input.",
        "\n".join(parts),
        max_tokens=256,
    )


async def _generate_realistic_day(profile: dict, llm_summary: str | None) -> str:
    """Generate a realistic daily schedule based on the user's ideal day and profile."""
    context_parts = []
    if llm_summary:
        context_parts.append(f"User profile: {llm_summary}")
    if profile.get("family"):
        context_parts.append(f"Family: {profile['family']}")
    if profile.get("training_routine"):
        context_parts.append(f"Training: {profile['training_routine']}")

    return await _llm_call(
        "You are a personal development assistant creating a realistic daily schedule. "
        "The user has provided their ideal day. Create a realistic version that accounts for "
        "their real obligations (family, work, energy levels). "
        "Format as time-blocked phases with practical notes. "
        "Be honest about constraints — if they have young kids, morning routine needs to be flexible. "
        "Write in the same language the user used in their ideal day description.",
        f"User context:\n{chr(10).join(context_parts)}\n\nIdeal day:\n{profile['ideal_day']}",
        max_tokens=2048,
    )


async def _expand_goals(db, llm_summary: str | None) -> None:
    """For each Level 3 (10yr) goal, generate Level 2 (3yr, ~35%) and Level 1 (1yr, ~10%)."""
    rows = await db.execute_fetchall(
        """SELECT g.id, g.area_id, g.content, ga.name as area_name
           FROM goals g JOIN goal_areas ga ON g.area_id = ga.id
           WHERE g.horizon = '10yr'"""
    )
    if not rows:
        return

    goals_text = "\n".join(
        f"- {row['area_name']}: {row['content']}" for row in rows
    )

    context = f"User profile: {llm_summary}\n\n" if llm_summary else ""

    raw = await _llm_call(
        "You are a goal-setting assistant. For each end goal (Level 3, 10-year vision), "
        "create two milestone levels:\n"
        "- Level 2 (3-year, ~35% of the end goal): A meaningful intermediate milestone.\n"
        "- Level 1 (1-year, ~10% of the end goal): A concrete, achievable first step.\n\n"
        "Return ONLY valid JSON, no markdown fences. Format:\n"
        '[{"area_name": "...", "level2": "...", "level1": "..."}]\n'
        "Write goals in the same language as the input.",
        f"{context}End goals (Level 3):\n{goals_text}",
        max_tokens=2048,
    )

    # Parse response.
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    try:
        goal_levels = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Could not parse goal expansion JSON")
        return

    if not isinstance(goal_levels, list):
        return

    for item in goal_levels:
        area_name = item.get("area_name", "")
        area_row = await db.execute_fetchall(
            "SELECT id FROM goal_areas WHERE name = ?", (area_name,)
        )
        if not area_row:
            continue
        area_id = area_row[0]["id"]

        for horizon, key in [("3yr", "level2"), ("1yr", "level1")]:
            content = item.get(key, "")
            if content:
                await db.execute(
                    """INSERT INTO goals (area_id, horizon, content, display_order)
                       VALUES (?, ?, ?, 1)
                       ON CONFLICT DO NOTHING""",
                    (area_id, horizon, content),
                )

    await db.commit()


async def _analyze_habits(db, profile: dict, llm_summary: str | None) -> None:
    """Check for Feniks trigger and suggest one experiment."""
    habits_bad = (profile.get("habits_bad") or "") + " " + (profile.get("habits_break") or "")

    # Check for Feniks trigger words.
    feniks_keywords = ["porn", "pmo", "masturbat", "nofap", "porno"]
    if any(kw in habits_bad.lower() for kw in feniks_keywords):
        await set_setting(db, "feature_feniks", "1")
        logger.info("Feniks feature auto-enabled based on onboarding habits")

    # Suggest one experiment to replace a bad habit.
    if not profile.get("habits_break"):
        return

    context = f"User profile: {llm_summary}\n\n" if llm_summary else ""

    raw = await _llm_call(
        "You are a habit coach. Pick the ONE most impactful bad habit from the list and suggest "
        "a replacement experiment. Return ONLY valid JSON:\n"
        '{"title": "...", "description": "...", "num_weeks": 4-8, '
        '"weekly_target_min": minutes_per_week, "weekly_target_max": minutes_per_week}\n'
        "The experiment should be realistic and specific. Write in the same language as the input.",
        f"{context}Bad habits to break:\n{profile['habits_break']}\n\n"
        f"Good habits to build:\n{profile.get('habits_build', 'none mentioned')}",
        max_tokens=512,
    )

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    try:
        exp = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Could not parse experiment suggestion JSON")
        return

    if not isinstance(exp, dict) or not exp.get("title"):
        return

    today = date.today().isoformat()
    num_weeks = min(12, max(2, exp.get("num_weeks", 4)))

    await db.execute(
        """INSERT INTO experiments (title, description, start_date, num_weeks,
           weekly_target_min, weekly_target_max, status)
           VALUES (?, ?, ?, ?, ?, ?, 'active')""",
        (
            exp["title"],
            exp.get("description", ""),
            today,
            num_weeks,
            exp.get("weekly_target_min", 60),
            exp.get("weekly_target_max", 120),
        ),
    )
    await db.commit()
```

- [ ] **Step 2: Lint**

Run: `cd /Users/krzysztofbury/PRIV/virgil && uv run ruff check app/services/onboarding.py && uv run ruff format app/services/onboarding.py`

- [ ] **Step 3: Commit**

```bash
git add app/services/onboarding.py
git commit -m "feat: add LLM enrichment service for onboarding (profile, day, goals, habits)"
```

---

### Task 7: Wire up auth redirect for existing users

**Files:**
- Modify: `app/routers/auth.py` (setup_submit handler)

- [ ] **Step 1: Read auth.py setup handler**

Read `/Users/krzysztofbury/PRIV/virgil/app/routers/auth.py` to find the `setup_submit` function.

- [ ] **Step 2: Update setup redirect**

In the `setup_submit` handler, after account creation succeeds, change the redirect from `/` to `/onboarding`:

Find the line that returns `RedirectResponse("/", status_code=303)` in the setup handler and change it to `RedirectResponse("/onboarding", status_code=303)`.

- [ ] **Step 3: Lint**

Run: `cd /Users/krzysztofbury/PRIV/virgil && uv run ruff check app/routers/auth.py`

- [ ] **Step 4: Commit**

```bash
git add app/routers/auth.py
git commit -m "feat: redirect new users to /onboarding after account creation"
```

---

### Task 8: End-to-end verification

- [ ] **Step 1: Kill any running server**

Run: `lsof -ti:8123 | xargs kill -9 2>/dev/null; true`

- [ ] **Step 2: Start server**

Run: `cd /Users/krzysztofbury/PRIV/virgil && uv run python -m app`
Expected: Starts with no errors. Migration 008 should apply.

- [ ] **Step 3: Verify migration**

Check logs for: `Applying migration 008: 008_onboarding.py`

- [ ] **Step 4: Test the wizard flow**

Navigate to http://localhost:8123 — should redirect to `/onboarding?step=1`.
Walk through all 6 steps. Verify:
- Progress bar updates
- Each step saves and advances
- Skip links work
- Back buttons work
- Step 6 summary shows only filled sections
- "Confirm & Start" redirects to dashboard

- [ ] **Step 5: Test skip**

Clear the DB and re-run setup. On the onboarding page, click "Skip onboarding" — should go to dashboard.

- [ ] **Step 6: Lint all files**

Run: `cd /Users/krzysztofbury/PRIV/virgil && uv run ruff check app/ && uv run ruff format --check app/`

- [ ] **Step 7: Final commit**

```bash
git add -A
git commit -m "feat: complete onboarding wizard — 6-step flow with LLM enrichment"
```
