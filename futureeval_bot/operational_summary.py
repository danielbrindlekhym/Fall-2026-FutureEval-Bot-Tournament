"""Publish fixed, privacy-limited workflow diagnostics from read-only SQLite."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

_PHASES = {"discovered", "researched", "forecasted", "submitted", "verified", "missed", "external_forecast"}
_ERROR_TYPES = {"NoValidatedForecast", "ServiceError", "ValueError", "KeyError", "TypeError",
                "IndexError", "OSError", "RuntimeError", "TimeoutError"}
_CATEGORIES = {
    "invalid_json", "missing_field", "invalid_structure", "provider_error_response", "missing_json",
    "missing_required_reasoning", "option_labels_mismatch", "probability_sum_invalid",
    "unsupported_option_count", "percentile_schema_invalid", "nonfinite_quantile", "nonnumeric_probability",
    "probability_out_of_bounds", "payload_schema_invalid", "cdf_length_invalid", "cdf_not_monotonic",
    "cdf_lower_tail_invalid", "cdf_upper_tail_invalid", "cdf_bin_cap_exceeded", "validation_rejected",
    "call_allowance_exhausted", "transport_unconfirmed", "http_authentication", "http_payment_required",
    "http_forbidden", "http_not_found", "http_timeout", "http_rate_limited", "http_server_error",
    "http_other", "request_failed",
}
_OUTCOMES = {"success", "failure", "cancelled", "skipped", "unknown"}
_EVENTS = {"schedule", "workflow_dispatch", "unknown"}
_STAGES = {
    "checkout": "BOT_STEP_CHECKOUT_OUTCOME",
    "setup_python": "BOT_STEP_SETUP_PYTHON_OUTCOME",
    "install_dependencies": "BOT_STEP_INSTALL_OUTCOME",
    "verify_baseline": "BOT_STEP_BASELINE_OUTCOME",
    "verify_reliability": "BOT_STEP_VERIFY_OUTCOME",
    "restore_progress": "BOT_STEP_RESTORE_OUTCOME",
    "record_verification": "BOT_STEP_RECORD_OUTCOME",
    "run_selected_operation": "BOT_OPERATION_OUTCOME",
}


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _outcome(value: object) -> str:
    return value if isinstance(value, str) and value in _OUTCOMES else "unknown"


def _counts(raw: object, allowed: set[str]) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    counts: Counter[str] = Counter()
    for name, count in raw.items():
        if type(count) is int and count >= 0:
            counts[name if isinstance(name, str) and name in allowed else "other"] += count
    return dict(sorted(counts.items()))


def _correlated_last_run(raw_value: object, *, run_id: str | None, attempt: str | None,
                         start: datetime | None, finish: datetime) -> tuple[str, object]:
    if not run_id or not attempt or not start or not isinstance(raw_value, str):
        return "unknown", None
    try:
        record = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError):
        return "unknown", None
    if not isinstance(record, dict):
        return "unknown", None
    record_start = _time(record.get("operation_started_at"))
    recorded_at = _time(record.get("at"))
    if (record.get("workflow_run_id") != run_id or record.get("workflow_run_attempt") != attempt
            or record_start != start or not recorded_at or not start <= recorded_at <= finish
            or not isinstance(record.get("failures"), list)):
        return "unknown", None
    return "matched", record


def _halt_status(raw_value: object, present: bool) -> str:
    if not present:
        return "clear"
    if not isinstance(raw_value, str):
        return "unknown"
    try:
        value = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError):
        return "unknown"
    if value is False:
        return "clear"
    if (isinstance(value, dict) and set(value) == {"call_id", "reason", "at"}
            and type(value.get("call_id")) is int and value["call_id"] > 0
            and value.get("reason") == "pending_zero_cost_receipt" and _time(value.get("at"))):
        return "active"
    return "unknown"


def _context(env: dict[str, str], config_path: str | Path) -> dict:
    start = _time(env.get("BOT_OPERATION_STARTED_AT", ""))
    finish = datetime.now(timezone.utc)
    raw_run = env.get("BOT_WORKFLOW_RUN_ID", "")
    raw_attempt = env.get("BOT_WORKFLOW_RUN_ATTEMPT", "")
    run_id = raw_run if re.fullmatch(r"[0-9]{1,24}", raw_run or "") else None
    attempt = raw_attempt if re.fullmatch(r"[0-9]{1,8}", raw_attempt or "") else None
    source = env.get("BOT_SOURCE_SHA", "").lower()
    source = source if re.fullmatch(r"[0-9a-f]{40}", source) else None
    fingerprint = None
    try:
        public_config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        if isinstance(public_config, dict):
            canonical = json.dumps(public_config, sort_keys=True, separators=(",", ":"), allow_nan=False)
            fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except (OSError, ValueError, TypeError):
        pass
    stage_outcomes = {stage: _outcome(env.get(variable, "unknown")) for stage, variable in _STAGES.items()}
    event = env.get("BOT_TRIGGER_EVENT", "unknown")
    event = event if event in _EVENTS else "unknown"
    base = {
        "summary_status": "available",
        "operation_step_outcome": stage_outcomes["run_selected_operation"],
        "stage_outcomes": stage_outcomes,
        "trigger_event": event,
        "workflow_run_id": run_id,
        "workflow_run_attempt": attempt,
        "source_sha": source,
        "public_config_fingerprint_sha256": fingerprint,
        "operation_started_at": _iso(start),
        "operation_finished_at": _iso(finish) if start else None,
        "run_binding_status": "unknown",
        "failure_count": None,
        "failure_type_counts": {},
        "validation_failure_categories": {},
        "request_failure_categories": {},
        "lifetime_phase_counts": None,
        "window_phase_counts": None,
        "lifetime_calls": None,
        "lifetime_calls_with_recorded_cost": None,
        "lifetime_calls_missing_cost": None,
        "lifetime_recorded_cost_sum_usd": None,
        "window_calls": None,
        "window_calls_with_recorded_cost": None,
        "window_calls_missing_cost": None,
        "window_recorded_cost_sum_usd": None,
        "window_aggregate_status": "unknown",
        "cost_halt_status": "unknown",
        "delivery_proof_status": "unknown",
        "delivery_proof_observed_rows": None,
        "delivery_proof_scope": None,
        "delivery_proof_counts": None,
    }
    return base, start, finish, run_id, attempt


def collect_summary(database: str | Path, *, config_path: str | Path = "config.json",
                    env: dict[str, str] | None = None) -> dict:
    """Read only allowlisted aggregate/status columns; never select payload or response data."""
    env = dict(os.environ if env is None else env)
    result, start, finish, run_id, attempt = _context(env, config_path)
    db_path = Path(database)
    if not db_path.is_file():
        result.update(summary_status="unavailable", unavailable_reason="database_missing")
        return result
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
            db.execute("PRAGMA query_only=ON")
            lifetime_phases = Counter()
            for (phase,) in db.execute("SELECT phase FROM jobs"):
                lifetime_phases[phase if phase in _PHASES else "other"] += 1
            lifetime_calls = db.execute(
                "SELECT COUNT(*), COUNT(cost), COALESCE(SUM(CASE WHEN cost IS NULL THEN 1 ELSE 0 END),0), "
                "COALESCE(SUM(cost),0) FROM calls"
            ).fetchone()
            raw_meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN (?,?)",
                                       ("last_run", "cost_guard_halt")).fetchall())
            proof_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", ("delivery_proof",)
            ).fetchone()
            if proof_table and run_id and attempt:
                delivery_rows = db.execute(
                    "SELECT paired,status FROM delivery_proof "
                    "WHERE workflow_run_id=? AND workflow_run_attempt=?", (run_id, attempt)
                ).fetchall()
            else:
                delivery_rows = None
            if start:
                window_calls = db.execute(
                    "SELECT COUNT(*), COUNT(cost), COALESCE(SUM(CASE WHEN cost IS NULL THEN 1 ELSE 0 END),0), "
                    "COALESCE(SUM(cost),0) FROM calls "
                    "WHERE julianday(created)>=julianday(?) AND julianday(created)<=julianday(?)",
                    (_iso(start), _iso(finish)),
                ).fetchone()
                window_phases = Counter()
                for (phase,) in db.execute(
                    "SELECT phase FROM jobs WHERE julianday(updated)>=julianday(?) AND julianday(updated)<=julianday(?)",
                    (_iso(start), _iso(finish)),
                ):
                    window_phases[phase if phase in _PHASES else "other"] += 1
            else:
                window_calls, window_phases = None, None
    except (OSError, sqlite3.Error, ValueError):
        result.update(summary_status="unavailable", unavailable_reason="database_read_failed")
        return result

    binding, last_run = _correlated_last_run(raw_meta.get("last_run"), run_id=run_id, attempt=attempt,
                                              start=start, finish=finish)
    result["run_binding_status"] = binding
    if binding == "matched":
        failures = last_run["failures"]
        types: Counter[str] = Counter()
        validation: Counter[str] = Counter()
        requests: Counter[str] = Counter()
        for failure in failures:
            if not isinstance(failure, dict):
                continue
            error_type = failure.get("error_type")
            types[error_type if isinstance(error_type, str) and error_type in _ERROR_TYPES else "other"] += 1
            diagnostics = failure.get("diagnostics")
            if isinstance(diagnostics, dict):
                validation.update(_counts(diagnostics.get("validation_failures"), _CATEGORIES))
                requests.update(_counts(diagnostics.get("request_failures"), _CATEGORIES))
        result["failure_count"] = sum(types.values())
        result["failure_type_counts"] = dict(sorted(types.items()))
        result["validation_failure_categories"] = dict(sorted(validation.items()))
        result["request_failure_categories"] = dict(sorted(requests.items()))
        if delivery_rows is not None:
            proof_counts = Counter()
            paired_count = 0
            for paired, status in delivery_rows:
                if paired == 1:
                    paired_count += 1
                proof_counts[status if status in {"on_time", "late", "unknown"} else "unknown"] += 1
            result["delivery_proof_status"] = "available"
            result["delivery_proof_observed_rows"] = len(delivery_rows)
            result["delivery_proof_scope"] = "run_observed_subset_not_released_question_or_seasonal_denominator"
            result["delivery_proof_counts"] = {
                "paired": paired_count,
                "on_time": proof_counts["on_time"],
                "late": proof_counts["late"],
                "unknown": proof_counts["unknown"],
            }

    result["lifetime_phase_counts"] = dict(sorted(lifetime_phases.items()))
    result["lifetime_calls"] = int(lifetime_calls[0])
    result["lifetime_calls_with_recorded_cost"] = int(lifetime_calls[1])
    result["lifetime_calls_missing_cost"] = int(lifetime_calls[2])
    result["lifetime_recorded_cost_sum_usd"] = float(lifetime_calls[3] or 0)
    if start and window_calls is not None:
        result["window_phase_counts"] = dict(sorted(window_phases.items()))
        result["window_calls"] = int(window_calls[0])
        result["window_calls_with_recorded_cost"] = int(window_calls[1])
        result["window_calls_missing_cost"] = int(window_calls[2])
        result["window_recorded_cost_sum_usd"] = float(window_calls[3] or 0)
        result["window_aggregate_status"] = "available"
    result["cost_halt_status"] = _halt_status(raw_meta.get("cost_guard_halt"), "cost_guard_halt" in raw_meta)
    return result


def main() -> int:
    try:
        config = json.loads(Path("config.json").read_text(encoding="utf-8"))
        state_dir = config.get("state_dir", "state")
        if not isinstance(state_dir, str) or not state_dir:
            raise ValueError
        summary = collect_summary(Path(state_dir) / "bot.db")
    except Exception:
        summary = {"summary_status": "unavailable", "unavailable_reason": "summary_generation_failed"}
    rendered = json.dumps(summary, sort_keys=True, separators=(",", ":"), allow_nan=False)
    summary_path = os.getenv("GITHUB_STEP_SUMMARY", "")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as stream:
                stream.write("### Sanitized operational summary\n\n```json\n" + rendered + "\n```\n")
        except OSError:
            pass
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
