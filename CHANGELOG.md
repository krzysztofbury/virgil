# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
- **Structured LLM calls no longer fail outright on OpenAI models.** Every
  caller pinned `reasoning_effort="disable"`, which Gemini accepts and OpenAI
  rejects with "Unsupported value". litellm's `drop_params` removes unsupported
  parameters, never unsupported values, so the call failed rather than degrading.
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
- **Every unfinished capture is listed.** A `Niedokończone` card on `/training`
  shows every session with a pending parse whatever its date, and the history
  below it is paginated (`?page=`) instead of capped at the newest 20. A
  backdated capture used to fall off that list together with the one link that
  leads back to its confirm screen.
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
