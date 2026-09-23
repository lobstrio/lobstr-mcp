# Changelog

All notable changes to `lobstr-mcp` (the server behind `mcp.lobstr.io`) are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) and are pre-1.0, so a minor bump may change behaviour.
The number lives in `pyproject.toml` and is read through `lobstr_mcp.__version__`.

## [0.4.2] - 2026-09-23

### Fixed

- `run_scraper` no longer refuses a run for credits on its own arithmetic. Since 0.4.0 it compared
  its own cost estimate against `remaining` (available minus consumed) and refused with
  `insufficient_credits` whenever the estimate exceeded it. The API never makes that comparison: it
  refuses only an ordinary account whose period spend has reached its allowance
  (`NotEnoughCredits`), and a staff/admin account runs regardless, including at a negative balance
  — so this client's own guard blocked admin runs the API would have accepted. The client-side refusal is removed entirely:
  `run_scraper` always attempts the run once `confirm` passes, and a real API refusal is surfaced
  through the normal structured-error path (`errors.to_error_dict`, unchanged) exactly as any other
  upstream rejection is. The estimate is still returned; `remaining` (still computed, still
  net-of-consumed) now rides on every response, success or failure, and a thin-but-affordable
  balance becomes at most a one-line `credit_warning` on a successful run — never a refusal.
  **A caller that relied on the old pre-run `insufficient_credits` (estimate vs. balance) now gets
  the API's own refusal instead, at the same point in the call** — the error code and shape are the
  same, only the source of truth changed.

  The `replace_tasks=true` destructive rewrite can no longer be skipped ahead of a predicted
  refusal (there is no longer a prediction to skip it for): credits can only be refused by
  `POST /runs`, which necessarily runs after the rewrite has already replaced the squid's saved
  rows. A refusal at that point now restores the rows it deleted, the same way every other failure
  past that point in `_replace_squid_input` already does, and reports `tasks_lost`/
  `tasks_restored` exactly as those do — so "nothing destructive before a refusal" becomes "a
  refusal past that point is undone and said plainly," which is what happens for a rejected
  parameter today.

- Trimmed the multi-sentence notes `run_scraper` added in 0.4.0 (and `estimate_run`'s
  `estimate_note`/`estimated_time_note`) to at most one short sentence each; detail that was only in
  prose moved to structured fields. `squid_has_tasks`'s four-option paragraph is now a short
  `message` plus a structured `options` list of ready tool-call strings and
  `cost_multiplier_if_added`; no structured field any caller already reads (`tasks_action`,
  `task_count`, `error_code`, `retry_safe`, `request_state`, `estimate`, `remaining`, …) was
  removed. Measured on the same two shapes: a typical successful `run_scraper` response was 474
  chars, now 493 (it gained `remaining`, not prose); a `squid_has_tasks` refusal was 947 chars, now
  691 (-27%). A loose regression test (`tests/test_run_scraper_response_size.py`) keeps both under
  a ceiling so the prose can't silently regrow.

## [0.4.1] - 2026-09-23

### Added

- `search_scrapers` gains a `similar` field: up to 5 ranked, partial-match crawlers (BM25 over
  name, slug and description — reused, unchanged, from the shelved ranking spike)
  for when a query's words are not all in any one crawler's listing. `results` keeps today's exact,
  strict matching untouched — every query word must still appear for a crawler to land there — so
  this only adds information, it does not change what counts as a match. A query like "Reddit
  search posts comments" used to come back `count: 0` even though the Reddit Scraper exists,
  because its listing never says "search" or "comments"; an agent reading a bare zero reasonably
  concluded nothing matched and started building a workaround instead of running the scraper that
  was right there. Each `similar` row carries the
  same fields as a `results` row plus `matched`/`missing` (which query words it did / did not
  contain, informational only — not part of the ranking), capped at 5. `similar` is populated only
  when `results` is empty — it is a pointer for when there is no exact match, not a second results
  page, so an exact hit never shares the response with a partial one. When `results` is empty and
  `similar` is not, `hint` now names the closest partial match and what it matched; when both are
  empty, `hint` says so plainly. The key is always present (`similar: []` whenever `results` is
  non-empty, or nothing partial matched either) so callers can rely on it without a presence check.
  `full=true` is unaffected — it already returns the whole catalog, so there is nothing for
  `similar` to add there.
- `similar` ranks by BM25 score first (reused from the shelved spike above — its idf already
  weights a rare, distinctive word like "reddit" far above a common one like "posts", which is what
  puts Reddit Scraper first for "Reddit search posts comments" without a separate rule), then
  `total_runs` (lifetime run count) descending as the tie-break, then name for determinism.
  `total_runs` is read tolerantly (`crawler.get("total_runs") or 0`) since `GET /crawlers` does not
  carry it yet (a parallel Lobstr API change adds it); ordering is unaffected when the field is
  absent everywhere, as it is today. `results` is not reordered. An earlier version of this ranked
  by matched-word count first and BM25 score only as a tie-break within an equal count; that put a
  crawler matching 3 generic words above one matching 2 words including the one that actually named
  the platform asked for — wrong, and reverted before merge.

## [0.4.0] - 2026-09-22

### Changed

- `run_scraper(squid_id=..., input=...)` no longer replaces a squid's saved inputs. A squid holds
  a list of task rows and **every run scrapes all of them**, so the old behaviour silently deleted
  whatever its owner had built — 50 rows saved in the dashboard were gone the first time an agent
  re-ran that scraper with an input. Replacement is now opt-in through
  `replace_tasks` (below); by default:
  - a task-level input (`url`, `query`, …) on a squid that **already has saved rows** is refused
    with `squid_has_tasks`, writing nothing and starting no run. The error carries
    `existing_task_count`, the `new_task` that was not applied, and the four ways forward: run the
    saved rows as they are, run the input on a separate new scraper (`scraper=...`), `add_tasks`
    the new row and run the whole list (named with the multiplied cost), or pass
    `replace_tasks=true`. Refusing rather than appending is deliberate — appending would start a
    run over every saved row, spending roughly that many times the estimate the same response
    quotes for one input;
  - a task-level input on a squid with **no** saved rows is simply added and run, without the
    `/squids/{id}/empty` call the old path always made;
  - an input carrying **only** squid-level settings (`max_results`, function toggles) is merged
    into the saved settings, exactly as before, and the saved rows are left alone and run;
  - the rows cannot be read at all (`GET /tasks` fails): a task-level input is refused with
    `tasks_unreadable` rather than risking rows that may be there; a settings-only input still
    runs, with the count reported as unknown.

  **A caller that relied on the old silent replacement must now pass `replace_tasks=true`.** Its
  behaviour is otherwise unchanged, snapshot/restore included.
- Every successful `run_scraper` response now states what happened to the saved inputs:
  `tasks_action` (`created`, `added`, `replaced`, `unchanged`), `task_count` — how many rows this
  run actually scrapes, `null` when unknown — a one-line `tasks_note`, and `replaced_task_count`
  on the replace path. A model that only saw `run_id` could not tell "your input is all this run
  scrapes" from "your input plus 50 rows somebody else saved", which is the difference in the
  bill. The credit estimate covers every row the run scrapes (see below); it used to be computed
  for a single input, with the note saying so in words after the confirmation gate had passed.

- `check_credits` now reports `interval` and `reset_time` next to `available` and `consumed`, with
  a `credits_note` saying what period those two figures cover. `GET /user/balance` answers a
  different question depending on the account's reset interval and the tool threw both fields
  away: on a `daily` account `available` is the billing period's remaining credits divided by the
  days left in it and `consumed` is *today's* usage, so a model reading them as the account
  balance budgets a run against one day's allowance and reports "0 consumed" for an account that
  has spent millions. On any other interval the note says the figures cover the
  billing period; when the API reports no interval it says the period is unknown rather than
  guessing one. The numbers themselves are unchanged.
- `check_credits` now describes `used_slots` / `total_slots` as the deployed API means them today.
  `used_slots` is the concurrency reserved by the account's **active** scrapers (the sum of their
  `concurrency`, recomputed on every call), `total_slots` is what the plan allows, and the cap is
  checked when a scraper is created, activated or has its concurrency raised — never when a run
  starts. So the pair can read over itself — the report that opened the card had `used_slots: 608`
  against `total_slots: 1`, with squid creation still succeeding — after a plan change or on a
  staff account, which skips the check. The docstring and a `slots_note` on the response now say
  that, and that it is not on its own an error, instead of leaving a model to read it as a quota
  it has to free before it can work. Semantics are untouched; this is wording catching up with
  behaviour.
- `whoami` no longer passes through `/me`'s `plan` list, and reports no credit figure at all. Each
  entry of that list carries `total_consumed` = lifetime credits consumed minus bonus credits,
  which is a third quantity again, and that is what put two contradicting numbers in front of one
  agent: the same account, seconds apart, was told 16,382.6 by `whoami` and 0 by `check_credits`,
  both under the word "consumed". `whoami` now carries identity and the plan in
  force — `email`, `name`, `is_staff`, `plan` (the current entry's name), `plan_status`,
  `credit_interval`, `server_version` — and a note pointing at `check_credits`, which is the only
  tool in the server that reports credits or slots. **A caller that read `whoami()["plan"]` as a
  list now gets the plan name as a string, and one that read a credit figure out of it must call
  `check_credits`.**

### Added

- `run_scraper`'s pre-run estimate now counts the paid extra steps that are switched on, the same
  ones and the same way `ClusterEstimationView` counts them, so this client and `estimate_run` no
  longer answer one question with two numbers. A run reported 20 credits here and 26 from the API:
  20 rows × 1 credit/row, plus an "Extract Emails from Website" step at 1 credit on the 0.3 of rows
  it succeeds for = 6, which only the API was counting. Verified against the Test Server API on a
  squid capped at 20 unique results: both now answer 26.0, with the same two-line breakdown.
  Neither figure includes the result filters priced per row kept (`credits_per_filter`) — the API's
  estimate does not count them either — and `estimate_note` on `estimate_run` says so.
- `estimate_run` now carries `estimate_note` and `estimated_time_note`. `estimated_time` is derived
  from throughput × concurrency × the row cap with **no term at all** for rows a run discards, so
  on a filtered run it is a floor, not an ETA: reported finishes of 4m28s against a quoted "1 min"
  and 17m10s against "3-6 mins". The note says that, and says to poll `get_run` rather than call a
  run late. The number is passed through unchanged; what changed is that it is no longer offered
  bare to a model that will plan around it.
- `run_scraper`'s estimate now covers **every row the run scrapes**, not one. It read the squid's
  saved rows only after the confirmation gate, so a re-run over 50 saved rows was quoted at 1/50th
  of its cost and the `tasks_note` that said so in words arrived with the run already started.
  The rows are now read before the estimate (one `GET /tasks`, which the write path then reuses
  rather than repeating), `max_unique_results_per_run` is applied as the run-wide cap it is instead
  of being multiplied by the row count, and a reused squid is quoted against its **saved** settings
  when the call passes none. **This moves the confirmation gate**: a re-run over many saved rows
  now returns `needs_confirmation` where it used to execute, and the message names the row count.
  A caller that already passes `confirm=true` is unaffected. When the rows cannot be read the
  estimate covers one row and says the real total may be a multiple of it, instead of quoting the
  single-row figure as though it were the whole.
- `check_credits` now reports `remaining` — `available - consumed`, the figure to budget a run
  against — and both it and the docstring now say that `available` is an allowance that has
  **not** had `consumed` taken off it. `GET /user/balance` builds `available` from the plan's
  credits plus bonuses and subtracts the period's spend only on the daily branch, to size one
  day's slice. Measured on a test account: `available: 3000000` beside `consumed: 125000` on
  `monthly`, and the same account on `daily` reporting `available: 143750` =
  (3,000,000 − 125,000) ÷ 20 days left — which is only arithmetic if the monthly figure is gross.
  A model reading `available` as "what I can spend" over-budgets by `consumed` every time; the
  API's own pre-run gate does this subtraction internally and never showed it. The daily note also
  stops claiming that `reset_time` is when `available` resets: on that interval it is when
  **today's** counter rolls over, and the billing period's own reset is not in the payload.
- `run_scraper(..., replace_tasks=false)`: pass `true`, with `squid_id` and a task-level `input`,
  to delete every input that squid has saved and leave only the one you passed. Only meaningful
  for a squid that already exists — a scraper created by this call has nothing to replace.
- Squid inputs that only `GET /crawlers/{hash}/params` describes are now settable. The input
  schema was built from `crawler["input"]` alone, with `/params` consulted only for placement, so
  a key advertised solely in the `/params` squid section became no property at all: `run_scraper`
  could not classify it as squid-level and shipped it in the task row instead, where the API
  drops it and the run still bills. `auto_verify_emails` — offered for crawlers whose module does
  email verification, and deliberately kept out of `input[]` because the dashboard already draws
  its own "Verify emails" toggle — is the first of these, and
  `run_scraper(..., input={"auto_verify_emails": true})` now reaches the API as
  `{"params": {"auto_verify_emails": true}}`. Nothing here matches on a field
  name: any key the squid section describes and `input[]` omits becomes a property carrying its
  type (declared, else taken from its default), default and description, at the level `/params`
  gives it — function toggles included. Strictly additive: a name `input[]` already defines keeps
  its own definition, its own level and its place in the required set, so the Google Maps
  duplicate-`country` tie-break is untouched. A crawler that does not advertise the key simply
  has no property for it; the API's own `InvalidParam` stays the authority on what it accepts.

### Fixed

- `run_scraper`'s credit guard no longer answers a question it was not asked. `validate_input`
  skips any key it has no schema property for, so an invalid parameter passed local validation;
  the balance check then refused the call before the request carrying it was ever sent, and the
  caller was told to buy credits when the real problem was the input. That is how an unusable
  squid param went unnoticed for a while. The guard now runs **after** the
  configuration and rows have been written and accepted, so the API's own rejection — e.g.
  `InvalidParam: The specified parameter auto_verify_emails is invalid.` — is what comes back.
  Nothing before `POST /runs` spends a credit. The one exception is `replace_tasks=true`, where
  the guard still runs before the delete, so saved rows are never destroyed for a run that is then
  refused. When credits really are the problem the refusal now names the state it left behind: the
  scraper is saved and ready, nothing ran, and `run_scraper(squid_id=...)` runs it after a top-up
  rather than building it again.
- That guard also measured the wrong quantity. It compared the estimate against `available`, which
  never has `consumed` taken off it, so an account that had spent its allowance passed the check.
  It now compares against what is left, reports `available`, `consumed` and `remaining`, and
  distinguishes the two cases: nothing left at all (the API refuses any run outright) from an
  estimate larger than the remainder (this client's own guard, with the cap that would make the
  run fit named in the message).
- An input a crawler declares twice, at two levels, is no longer levelled as one. The Google Maps
  Leads Scraper declares `country` twice on purpose: a required **task**-level one that goes into
  the search URL (`/maps/search/{category}+in+{city}+{country}`) — what you search for — and an
  optional **squid**-level one, default "United States", that sets Google's `gl=` region — where
  you search from. `build.py` writes both into `public_params` with no dedup, so
  `GET /crawlers/{hash}/params` lists `country` under `task` *and* `squid` (confirmed on the live
  crawler row), and the squid section overwrote the task placement: `run_scraper` sent the
  required task field as a squid setting and every Google Maps location run died with
  `ParamsNeeded: Missing required parameters: country`, because the API builds its required-group
  check from `public_params["task"]` alone and never consults the squid. A name
  the task section claims now keeps its task placement, and where `input[]` declares one name at
  two levels the tie-break — not `/params` — decides which entry keeps the plain name.
- The shadowed half of such a pair is no longer dropped. It is published under a mechanical
  `<level>_<name>` alias — `squid_country` here — whose description names both inputs and says
  which is which, and `run_scraper` sends it under the name the API knows (sent raw, the API
  answers `InvalidParam: The specified parameter squid_country is invalid`). Dropping it was not
  cosmetic: the region silently falls back to `gl=US`, which changes which businesses Google
  returns. `get_scraper_details` levels the alias truthfully in `param_levels` and maps it in a
  new `param_wire_names` — `create_squid`'s `config` and `add_tasks` go straight to the API, so
  they take the name on the right of that map. Nothing matches on `country`: the next crawler
  that reuses a name is handled with no code change, and a crawler with no such collision is
  unaffected, alias-free and `param_wire_names`-free. `country` stays in `input_modes.either`, so
  the published `required` is still just `["language"]`.

- A transport failure — a timeout, a refused connection, or the API hanging up without sending
  response headers — no longer escapes a tool as a raw exception. Seen in production on 0.3.1:
  `run_scraper` posted to `/squids/{hash}`, the connection dropped mid-flight, and the calling
  model received `Error calling tool 'run_scraper': Server disconnected without sending a
  response.` — no `error_code`, no `upstream_status`, no hint, because only non-2xx *responses*
  were being mapped onto the error contract. The same hole made a `GET /me`
  that outlives `LOBSTR_REQUEST_TIMEOUT` crash `whoami` instead of answering. Every tool now
  returns `upstream_unavailable` with a `message`, a `hint`, the httpx error in
  `transport_error`, `upstream_status: null` (there was no response to have a status) and the
  two fields a model has to branch on:
  - `request_state: "not_sent"`, `retry_safe: true` — the request never left this client
    (connection refused, connect or pool timeout), so nothing upstream was started, changed or
    charged and the same call can simply be repeated;
  - `request_state: "unknown"`, `retry_safe: false` on a tool that writes — the request was sent
    and only the answer was lost (read timeout, server disconnect mid-response), so whether it
    took effect is **unknown, not rejected**. Retrying blind can apply the write twice, so the
    message names the read tool that settles it: `list_runs(squid_id=...)` for `run_scraper`,
    `estimate_run(squid_id=...)` (its `tasks.count`) for `add_tasks` and `empty_scraper`,
    `get_my_scraper(squid_id=...)` for `attach_account` and `deactivate_scraper`,
    `get_run(run_id=...)` for `abort_run`, `list_my_scrapers(name=...)` for `create_squid`.
    Read-only tools stay `retry_safe: true` in this case: repeating a read duplicates nothing.

  `run_scraper`'s replacement path is unchanged. It already handled a transport failure at each
  of its three steps and its answers say more (`tasks_lost`, `tasks_restored`, `lost_tasks`) than
  the generic envelope could; those handlers are inside the wrapped function, so they still run
  first.

- `whoami` no longer ships two keys that were null for every user. It mapped `id` and
  `name`/`full_name`, and `GET /me` returns neither: no id at all, and the name split across
  `first_name` and `last_name`. A key that is structurally always null cannot be
  told apart from a user who set no name. `id` is gone; `name` is now joined from the two fields
  the endpoint does return, so it is null only when the account really has no name.

## [0.3.2] - 2026-09-22

### Fixed

- A rejected `run_scraper(squid_id=..., input=...)` no longer destroys the squid's saved tasks.
  The rewrite emptied the squid *before* saving the new config and adding the new row, so anything
  the API refused afterwards — a param the crawler's `/params` doesn't describe, which the local
  schema check cannot know about — left the squid with no inputs at all and the caller with no
  copy of them (found while writing the Google Maps article). There is no atomic
  task swap upstream (`/squids/{id}/empty` drops every row, and adding first is refused on a squid
  at its crawler's max task count), so the destructive step is now as late and as reversible as
  possible: the config is saved first, the existing rows are snapshotted, and they are restored if
  emptying or adding then fails. When they cannot be put back the response says so (`tasks_lost`)
  and carries them verbatim under `lost_tasks` — the only remaining copy — pointing at `add_tasks`
  to re-add them; when they could not be read beforehand it says they are unrecoverable instead of
  implying the squid is fine.

## [0.3.1] - 2026-09-15

### Fixed

- `create_squid`: when the second call that saves `config` fails, the squid (already created by
  the first call) no longer comes back as a bare error with no way to reach it. The error carries
  the squid's `squid_id` and, depending on which of the two ways it failed, an opening clause that
  states only what's actually known:
  - **Rejected** (the API responded with a 4xx): nothing was saved, said plainly.
  - **The request itself failed** (timeout, connection reset — nothing in the client wraps those
    into a structured error, so they're now caught specifically rather than through a bare
    `except Exception`, which would have relabeled an unrelated bug in this code as a retryable
    upstream outage and thrown away its traceback): whether it saved is *unknown*, not rejected —
    it may have reached the server anyway, so the message points at `get_my_scraper` to check
    first, rather than assuming the worst and risking a caller building a duplicate squid.

  Both point at two real fixes: `create_squid` again under a different name — no run risk, with
  `deactivate_scraper` offered as its tail so the half-built squid isn't left holding a
  concurrency slot — or `run_scraper(squid_id=..., input=<the crawler's FULL, FLAT input —
  task-level fields together with the corrected squid/function-level ones; unlike `config`,
  `run_scraper` does its own level-splitting and "functions" nesting, so nesting a value by hand
  here isn't rejected, it's silently misclassified as task-level and shipped in the task row,
  dropping the setting while the run still bills>)`, named plainly as starting a run and spending
  credits immediately unless the cost is unknown or above the confirmation threshold, not merely
  "can". Retrying `create_squid` with the same name fails either way (the name is taken). The
  squid itself is left alone rather than deleted — an agent that mixed up a function-level param
  for a squid-level one (the case found in production) can't reliably tell
  whether the squid is otherwise fine, and there is no delete tool exposed here to make that call
  safely.
- `get_scraper_details`: now surfaces `required_account_type` — null when the crawler needs no
  platform account, else the account-type slug a squid built from it must have attached before a
  run can succeed. The API's crawler endpoint always carried this (under `account`); dropping it
  meant a model only learned an account was needed from a run failing asynchronously with
  `done_reason: "no_accounts"`. `param_levels` (which marks each input
  task/squid/function-level, the gap that let that orphan happen) was already
  present in this tool's output — its docstring now says so explicitly.
- `run_scraper`'s `account_attached` field now reports whether the squid has an account attached
  *now*, not whether this specific call was the one that attached it. A squid reused via
  `squid_id` whose account was attached by an earlier call read as `account_attached: false`,
  even though a run started fine on it — the name reads as squid state, so this could mislead a
  client into attaching an account again needlessly. Still never leaks an
  auto-picked or pre-existing account's id to a `runs:execute`-only token: only the
  caller-supplied `account_id` case echoes an id back, same as before.

## [0.3.0] - 2026-09-15

### Added

- `attach_account(squid_id, account_id=None)`: the only client-side way to link a connected
  platform account to a squid. A squid created by `run_scraper`/`create_squid` starts with an
  empty account list, so any account-backed crawler (LinkedIn Leads, Sales Navigator Leads, …)
  could never actually launch — the run was created but always failed asynchronously with
  `done_reason: "no_accounts"`. The tool reads the squid's current
  accounts first and only ever adds to that set, because the underlying API field
  (`POST /squids/{hash}` `accounts`) is full-replace and would otherwise silently detach
  whatever was already linked (the read and the write are still two separate requests, not an
  atomic compare-and-set — the API offers none — so a concurrent change can still race). With
  no `account_id` it auto-picks, but only when the squid has no account yet and exactly one
  unlocked, healthy (`status "200"`, not `cookies_expired`), exact-type-match candidate is
  connected; with several such candidates it names them instead of guessing. A locked-only
  candidate is reported separately (`account_locked`) from there being none at all — locks on
  LinkedIn/Sales Navigator are routine and transient, so "connect another account" would be the
  wrong remedy; it can still be attached explicitly by id. An explicit `account_id` is never
  refused for being unhealthy (that choice is the caller's) — the result carries
  `account_status` and a `warning` instead. A wrong-platform account, an unknown account id,
  and a crawler that needs no account each return a distinct `error_code` and a message that
  says what to do next, not the raw upstream wording.
- `run_scraper` gained the same resolution inline: a new `account_id` parameter, and automatic
  narrow auto-pick for a freshly-created (or account-less) squid, so the common "run a NEW
  account-backed scraper" path is fixed without a second tool call. It never touches a squid
  that already has an account attached, and the same locked-account and health-warning handling
  applies.
- New shared module `account_linking.py` (used by both) implementing the auto-pick rule, kept
  in one place so `attach_account` and `run_scraper` can never disagree on what counts as
  "safe to pick automatically."

### Changed

- `LobstrClient.list_accounts()` accepts optional `type`/`status` query filters
  (`GET /accounts?type=...&status=...`), used internally to find auto-pick candidates
  server-side before the client-side health/type check.

### Fixed

- `squid_account_ids()` now accepts both real shapes of a squid's `accounts` field — a list of
  dicts (`GET /squids/{hash}`) and a list of plain hash strings (the `POST /squids/{hash}`
  update echo) — and raises rather than returning `[]` on anything else, so an unrecognized
  shape can never be read as "no accounts" and unioned away to just the new one.

### Security

- `attach_account` requires `account:read` in addition to `runs:execute` (it can enumerate the
  user's connected accounts, which `account:read` — not `runs:execute` — is meant to gate); no
  new OAuth scope was introduced, `runs:execute` already covers the squid write itself, same as
  every other write `run_scraper` performs. `run_scraper`'s own account auto-resolution stays
  gated on `runs:execute` alone for backward compatibility, but an ambiguous-pick or
  locked-account error is reported without naming the candidates (ids/usernames) through that
  narrower scope — call `list_accounts` (or `attach_account`) to see them. Its
  `account_attached` field is likewise the account id only when the caller supplied it
  themselves (echoing their own input back isn't a leak); an auto-picked account's id is never
  exposed to a `runs:execute`-only token, which gets a bare `true`/`false` instead.

## [0.2.3] - 2026-09-11

### Fixed

- Sentry no longer pages on `uvicorn.error`'s "ASGI callable returned without
  completing response." (MCP-3). It fires with no exception and no user
  whenever a `/mcp` Streamable HTTP/SSE stream is still open when the
  container is redeployed: `sse_starlette`'s `EventSourceResponse` cancels its
  task group on the shutdown signal before sending the closing response
  frame, which is exactly the state uvicorn's own sanity check reports as an
  error. Confirmed by driving `EventSourceResponse` directly; the fix is a
  `before_send` filter in `observability.py`, scoped to that logger and
  message so unrelated bugs still page.

## [0.2.2] - 2026-09-11

### Fixed

- The production OAuth server never registered the composable primitives, so `mcp.lobstr.io`
  served 16 tools while the code has 19. `create_squid`, `add_tasks` and `estimate_run` are now
  registered there too, scope-gated like every other tool, and a test keeps the three server
  builders' tool sets identical.

## [0.2.1] - 2026-09-11

### Fixed

- A bare `OPTIONS` on `/register`, `/token` or `/revoke`, or a `POST /register` whose body is
  not a JSON object, returned a 500 from the `mcp` SDK's handler and paged Sentry (MCP-4).
  `AuthEndpointGuard` now answers 204 with `Allow: POST, OPTIONS` and 400
  `invalid_client_metadata` respectively; CORS preflights and valid requests pass through.

## [0.2.0] - 2026-09-11

### Added

- One version source: `lobstr_mcp.__version__` (from the installed package metadata) feeds the
  `User-Agent: lobstr-mcp/<version>` sent to the Lobstr API, the Sentry `release`, and a new
  `server_version` field on the `whoami` tool.
- This changelog, with the history since launch reconstructed from the merged PRs.

### Changed

- Sentry release is no longer hardcoded to `lobstr-mcp@0.1.0`; each deploy reports its own build.

## [0.1.0] - 2026-09-09

The launch line, deployed on `mcp.lobstr.io` from 2026-09-03 and extended through 2026-09-09
without a version bump. Everything below shipped under `0.1.0`.

### Added

- OAuth 2.1 + PKCE + DCR server with the Lobstr frontend consent page (`/connect-ai`) as the
  login step; grants redeemed against `POST /v1/oauth/mcp-grant/redeem`.
- Public landing page at the server root with the brand assets (PR #2, #3).
- Tool surface 7 → 19: `search_scrapers`, `get_scraper_details`, `list_my_scrapers` (paged and
  filtered), `get_my_scraper`, `run_scraper` (confirm above a credit threshold), `get_run`,
  `get_results`, `get_results_url`, `list_runs`, `abort_run`, `empty_scraper`,
  `deactivate_scraper`, `check_credits`, `whoami`, `list_accounts`, `get_account`, and the
  composable primitives `create_squid`, `add_tasks`, `estimate_run` (PR #9, #14).
- Sentry with a scrubber, and the `lobstr-mcp/<version>` User-Agent (PR #6).
- `profile:read` split out of `account:read` (PR #13).

### Changed

- Data layer rebased on `lobstrio-sdk` (thin adapter, no duplicated HTTP client) (PR #8).
- Output slimming: JSON by default, TOON on request, ~98% smaller tool responses (PR #4).
- `get_scraper_details` accepts a slug as well as a hash (PR #5).
