# Error contract

Tools never surface a raw upstream 4xx/5xx. Every failure is a stable
`error_code` plus a human `message`, and a remediation `hint` where one
exists, so the model can self-correct instead of guessing. Every error also
carries `upstream_status`, and `upstream_type` when Lobstr supplied one.
Source: `src/lobstr_mcp/errors.py` and the per-tool error paths in
`src/lobstr_mcp/tools/*.py` and `src/lobstr_mcp/execution.py`.

`needs_confirmation` (`run_scraper`) and `status: "already_submitted"` (the
idempotency short-circuit) are **flags, not errors** — see
[`docs/tools.md`](tools.md).

| `error_code` | Meaning |
|---|---|
| `validation_error` | Input failed schema validation. `errors` lists per-field messages. |
| `invalid_request` | Bad tool arguments (e.g. neither `run_id` nor `squid_id`, or an empty `tasks` list). |
| `insufficient_credits` | The API refused the run for credits (`NotEnoughCredits`) — an ordinary account whose period spend reached its allowance. A staff/admin account runs regardless. |
| `slots_limit_exceeded` | The account's concurrency-slot limit is reached (checked on squid create/activate, not run start). Free one with `deactivate_scraper` on a squid you're not using. |
| `unauthorized` | Token invalid, expired, or an OAuth grant already consumed — re-authorize. |
| `forbidden` | Authenticated but not permitted. |
| `not_found` | No such run / squid / scraper for a generic lookup. |
| `scraper_not_found` | Unknown crawler id. |
| `scraper_not_ready` | Settings were not saved before starting the run (`SquidNotReady`). |
| `no_account_needed` | An `account_id` (or `attach_account`) was used on a crawler that needs no platform account. |
| `account_not_found` | The given `account_id` doesn't exist, or belongs to another user. |
| `account_type_mismatch` | The account's platform doesn't match what the crawler needs. Carries `required_type` / `given_type`. |
| `account_already_attached` | `attach_account` called with no `account_id` on a squid that already has one — auto-pick only runs on a squid with none; pass one explicitly. |
| `no_account_available` | No account of the required platform (healthy, unlocked) is connected. Carries `required_type`. |
| `account_locked` | Only a locked account of the required platform is connected — routine on LinkedIn/Sales Navigator, not "none available". Attach it explicitly by id to use it anyway. |
| `multiple_accounts_available` | Several healthy candidates exist; auto-pick won't guess — call `list_accounts`, then retry with `account_id`. |
| `accounts_unreadable` | The squid's current `accounts` field wasn't in a recognised shape; nothing was written, to avoid silently dropping an existing link. |
| `squid_has_tasks` | A task-level `input` was given for a squid that already has saved rows; nothing was written. Carries `existing_task_count` and ready-to-call `options`. |
| `tasks_unreadable` | The squid's saved task rows couldn't be listed, so `run_scraper` can't tell whether applying the input would discard them; nothing was written. |
| `conflict` | Upstream 409. |
| `rate_limited` | Upstream 429. |
| `upstream_rejected` | Upstream 400 with a reason (carries `upstream_type`). |
| `upstream_unavailable` | Upstream 5xx, or no response came back at all (`request_state`: `"not_sent"` = safe to retry, `"unknown"` = check state first, see `docs/tools.md`). |
| `upstream_error` | Any other upstream failure. |
