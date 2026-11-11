"""Synthetic regression for PR15's sanitized category allowlist and stable projection."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from futureeval_bot.operational_summary import collect_summary


def test_pr15_category_allowlist_and_projection_remain_privacy_limited(tmp_path: Path) -> None:
    db_path = tmp_path / "synthetic.sqlite3"
    config_path = tmp_path / "config.json"
    config_path.write_text('{"state_dir":"unused"}', encoding="utf-8")
    started = datetime.now(timezone.utc) - timedelta(seconds=5)
    started_at = started.isoformat().replace("+00:00", "Z")
    recorded_at = (started + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    unknown_private_category = "synthetic-private-answer-marker"

    with sqlite3.connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE jobs (phase TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE calls (cost REAL, created TEXT NOT NULL);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        db.executemany("INSERT INTO jobs VALUES (?, ?)", [("discovered", recorded_at), ("submitted", recorded_at)])
        db.executemany("INSERT INTO calls VALUES (?, ?)", [(0.0, recorded_at), (None, recorded_at)])
        last_run = {
            "workflow_run_id": "12345",
            "workflow_run_attempt": "1",
            "operation_started_at": started_at,
            "at": recorded_at,
            "failures": [{
                "error_type": "NoValidatedForecast",
                "diagnostics": {
                    "request_failures": {
                        "http_rate_limited": 2,
                        "rate_limit_circuit_open": 3,
                        "readiness_stopped": 4,
                        unknown_private_category: 5,
                    },
                    "validation_failures": {"validation_rejected": 1},
                },
            }],
        }
        db.executemany("INSERT INTO meta VALUES (?, ?)", [("last_run", json.dumps(last_run)),
                                                            ("cost_guard_halt", json.dumps(False))])

    summary = collect_summary(db_path, config_path=config_path, env={
        "BOT_OPERATION_STARTED_AT": started_at, "BOT_WORKFLOW_RUN_ID": "12345",
        "BOT_WORKFLOW_RUN_ATTEMPT": "1", "BOT_SOURCE_SHA": "a" * 40,
        "BOT_OPERATION_OUTCOME": "failure", "BOT_OPERATION_MODE": "live",
        "BOT_TRIGGER_EVENT": "schedule",
    })

    assert set(summary) == {
        "summary_status", "operation_step_outcome", "stage_outcomes", "trigger_event",
        "workflow_run_id", "workflow_run_attempt", "repository", "workflow_path", "requested_mode",
        "source_sha", "public_config_fingerprint_sha256", "config_fingerprint",
        "operation_started_at", "operation_finished_at", "run_binding_status", "failure_count",
        "failure_type_counts", "validation_failure_categories", "request_failure_categories",
        "lifetime_phase_counts", "window_phase_counts", "lifetime_calls",
        "lifetime_calls_with_recorded_cost", "lifetime_calls_missing_cost", "lifetime_recorded_cost_sum_usd",
        "window_calls", "window_calls_with_recorded_cost", "window_calls_missing_cost",
        "window_recorded_cost_sum_usd", "window_aggregate_status", "cost_halt_status",
        "delivery_proof_status", "delivery_proof_observed_rows", "delivery_proof_scope",
        "delivery_proof_counts", "run_attribution_status", "operation_outcome",
        "operation_failure_stage_counts", "operation_failure_category_counts", "run_reservations",
        "run_reservation_status", "run_completed_with_cost_receipt", "run_missing_cost_receipt",
        "run_unknown_request_outcome", "run_recorded_cost_sum_usd", "run_cost_status",
        "run_observed_questions", "run_receipt_counts",
    }
    assert summary["summary_status"] == "available"
    assert summary["operation_step_outcome"] == "failure"
    assert summary["trigger_event"] == "schedule"
    assert (summary["workflow_run_id"], summary["workflow_run_attempt"]) == ("12345", "1")
    assert summary["source_sha"] == "a" * 40
    # Timestamp-only legacy state is never relabeled as this workflow attempt.
    assert summary["run_binding_status"] == "unknown"
    assert summary["failure_count"] is None
    assert summary["validation_failure_categories"] == {}
    assert summary["request_failure_categories"] == {}
    assert unknown_private_category not in json.dumps(summary, sort_keys=True)
    assert summary["lifetime_phase_counts"] == {"discovered": 1, "submitted": 1}
    assert (summary["lifetime_calls"], summary["lifetime_calls_with_recorded_cost"],
            summary["lifetime_calls_missing_cost"], summary["lifetime_recorded_cost_sum_usd"]) == (2, 1, 1, 0.0)
    assert summary["window_calls"] is None
    assert summary["window_recorded_cost_sum_usd"] is None
    assert summary["cost_halt_status"] == "clear"

    mismatched = collect_summary(db_path, config_path=config_path, env={
        "BOT_OPERATION_STARTED_AT": started_at, "BOT_WORKFLOW_RUN_ID": "54321",
        "BOT_WORKFLOW_RUN_ATTEMPT": "1", "BOT_SOURCE_SHA": "a" * 40,
        "BOT_OPERATION_OUTCOME": "failure", "BOT_OPERATION_MODE": "live",
        "BOT_TRIGGER_EVENT": "schedule",
    })
    assert mismatched["run_binding_status"] == "unknown"
    assert mismatched["failure_count"] is None
    assert mismatched["request_failure_categories"] == {}
