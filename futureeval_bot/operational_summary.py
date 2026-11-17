"""Publish fixed, privacy-limited workflow diagnostics from read-only SQLite."""
from __future__ import annotations

import hashlib
import json
import math
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
    "http_other", "request_failed", "rate_limit_circuit_open", "readiness_stopped",
}
_OUTCOMES = {"success", "failure", "cancelled", "skipped", "unknown"}
_EVENTS = {"schedule", "workflow_dispatch", "unknown"}
_OP_STAGES = {"operation_init", "checkpoint_restore", "readiness", "inventory", "research",
    "model_reservation", "model_request", "model_cost_receipt", "forecast_validation", "payload_freeze",
    "forecast_submission", "reasoning_submission", "receipt_verification", "summary_publication", "cleanup", "unknown"}
_OP_CATEGORIES = {"timeout", "transport_unconfirmed", "authentication", "rate_limited", "payment_or_cost",
    "provider_error", "invalid_response", "validation", "deadline", "frozen_payload_conflict",
    "receipt_missing", "checkpoint_stale", "inventory_incomplete", "internal", "other", "unknown"}
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
    public_config_fingerprint = None
    try:
        public_config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
        if isinstance(public_config, dict):
            canonical = json.dumps(public_config, sort_keys=True, separators=(",", ":"), allow_nan=False)
            fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            normalized = {
                "version": "free-baseline-0.1", "spending_ceiling_usd": 0,
                "registration_confirmed": False, "live_enabled": False,
                "expected_bot_user_id": 310267, "season_project": 33121,
                "practice_project": 32977,
                "models": ["nvidia/nemotron-3-super-120b-a12b:free", "google/gemma-4-31b-it:free"],
                "samples_per_question": 2, "max_calls_per_question": 3,
                "max_calls_per_day": 40, "max_output_tokens": 4096,
                "model_timeout_seconds": 150, "question_budget_seconds": 480,
                "deadline_reserve_seconds": 300, "max_questions_per_run": 10,
                "search_results_per_query": 4, "max_pages_to_fetch": 3, "state_dir": "state",
            }
            normalized.update(public_config)
            fingerprint_payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False, allow_nan=False)
            public_config_fingerprint = hashlib.sha256(fingerprint_payload.encode()).hexdigest()[:16]
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
        "repository": env.get("BOT_REPOSITORY") or None,
        "workflow_path": env.get("BOT_WORKFLOW_PATH") or None,
        "requested_mode": env.get("BOT_OPERATION_MODE") or env.get("REQUESTED_MODE") or None,
        "source_sha": source,
        "public_config_fingerprint_sha256": fingerprint,
        "config_fingerprint": public_config_fingerprint,
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
        "run_attribution_status": "unknown",
        "operation_outcome": "unknown",
        "operation_failure_stage_counts": {},
        "operation_failure_category_counts": {},
        "run_reservations": None,
        "run_reservation_status": "unknown",
        "run_completed_with_cost_receipt": None,
        "run_missing_cost_receipt": None,
        "run_unknown_request_outcome": None,
        "run_recorded_cost_sum_usd": None,
        "run_cost_status": "unknown",
        "run_observed_questions": None,
        "run_receipt_counts": None,
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
            db.row_factory = sqlite3.Row
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
            table_names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            repository = env.get("BOT_REPOSITORY", "")
            workflow_path = env.get("BOT_WORKFLOW_PATH", "")
            source = result.get("source_sha")
            config_fingerprint = result.get("config_fingerprint")
            event = result.get("trigger_event")
            mode = result.get("requested_mode")
            exact_context = bool(run_id and attempt and start and source and repository.count("/") == 1
                and workflow_path == ".github/workflows/forecast.yml" and event in {"schedule", "workflow_dispatch"}
                and mode in {"live", "practice"} and config_fingerprint)
            op_id = "/".join((repository, run_id, attempt)) if exact_context else None
            op = None
            call_rows = None
            receipt_rows = None
            if op_id and {"operations", "call_attribution", "receipt_events"} <= table_names:
                op = db.execute(
                    "SELECT operation_id,repository,workflow_path,workflow_run_id,workflow_run_attempt,"
                    "source_sha,config_fingerprint,trigger_event,requested_mode,started_at,finished_at,"
                    "status,failure_stage,failure_category,failure_stage_counts,failure_category_counts,"
                    "checkpoint_continuity,observed_questions FROM operations WHERE operation_id=?", (op_id,)
                ).fetchone()
                if op is not None:
                    call_rows = db.execute(
                        "SELECT c.id,c.status,c.cost,a.workflow_run_id,a.workflow_run_attempt,a.source_sha,"
                        "a.outcome_status,a.cost_receipt_status,a.cost_received_at "
                        "FROM call_attribution a JOIN calls c ON c.id=a.call_id "
                        "WHERE a.operation_id=? ORDER BY c.id", (op_id,)).fetchall()
                    receipt_rows = db.execute(
                        "SELECT question_id,receipt_type,match_status,timing_status,server_at,deadline_at,id "
                        "FROM receipt_events WHERE operation_id=? ORDER BY id", (op_id,)).fetchall()
    except (OSError, sqlite3.Error, ValueError):
        result.update(summary_status="unavailable", unavailable_reason="database_read_failed")
        return result

    result["run_binding_status"] = "unknown"
    if op is not None:
        fields = {"repository": repository, "workflow_path": workflow_path, "workflow_run_id": run_id,
                  "workflow_run_attempt": attempt, "source_sha": source,
                  "config_fingerprint": config_fingerprint, "trigger_event": event,
                  "requested_mode": mode}
        matches = all(op[key] == value for key, value in fields.items())
        matches = matches and _time(op["started_at"]) == start and op["checkpoint_continuity"] == "verified"
        if matches and op["status"] in {"completed", "completed_with_failures", "failed"}:
            row_mismatch = any(row[3] != run_id or row[4] != attempt or row[5] != source for row in call_rows)
            if not row_mismatch:
                result["run_binding_status"] = "bound_complete"
                result["run_attribution_status"] = "complete"
                result["operation_outcome"] = op["status"]
                result["run_observed_questions"] = op["observed_questions"]
                def safe_counter(raw, allowed):
                    try:
                        parsed = json.loads(raw) if isinstance(raw, str) else {}
                    except (TypeError, json.JSONDecodeError):
                        return {}
                    return {k: v for k, v in parsed.items() if k in allowed and type(v) is int and v > 0}
                stage_counts = safe_counter(op["failure_stage_counts"], _OP_STAGES)
                category_counts = safe_counter(op["failure_category_counts"], _OP_CATEGORIES)
                result["operation_failure_stage_counts"] = dict(sorted(stage_counts.items()))
                result["operation_failure_category_counts"] = dict(sorted(category_counts.items()))
                result["failure_count"] = sum(category_counts.values())
                result["failure_type_counts"] = {}
                result["validation_failure_categories"] = {}
                result["request_failure_categories"] = dict(sorted(category_counts.items()))
                result["run_reservations"] = len(call_rows)
                result["run_reservation_status"] = (
                    "no_instrumented_model_reservations" if not call_rows
                    else "complete_bound_ledger"
                )
                completed, missing, unknown, cost_sum = 0, 0, 0, 0.0
                for row in call_rows:
                    finite_cost = type(row[2]) in (int, float) and math.isfinite(row[2])
                    if row[7] == "confirmed" and finite_cost and _time(row[8]):
                        completed += 1
                        cost_sum += float(row[2])
                    else:
                        missing += 1
                    if row[1] != "done" or row[6] != "completed":
                        unknown += 1
                result["run_completed_with_cost_receipt"] = completed
                result["run_missing_cost_receipt"] = missing
                result["run_unknown_request_outcome"] = unknown
                result["run_recorded_cost_sum_usd"] = cost_sum
                halt = _halt_status(raw_meta.get("cost_guard_halt"), "cost_guard_halt" in raw_meta)
                result["cost_halt_status"] = halt
                result["run_cost_status"] = ("unknown" if halt != "clear" or missing or unknown else
                    ("confirmed_zero" if cost_sum == 0 else "confirmed_nonzero"))
                result["window_calls"] = len(call_rows)
                result["window_calls_with_recorded_cost"] = completed
                result["window_calls_missing_cost"] = missing
                result["window_recorded_cost_sum_usd"] = cost_sum
                result["window_aggregate_status"] = "run_bound_complete"

                latest = {}
                for row in receipt_rows:
                    latest[(row[0], row[1])] = row
                per_question = {}
                for (question_id, receipt_type), row in latest.items():
                    per_question.setdefault(question_id, {})[receipt_type] = row
                forecasts = sum("forecast" in items and items["forecast"][2] == "matched"
                                 for items in per_question.values())
                reasoning = sum("reasoning" in items and items["reasoning"][2] == "matched"
                                for items in per_question.values())
                pairs = on_time = late = incomplete = 0
                for items in per_question.values():
                    f, c = items.get("forecast"), items.get("reasoning")
                    if not (f and c and f[2] == c[2] == "matched"):
                        incomplete += 1
                        continue
                    pairs += 1
                    times = (_time(f[4]), _time(c[4]))
                    deadlines = (_time(f[5]), _time(c[5]))
                    if "late" in {f[3], c[3]} or (all(times) and all(deadlines)
                        and any(t > d for t, d in zip(times, deadlines))):
                        late += 1
                    elif f[3] == c[3] == "on_time" and all(times) and all(deadlines):
                        on_time += 1
                    else:
                        incomplete += 1
                receipt_counts = {"forecast_receipts": forecasts, "reasoning_receipts": reasoning,
                    "verified_pairs": pairs, "on_time_proven_pairs": on_time, "late_pairs": late,
                    "unknown_or_incomplete_pairs": incomplete,
                    "scope": "questions_observed_by_this_operation_only_not_official_release_denominator"}
                result["run_receipt_counts"] = receipt_counts
                result["delivery_proof_status"] = "available"
                result["delivery_proof_observed_rows"] = len(receipt_rows)
                result["delivery_proof_scope"] = receipt_counts["scope"]
                result["delivery_proof_counts"] = {"paired": pairs, "on_time": on_time,
                    "late": late, "unknown": incomplete}
        elif matches:
            result["run_binding_status"] = "incomplete"
            result["run_attribution_status"] = "incomplete"
            result["operation_outcome"] = "incomplete"

    result["lifetime_phase_counts"] = dict(sorted(lifetime_phases.items()))
    result["lifetime_calls"] = int(lifetime_calls[0])
    result["lifetime_calls_with_recorded_cost"] = int(lifetime_calls[1])
    result["lifetime_calls_missing_cost"] = int(lifetime_calls[2])
    result["lifetime_recorded_cost_sum_usd"] = float(lifetime_calls[3] or 0)
    if result["cost_halt_status"] == "unknown":
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
