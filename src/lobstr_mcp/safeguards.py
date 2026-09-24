"""Execution safeguards: input validation, cost estimation, idempotency.

These protect AI-triggered runs, which cost real credits.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

_PY_TYPE_OK = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def validate_input(values: dict, json_schema: dict,
                   input_modes: dict | None = None,
                   check_required: bool = True) -> list[str]:
    """Return a list of human-readable validation errors (empty if valid).

    `input_modes` (from the schema translator) expresses alternative input sets —
    e.g. Google Maps takes `url` OR `category`+`country`+`city`. When present, at
    least one complete alternative group must be supplied.

    `check_required=False` skips the required/`input_modes` checks and only
    type-checks fields present in `values` — for a squid re-run with
    settings-only input, whose saved tasks already satisfy what's required.
    """
    errors: list[str] = []
    props = json_schema.get("properties", {})
    if check_required:
        for req in json_schema.get("required", []):
            if req not in values:
                errors.append(f"{req} is required")
        if input_modes:
            groups = input_modes.get("either") or []
            if groups and not any(all(f in values for f in g) for g in groups):
                opts = " OR ".join("(" + " + ".join(g) + ")" for g in groups)
                errors.append(f"provide one of these input sets: {opts}")
    for key, val in values.items():
        spec = props.get(key)
        if not spec:
            continue
        check = _PY_TYPE_OK.get(spec.get("type"))
        if check and not check(val):
            errors.append(f"{key} must be {spec.get('type')}")
    return errors


def resolve_credit_rate(value) -> float | None:
    """Live crawlers expose credits_per_row / credits_per_email as
    {"legacy": n, "current": m}; a run is billed at the current rate. Plain
    numbers (older/looser shapes) pass through."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("current", "legacy"):
            v = value.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
    return None


@dataclass
class CostEstimate:
    credits: float | None
    basis: str
    currency: str = "credits"
    # Live crawlers price per result row, so the total is unknowable up front;
    # the rate itself is still real information worth handing to the model.
    rate: float | None = None


def _positive_int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def paid_steps(crawler: dict, settings: dict | None = None) -> list[dict]:
    """The crawler's paid extra steps that are switched ON for this run.

    A row's price is not only `credits_per_row`. Every input carrying
    `credits_per_function` is a step billed on top — Google Maps' "Extract
    Emails from Website" is one, at 1 credit per row where an e-mail is found —
    and several are ON by default, so a caller who set nothing still pays for
    them. Leaving them out is what made this client quote 20 credits where the
    API's own estimate said 26: 20 rows x 1 credit/row, plus 20 x the step's
    0.3 success ratio x 1 credit = 6.

    Read exactly as `ClusterEstimationView` reads them, so the two figures
    describe the same thing: the state is the value given for the step (under
    `functions` or flat), else the input's own `default`; the count of rows it
    is billed on is `rows x worker_stats.success_ratio`.

    Steps priced with `credits_per_filter` (the result filters) are deliberately
    NOT counted: the API's estimate does not count them either, and a client
    quoting a figure the authoritative estimate contradicts is the problem this
    is fixing. They do cost credits, which is a gap in the API's estimate, not
    something to paper over here.
    """
    settings = settings or {}
    functions = settings.get("functions")
    functions = functions if isinstance(functions, dict) else {}
    steps = []
    for item in crawler.get("input", []):
        if not isinstance(item, dict) or "credits_per_function" not in item:
            continue
        name = item.get("name")
        if name in functions:
            state = functions.get(name)
        elif name in settings:
            state = settings.get(name)
        else:
            state = item.get("default")
        if not state:
            continue
        rate = resolve_credit_rate(item.get("credits_per_function"))
        if rate is None:
            continue
        stats = item.get("worker_stats")
        ratio = (stats or {}).get("success_ratio", 1.0)
        ratio = float(ratio) if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) else 1.0
        steps.append({"name": name, "rate": rate, "success_ratio": ratio})
    return steps


def estimate_cost(crawler: dict, task_count: int = 1,
                  max_results_per_task: int | None = None,
                  run_result_cap: int | None = None,
                  settings: dict | None = None) -> CostEstimate:
    """Best-effort pre-run cost estimate, in the same terms as the API's.

    Lobstr generally prices per result produced, which is unknown before a run.
    But when the run is capped and the per-row rate is known, we can give a real
    **upper bound** rather than "unknown". How many rows that cap allows:

    * `run_result_cap` (`max_unique_results_per_run`) caps the WHOLE run, so it
      is the row count outright, however many tasks there are. Collapsing it
      with the per-task cap was quietly wrong in both directions;
    * otherwise `max_results_per_task x task_count` — every task row is scraped
      by every run, which is why `task_count` has to be the squid's real row
      count and not 1.

    On top of the per-row price come the paid steps that are on (see
    `paid_steps`), exactly as `ClusterEstimationView` adds them, so this figure
    and `estimate_run`'s answer the same question. This one stays the upper
    bound of the two: it assumes the cap is reached, where the API projects the
    unique results a run of that size tends to yield.

    Without a cap and without a per-task price we return credits=None so the
    orchestrator requires confirmation.
    """
    for key in ("credits_per_task", "price_per_task", "cost_per_task"):
        val = crawler.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return CostEstimate(float(val) * task_count, f"{key} x {task_count} task(s)")
    rate = resolve_credit_rate(crawler.get("credits_per_row"))
    if rate is None:
        return CostEstimate(
            None,
            "unknown: crawler exposes no pre-run price; cost depends on results produced",
        )

    steps = paid_steps(crawler, settings)
    run_cap, per_task = _positive_int(run_result_cap), _positive_int(max_results_per_task)
    if run_cap is None and per_task is None:
        extras = ("" if not steps else
                  ", plus " + ", ".join(s["name"] for s in steps) + " on each row it applies to")
        return CostEstimate(
            None,
            f"{rate} credit(s) per row across {task_count} task(s){extras}; the total "
            "depends on how many rows the run produces",
            rate=float(rate),
        )

    max_rows = run_cap if run_cap is not None else per_task * task_count
    rows_basis = (f"up to {max_rows} row(s) (capped for the whole run by "
                  "max_unique_results_per_run)" if run_cap is not None else
                  f"up to {max_rows} row(s) ({per_task} per task x {task_count} task(s))")
    total = float(rate) * max_rows
    step_basis = ""
    for step in steps:
        billed = int(max_rows * step["success_ratio"])
        total += billed * step["rate"]
        step_basis += (f"; + {step['name']} on ~{billed} row(s) x {step['rate']} "
                       "credit(s)")
    return CostEstimate(
        round(total, 4),
        f"{rows_basis} x {rate} credit(s)/row{step_basis}. Upper bound: the actual "
        "total is lower if the run yields fewer rows. estimate_run(squid_id=...) "
        "is the API's own figure and the authoritative one",
        rate=float(rate),
    )


def compute_idempotency_key(scraper: str, values: dict) -> str:
    blob = scraper + "|" + json.dumps(values, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class IdempotencyStore:
    """Maps an idempotency key to the run_id it created (in-memory for now)."""

    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._d.get(key)

    def put(self, key: str, run_id: str) -> None:
        self._d[key] = run_id
