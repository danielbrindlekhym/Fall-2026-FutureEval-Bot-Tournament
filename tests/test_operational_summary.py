import json
import math
import sqlite3
from dataclasses import asdict
import pytest

from futureeval_bot.cloud import signature
from futureeval_bot.core import Config, Contract, State, canonical, utcnow
from futureeval_bot.operational_summary import collect_summary

RUN, ATTEMPT = "50123456789", "2"
SOURCE = "a" * 40
REPOSITORY = "danielbrindlekhym/Fall-2026-FutureEval-Bot-Tournament"
WORKFLOW = ".github/workflows/forecast.yml"
START = "2026-11-10T10:00:00Z"
DEADLINE = "2026-11-11T10:00:00Z"


def _config_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(asdict(Config())), encoding="utf-8")
    return path


def _env(config_path, **overrides):
    env = {
        "BOT_REPOSITORY": REPOSITORY, "BOT_WORKFLOW_PATH": WORKFLOW,
        "BOT_WORKFLOW_RUN_ID": RUN, "BOT_WORKFLOW_RUN_ATTEMPT": ATTEMPT,
        "BOT_SOURCE_SHA": SOURCE, "BOT_TRIGGER_EVENT": "schedule",
        "BOT_OPERATION_MODE": "live", "BOT_OPERATION_STARTED_AT": START,
        "BOT_OPERATION_OUTCOME": "success", "BOT_STEP_CHECKOUT_OUTCOME": "success",
        "BOT_STEP_SETUP_PYTHON_OUTCOME": "success", "BOT_STEP_INSTALL_OUTCOME": "success",
        "BOT_STEP_BASELINE_OUTCOME": "success", "BOT_STEP_VERIFY_OUTCOME": "success",
        "BOT_STEP_RESTORE_OUTCOME": "success", "BOT_STEP_RECORD_OUTCOME": "success",
    }
    env.update(overrides)
    return env


def _state(path, config_path, run=RUN, attempt=ATTEMPT):
    state = State(path)
    state.checkpoint = lambda _state: None
    config = Config()
    assert state.start_operation({"repository": REPOSITORY, "workflow_path": WORKFLOW,
        "workflow_run_id": run, "workflow_run_attempt": attempt, "source_sha": SOURCE,
        "config_fingerprint": config.fingerprint, "trigger_event": "schedule",
        "requested_mode": "live", "started_at": START})
    return state, config


def _finish(state, status="completed", **kwargs):
    state.finish_operation(status, **kwargs)
    state.close()


def test_exact_operation_binds_only_its_run_attempt_and_source(tmp_path):
    db = tmp_path / "state.db"
    config_path = _config_file(tmp_path)
    state, config = _state(db, config_path)
    call = state.reserve_call(11, config.models[0], config)
    state.finish_call(call, {"usage": {"cost": 0}}, 0)
    other, other_config = _state(db, config_path, run=RUN, attempt="3")
    call2 = other.reserve_call(12, other_config.models[0], other_config)
    other.finish_call(call2, {"usage": {"cost": 0}}, 0)
    other.finish_operation("completed")
    state.finish_operation("completed")
    state.close()
    result = collect_summary(db, config_path=config_path, env=_env(config_path))
    assert result["run_binding_status"] == "bound_complete"
    assert result["run_reservations"] == 1
    assert result["run_reservation_status"] == "complete_bound_ledger"
    assert result["run_completed_with_cost_receipt"] == 1
    assert result["run_recorded_cost_sum_usd"] == 0
    assert result["run_cost_status"] == "confirmed_zero"


def test_missing_mismatched_or_stale_identity_is_unknown_not_zero(tmp_path):
    db, config_path = tmp_path / "state.db", _config_file(tmp_path)
    state, _ = _state(db, config_path)
    state.finish_operation("completed")
    state.close()
    for env in (_env(config_path, BOT_WORKFLOW_RUN_ID=""),
                _env(config_path, BOT_WORKFLOW_RUN_ATTEMPT="3"),
                _env(config_path, BOT_SOURCE_SHA="b" * 40),
                _env(config_path, BOT_OPERATION_STARTED_AT="2026-11-10T10:01:00Z")):
        result = collect_summary(db, config_path=config_path, env=env)
        assert result["run_binding_status"] == "unknown"
        assert result["run_reservations"] is None
        assert result["run_recorded_cost_sum_usd"] is None


def test_legacy_checkpoint_schema_compares_as_empty_additive_tables(tmp_path):
    old = sqlite3.connect(tmp_path / "old.db")
    old.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE jobs(question_id INTEGER PRIMARY KEY,project INTEGER NOT NULL,contract TEXT NOT NULL,
            config TEXT NOT NULL,phase TEXT NOT NULL DEFAULT 'discovered',research TEXT,payload TEXT,
            explanation TEXT,samples TEXT,comment_id INTEGER,error TEXT,created TEXT NOT NULL,updated TEXT NOT NULL);
        CREATE TABLE calls(id INTEGER PRIMARY KEY,question_id INTEGER NOT NULL,day TEXT NOT NULL,
            model TEXT NOT NULL,status TEXT NOT NULL,response TEXT,cost REAL,created TEXT NOT NULL);
    """)
    new = State(tmp_path / "new.db")
    assert signature(old) == signature(new.db)
    old.close()
    new.close()


def test_delivery_proof_mutation_changes_checkpoint_signature(tmp_path):
    state = State(tmp_path / "state.db")
    before = signature(state.db)
    state.db.execute(
        "INSERT INTO delivery_proof(question_id,workflow_run_id,workflow_run_attempt,paired,status) "
        "VALUES(?,?,?,?,?)", (41, RUN, ATTEMPT, 1, "on_time"))
    state.db.commit()
    assert signature(state.db) != before
    state.close()


def test_no_validated_forecast_is_published_as_validation_failure(tmp_path):
    import time
    from futureeval_bot.__main__ import _fixed_failure_category
    from futureeval_bot.forecast import NoValidatedForecast, prepare

    db, config_path = tmp_path / "state.db", _config_file(tmp_path)
    state, config = _state(db, config_path)
    contract = Contract(question_id=41, post_id=42, kind="binary", title="synthetic",
        background="", resolution="", fine_print="", close_time=DEADLINE,
        open_time=None, project=33121)
    state.discover(contract, config)
    call = state.reserve_call(contract.question_id, config.models[0], config)
    state.finish_call(call, {"choices": [{"message": {"content": "not JSON"}}]}, 0)
    with pytest.raises(NoValidatedForecast) as raised:
        prepare(state, contract, config, model=None, evidence={"sources": []},
                stop_at=time.monotonic() - 1)
    stage = state.operation_stage
    category = _fixed_failure_category(raised.value, stage)
    state.finish_operation("failed", failures=[{"stage": stage, "failure_category": category}])
    state.close()

    summary = collect_summary(db, config_path=config_path, env=_env(
        config_path, BOT_OPERATION_OUTCOME="failure"))
    assert stage == "forecast_validation"
    assert category == "validation"
    assert summary["operation_failure_stage_counts"] == {"forecast_validation": 1}
    assert summary["operation_failure_category_counts"] == {"validation": 1}


def test_incomplete_reservation_is_counted_but_cost_and_request_stay_unknown(tmp_path):
    db, config_path = tmp_path / "state.db", _config_file(tmp_path)
    state, config = _state(db, config_path)
    state.reserve_call(13, config.models[0], config)
    state.finish_operation("failed", failure_stage="model_request", failure_category="timeout")
    state.close()
    result = collect_summary(db, config_path=config_path, env=_env(config_path))
    assert result["run_reservations"] == 1
    assert result["run_unknown_request_outcome"] == 1
    assert result["run_missing_cost_receipt"] == 1
    assert result["run_cost_status"] == "unknown"
    assert result["operation_failure_stage_counts"] == {"model_request": 1}
    assert result["cost_halt_status"] == "clear"


def test_complete_zero_reservations_are_instrumented_zero_not_no_remote_effects(tmp_path):
    db, config_path = tmp_path / "state.db", _config_file(tmp_path)
    state, _ = _state(db, config_path)
    state.finish_operation("completed", observed_questions=0)
    state.close()
    result = collect_summary(db, config_path=config_path, env=_env(config_path))
    assert result["run_reservations"] == 0
    assert result["run_reservation_status"] == "no_instrumented_model_reservations"
    assert result["run_cost_status"] == "confirmed_zero"
    assert "no_remote_effects" not in json.dumps(result)

    early = collect_summary(db, config_path=config_path, env=_env(config_path, BOT_WORKFLOW_RUN_ID="50123456780"))
    assert early["run_reservations"] is None


def test_partial_receipts_and_missing_server_times_are_not_on_time_pairs(tmp_path):
    db, config_path = tmp_path / "state.db", _config_file(tmp_path)
    state, _ = _state(db, config_path)
    state.append_receipt_event(21, "forecast", "matched", "unknown", None, DEADLINE)
    state.append_receipt_event(22, "forecast", "matched", "unknown", "2026-11-10T11:00:00Z", DEADLINE)
    state.append_receipt_event(22, "reasoning", "mismatched", "unknown", "2026-11-10T11:01:00Z", DEADLINE)
    state.append_receipt_event(23, "forecast", "matched", "unknown", None, DEADLINE)
    state.append_receipt_event(23, "reasoning", "matched", "unknown", "2026-11-10T11:01:00Z", DEADLINE)
    state.finish_operation("completed")
    state.close()
    counts = collect_summary(db, config_path=config_path, env=_env(config_path))["run_receipt_counts"]
    assert counts["forecast_receipts"] == 3
    assert counts["reasoning_receipts"] == 1
    assert counts["verified_pairs"] == 1
    assert counts["on_time_proven_pairs"] == 0
    assert counts["unknown_or_incomplete_pairs"] == 3


def test_valid_pair_on_time_late_and_unknown_deadline_are_distinguished(tmp_path):
    db, config_path = tmp_path / "state.db", _config_file(tmp_path)
    state, _ = _state(db, config_path)
    for q, ftime, ctime, deadline, timing in [
        (31, "2026-11-10T11:00:00Z", "2026-11-10T11:01:00Z", DEADLINE, "on_time"),
        (32, "2026-11-12T11:00:00Z", "2026-11-10T11:01:00Z", DEADLINE, "late"),
        (33, None, "2026-11-10T11:01:00Z", DEADLINE, "unknown"),
    ]:
        state.append_receipt_event(q, "forecast", "matched", timing, ftime, deadline)
        state.append_receipt_event(q, "reasoning", "matched", timing, ctime, deadline)
    state.finish_operation("completed")
    state.close()
    counts = collect_summary(db, config_path=config_path, env=_env(config_path))["run_receipt_counts"]
    assert counts["verified_pairs"] == 3
    assert counts["on_time_proven_pairs"] == 1
    assert counts["late_pairs"] == 1
    assert counts["unknown_or_incomplete_pairs"] == 1


def test_missing_summary_can_be_reconstructed_from_durable_bound_ledger(tmp_path):
    db, config_path = tmp_path / "state.db", _config_file(tmp_path)
    state, config = _state(db, config_path)
    call = state.reserve_call(41, config.models[0], config)
    state.finish_call(call, {"usage": {"cost": 0}}, 0)
    state.finish_operation("completed")
    state.close()  # No summary artifact is written by the producer in this test.
    result = collect_summary(db, config_path=config_path, env=_env(config_path))
    assert result["run_reservations"] == 1
    assert result["run_completed_with_cost_receipt"] == 1


def test_output_uses_allowlisted_counts_and_fixed_failure_categories_only(tmp_path):
    db, config_path = tmp_path / "state.db", _config_file(tmp_path)
    state, _ = _state(db, config_path)
    state.finish_operation("failed", failure_stage="receipt_verification", failure_category="receipt_missing")
    state.close()
    rendered = json.dumps(collect_summary(db, config_path=config_path, env=_env(config_path)))
    for forbidden in ("question_id", "forecast_values", "response", "payload", "explanation", "secret", "raw exception"):
        assert forbidden not in rendered
    assert "receipt_missing" in rendered
    assert not math.isnan(json.loads(rendered)["run_recorded_cost_sum_usd"] or 0) if json.loads(rendered)["run_recorded_cost_sum_usd"] is not None else True
