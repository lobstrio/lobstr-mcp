# Tool reference

Detailed behaviour for each of the 20 tools. For the one-line summary and
scopes, see the [README](../README.md#tools). This page follows the tool
docstrings the AI client actually reads (`src/lobstr_mcp/tools/*.py`); if the
two ever disagree, the docstring in the code is the source of truth.

A **crawler** is a scraper template (e.g. "Google Maps"). Running one creates
a **squid**, your saved, configured instance of it, which holds a list of
**tasks** (input rows) — every run scrapes all of them. A **run** is one
execution of a squid.

## Discovery

### `search_scrapers(query, full=False, toon=False)`
Scope: `crawlers:read`.

Find crawlers matching every word in `query` (name, description or slug).
`results` are exact matches; when `results` is empty, up to 5 `similar`
partial matches (BM25-ranked) are returned instead, each with `matched` /
`missing` words — an empty `results` with a non-empty `similar` does **not**
mean the scraper doesn't exist. `full=true` returns the whole catalogue (no
`similar` in that mode). `toon=true` returns compact TOON instead of JSON.

### `get_scraper_details(scraper, full=False)`
Scope: `crawlers:read`.

A crawler's input schema, output fields and pricing — read this before
calling `run_scraper`. `param_levels` marks each input `task` (→ `add_tasks`),
`squid` (→ `create_squid`'s `config`) or `function` (nested under
`config.functions`, never flat). When an input is named the same at two
levels, the second is published under a prefixed alias and `param_wire_names`
maps it back to the API's own name. `required_account_type` is `null` when no
platform account is needed, else the account type a squid needs attached
before a run can succeed.

### `list_my_scrapers(name=None, crawler=None, page=1, page_size=50, toon=False)`
Scope: `crawlers:read`.

The user's saved squids, paged, optionally filtered by name or crawler.

### `get_my_scraper(squid_id)`
Scope: `crawlers:read`.

One saved squid's full configuration.

## Building and running

### `create_squid(scraper, name=None, config=None)`
Scope: `runs:execute`.

Creates a squid from a crawler **without** running it. `config` takes
squid-level params directly and function-level toggles nested under
`"functions"` — never task-level fields (those go through `add_tasks`). If the
config is rejected, the squid still exists with no saved config, and the error
names the two ways forward: create again under a new name, or fix and reuse
this squid's id with `run_scraper`. Retrying with the same name always fails
(the name is taken).

### `add_tasks(squid_id, tasks)`
Scope: `runs:execute`.

Adds task rows (a list of dicts of the crawler's task-level inputs) to an
existing squid. Callable repeatedly to build up a batch before running.

### `estimate_run(squid_id, toon=False)`
Scope: `crawlers:read`.

The API's own pre-run estimate for a squid that already has tasks: credits,
projected results, estimated time. More accurate than `run_scraper`'s built-in
upper bound. `total_credits` excludes per-row result filters; `estimated_time`
is a floor (assumes every row fetched is kept), not an ETA.

### `run_scraper(scraper=None, input=None, confirm=False, idempotency_key=None, squid_id=None, account_id=None, replace_tasks=False)`
Scope: `runs:execute`.

The one-call default: create/configure/add tasks/run in one shot, **spends
credits**.

- Pass `scraper` to create a new squid holding only this input. Pass
  `squid_id` (from `list_my_scrapers`) to re-run an existing one instead.
- A squid's saved tasks are **never replaced unless you ask.** With
  `squid_id` and a task-level `input`: no saved tasks → yours is added and
  run; saved tasks already exist → refused with `squid_has_tasks` (nothing
  written) and a list of ready-to-call `options`, one of which is
  `replace_tasks=true` to delete them on purpose; a squid-level-only `input`
  merges into the saved settings and every existing task still runs.
- `replace_tasks=true` deletes every saved task and runs only yours. The
  config is saved first, the rows are snapshotted, and a failure at any later
  step restores them — an unrecoverable loss (no atomic swap exists upstream)
  is reported as `tasks_lost` + `lost_tasks` verbatim rather than silently
  eaten.
- The estimate covers the whole run; above the confirmation threshold, or
  unknowable, the response is `{needs_confirmation: true, estimate, ...}` —
  call again with `confirm=true`.
- Account-backed crawlers (LinkedIn Leads, Sales Navigator Leads, …)
  auto-attach a connected account when exactly one healthy, unlocked,
  right-type candidate exists; pass `account_id` to pick one explicitly.
  Several candidates → `multiple_accounts_available` (call `list_accounts`,
  retry with `account_id`); none → `no_account_available`.
- On success: `run_id`, `tasks_action` (`created` / `added` / `replaced` /
  `unchanged`), `task_count` (rows this run actually scrapes, `null` if
  unknown), `account_attached`, and `remaining` credits (net of consumed).
  Affordability is the API's own call — a thin-but-affordable balance rides
  as `credit_warning` on success rather than a refusal.
- A dropped connection reports `request_state`: `"not_sent"` (safe to retry)
  or `"unknown"` (check `list_runs`/`get_run` before retrying — a blind retry
  can start and pay for the same run twice).

### `attach_account(squid_id, account_id=None)`
Scope: `account:read + runs:execute`.

Links a connected platform account to a squid, standalone (the auto-attach
`run_scraper` does inline). **Only ever adds**: reads the squid's current
accounts, then re-sends that set plus the new one, so an existing link is
never silently dropped (not an atomic compare-and-set — a concurrent change to
the same squid can still race). No `account_id` → auto-picks only when the
squid has none yet and exactly one healthy, unlocked, right-type account is
connected; a locked candidate is reported separately rather than as "none
available". An explicit `account_id` is never refused for being unhealthy —
the result carries `account_status` and a `warning` instead.

### `empty_scraper(squid_id)`
Scope: `runs:execute`.

Removes all tasks from a squid, keeping the squid and its config so it can be
reused with new inputs. Does not delete the scraper.

### `deactivate_scraper(squid_id)`
Scope: `runs:execute`.

Deactivates a squid to free the concurrency slot it holds while active
(checked when a squid is created, activated, or has its concurrency raised —
not when a run starts), **without deleting it** — config and results stay,
and it can be reactivated from the dashboard. Stops any run of that squid
currently in progress. Prefer this over deleting a squid you might reuse.

## Runs and results

### `get_run(run_id, full=False, toon=False)`
Scope: `runs:read`.

Status/progress of a run: `status` (`pending` / `running` / `done` / `error` /
`paused` / `aborted`), `progress`, `tasks_total`/`tasks_done`,
`total_results`, `credits_consumed`, `done_reason`. `full=true` includes the
raw stats blob.

### `list_runs(squid_id, limit=20, toon=False)`
Scope: `runs:read`.

Recent runs for a squid — status, result counts, credits, timings.

### `get_results(run_id=None, squid_id=None, page=1, page_size=None, fields=None, full=False, toon=False)`
Scope: `results:read`.

One page of results, capped by default so a large dataset doesn't flood
context. Pass exactly one of `run_id` or `squid_id`. `fields` selects columns;
`full=true` keeps every field (including empty ones) and more rows.

### `get_results_url(run_id)`
Scope: `results:read`.

A signed URL to download a run's full result set as a file — use instead of
`get_results` when the caller wants the whole dataset, not paged rows.

### `abort_run(run_id)`
Scope: `runs:execute`.

Stops a running job. Already-scraped results are kept. Poll `get_run` to
confirm it stopped.

## Account and profile

### `list_accounts(platform=None, limit=50, page=1, toon=False)`
Scope: `account:read`.

The user's connected platform accounts (LinkedIn, Facebook, Instagram, …) and
each one's health. Metadata only, never credentials.

### `get_account(account_id)`
Scope: `account:read`.

One connected account by id — platform, status, which squids use it.

### `check_credits(toon=False)`
Scope: `profile:read`.

The only tool that reports credits. Budget a run against `remaining`
(`available - consumed`), not `available`. What period the figures cover
depends on `interval` (`"daily"` = today only; anything else = the current
billing period) — `credits_note` in the response says which applies. Also
reports `used_slots` / `total_slots` (see `deactivate_scraper` above for what
a slot is).

### `whoami()`
Scope: `profile:read`.

The authenticated user's identity and this server's version: `email`, `name`,
`is_staff`, `plan`, `plan_status`, `credit_interval`, `server_version`.
Carries no credit figure — that's `check_credits` alone, so the two tools
never disagree on a number.
