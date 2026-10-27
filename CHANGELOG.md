# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Dependabot version updates for GitHub Actions, the Dockerfile base image and
  the Compose images, weekly on Monday with a 7-day cooldown. Python packages
  stay on the `exclude-newer` cutoff. `SECURITY.md` names the private
  vulnerability report route and the repository protections.
- An evaluation of the WOD parser (#155): 30 synthetic notes in English,
  Polish and both, with the exact entries and unmatched names each should
  produce; a deterministic scorer (per-field accuracy, exact matches,
  out-of-vocabulary handling, invented values, JSON repair, latency and cost);
  and a harness that runs them through the real parser across models and
  reasoning efforts with a spending cap and writes a dated report to
  `docs/evals/`. Live runs are opt-in and never part of the test suite.
- The interface in Polish (#152). English stays the default; choose Polski
  on the sign-in pages or under Settings > General, and the choice is kept on
  the account so it follows you to another device. Every page, form, message,
  chart label and script string has a Polish version, with Polish plural
  forms. Dates stay ISO. Your own data, the starter content of a new account,
  the REST API, MCP and exports stay as they are.

### Changed
- The UI is no longer English only (reverses #40). A validation error from a
  shared service, such as an out-of-range measurement, reads in the page's
  language; the API keeps its English wording.
- Messages that htmx receives in a response header are percent-encoded, so
  they can carry Polish letters.
- Python 3.14.7 in the image, CI and development (was 3.12.14), and the
  project requires 3.14. Ruff targets py314. The MCP stdio script still runs
  on 3.12 or later, since it runs on the client's machine.

### Fixed
- From the design re-check (#163):
  - The dashboard no longer lists an experiment past its last day; the
    Experiments page already showed it as ended.
  - The onboarding Summary shows every section, with "Not filled in yet."
    and an Add link for an empty one, instead of an empty card.
  - Record balances shows amounts grouped by thousands (9 500.00), and an
    amount pasted with no-break spaces saves.
  - The cushion chart says its unit: months of essential cost.
  - Monthly Oura charts other than the scores leave headroom around the line.
  - A load on a bodyweight movement reads as added: +20.0 kg.
  - Helper text is brighter in both themes and at least 13px; Settings tabs
    sit in a grid on a phone; text links that act as controls get a 44px
    touch row.
  - Daily says what A.N.D.Y. is: a daily check-in for Body, Spirit, Self and
    Relations, apart from the life areas in Goals.
- Onboarding step 2 failed with an Internal Server Error since the Polish
  interface (#163): a literal percent sign in its example text was read as a
  format code. Every onboarding step is now rendered in both languages by the
  tests, and a template string with an unescaped percent fails them.
- Goal rep messages read "Execution rep skipd" and "carryd"; they now say
  skipped and carried over.

### Security
- cloudflared moves to 2026.10.4. Releases before 2026.10.2 still trust eight
  eMudhra (emSign) intermediate certificates with weak private keys, which
  anyone holding those keys could use to impersonate a server. 2026.10.4 also
  limits the metrics server's debug endpoints to an allowlist.
- The dependency audit ignores three filelock advisories until litellm allows
  filelock 4 (#154): two are Windows only, and the third needs a second local
  user, which the single-user container does not have.
- Signup and the admin invite form check an email address in linear time. The
  old pattern backtracked quadratically on a run of dots, so one signup
  request could hold a worker for minutes.
- A staged medical upload, and the text extracted from a PDF, are encrypted
  with the app key while they wait for the import job. A file staged before
  this change is treated as expired and is uploaded again.
- The keyboard shortcut overlay is built from elements, so markup in a
  translation stays text, and a swipe on the Daily page clicks the page's own
  previous or next day link instead of reading a URL from the page.
- virtualenv, used only by the development hooks, moves to 21.14.13 for
  GHSA-cx6f-vjhg-g3cc, ahead of the `exclude-newer` cutoff.

## [0.9.0] - 2026-10-23

### Added
- Change your password under Settings > Security (#94). Every other device
  signed in to the account is signed out; this browser stays in. It, the
  MFA disable form and factory reset, which all check the password, share the
  sign-in rate limit, so a stolen session cannot guess it fast.
- A security-event log on the admin page (#94): sign-ins and failed attempts,
  MFA lockouts, sign-outs, password and MFA changes, factory resets and the
  admin's account actions, each with the client address. It holds no secrets
  and no health data, never stores an unknown sign-in address (it may be a
  password typed in the wrong field), and drops rows older than 180 days.
- Invites (#94). With registration closed, an admin creates a single-use link
  on the admin page, optionally for one email address, as user or admin, and
  with or without the server's LLM key. The link is shown once and lasts 7
  days; only a hash of its secret is stored, and uvicorn's access log records
  `invite=[redacted]`. The signup it opens checks and uses up the invite in
  the same transaction as the new account; a wrong address or a taken one
  leaves the link usable. Open invites can be revoked, and an invite stops
  working once its issuer is no longer an active admin. Each account is
  separate: its own data, sign-in and keys.
- Admins can make another account an admin or a user (#94).
- Personal API tokens (#94 phase 2). A token serves its own account on the
  REST API and `/mcp`, with the scopes it was created with: `read`, `write`,
  `sensitive` (Freedom) and `finance`. GET routes need `read`, every other
  method `write`, and a refusal names the missing scope. Tokens are sent in
  `X-API-Key` or `Authorization: Bearer`; two different credentials in one
  request are refused. Only a hash is stored (central migration 007), use is
  recorded at most once a minute, and a revoked or expired token, or one of a
  disabled account, gets 401. The server's `VIRGIL_API_KEY` works as before,
  for `VIRGIL_API_USER_EMAIL`, and `VIRGIL_API_SENSITIVE` and
  `VIRGIL_API_FINANCE` now govern that key only.
- API tokens in Settings > Security (#94): create one with a name, scopes and
  an expiry (30, 90 or 365 days, or never), see it once with ready-to-paste
  commands for Claude Code, the MCP wrapper and curl, list your tokens with
  their last use, and revoke one at once. Creating one asks for your
  password and shares the sign-in rate limit: a token outlives the session
  and a later password change, so an open session alone must not mint one.
- Password reset links (#94). There is no mailer, so an admin creates the link
  on the admin page and hands it over. It sets a new password once, lasts 15
  minutes, replaces any earlier link for the account, and signs out every
  session and revokes every API token when used; MFA stays as it was. Only a hash is stored. No link for
  your own account, a disabled one, or one in `VIRGIL_ADMIN_EMAILS`, which
  any admin could otherwise take over.

### Changed
- Factory reset asks for your password (#94).
- Hosted `/mcp` tool calls carry the caller's own credential to the REST API
  (#94). They used to carry the server key, which would have let any other
  credential act on the server key's account through a tool.
- Accounts in `VIRGIL_ADMIN_EMAILS` can no longer be disabled, demoted or
  deleted from the admin page; delete was only hidden before, and disable was
  allowed (#94).
- An address in `VIRGIL_ADMIN_EMAILS` can no longer be claimed by someone else
  (#94). Nothing verifies email ownership and the list promotes on every
  restart, so after the first account such an address comes in only through
  an invite made for exactly that address; open registration refuses it.
- The server's LLM key (`VIRGIL_INTERNAL_LLM_*`) serves only accounts allowed
  to spend it (#94). An account's own key always comes first. Existing
  accounts and the first one keep the server key; invited accounts get what
  the invite said, and the admin page shows and switches it per account.
  Every paid call follows the same rule, scheduled ones included, and
  onboarding enrichment and medical import no longer read the server key
  directly, so they also run on an account's own key. Settings says when the
  server's key is in use, and that whoever runs the server can read stored
  keys. Central migrations 004 to 006 snapshot the registry before running.

## [0.8.0] - 2026-10-22

### Added
- Finance over the API and MCP (#135): a fortress of user-defined layers,
  accounts and monthly balances, with the cushion, cushion months, income
  shares, insurance policies and a score from editable rules. Seven MCP tools
  record a month in one call and read it back; every route needs
  `VIRGIL_API_FINANCE=true`. Finance is part of the JSON export and restore.
- The Finance page (#135), on under Settings > Modules: the cushion, its
  months, the score, the cushion over time, each layer with its accounts,
  policies or income, and a form that records a month starting from the
  latest balances. Finance setup edits layers and their score rules,
  accounts, essential cost, income and insurance.
- Restore one account from a JSON export (#48). Export JSON is now a versioned
  document that names its schema version, and Settings > Data can restore one.
  The file is checked on a staged copy first, older exports are migrated, and
  the page shows what would change before anything is replaced. A copy of the
  account is saved first. LLM keys, the Oura connection and this server's
  state are never exported or replaced. Settings from the file are checked
  with the same limits as the Settings forms. docs/recovery.md describes the
  format.
- `scripts/load_check.py` measures uploads and background jobs alongside
  ordinary reads on the deployment hardware (#59). It runs in a throwaway
  container from the production image with no network and reports latency, a
  liveness probe for server-wide stalls, job outcomes and peak memory.
  docs/load-check.md has the command and the results.
- A browser smoke test (#102) loads the dashboard, Daily, Training and Freedom in
  Chromium at desktop and phone width on a seeded instance. It fails on console
  errors, horizontal overflow or a card clipping its content, saves a Daily
  change over HTMX, checks that a lost save is reported and waits to be
  dismissed, and checks the notification tray's place and self-dismissal. CI
  installs Chromium for it. It replaces the CSS and JavaScript string assertions
  it covers.
- Off-site copies (#46): with `VIRGIL_OFFSITE_*` set, the whole instance is
  uploaded once a day to an S3-compatible bucket such as Cloudflare R2, each
  database, the registry and the encryption key as its own object, compressed
  and encrypted to an age public key. The month's first run is kept as a monthly
  copy; the bucket's lifecycle rules set how long either stays (docs/recovery.md).
  Deleting or resetting an account deletes its copies there too. Failures retry
  hourly, alert once a day, and show in Settings > Automation for administrators.
  `scripts/recovery.py unseal` turns a downloaded run back into a bundle.
- Freedom's relapse records are listed per habit and can be corrected (#52). A
  record a slipped day wrote follows that day, so it links to the day form,
  which can now also delete a logged day (its record goes with it). Any other
  record, older or imported, edits its date, type and note on its own, or is
  deleted after a confirmation; two relapses on one date are refused.
- One set of a stored workout can be corrected or deleted (#52) from its day's
  dialog: movement (a library name), set, reps, weight, time in minutes,
  equipment and note, with the bounds a new set has. Personal Bests follow, and
  deleting a set keeps the rest of the workout. On a phone each set is a card.
- Experiment entries can be corrected (#52): each one in Recent Entries edits its
  date, value and note, within the experiment's dates and the metric's bounds,
  and a yes/no metric keeps one answer per day. This works on a completed or
  abandoned experiment too, whose entries are now listed. Deleting an entry asks
  first, and says when an Oura import would come back on the next sync. On a
  phone each entry is a card instead of a table that ran off the screen.
- Blood results can be corrected or deleted on the page (#52): a marker's chart
  view lists each result with its date, value, text and flag. A correction
  recomputes the flag from the reference range unless a lab flag is chosen, and
  a date another result already holds is refused.
- A deleted goal can be restored for 30 days (#53), from `Recently deleted` on
  the Goals page, with its id, focus and dates, and the sub-goals and
  experiments it was linked to. Each goal keeps its last 20 changes (what
  happened and which fields changed, never the text) under its details. The
  JSON and CSV exports include both. Migration 039 adds the two tables.
- Accessibility (#51): a `Skip to content` link first in the tab order, focus
  returned to the opener when `Esc` closes the phone `More` menu, a training
  day's dialog or the shortcut overlay (now a modal dialog), a closed `More`
  menu that no longer takes focus, a stronger focus ring on goal text, no
  animation, smooth scrolling or chart drawing under reduced motion, and a text
  alternative on every chart. Keyboard focus now stays on the dashboard's
  toggles, Freedom's `Clean`, experiment quick-logs and Settings tabs when they
  redraw, and the year calendar's days are no longer one tab stop each.
- The dashboard's Oura card and the Oura page say when Oura data last synced
  (#55), and turn amber with "Last synced" once that is over a day ago, when
  today's sleep and readiness may be missing.
- History views share one set of windows (#55): 4, 12 or 26 weeks, 12 by
  default, picked from tabs on Life Scores, Oura's compare chart and daily
  history, and Training's Personal Bests. They replace 10 days of Oura trends,
  30 Oura rows and a fixed 12 weeks of personal bests.
- Life Scores on the dashboard chart the power level over that window, each area
  one legend tap away, and History lists every assessment in it. It used to
  list only the latest two complete ones.
- Daily > Your trends shows the last 12 weeks of completion (#54), one column
  per week, with each week's average under it. It is a table, so a screen reader
  reads each day as "5 of 7" and a day without a log as not logged.
- An open page that loses its connection to Virgil turns read-only (#50): a
  banner says so and every control that writes is disabled, until Virgil
  answers again. A failed background read no longer reports unsaved changes.
- Watchtower reports each deploy, and each poll while an update keeps failing,
  to the optional `WATCHTOWER_NOTIFICATION_URL` (a shoutrrr URL; `.env.example`
  shows the form for the alert webhook and for Telegram). Existing installs
  need the updated `docker-compose.yml` and the variable; an invalid URL stops
  Watchtower, so check its log for `Using notifications` afterwards.
- Settings > Automation shows how old the newest backup is and whether the
  backups are OK, overdue (two intervals without one), running, off, or failed
  on the last attempt. The factory reset asks for a download first, and says
  how old the newest server backup is that the reset deletes.
- `scripts/screenshots.py` regenerates the README screenshots from the demo
  seeder on a throwaway instance, and shoots other pages or phone widths to check
  a change. The seeder now includes fictional goals, three of them in focus.
- The dashboard checks Freedom habits off for today in one tap, and each active
  experiment card logs today's entry (a yes/no, +1, a rating or minutes) and shows
  what is already logged. Both redraw in place; a slip opens the habit's day form,
  and a tap never overwrites a day already logged elsewhere.
- Freedom replaces the No Porn module and tracks any habit being broken, several
  at once: porn, alcohol, nicotine, cannabis, drugs, gambling, social media,
  sugar, or one in your own words. Each habit defines its own slip (label,
  amount unit and threshold, near miss, written definition) and has its own
  streak, weekly clean rate, bricks and timeline. A Today list checks every
  habit off in one tap; Settings > Modules picks the habits from a checklist.
- `GET /api/freedom` and the MCP tool `get_habits` list every tracked habit
  (sensitive scope). The Feniks REST calls and their MCP tools take an optional
  `habit`, defaulting to the first tracked one, and keep their old field names.
- The dashboard's Today block is editable in place: each A.N.D.Y. task and
  routine advances `done > skipped > pending` with one tap, without opening
  Daily. A Focus block lists the starred goals, and the week strip has a legend.
- The app serves the MCP tools over Streamable HTTP at `/mcp`, with the REST
  API's `X-API-Key` (plus the Cloudflare Access service-token headers behind
  Access), so a client needs no checkout or `uv`.
- REST and MCP complete CRUD for the daily log (read and delete one day),
  Feniks (delete a day, edit and delete bricks, edit the module config),
  training sessions (read one) and body measurements (list, merge, delete).
- REST and MCP writes for a day check-in: the daily log with A.N.D.Y. as a
  partial merge (`log_daily`), Feniks day logs and bricks behind the sensitive
  scope (`log_feniks_day`, `add_brick`), and structured training sessions with
  their sets (`log_training`, `update_training`, `delete_training`).
- Movement taxonomy: families that group variants, aliases for the athlete's
  own spellings, a default equipment per movement and equipment per set
  (including a picker on the WOD confirm screen). The vocabulary gains the
  CrossFit variants it lacked, such as hang power snatch, pistol squat, wall
  walk, D-ball over shoulder and devil press. Library writes over REST/MCP
  accept family, default equipment and aliases, also on builtin entries.

### Changed
- The Virgil container gets `mem_limit: 1g` in docker-compose.yml (#122). The
  load check on the NAS, rerun after the event-loop fixes, peaked at 585 MB;
  docs/load-check.md has the results.
- Export CSV covers the same tables as the JSON export: exercise families and
  aliases are added, and app settings are no longer included.
- The last of the design review (#95):
  - Oura history shows sleep at one decimal with "h" ("7.3 h", not 7.26 next
    to 7.1), with numbers right-aligned under right-aligned headings.
  - The training calendar's three greens are further apart, and "Hover for a
    summary" is left out on screens without hover.
  - Personal Bests state the best weight once, with its reps or time beside
    it, and list only the other records under it.
  - The Goals horizon tabs and Add goal sit right above the area cards they
    filter, below the focus panel and the execution queue they never changed.
  - The offline page uses the app's logo and says "Can't reach your Virgil
    server", which may be on the home network, not "the internet".
- Settled from the design review (#95):
  - Life areas are the eight in Goals, the same ones Life Scores rates.
    Onboarding's four end-goal fields each say which Goals area they are saved
    under, read from the area list. They now also show what was entered before,
    which they never did. The dashboard's daily block is titled "Today's
    A.N.D.Y.", A.N.D.Y.'s four practices rather than another list of areas.
  - Freedom, with two or more habits, logs today once: the Today list.
    - For an unlogged today the day card becomes "Add details or log a slip".
    - Slipped opens the card with the slip already chosen.
    - Another day, or a logged one, still opens as an edit. A single habit
      has no Today list, so its card stays the log.
    - The habit shown below the list is marked with an accent edge.
  - Settings > Modules can leave Freedom off the dashboard, where every
    habit's name and streak showed to anyone glancing at the home screen. It
    is on by default.
  - The dashboard's Oura sleep, readiness, HRV and resting heart rate each
    show their change against the previous seven scored days, by the rule the
    Oura page uses for readiness, now in `app/services/oura_baseline.py`.
    "Insights" has the same small-caps heading as every other section.
- From the design review (#95):
  - Helper text is brighter on the dark theme and 13px, up from 12px.
  - Empty Oura and measurement cards on the dashboard link to the next step:
    connecting Oura, and Daily on the latest Saturday, when it asks for
    measurements.
  - The keyboard shortcut overlay has a solid panel over a darker backdrop, and
    does not open on touch-only screens.
  - New and edited experiments say what is wrong under each field, with a count
    beside the button, instead of a browser bubble that showed nothing on a
    phone.
  - Editing an experiment saves its basics and every metric with one button.
    One invalid row saves nothing. A metric's Delete sits at the end of its row
    and asks first.
  - Experiments write dates in ISO, like every other page, and week rows as
    month-day.
  - Training days in Settings are seven toggles. The planner still stores and
    reads the same comma list.
  - Freedom habit settings say what an empty field means under it, save with
    "Save changes", and Stop tracking is styled as the destructive action it is.
- Onboarding, from the design review (#95):
  - It hides the app's navigation, which led to pages that only redirect back
    to it, and keeps the logo and Log out.
  - Habits are asked once, in step 4; step 1 no longer asks them too, and no
    longer clears what an earlier version stored there.
  - Step 5 without an internal LLM key says so and offers Next, with no upload
    form it would refuse. With a key, Skip sits under the form as on every
    other step, and the file picker matches the theme.
- Settings > Integrations lists the Oura setup as steps, with the redirect URI
  to paste into Oura's application form and a Copy button (#95).
- On a phone, from the design review (#95):
  - The header keeps only the theme and admin icons. Settings and Log out
    live in the More menu, where Log out is the last item, set apart.
  - The tab row brings the active tab into view and fades at an edge with
    more tabs past it. On Settings > Security no visible tab was marked.
  - The exercise library, the WOD confirm form and Oura history show each row
    as a card with labelled values. Their tables showed two or three of eight
    columns, the fields to fill in among the hidden ones.
  - A goal's delete moves into its "Details and execution". The trash icon sat
    next to the star in every row.
  - Section toggles, small buttons, the goal star, an entry's delete and the
    Settings checkboxes are at least 44px tall to tap.
- WOD confirm on a wide screen: reps, weight, time and note fit what is typed
  (15 showed as "1", 43 kg as "4"), and an unrecognised movement is an amber
  warning that says what to do, in place of small grey text (#95).
- Every third-party asset is served from Virgil itself (#60): HTMX, Alpine.js,
  Chart.js, Lucide, Flatpickr, and the DM Sans and JetBrains Mono fonts, pinned
  by file name, with their sources and SHA-384 hashes in
  `app/static/vendor/SOURCES.json` and a test that the files still match. Pages
  make no request to a CDN or Google Fonts.
- The Content-Security-Policy no longer allows inline scripts or eval (#60).
  Scripts load from this origin or carry a nonce made for that response.
  Alpine.js is its CSP build, its components live in `static/js/components.js`,
  and every inline event handler became a data attribute that `app.js` acts
  on. htmx eval is off. The policy also sets `object-src 'none'`,
  `base-uri 'self'` and `frame-ancestors 'none'`. Styles keep `'unsafe-inline'`
  for style attributes and what Chart.js sets.
- Backups and pre-migration snapshots can live off the data volume:
  `VIRGIL_BACKUP_DIR` sets where they are written, and compose mounts
  `VIRGIL_BACKUP_HOST_DIR` (default `./backups`) there. Startup moves backups
  left in `data/backups/` to it, deleting each only once an identical copy is on
  disk, and logs an error when the directory cannot hold backups (not writable,
  or no hard links on exFAT or FAT).
  Deleting or resetting an account removes its copies from both places.
- Compose adds an `autoheal` service that restarts `virgil` when it stops
  answering the new `/livez`, which the healthcheck now probes instead of
  `/healthz`. A start that leaves a database unmigrated keeps `/healthz` at 503
  and posts one alert to `ALERT_WEBHOOK_URL` instead of being restarted, since a
  restart cannot fix it. The healthcheck's start period is 300 seconds, so a
  migration gets about 6.5 minutes. Rollback stays manual, because an older
  image refuses a newer database.
- **Existing installs**: Watchtower does not apply compose changes. Create
  `backups/` next to `data/`, writable by uid 1000 (`chown 1000:1000 backups`),
  copy the new `docker-compose.yml`, then run `docker compose up -d` (with
  Container Station, edit its compose instead and recreate; see the README). Until
  then backups stay in `data/backups/`, and production startup logs a warning.
- CI installs the smoke-test browser without apt and caches it, after a slow
  Ubuntu mirror ran a job past its limit. A test stuck for two minutes dumps
  every thread's stack, so a hang names its test.
- `cloudflared`, Watchtower and autoheal are pinned by version and digest, and
  the CI actions, including those in the job that publishes `latest`, by commit.
- Each per-user migration runs in its own immediate transaction, so a
  failure leaves no half-applied schema behind, and a migration that commits
  on its own is refused. A database migrated by newer code is refused too:
  after an image rollback the account is marked failed and `/healthz` reports
  degraded instead of staying green.
- Migration 001 carries its own frozen schema and seeds; `app/db.py` no longer
  doubles as migration input. Fresh databases are unchanged.
- The API-key and CSRF exemptions come from the routes that depend on the API
  key (plus `/mcp`) instead of two hand-kept path lists.
- The pre-multi-user `POST /api/oura/webhook` address, which only answered
  410, is gone. Per-user callbacks are unaffected.
- `TZ` is documented and overridable in compose (default `Europe/Warsaw`), and
  startup warns when it is unset and every "today" would follow UTC.
- Second Brain is gone. Virgil no longer reads markdown from a server folder
  (the Settings import, `scripts/import_data.py`, the A.N.D.Y. `plan.md`) or
  writes exports into it (manual and scheduled). Markdown export is a download
  from Settings > Data instead. `VIRGIL_SECOND_BRAIN_PATH` and its compose volume
  are removed; migration 037 clears the leftover export jobs and settings.
- Every account goes through onboarding once: the skip button and route are
  gone, and "Continue without AI" finishes it without the paid extras.
- The UI is English only. The training capture and confirm flow, its messages and
  the equipment labels were Polish. New accounts get English goal areas, and
  migration 036 renames the seeded areas and the importer's blood-test categories
  in existing ones; names the person chose stay. A test keeps Polish out of
  templates, scripts and route messages.
- The seeded No Porn config no longer carries a personal motivation text, and
  the dropped milestone table is no longer seeded.
- Freedom's REST paths and MCP tools carry its name: `/api/freedom/{habit}/days/{date}`,
  `/api/freedom/{habit}/bricks`, `/api/freedom/bricks/{id}`, `PATCH /api/freedom/{habit}`
  (every habit setting) and the tools `log_freedom_day`, `delete_freedom_day` and
  `update_habit`. The `/api/feniks/*` paths and the `log_feniks_day`,
  `delete_feniks_day` and `update_feniks_config` tools are deprecated aliases for one
  release.
- Migration 035 drops `feniks_config`, now on the Porn habit, and the unused
  `feniks_milestones`. The No Porn journal and pleasures stay.
- The roadmap moved from `TODO.md` to GitHub issues.
- `/feniks` redirects to `/freedom`, and `g f` opens Freedom. Migration 034
  moves No Porn days, bricks, relapse events and settings onto a Porn habit and
  renames the `feature_no_porn` flag to `feature_freedom`.
- On phones the bottom bar holds Home, Daily, Training, Freedom (Goals when
  Freedom is off) and More, which opens the read-mostly pages. The separate
  Data menu (Oura, Blood) is folded into More.
- A UI pass for phones, the light theme and legibility: surface overlays follow
  the theme, readable text has a 12px floor, inputs are 16px and controls 44px
  on phones, card headers and tabs wrap cleanly, and status toggles, day arrows
  and charts have accessible names. Steps print with thousands separators and
  a one-sided lab range reads `< 100`.
- Brick edits and body measurements share their persistence with REST. The
  measurement form refuses values outside plausible bounds instead of storing
  them.
- Duplicate movements are merged with their history: Kettlebell Swing into
  KB Swing, Shoulder Press into Overhead Press, Halo (KB) into KB Halo. The old
  names remain as aliases.
- Personal bests separate a load on non-default equipment.
- Daily-log and Feniks forms share their persistence with REST, so both paths
  validate and write the same way.

### Fixed
- Finance no longer lists an account with no balance yet on a date as stale
  (#138). Stale means an older balance was carried into the latest snapshot;
  an account added later, or a date before its first balance, has nothing to
  carry and shows "no balance yet" without the warning.
- Blood Work, from the design review (#95):
  - Each marker is shown at the precision its lab reports, taken from its
    reference range and results, so HbA1c 5.0 against a 5.6 limit no longer
    reads "5".
  - The change from the previous result says what it is from ("from 121, +21").
    Its arrow is red when the marker moves away from its range or leaves it,
    green when it moves back towards it or into it, and neutral inside the
    range or without one.
  - The chart link uses a chart icon. Its rising-trend icon read as a green
    "up" on every row.
- An active experiment past its last day shows as Ended (#95): in the list,
  with a prompt on its page to mark it completed or extend it, without the
  quick log for today, and with the entry form starting on its last day.
- Oura charts no longer overshoot and run flat along the edge (#95). Lines use
  monotone curves everywhere, Oura scores sit on a 50 to 100 axis instead of
  one fitted to a five-point spread, and other values get room above and
  below.
- An experiment's total target is labelled as covering all its weeks, next to
  the per-week targets in the week rows (#95).
- The Oura webhook no longer stays at "Retrying" with "Oura subscription
  expiration_time has no timezone". Oura sends that time in UTC without an
  offset, and Virgil refused it, so no subscription was ever recorded. It is
  read as UTC now; the hourly retry picks it up, and duplicates left at Oura by
  the failed attempts are removed by the same pass.
- Oura resting heart rate is filled again. `/heartrate` takes `start_datetime`
  and `end_datetime` and ignored the `start_date`/`end_date` it was sent, so it
  answered for its default window only. It is now asked a week at a time, and
  each sample counts for its local day (Oura stamps them in UTC).
- Today's Oura activity and steps arrive with the sync: Oura leaves the
  `end_date` day out, so the range now ends tomorrow.
- Oura responses split across pages (`next_token`) are fetched in full.
- A logged error no longer pauses the whole server (#122). The MCP SDK had
  installed Rich's log handler, which rendered every traceback with syntax
  highlighting on the event loop: 0.9 s per failed medical import on a fast
  machine, with every other request waiting. Logs are plain lines now.
- Signing in no longer pauses other requests: the password check (bcrypt, about
  0.2 s) runs in a thread (#122).
- `scripts/load_check.py` sends imports to a stand-in provider on loopback, so
  the provider request and its failure are measured too.
- Submitting the WOD confirm form no longer throws in the page script: a field
  named `action` hid the form's address from the logout check.
- Static files no longer count against the per-address rate limit. A cold cache
  fetches every stylesheet and script on each page, so a few quick page views
  could run a reader into 429 Too Many Requests on the pages themselves.
- A name with a quote in it can no longer run script or break a delete
  confirmation (#52). The blood-result, exercise-library, experiment-metric and
  admin user confirmations, and the Freedom day picker, wrote names and emails
  into inline JavaScript, where HTML escaping does not hold; they now read them
  from a data attribute. Marker names can come from imported lab PDFs, so this
  was more than a self-inflicted case. A test keeps template values out of
  inline handlers.
- Adding a blood result refuses infinity and not-a-number, and a text result
  ("negative") is no longer flagged H or L from the number stored beside it.
- Logging out now signs out every session of the account, not only the
  browser's cookie: a copied session token used to stay valid for 7 days.
  Turning MFA on or off signs out the other sessions, and disabling an account
  revokes its sessions so re-enabling it does not bring them back. Central
  migration 003 adds the session epoch and the MFA attempt counter; sessions
  from before it stay valid until the account's first sign-out. Rolling the
  image back past this release needs the central pre-migration snapshot
  restored (see docs/recovery.md): older images refuse the newer registry.
- Five wrong MFA codes lock verification for that account for 15 minutes.
  Before, only the per-IP rate limit slowed a guesser down.
- An anonymous request could make the server buffer up to 21 MB of body before
  CSRF, auth or the rate limit refused it. The rate limit and auth now run
  first, and a write without the CSRF cookie is refused before its body is read.
  Security headers wrap every response, so the 403, 429 and 503 responses and
  login redirects carry the CSP and X-Frame-Options too.
- Behind the Cloudflare tunnel with header trust off, every visitor shared
  cloudflared's rate-limit bucket: ten failed logins from anyone blocked login
  for everyone. `VIRGIL_TRUSTED_PROXY_IPS` now takes CIDR ranges, so trust
  survives cloudflared getting a new address on the compose network, and the
  log warns the first time `CF-Connecting-IP` arrives untrusted. A `/0` range
  is ignored with a warning, since it would let any peer pick its address.
- A blood-panel PDF whose markers failed to parse was missing its extraction
  charge from the paid-call ledger, so a retry paid for the extraction again.
  The extraction is now recorded on its own and a retry only parses.
- Saving the daily form without rating energy no longer stores 5. An unrated
  day stays empty, and a rating already entered is kept.
- Auth closes the user database when loading feature flags fails, instead of
  leaking the connection.
- Hosted MCP tool calls dial the configured bind host and port. Wildcard and
  IPv6 binds no longer send every tool to 127.0.0.1:8123.
- Long goal titles wrap on phones instead of being cut off mid-word; they stay
  editable in place, and Enter still saves.
- Leaving an unfinished MFA login for `/login` could loop between `/login` and
  `/`. A pending MFA login is now treated as signed out there, and it expires
  after 10 minutes instead of 7 days.
- `/api/summary` (and the MCP `get_streaks` tool) return the Freedom status only
  in the sensitive scope, like the other Freedom reads.
- A signed session cookie for a deleted or disabled account sent the browser
  back and forth between `/` and `/login`. The cookie is now cleared.
- The keyboard shortcut help read a feature flag that no longer existed, so it
  never listed `g f`.
- Spacing: the Freedom timeline touched the habit settings card, the exercise
  library's search, section filter and `Add exercise` had no gaps, and Daily's
  Edit field sat on its button. The exercise table scrolls sideways on phones
  instead of wrapping each name word by word.
- MCP tools now return the API's error detail to the client. The SDK hid the
  text of every failure, so a refused write read only "Error executing tool".
- The daily, measurement, life-score, bloodwork, training and experiment forms
  store dates as `YYYY-MM-DD`. They accepted any spelling Python parses
  (`20261017`, `2026-W42-6`) and stored it as typed, so those rows were missing
  from date ranges and could duplicate a day.
- A request that read an account just before a factory reset or a delete no
  longer creates an empty, unmigrated database under the old file name. User
  databases open read-write without create: a request that raced a reset starts
  over at `/`, and an account whose file is gone answers 503 and is logged.
  Startup skips it instead of recreating it, and `/healthz` stays green.
- Two concurrent writes to the same measurements day, habit settings or brick
  no longer drop one of the changes: each read-merge-write runs in one
  immediate transaction.
- Deleting an account or a factory reset deletes that database's backups and
  pre-migration snapshots too. A reset moves the account to a new file name, so
  nothing ever pruned them. Settings and the admin page say so.
- One failed Oura token refresh no longer disconnects the integration for good.
  Only Oura refusing the refresh token or the client (`invalid_grant`,
  `invalid_client`) asks for a reconnect; a timeout, 5xx or 429 keeps it
  connected and the next sync retries. Refreshes run one at a time per account,
  so the scheduler and a webhook job cannot spend the same single-use refresh
  token.
- Freedom habits named in non-Latin script no longer collapse into one. Slugs
  transliterate accented Latin letters (`Słodycze` is `slodycze`), and a
  different name that folds to a slug in use is numbered (`habit-2`). The same
  name in another case or punctuation still resumes the existing habit.
- A training note captured while another note's parse was still queued is
  parsed too. It used to be saved with no parse queued and a message saying
  its entries were being read. Migration 038 lets each note queue its own
  parse; the other paid jobs keep one queued at a time.

## [0.7.0] - 2026-10-11

### Fixed
- Central backups validate recent snapshots before skipping a run and publish
  complete files atomically. Interrupted or corrupt snapshots no longer suppress
  replacement for a day.
- The import CLI requires an existing account selected with `--email`; Settings
  displays that account's real database. Removed the legacy global connection.
- API date-window tests use a fixed clock. Tests and production images use
  bundled LiteLLM capability metadata instead of mutable remote metadata.
- Upgraded the locked web stack, including FastAPI, Starlette, multipart parsing,
  and Jinja2. Template calls use the current request-first interface; blank
  login passwords and explicit empty library fields retain their behavior.
- Version tags cannot overwrite `latest`. Only a verified main image can be
  promoted to a version alias, with matching package and changelog versions.
- Corrected the secret scanner's allowlist filter and marked only verified
  synthetic fixture credentials as exceptions.

### Changed
- One CI workflow owns verification, container qualification, and publication.
  PRs no longer duplicate feature-branch push checks. Python and uv are pinned,
  dependency installation is locked, and Docker bases are pinned by digest.
- Pre-commit and CI use the same locked Ruff and Bandit versions. CI includes
  dependency audits and the independently locked MCP stdio smoke test.
- Forms and REST share one atomic exercise-library persistence service,
  including collision checks, tag replacement, and rollback.
- The active roadmap replaces historical status duplication and unavailable
  implementation-plan links. Recovery and release procedures are tracked docs.

### Added
- Offline operator recovery bundles include the central registry, every
  referenced user database, the encryption key, and a versioned checksum
  manifest. Verify before restoring to a new directory; existing data is never
  overwritten. An off-machine copy and production restore drill remain operator
  actions.
- Container smoke testing covers signup, migrations, templates, static assets,
  restart, and login after an offline recovery before publishing the same image.

- **Training history is a year calendar instead of a date list.** A dashboard-style
  heatmap groups every session on its training day, uses total duration for three
  intensity levels, previews the day on hover or keyboard focus, and opens
  bounded pages of session, set, note, recovery, and delete details on selection.
  Recorded years remain reachable through bounded year navigation.
- **Personal Bests group comparable efforts per movement.** Each compact movement
  panel keeps the heaviest load visible and separates records by rep count or
  timed duration, so a 3-rep set no longer hides the useful 5-rep record.
- **Remote subscription lifecycle is provider-neutral and self-healing.** Central
  migration 002 adds desired state, item-level renewal metadata, fenced claims
  and heartbeat-maintained user lifecycle leases. A bounded adapter contract
  keeps provider-specific discovery, ownership and teardown outside the generic
  coordinator. Oura subscriptions are reconciled periodically, renewed before
  expiry, recovered across restarts and removed before credentials or user data
  can be replaced.
- **Goals and execution reps are canonical Virgil data.** Migration 031 adds
  explicit goal lifecycle and date windows, first-class one-off reps with
  calendar periods, due dates, completion/carry/skip transitions, and optional
  goal links for repeated-metric experiments. Browser, REST, and MCP writes use
  one validation contract; API writes support idempotency keys and history is
  bounded.
- **The MCP server targets Python SDK 2.x.** The PEP 723 dependency is bounded to
  `mcp>=2,<3`, uses `MCPServer`, and exposes goal-area, goal, and execution-rep
  read/write tools.
- **Feniks states its measurement contract next to the forms.** Watched means at
  least five total minutes of intentional viewing, shorter exposure can be
  recorded as a clean-day note, and a brick is one distinct decision episode
  resolved before exposure. Brick records now support correction and removal.
- **Every paid LLM call now runs as a durable job.** Morning briefings, training
  note analysis, A.N.D.Y. suggestions, experiment week summaries, onboarding
  enrichment and medical record import all leave the request path: routes and
  the scheduler persist intent and enqueue, and the worker performs the provider
  call. Paid kinds get one attempt and manual retry, because a timeout does not
  prove the provider refused the request. Migration 029 adds a publication
  ledger that outlives terminal job pruning, so a crash between a domain write
  and job completion cannot buy the same answer twice, and permits one queued
  paid job per kind.
- **The onboarding screen reports each AI extra and offers to skip them.**
  Enrichment became four independently published steps, so a retry re-buys only
  what is missing and one failing step no longer costs the others. Step 6 shows
  each as ready, not yet, or nothing to use, beside an explicit
  "Continue without AI".
- **The thinking budget is a setting.** Settings > General offers none, low,
  medium, high and xhigh as one scale, defaulting to medium, and steps xhigh
  down for providers that reject it. Picking a level saves it; a select plus a
  Save button made three controls out of one decision.
- **Backup, markdown export and Oura sync now run as durable workloads.**
  Manual routes, schedules and HMAC-verified webhooks only enqueue bounded,
  restart-safe jobs; a static worker registry performs external I/O with automatic
  retries. Migration 028 permits one queued successor per workload kind, safely
  reconciles v27 duplicates and keeps interrupted work from losing a wider scope.
  Backup and export publication is replay-safe, Oura partial results remain
  visible, and webhook deliveries have persistent fingerprints plus timestamp
  replay protection.
- **Durable jobs now have session-scoped status and explicit retry controls.**
  Settings shows a bounded recent-job list with accessible HTMX polling for
  active work and no worker, payload or idempotency metadata. Failed work can be
  retried explicitly; ambiguous outcomes require an additional warning and
  confirmation, with an atomic state-and-attempt check preventing stale retries.
- **Durable jobs now have a bounded scheduler worker.** Each tick rotates through
  a bounded user batch with bounded concurrency, recovers stale leases and runs
  at most one claimed job per user. Handlers use a separate database connection,
  receive heartbeats outside queue transactions and must pass fail-closed
  transaction checks. Unsupported, timed-out and ambiguous outcomes require
  attention, while a lost lease cannot overwrite a newer attempt.
- **Durable background work now has a restart-safe state core.** Migration 027
  adds a bounded per-user job queue with idempotent enqueue, atomic single-runner
  claims, persisted retry timing, stale-job recovery and explicit
  `needs_attention` handling. Every claim receives a unique lease token, so a
  recovered process cannot complete a newer attempt; unknown work defaults to
  manual retry rather than risking duplicate external side effects.
- **Browser writes now share one accessible feedback contract.** Native PRG
  responses and HTMX requests use bounded `msg`/`err` outcomes, one persistent
  error/status region, and a submit state that preserves the control's original
  label. Network, timeout, validation, and server failures remain distinct.
- **Daily and Feniks retain bounded drafts after transport failures.** Drafts
  stay tab-scoped in `sessionStorage`, restore by route/date, and clear only
  after confirmed success or logout.
- **The central identity registry now has versioned migrations.** Startup takes
  a bounded, non-overwriting snapshot before upgrading an existing registry;
  schema changes and version stamps commit atomically. A failed or unsupported
  central migration leaves dependent work stopped and reports a detail-free
  degraded state through `/healthz`.

### Fixed
- **Native movement options now follow the selected theme.** The WOD confirmation
  picker gives its select, option, and optgroup surfaces explicit dark and light
  color schemes instead of opening a white options panel in dark mode.
- **Clean Feniks days no longer retain hidden watched-only measurements.** Saving
  clean clears minutes and edging while preserving the day note. Migration 030
  applies the same sanitation to existing rows.
- **Goal bootstrap and experiment metrics are replay-safe and unambiguous.** The
  current and legacy `cele.md` formats import idempotently, existing duplicate
  goals merge without losing focus, and duplicate metric names are renamed while
  preserving their IDs and logged entries. Future duplicate metric writes are
  refused.
- **Structured LLM calls no longer fail outright on OpenAI models.** Every
  caller pinned `reasoning_effort="disable"`, which Gemini accepts and OpenAI
  rejects with "Unsupported value". litellm's `drop_params` removes unsupported
  parameters, never unsupported values, so the call failed rather than degrading.
- **A job you ask for starts at once.** Enqueueing used to leave the work for
  the next scheduler tick, so clicking Generate could sit at "Waiting for a
  worker" for up to a minute before anything happened. The route now wakes a
  bounded worker pass: measured in a browser, the click is answered in 0.16s and
  the job is running 0.07s later. The tick remains the safety net, and
  `VIRGIL_WORKER_WAKE=false` restores the old behaviour. A woken pass is safe to
  race the tick, because claims are atomic and one runner per database is a
  database-level invariant.
- **The tray follows live work closely and idles quietly.** The poller travels
  inside the swapped content, so each response picks its own interval: every 3
  seconds while something is running, every 20 seconds when nothing is.
- **The tray shows what is queued, on every page.** It used to know only about
  the job named in the URL, so a refresh or a click elsewhere lost sight of
  background work entirely, which looks exactly like the job never having been
  enqueued. The tray now asks `/api/jobs/active` for everything still running,
  plus the outcome of whatever the current page started.
- **The A.N.D.Y. generate button posted the whole daily log as a GET.** Its form
  sat inside `#daily-form`, and one form nested inside another is invalid HTML
  that browsers flatten, so the button became a submit control of the outer
  form. It now names its own form with the `form` attribute.
- **Confirmations disappear on their own and stop moving the page.** Feedback
  and background-job cards share one fixed top-right tray outside the document
  flow. A success fades after four seconds, with hover and focus holding the
  timer open; errors and unfinished work still wait for the user. A job no
  longer grows a new section inside the page that started it, while Settings >
  Automation keeps the full history.
- **A brick can carry the day the urge happened.** The No Porn brick form posted
  a hidden date fixed to today, so a brick laid while reviewing an earlier day
  landed on the wrong day. The date is now a field, defaulted to the day on
  screen, capped at today and normalised before it is stored.
- **Opening an experiment no longer spends money.** The detail page bought a
  summary for every completed week that lacked one, guarded only by a
  process-local cooldown dictionary that neither survived a restart nor
  separated users. The scheduler queues one missing week at a time instead.
- **Medical uploads stay out of the job queue.** The bytes are staged under the
  user's own directory and the payload carries an opaque token, so a blood panel
  never reaches the jobs table, the status UI or the logs. The handler deletes
  them once the markers are stored and prunes anything left after a day.
- **Manual Oura sync no longer reports partial domain results as success.** The
  browser receives visible success, partial-failure, or exception feedback from
  the same native/HTMX contract.
- **Authenticated tests no longer contaminate anonymous tests.** `client` and
  `auth_client` use separate cookie jars on one application lifespan portal,
  with a regression that proves login state does not leak between them.
- **Concurrent WOD movement resolution is atomic** (migration 026).
  `training_exercises.name` now has a case-insensitive unique index and the
  resolver uses an upsert. Legacy race duplicates keep all linked training
  entries; a semantic conflict aborts migration rather than rewriting history.
- **UUID validation is canonical and shared.** Admin paths and session auth
  reject trailing newlines before any mutation or central database lookup.

### Security
- **The API key no longer follows the first active admin.** `VIRGIL_API_USER_EMAIL` is required whenever `VIRGIL_API_KEY` is set. If it is empty, or that account is missing or deactivated, `/api` and `/mcp` return 503 and startup logs the reason. The web UI keeps working. Upgrade: set the variable to the email the key should serve (previously the earliest active admin, usually the owner) and restart.
- **The TOTP secret is no longer placed in a URL during MFA setup.** `/mfa/qr.png` builds the QR for the signed-in user, and the setup page and image are `Cache-Control: no-store`.
- **Forwarded client IPs have an explicit trust boundary.** Rate limiting uses
  `CF-Connecting-IP` only when Cloudflare trust is enabled and the direct peer
  is listed in `VIRGIL_TRUSTED_PROXY_IPS`. Direct ingress ignores spoofed
  forwarding headers, and Compose defaults to the safe disabled state.

## [0.6.0] - 2026-08-27

### Added
- **Daily states read as words.** Each toggle carries `Done`, `Skipped` or
  `Pending` beside it and an `aria-pressed` state, the card states
  `n/7 complete today` (three routines plus four A.N.D.Y. tasks) and keeps that
  count in step as toggles change, the energy slider has `Low / OK / High`
  anchors, and a finished A.N.D.Y. task reads as content with an `Edit`
  disclosure instead of a permanent text input. Habit Streaks and the Completion
  Heatmap moved into one `Your trends` disclosure.
- **Oura leads with the baseline.** Today's readiness against the mean of the
  previous 7 days, with the delta and one word (`above`, `steady`, `below`, at a
  tolerance of 3 points). By decision this is a rule over stored numbers: no
  generated sentence and no LLM call. Today's Vitals group into Sleep, Activity
  and Recovery, and the four-series comparison chart moved last under
  `Compare metrics` - reordered rather than collapsed, because a Chart.js canvas
  inside a closed `<details>` renders at zero size.
- **The exercise library can be searched.** A search box matches on movement name
  and tags and opens the section holding a hit; a section filter narrows to one
  group; `Add exercise` moved into its own disclosure. Every row stays
  server-rendered and visible, so the filter is an enhancement rather than the
  only way to see the list.
- **The dashboard leads with today.** The first block is the check-in CTA and the
  A.N.D.Y. list with an explicit `n/4 done` count; Oura, measurements and Life
  Scores moved under an `Insights` heading, and the year calendar collapsed. The
  page used to open on the week strip, which says where you have been and nothing
  about what to do now. By decision there is no interpretation sentence: the hero
  is the next action, not a readiness verdict.
- **Goals has a current-focus set** (migration 025, `goals.active`). Starring a
  goal brings it into `Current focus`, whatever its area or horizon. The limit is
  advisory: above three the page says so and still saves the fourth, so no goal
  can be lost to a cap. The page now shows one horizon at a time behind a switch,
  areas with nothing in that horizon render as one compact line each, and a
  single `Add goal` flow asks for area and horizon. The empty state used to be 24
  identical inputs and about 3356 px on a phone.
- **The experiment detail page asks one question.** The one-tap today log stays on
  screen; the generic form moved behind `Different date or more detail`, and it
  renders open when a duration-only experiment has no one-tap path. The stats bar
  is outcome, target and `DAYS LEFT`, with `Week n of m` in words below and the
  remaining metrics under `All metrics`. Complete, Abandon and Delete moved behind
  an `Actions` disclosure.
- **Blood Work reads on a phone.** A `.bw-list` shows each marker's latest value,
  its H/L status and the signed change from the result before it, plus the chart
  link. The full date matrix stays: directly on a wide screen, and under
  `All results` on a phone, where three metadata columns used to fill the
  viewport and push every value off-screen. One Jinja macro renders the matrix
  for both, and two utilities (`.on-mobile`, `.on-desktop`) switch the views
  with CSS alone. The change is reported only when both results are numeric: a
  text result such as "negative" has no direction.
- **A stranded session can still gain entries.** `POST /training/session/{id}/manual`
  arms an empty parse, so a note left with no entries and no pending parse (a
  container recreated between `capture_wod`'s two commits) gets the same manual
  rows a failed parse does. `/training` offers the button on exactly those rows.
- **Unfinished captures stay independent of history.** A bounded `Niedokończone`
  card on `/training` shows pending parses whatever their date or the selected
  history year, and reports when more remain. A backdated capture used to fall
  off history together with the one link that leads back to its confirm screen.
- **One capture per click** (migration 024). The capture form carries a
  `capture_token` and a partial unique index enforces one session per token, so
  a double submit or an F5 costs one session and one paid parse. A reused page
  submitting DIFFERENT text is treated as a new capture, not a replay, so the
  back button cannot silently drop the note the user just wrote.
- **The movement picker can be searched.** Options group by `<optgroup>` per
  section, carry their tags, and mark recently logged movements, which Alpine
  promotes into an `Ostatnio używane` group. A per-row search box filters on
  name or tag. The submitted control stays a native `<select>`, so the screen
  works without the CDN and can only ever submit an exact library name.
- **The planner scores the swim target.** `schedule_block` labels each logged
  session `swim` or `training`, derived from the `swim` tag on its movements,
  and states `Swims this week: n of m`. The target was unscorable before.
- **Per-set notes have readers.** The metcon result now reaches the `/training`
  history table, `GET /api/training/detail`, the MCP `get_training_detail`
  docstring and the markdown export.
- **No Porn rebuilt as a single flow** (migrations 022 + 023). The page asks one
  question — *Clean day* or *I watched* — and watching reveals minutes, an
  edging toggle and an optional one-line note (`feniks_daily`, upsert per date).
  A day-based relapse counter cannot see edging (hours of sustained use log as
  one event), so this is the honest metric. `used=1` records the day's relapse
  `pmo_event` once (idempotent); correcting the day back to clean removes only
  that marker — the streak and weekly 75% clean-rate keep working unchanged.
- **Bricks (No Porn)** — `feniks_bricks`: a brick = one urge survived, captured
  in Gola's brick structure: memory hook (required), craving 0-10, story.
  Bricks — not clean-day streaks — are the hero number; the streak and a
  Monday-to-Sunday week strip show below it.
- **Unified timeline (No Porn)** — days and their bricks in one feed, replacing
  the Journal / Bricks / Pleasures tabs. The journal and pleasures tables (and
  their historical data) remain in the DB and the API; only their forms are
  retired from the UI, along with the separate relapse form.
- **`GET /api/noporn` carries `daily`, `bricks`, `bricks_total`** (still gated
  behind `VIRGIL_API_SENSITIVE`); the MCP `get_noporn` docstring documents both.

### Fixed
- **Training capture copy describes the action.** `Zapisz i sparsuj` became
  `Zapisz trening`, duration is marked optional, and a line states that the note
  is saved before parsing - the property that makes a failed parse harmless.
- **`% elapsed` measures elapsed time.** The experiments list divided completed
  WEEKS by total weeks, so the whole of week 1 read `0% elapsed` next to a
  `Week 1/4` label saying the opposite. It counts days now.
- **Training weekly KPIs and PBs fit a phone.** `training.html` pinned three
  columns in an inline style, which outranks the stylesheet's own two-column
  mobile rule, so the values were cut by the card's `overflow: hidden`. The
  stylesheet owns the column count again, `.stat-card` may shrink below its
  content (`min-width: 0`), and padding and the mono value scale down below
  768 px. A test refuses a fixed inline column count in any template.
- **Card headers stack below 768 px.** A title and a long helper sentence shared
  one flex row, which squeezed the title into a one-word-per-line column in
  Settings App Config.
- **A rejected field no longer discards the rest of the form.** `confirm_wod`
  answered `_ConfirmRejected` with a redirect to a clean GET, which rebuilt the
  form from the stored parse and threw away every other edit, rows the user had
  added included. It re-renders the submitted rows instead, with the message
  inline. That path answers a POST with 200: it writes nothing and leaves
  `wod_parsed` armed, so a refresh earns the same refusal.
- **A partial resolve says what it skipped.** Writing 1 of 2 rows redirected
  with a success page and no message. `/training?msg=` now names the movements
  that no longer resolve. A blank movement stays silent - that is the deliberate
  skip.
- **A big WOD can be saved.** The parser bounds its own output at
  `MAX_PARSED_ENTRIES` (200, pinned to the confirm limit by a test) and the
  confirm screen states how many rows it dropped. A note parsing to 250 entries
  could previously only be discarded.
- **The markdown export carries `duration` and per-set `notes`.** It dropped the
  one column that had just gained a canonical unit (seconds, migration 020).
- **An unset training schedule reads as unset.** `DEFAULT_DAYS` and
  `DEFAULT_SWIM` are empty, so a new user's planner is told "No fixed training
  days set." instead of a Mon/Wed/Fri week nobody chose.
- **A backslash in an error toast no longer truncates the message.** The `?err=`
  value went into a JavaScript string literal; both toasts use `|tojson`.
- **JSON export honours its "every user-owned table" invariant again** -
  `exercise_library_tags` (missing since migration 019) plus the new
  `feniks_daily` and `feniks_bricks` are exported.
- **Tags replace `exercise_library.category`** (migration 019, `exercise_library_tags`
  join table). A movement can carry several free-form tags (`kettlebell`, `warmup`, ...)
  or none — tags organise the Settings picker without gating what the WOD parser may
  recognise, unlike the single category they replace.
- **`?tag=` filter on `GET /api/library`** — filters to entries carrying the given tag
  (normalised the same way tags are on write: lowercased, transliterated, kebab-cased).
  Each returned entry now carries its own `tags: list[str]`.
- **MCP tag parameters** — `get_exercise_library` takes `tag` to filter; `add_exercise`
  and `update_exercise` take `tags: list[str]` (`update_exercise`'s replaces the full set).
- **Comma-separated tags input** in Settings → App Config, with a datalist of
  existing tags. (This release also shipped tag-filter chips in the `/training`
  exercise picker. That picker was part of the protocol table removed later in
  the same release — see Removed — so the chips never reached a tagged release.)
- **ASCII transliteration for tags.** `normalize_tag()` maps Polish letters that
  NFKD + ASCII-encoding alone would silently delete (`ł`, `đ`, `ø`, `æ`, `ß`) before
  folding, so `siłowy` becomes the tag `silowy` instead of vanishing to `siowy`.

### Changed
- **The WOD confirm screen renders one row contract.** Parsed entries, unmatched
  names and blank seed rows come from one numbered list built in the router
  (`_confirm_rows`), and `app/templates/partials/wod_row.html` owns the row and
  the picker. Four copies of that markup used to exist, each with its own index
  arithmetic and its own flat option list.
- **The schedule block is sport-neutral.** `Training days:` rather than
  `CrossFit days:`, so the copy survives a change of sport.
- **The A.N.D.Y. planner is given a weekly schedule instead of an exercise
  prescription.** The prompt used to list every non-archived, non-ad_hoc row of
  `training_exercises` as `- <name>: <sets>x<reps>`. Those rows outlive the
  program that created them by design, so the block kept describing a basement
  kettlebell routine after training had moved to a CrossFit box — and removing
  the UI alone would have hidden that rather than fixed it. The planner now
  reads which days are training days, whether today is one, and what has
  actually been logged this week.
- **Training schedule is configurable** in Settings → Configuration
  (`training_days`, `training_swim_per_week` in `app_settings`, defaulting to
  `mon,wed,fri` and `1`). Kept out of code deliberately: the schedule changed
  once within a week of first being written down, and this deployment
  auto-pulls images unattended.
- `/training` renamed its capture card from "Zapisz WOD" to "Zapisz trening" —
  the parser resolves against the whole library, so a swim or a walk goes
  through the same box a WOD does.
- `settings.py` reads `LIBRARY_SECTIONS` for its section grouping; `SECTION_ORDER`
  in `training.py` was a second copy of the same four values and is gone.

- **`category` removed from the API — breaking.** `GET /api/library` no longer
  returns a `category` string; `POST /api/library` and `PATCH /api/library/{id}`
  no longer accept one — both request models reject unknown fields, so a
  `category` key in the request body now fails with 422. Use `tags` instead.
- **Exercise names are unique library-wide** (migration 019,
  `UNIQUE(name COLLATE NOCASE)`) — a name can no longer exist twice under
  different categories. Existing same-name duplicates were merged into one row
  on upgrade (their tags merged together), preferring whichever row matched the
  seeded CrossFit vocabulary.
- **Settings → App Config's library listing is grouped by section**
  (Warmup/Core/Cardio/Stretching) instead of by category.

### Removed
- **The training protocol table, the per-set log form and the rest timer**
  (`/training`). All three assumed a fixed prescription followed at home; a
  class-based session is programmed by the box, so a free-text note is the
  record. Gone with them: `POST /training/session`, `POST /training/exercise`,
  `POST /training/exercise/{id}/edit`, `POST /training/exercise/{id}/delete`.
  The `training_exercises` table stays — `training_entries` references it, so
  every logged set past and future depends on it, and the parser keeps creating
  rows there — but it no longer has a UI or a prescription role.

### Fixed
- **A failed parse now leaves a usable manual-entry path.** It offered one blank
  row, whose only labelled action was "+ dodaj serię" - another *set* of one
  movement. A WOD is never one set of one movement, and reaching a second
  *exercise* meant adding a "set" and then changing its select, so the only
  labelled way in described the wrong action. Five rows are now seeded, and a
  distinct "+ dodaj ćwiczenie" appends a row with a blank movement at set 1.
  The row count is server-rendered because the add-row buttons are Alpine, which
  loads deferred from a CDN with no vendored fallback: on the exact screen a
  parse failure lands on, those rows are the only manual entry that survives the
  script not arriving. Unfilled rows are skipped on submit, as the copy now says.
  Both the template and `confirm_wod` take the row bound from one constant
  (`MAX_CONFIRM_ENTRIES`), so the client stops offering rows at the number the
  handler accepts - past it the whole submission is rejected, which would have
  cost the user every row they had typed.

- **A WOD note long enough to matter no longer parses to nothing.** The reported
  session was a warm-up, 6 snatch singles and "Cindy" (an AMRAP whose 7 rounds
  expand, by design, to one entry per round). At ~28 entries it is the
  token-hungriest input the parser sees, and it was the only structured LLM call
  in the app passing neither `reasoning_effort` nor a budget sized for its own
  output. Omitting `reasoning_effort` is not the same as leaving a default alone:
  litellm then sends no `thinkingConfig` at all, so Gemini 3 Pro thinks at its
  own default level and spends that inside `max_tokens`. It consumed nearly the
  whole 4096-token allowance and the response arrived cut off after 837
  characters, mid-object. Now capped to the cheapest thinking level the model
  family offers and budgeted at 16384. `"disable"` is aspirational and
  deliberately so - litellm clamps it to `thinkingLevel: low` because Gemini 3
  Pro cannot turn thinking off, which is why the cap is generous rather than
  merely sufficient. A test pins that mapping so a litellm upgrade that changes
  it fails loudly instead of silently.

- **A truncated response no longer costs the whole session.**
  `parse_andy_response` repaired truncation by appending a single `}` or `"}`,
  which closes exactly one level. That was enough for the flat 4-field A.N.D.Y.
  object it was written for, but the WOD payload nests three deep
  (`{"entries": [{...}]}`), so the repair was silently dead code for that caller
  and 25 correctly-parsed movements were discarded whole. It now scans for every
  structural boundary where an element ends and works back from the truncation
  point, cutting the incomplete tail and closing whatever is still open, so the
  longest repairable prefix wins. The half-written entry keeps the fields that
  did arrive. Attempts are bounded: each one re-parses the response, and on a
  body that is malformed mid-document rather than merely cut short that was
  quadratic - 1.9s on 32 KB, where the cap permits roughly twice that length.

- **`Air Squat` was missing from the WOD vocabulary** (migration 021). Migration
  016 seeded Back, Front and Overhead Squat but no bodyweight squat. That
  vocabulary is closed and the prompt forbids guessing a near match - correctly,
  or the catalogue rots - so "15 squats" had nothing to map to and came back as
  `unmatched`. Air Squat is a third of Cindy and a fifth of Murph, so the gap
  cost most of any benchmark WOD logged. `INSERT OR IGNORE`, so a user who
  already added the movement keeps their own metric and section.

- **`training_entries.duration` is seconds, and now reads that way.** The history
  row printed the raw value with a literal " min", so a 69-minute ride stored as
  4140 rendered "4140.0 min" beside a header that correctly said "69 min".
  Relabelling alone would have been wrong: the deleted per-set log form wrote
  *minutes* for Warmup/Cardio/Stretching and seconds only for Core+`time`, so a
  3-minute jump rope sits in the column as `3` and would have become "3 s".
  Migration 020 converts the legacy minute rows using a structural rule — which
  branch of the old writer produced each row — rather than guessing from
  magnitude, where minutes and seconds overlap across 30-120. Verified against
  every duration-carrying row in the deployed database; 18 convert, 17 stay.

- **A submission that resolves no movements no longer strands the session.**
  `confirm_wod` nulls `wod_parsed` to make a replay a no-op, and the confirm GET
  redirects away once it is NULL — so a submission that wrote nothing took the
  parse with it and left no route back. Two earlier attempts guarded on whether
  any row *named* a movement, which is not the predicate the write uses:
  `resolve_movement` also returns None for a name that resolves to nothing — a
  client can post any string, and archiving an `exercise_library` row breaks
  resolution for any movement not already in `training_exercises`. The parse is now re-armed when nothing resolved, and
  "Odrzuć parsowanie" is the explicit way to end with the note and no entries.
- **Sessions with an unfinished parse are linked from `/training`.** The confirm
  screen told the user to come back later; nothing on the page led back to it.
- **A failed parse no longer strands the workout.** `POST /training/wod/confirm`
  is the only route that writes `training_entries`, and its form was gated on
  having at least one parsed row — so with the LLM unavailable there was no form,
  nothing for "+ dodaj serię" to clone, and weekly volume and Personal Bests
  silently stopped accruing. The form now always renders, seeding one blank
  editable row. The message that told the user to delete the session and log the
  workout manually pointed at a path that had just been removed; it now points at
  the row above it.
- **The swim target is validated like the day list.** `abc` or an out-of-range
  value used to be stored as `0` while reporting success, dropping swimming from
  the plan on a typo. Both are rejected now, and both fields are checked before
  either is written — the two `set_setting` calls commit separately, so there is
  no transaction to roll back.

- **WOD parser vocabulary widened from CrossFit-only to the whole exercise
  library.** `canonical_movements()` and `resolve_movement()` both filtered
  `category = 'CrossFit'`, so the parser only recognised 31 of the 77 rows in
  `exercise_library` — a real session's warm-up and stretching (Band
  Pull-apart, Goblet Hold, Hamstring Stretch, ...), the kettlebell program,
  and the Gym-classics barbell lifts came back "unrecognised" even though
  they were already in the library under other categories. Both functions now
  scope to every non-archived row. (A name could briefly exist under two
  categories at the time — `Back Squat`, `Bench Press`, `Deadlift`,
  `Pull-up` — until migration 019 below made names unique library-wide and
  merged those duplicates for good.)
- **WOD confirmation screen can no longer strand extra sets.** The screen only
  ever let you submit exactly the rows the server rendered — an unrecognised
  movement was synthesised as exactly one row, and the same limit applied
  whenever the parser under-counted sets, with nowhere to add what was
  missing. Each row now has a "+ dodaj serię" (add a set) control that appends
  an editable row at the end of the table (pre-selecting the source row's
  movement, `set_number` + 1), and the hidden `entry_count` submitted with the
  form now reflects the live row count instead of the server-rendered
  constant — `confirm_wod` loops `for i in range(entry_count)`, so a stale
  count used to silently drop any row added this way.

## [0.5.0] - 2026-07-31

### Added
- **CrossFit WOD tracking.** A free-text note written after training is parsed by
  an LLM into training entries, constrained to a seeded CrossFit movement
  vocabulary. The raw note is saved before parsing and stays the source of truth;
  entries are written only after an on-screen confirmation.
- `training_exercises.ad_hoc` (migration 016): movements created by the WOD parser
  keep their history, volume and PB contribution but stay out of the daily
  protocol form.
- **Exercise dictionary over REST and MCP** — `GET/POST /api/library`,
  `PATCH/DELETE /api/library/{id}`, surfaced as `get_exercise_library`,
  `add_exercise`, `update_exercise` and `delete_exercise`. Builtin entries may be
  archived but not edited or deleted, matching the Settings UI. The CrossFit
  vocabulary is no longer builtin (migration 017), so it can be curated —
  which also changes what the WOD parser is allowed to recognise.
- **Post/Redirect/Get on WOD capture** (migration 018, `training_sessions.wod_parsed`).
  Refreshing the confirmation screen used to create a second session *and* a
  second paid LLM call; the parse result is now cached and the redirect replays
  it for free.
- **`metric` is editable in Settings → App Config.** The form previously had no
  control for it, so a movement added through the UI silently defaulted to
  `reps` — including erg work that should be `time`, which then polluted the
  weekly rep count.
- **A single write contract for the exercise library** (`app/library_validation.py`),
  shared by the Settings form and the REST API. The two previously disagreed on
  every failure: the form silently coerced an invalid `section`/`metric`, ignored
  duplicates and no-op'd edits to protected rows, while the API returned 422/409.

### Fixed
- Confirming a WOD twice no longer writes the entries twice. The confirmation
  consumes its cached parse, so a browser Back-and-resubmit cannot double weekly
  volume.
- Out-of-range values submitted from the confirmation screen are rejected loudly
  instead of being silently dropped. Previously an over-large `entry_count`
  discarded the entire reviewed submission and still redirected as if it had
  saved.
- A movement added from the picker now inherits its real `metric` from the
  library. Previously the picker path dropped it, so the same movement behaved
  differently depending on which route created it — and, because a WOD reuses an
  existing row by name, whichever route ran first won permanently.
- A parse failure that is not a `ValueError` no longer strands the session. The
  note is still saved, the confirmation screen still renders, and corrupt cached
  parse data no longer makes that page fail permanently.
- Wrongly parsed entries can be dropped on the confirmation screen; previously
  only unrecognised movements could be skipped.
- A WOD naming an archived movement reactivates it instead of quietly feeding
  volume and Personal Bests from a retired exercise.
- The CrossFit vocabulary is bounded, so an automated client looping on
  `add_exercise` cannot grow the parser prompt until every parse fails.

### Changed
- **Weekly volume is no longer continuous with pre-CrossFit numbers.** Metcon work
  counts as real Core volume — a single 21-15-9 thruster couplet at 43 kg is
  ~1935 kg, against 2880 kg for all of July 2026. This is intentional: it is
  genuine barbell volume, and excluding it to keep the chart comparable would
  invert the purpose of the metric.
- `docs/superpowers/` (design specs and implementation plans) is no longer
  tracked; it is working material, not something the repository needs to carry.

## [0.4.0] - 2026-07-19

### Added

- **General experiments** (migration 015) — experiment metrics now have kinds: `duration` (minutes, Oura auto-import), `count` (events), `boolean` (daily yes/no, one row per day), `scale` (0-10 rating). Count/boolean metrics carry their own target (value per day/week/whole experiment). Entries store a generic `value` (replaces `duration_minutes`)
- **Experiments quick-log bar** — one-tap Today logging (✓/✗, `+1` with a note, 0-10 input); day-grid cells show per-metric markers; week rows and stats are kind-aware
- **Experiment edit page** (`/experiments/{id}/edit`) — works for any status (active/completed/abandoned): title, description, dates, status, and metric add/rename/retarget/delete (kind is immutable). `num_weeks` changes resync week rows preserving edited targets/labels
- **Experiment logging over the API** — `POST /api/experiments/{id}/entries` (the API's single write, X-API-Key auth) + MCP tool `log_experiment_entry`; `GET /api/experiments/active` now returns per-metric progress (logged today/week/total vs target)
- **Settings → App Config** — dictionary-table management (exercise library): add/edit/delete your own entries; built-in entries are archive-only (hidden from the Training picker, never deleted)

### Fixed

- **Oura reconcile is user-scoped** — it deletes only THIS user's stale subscriptions (current/previous id, legacy endpoint, unowned orphans); other users' active callbacks on a shared OAuth app are preserved
- **Startup survives a corrupt user DB** — `open_user_db` failures degrade that account via `/healthz` instead of aborting the whole lifespan
- **Webhook debounce race** — simultaneous Oura deliveries scheduled N sequential syncs; now an atomic pending-set guarantees at most one per user
- **A.N.D.Y. truncated-JSON failures** — max_tokens raised to 8192 for generation (thinking models with dropped `reasoning_effort` ate the 2048 budget) and truncated objects are repaired instead of rejected
- `virgil.md` export ownership no longer flips when the first account is disabled (primary = oldest account, active or not)

### Added

- **Pre-migration DB snapshots** (`data/backups/pre-migration/*-pre-migration-v<NNN>.db`) — migrations are one-way; this is the rollback path an image revert can't provide. Version-keyed and never overwritten (a retry after a failed migration can't destroy the pristine copy), stored outside the rotating-prune namespace, capped at 3 per database
- **Central registry backups** — `virgil-central.db` (identities, MFA, webhook routes) backed up daily by the scheduler; per-user backups never covered it
- **Backups enabled by default** with UTC-timestamped filenames (hourly schedules no longer overwrite one date-named file); migration 014 flips existing installs to on (opt-out policy)
- **Ordered releases** — GitHub Actions `concurrency` prevents a slow older run from overwriting `:latest` with stale code
- README documents deployment semantics honestly: Watchtower is best-effort auto-update, not health-gated rollback

## [0.3.0] - 2026-07-13

> **Deployment notes:** rotate any credentials that were in `.qnap.setup`; registration now
> defaults to closed (`VIRGIL_REGISTRATION_OPEN=false`, first account always bootstraps);
> `/api/noporn` requires `VIRGIL_API_SENSITIVE=true`; Oura webhooks must be re-enabled and —
> behind Cloudflare Access — need a **Bypass policy for `/api/oura/webhook/*`** (Oura's
> verification challenge and event deliveries are unauthenticated calls, HMAC-verified by the app).

- **Oura webhook protocol corrected against the live OpenAPI spec**: subscription management uses `x-client-id`/`x-client-secret` headers (was Bearer — every subscribe would have been rejected); verification is a GET challenge answered with `{"challenge": ...}`; event signatures verified as HMAC-SHA256(client_secret, timestamp + body), case-insensitive hex; event sync runs as a debounced background task inside Oura's 10-second response deadline; partial subscription coverage is surfaced to the user
- **Migration 007 no longer bricks legacy databases** — it rebuilds `llm_providers` without the provider CHECK before the claude→anthropic rename (upgrade test from a real pre-007 DB with a Claude row)
- **Factory reset provisions a NEW database filename** and repoints the central registry before deleting the old file — recreating at the same path raced live connections (SQLite WAL unlink hazard)
- **Markdown export filenames are derived from account identity** (primary keeps `virgil.md`, others get `virgil-{id}.md`) — user-chosen shared filenames allowed cross-user overwrite by construction
- Nested `<form>` removed from the Automation tab (Backup Now uses `formaction`) — invalid HTML that made Backup Now submit automation settings
- Bootstrap signup made atomic (guarded INSERT — two concurrent first signups can't both win); central account rolled back if user-DB provisioning fails
- Login with an empty password returns the normal error instead of 500; CSRF token comparison no longer 500s on non-ASCII input; webhook JSON shape validated pre-auth (no 500s)
- Startup migrations cover ALL users (disabled accounts no longer wake up with stale schemas)
- `/api/training/detail` groups sets by exercise id (duplicate names no longer merge) and returns `id`
- Central TOTP secrets Fernet-encrypted at rest (legacy plaintext migrates on next MFA enable); OAuth-state cookie gets `Secure` under HTTPS; `busy_timeout=5000` on every SQLite connection; `Retry-After` sleeps bounded to 60 s and parse-safe; `sync_log` included in JSON/CSV export

### Security

- **`.qnap.setup` excluded from the Docker build context** — the file can carry live deployment credentials (rotate any credentials that were in it)
- **Registration closed by default** (`VIRGIL_REGISTRATION_OPEN=false`); the first account (bootstrap owner) can always be created
- **Service worker no longer caches authenticated HTML** — dashboards/journals are no longer readable offline after logout
- **`/api/noporn` gated behind `VIRGIL_API_SENSITIVE=true`** (intimate journal content is opt-in)
- **Webhook secrets encrypted at rest**; CSRF tokens compared in constant time; login burns a dummy bcrypt verify for unknown emails (timing)
- Session cookie moved from `SameSite=Strict` to `Lax` so the Oura OAuth callback keeps its session (state-changing routes remain CSRF-protected)

### Fixed

- **Factory reset** no longer strands the account: the per-user DB is recreated and migrated, and the user is sent back to onboarding (previously: deleted DB + redirect to nonexistent `/setup`)
- **Multipart CSRF** — medical-PDF onboarding uploads were always rejected 403 (`parse_qs` cannot parse multipart); upload limits unified (20 MB)
- **Multi-user Oura webhooks** — per-user callback URLs (`/api/oura/webhook/{id}`) routed via a central registry instead of the retired global DB; subscriptions now register the handled data types (was `tag.updated`, which the handler ignored)
- **Partial Oura sync no longer erases data** — columns from failed endpoints keep their stored values instead of being overwritten with NULLs
- **Onboarding's suggested experiment is actually created** — targets go to `experiment_weeks` (+ a default activity type); previously the INSERT hit nonexistent columns and was silently swallowed
- **`llm_providers` CHECK constraint removed** (migration 012) — unblocks `anthropic`/`mistral`/`groq`/`ollama` providers and migration 007's rename
- **Internal LLM fallback recognized everywhere** — Daily A.N.D.Y. button and experiment summaries now work with only `VIRGIL_INTERNAL_LLM_KEY` set (`llm_available()`)
- **PWA icons committed** — the `Icon?` gitignore rule swallowed `app/static/icons/` on case-insensitive filesystems, breaking SW install on fresh clones
- **Backup Now** reports the real outcome (was an HTMX fire-and-forget that showed "started" even on failure)
- Deleting a training exercise archives it instead of erasing all its historical entries and PBs (migration 013)
- Empty and negative workout submissions are rejected server-side
- Bloodwork: out-of-range flags computed from reference ranges (manual override still wins); unknown marker ids no longer 500
- Dashboard radar only plots complete life-score assessments (missing areas rendered as fake zeros)
- Experiments: inverted weekly targets normalized at creation; completed/abandoned experiments can be reopened; start date prefilled

### Added

- **Per-user markdown export filename** (Settings > Data) — multi-user deployments no longer overwrite each other's `virgil.md`
- **Scheduled morning briefing** — the existing Automation toggle now actually generates the briefing once per day (after 06:00, 1 h failure backoff)
- **`/healthz` endpoint** (503 while any user DB failed startup migrations) — wired into the Docker healthcheck
- JSON/CSV export now includes `user_profiles`, `experiment_weeks`, `experiment_summaries`, `daily_briefings`, `exercise_library`, `app_settings`
- `/api/training/detail` batches entry queries (N+1 removed)
- **CI/CD pipeline**: GitHub Actions (`release.yml`) builds and pushes `ghcr.io/krzysztofbury/virgil`
  (`latest` + per-commit `sha-<short>` + semver tags) after a full lint/test gate; `watchtower`
  service on the NAS auto-deploys new images (label-scoped, 5-min poll, healthcheck-gated).
  No more building on the NAS or QSync-ing the repo. `ci.yml` now covers PRs/feature branches only.

## [0.2.0] - 2026-03-21

### Added

- **Multi-user architecture** with per-user isolated SQLite databases (`data/users/{uuid}.db`)
- **Central auth service** — user registry in `virgil-central.db`, signup/login against central DB
- **Signup page** (`/signup`) with email, display name, password — creates user + per-user DB
- **Admin panel** (`/admin/users`) — list, disable, enable, delete users (admin role required)
- **Admin role system** — super-admins via `VIRGIL_ADMIN_EMAILS` env var, promotable via admin panel
- **Registration control** — `VIRGIL_REGISTRATION_OPEN` env var to open/close signups
- **6-step onboarding wizard** (`/onboarding`) — profile, ideal day, goals, habits, medical records
- **LLM-powered onboarding enrichment** — generates realistic day, goal levels (10yr/3yr/1yr), experiment suggestion, Feniks auto-detection
- **LiteLLM integration** — unified LLM provider access replacing hand-rolled HTTP clients
- **Internal LLM provider** — `VIRGIL_INTERNAL_LLM_MODEL` + `VIRGIL_INTERNAL_LLM_KEY` for system features
- **Expanded LLM provider dropdown** — Anthropic, OpenAI, Gemini, Mistral, Groq, Ollama, Other (LiteLLM)
- **Medical record import** — PDF upload via multimodal LLM or free-text parsing into blood markers
- **Factory reset** in Settings > Security — wipes user DB for fresh start
- **Migration script** (`scripts/migrate_to_multiuser.py`) — converts single-user installs to multi-user

### Changed

- Auth middleware rewritten for multi-user (UUID sessions, per-user DB per request)
- All routers now use `get_user_db_from_request(request)` instead of global `get_db()`
- Scheduler iterates over all active users for per-user tasks
- Feature flags loaded per-user instead of global cache
- Typography upgraded from Inter to DM Sans + JetBrains Mono
- Color palette replaced with custom "Midnight Observatory" theme (teal accent #2cb67d)
- Stat values use solid color + mono font instead of gradient text

### Security

- bcrypt pre-hashed with SHA-256 to prevent 72-byte truncation
- SQL injection prevented via column whitelist in `update_user`
- Path traversal guard on per-user DB filenames
- `/signup` added to rate limiter auth tier (10 req/min)
- UUID format validation on session payloads
- Admin self-disable/self-delete prevention
- HSTS header when behind HTTPS
- `CF-Connecting-IP` used for rate limiting behind Cloudflare

---

## [0.1.0] - 2026-03-21

### Added

- **Dashboard** with weekly completion stats, life score radar chart, Oura vitals, 7-day sparklines, year calendar, and AI morning briefing
- **Daily Log** with energy tracking, morning/evening routines, A.N.D.Y. task system (AI-generated daily tasks), body measurements, markdown notes, streak counters, and 12-week heatmap
- **Training** with 4-section exercise protocol (Warmup/Core/Cardio/Stretching), exercise CRUD, section-specific logging, KPI cards, personal bests, rest timer
- **Feniks** 90-day personal development program tracker with streak hero, progress graph, journal, pleasures, and milestones
- **Oura Ring Integration** with OAuth2 connection, automatic daily sync, real-time webhook support (HMAC-SHA256 verified), rate limit handling
- **Bloodwork** tracking with marker categories, reference ranges, flag indicators, trend charts
- **Life Scores** periodic self-assessment across 8 life areas with radar chart
- **Goals** mapping across 8 life areas with 1yr/3yr/10yr horizons and inline editing
- **Experiments** with time-boxed activities, weekly targets, color-coded activity types, day-by-day grid, AI weekly summaries, Oura workout auto-import
- **Settings** with 5-tab layout (General, Integrations, Data, Automation, Security)
- **Authentication** with email + password (bcrypt), optional TOTP MFA, signed cookie sessions (7-day expiry)
- **Security middleware** — CSRF protection, rate limiting (120/min general, 10/min auth), security headers (CSP, X-Frame-Options, etc.)
- **Encryption at rest** for OAuth tokens, LLM API keys, and webhook secrets (Fernet)
- **Database migration system** with 6 versioned migrations
- **Background scheduler** for automated backups, Oura sync, and markdown export
- **Markdown export** with scoped output (weekly/monthly/yearly/all) for LLM-based reviews
- **Markdown import** for bootstrapping from existing Second Brain files
- **LLM integration** supporting Claude, OpenAI, and Gemini APIs
- **PWA support** with service worker (cache-first for static, stale-while-revalidate for CDN, network-first for pages)
- **Dark/light theme** with localStorage + server sync, theme-aware charts
- **Keyboard shortcuts** with `g`-prefix navigation and `?` help overlay
- **Swipe gestures** for mobile day navigation
- **Docker deployment** with Cloudflare Tunnel support for QNAP NAS

### Security

- Fixed OAuth callback missing `state` parameter (CSRF protection)
- Migrated LLM API keys from plaintext to Fernet encryption
- Added CSRF double-submit cookie protection on all POST forms
- Secured encryption key file permissions (0600)
- Required HMAC signature verification on webhook payloads
- Removed public MFA QR endpoint, validated URI scheme
- Fixed rate limiter memory leak with bucket eviction + 10K IP cap
- Replaced unstable `_signing_key` access with SHA-256 key derivation
- Added input length limits on all text fields
- Moved Gemini API key from URL query param to header
- Fixed XSS via exercise name in `confirm()` dialogs

### Fixed

- Dashboard, training, bloodwork, and experiments N+1 query patterns
- Hardcoded years/dates replaced with dynamic computation
- Variable shadowing in for loops
- Experiment summary LLM cooldown (5-minute per-experiment)
- Single DB connection health check with reconnection
- KPI volume calculation filtered to Core section only
