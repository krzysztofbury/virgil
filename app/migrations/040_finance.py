"""Finance: a user-defined fortress of layers, accounts and monthly balances.

Layers are the user's own dictionary (a cash cushion, protection, independent
income), so they are rows, not a fixed list. Amounts are integers in minor
units of the base currency. Score rules say how a layer turns into points and
are edited like any other row; the seeded ones are only a starting point.
"""

import json

import aiosqlite

_DEFAULT_LAYERS = (
    # name, description, counts toward the cushion, rule metric, max points, thresholds
    (
        "Cash cushion",
        "Money available within days: current and savings accounts, short bonds.",
        1,
        "cushion_months",
        40,
        [[12, 40], [6, 30], [3, 20], [1, 10]],
    ),
    (
        "Protection",
        "Insurance that stops one event from emptying the cushion.",
        0,
        "policies",
        35,
        {"life": 10, "health": 8, "property": 7, "vehicle": 5, "income_loss": 5},
    ),
    (
        "Independent income",
        "Income that does not depend on current work, such as rent.",
        0,
        "independent_income_share",
        25,
        [[50, 25], [30, 20], [20, 15], [10, 10]],
    ),
    ("Investments", "Long-term money: retirement accounts, funds, shares.", 0, None, 0, None),
)


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS finance_layers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK(length(trim(name)) BETWEEN 1 AND 60),
            description TEXT NOT NULL DEFAULT '',
            counts_toward_cushion INTEGER NOT NULL DEFAULT 0 CHECK(counts_toward_cushion IN (0, 1)),
            display_order INTEGER NOT NULL DEFAULT 0
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS finance_score_rules (
            layer_id INTEGER PRIMARY KEY REFERENCES finance_layers(id) ON DELETE CASCADE,
            metric TEXT NOT NULL CHECK(metric IN ('cushion_months', 'independent_income_share', 'policies')),
            max_points INTEGER NOT NULL CHECK(max_points BETWEEN 0 AND 100),
            thresholds TEXT NOT NULL
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS finance_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK(length(trim(name)) BETWEEN 1 AND 80),
            institution TEXT NOT NULL DEFAULT '',
            layer_id INTEGER NOT NULL REFERENCES finance_layers(id),
            kind TEXT NOT NULL DEFAULT 'cash'
                CHECK(kind IN ('cash', 'bonds', 'retirement', 'brokerage', 'crypto', 'property', 'other')),
            currency TEXT NOT NULL DEFAULT '' CHECK(currency = '' OR length(currency) = 3),
            ownership TEXT NOT NULL DEFAULT 'own' CHECK(ownership IN ('own', 'joint', 'excluded')),
            archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0, 1)),
            display_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS finance_balances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES finance_accounts(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            amount_minor INTEGER NOT NULL,
            native_amount_minor INTEGER,
            native_currency TEXT NOT NULL DEFAULT '' CHECK(native_currency = '' OR length(native_currency) = 3),
            source TEXT NOT NULL DEFAULT 'web' CHECK(source IN ('web', 'api')),
            note TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(account_id, date)
        )"""
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_finance_balances_date ON finance_balances(date)")
    await db.execute(
        """CREATE TABLE IF NOT EXISTS finance_assumptions (
            effective_from TEXT PRIMARY KEY,
            essential_monthly_cost_minor INTEGER NOT NULL CHECK(essential_monthly_cost_minor > 0),
            note TEXT NOT NULL DEFAULT ''
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS finance_income_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK(length(trim(name)) BETWEEN 1 AND 80),
            monthly_amount_minor INTEGER NOT NULL CHECK(monthly_amount_minor >= 0),
            independent_of_work INTEGER NOT NULL DEFAULT 0 CHECK(independent_of_work IN (0, 1)),
            confirmed_on TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT ''
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS finance_policies (
            type TEXT PRIMARY KEY CHECK(length(type) BETWEEN 1 AND 40),
            status TEXT NOT NULL CHECK(status IN ('present', 'absent', 'not_applicable')),
            confirmed_on TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT ''
        )"""
    )

    existing = await db.execute_fetchall("SELECT COUNT(*) FROM finance_layers")
    if existing[0][0] == 0:
        for order, (name, description, cushion, metric, points, thresholds) in enumerate(_DEFAULT_LAYERS):
            cursor = await db.execute(
                "INSERT INTO finance_layers (name, description, counts_toward_cushion, display_order) VALUES (?, ?, ?, ?)",
                (name, description, cushion, order),
            )
            if metric:
                await db.execute(
                    "INSERT INTO finance_score_rules (layer_id, metric, max_points, thresholds) VALUES (?, ?, ?, ?)",
                    (cursor.lastrowid, metric, points, json.dumps(thresholds)),
                )
    await db.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('feature_finance', '0')")
    await db.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('finance_base_currency', 'PLN')")
