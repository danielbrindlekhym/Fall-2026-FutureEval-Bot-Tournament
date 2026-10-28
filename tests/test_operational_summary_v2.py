import hashlib
import ast
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
START = "2026-10-28T10:00:00Z"
RUN_ID = "987654321"
ATTEMPT = "2"
SECRET_SENTINELS = {
    "METACULUS_TOKEN": "PRIVATE_METACULUS_SENTINEL",
    "OPENROUTER_API_KEY": "PRIVATE_OPENROUTER_SENTINEL",
    "GITHUB_TOKEN": "PRIVATE_GITHUB_SENTINEL",
}
STAGES = {
    "checkout": "BOT_STEP_CHECKOUT_OUTCOME",
    "setup_python": "BOT_STEP_SETUP_PYTHON_OUTCOME",
    "install_dependencies": "BOT_STEP_INSTALL_OUTCOME",
    "verify_baseline": "BOT_STEP_BASELINE_OUTCOME",
    "verify_reliability": "BOT_STEP_VERIFY_OUTCOME",
    "restore_progress": "BOT_STEP_RESTORE_OUTCOME",
    "record_verification": "BOT_STEP_RECORD_OUTCOME",
    "run_selected_operation": "BOT_OPERATION_OUTCOME",
}


def _workflow_text():
    return (ROOT / ".github/workflows/forecast.yml").read_text(encoding="utf-8")


def _base_env(tmp_path, *, failed_stage=None, missing_stage=None, run_id=RUN_ID, attempt=ATTEMPT, started=START):
    # Model the job-level environment and then apply the actual summary-step overrides.
    env = dict(os.environ)
    env.update(SECRET_SENTINELS)
    for name in SECRET_SENTINELS:
        assert f"{name}: ''" in _workflow_text()
        env[name] = ""
    env.update({
        "BOT_OPERATION_STARTED_AT": started,
        "BOT_WORKFLOW_RUN_ID": run_id,
        "BOT_WORKFLOW_RUN_ATTEMPT": attempt,
        "BOT_OPERATION_OUTCOME": "failure" if failed_stage == "run_selected_operation" else "success",
        "BOT_OPERATION_MODE": "live",
        "BOT_TRIGGER_EVENT": "schedule",
        "BOT_SOURCE_SHA": "1c9fc16ef8c4752472e712a9d3f3c1707e779da7",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
    })
    failed_index = list(STAGES).index(failed_stage) if failed_stage in STAGES else None
    for index, (stage, variable) in enumerate(STAGES.items()):
        env[variable] = ("failure" if stage == failed_stage else
                         "skipped" if failed_index is not None and index > failed_index else "success")
    if missing_stage in STAGES:
        env.pop(STAGES[missing_stage], None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _seed_database(path, *, last_run="matched", halt="false"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE jobs(phase TEXT NOT NULL, updated TEXT NOT NULL, contract TEXT, payload TEXT,
                              explanation TEXT, research TEXT, error TEXT);
            CREATE TABLE calls(id INTEGER PRIMARY KEY, status TEXT, cost REAL, created TEXT,
                               question_id INTEGER, response TEXT);
        """)
        db.executemany("INSERT INTO jobs(phase,updated,contract,payload,explanation,research,error) VALUES(?,?,?,?,?,?,?)", [
            ("verified", "2026-10-27T10:00:00Z", "PRIVATE_CONTRACT", "PRIVATE_ANSWER", "PRIVATE_REASON", "PRIVATE_RESEARCH", "PRIVATE_ERROR"),
            ("missed", "2026-10-28T10:20:00Z", "PRIVATE_CONTRACT_2", "PRIVATE_ANSWER_2", "PRIVATE_REASON_2", "PRIVATE_RESEARCH_2", "PRIVATE_ERROR_2"),
        ])
        db.executemany("INSERT INTO calls(status,cost,created,question_id,response) VALUES(?,?,?,?,?)", [
            ("done", 0.25, "2026-10-27T10:00:00Z", 88001, "PRIVATE_RESPONSE"),
            ("done", 0.0, "2026-10-28T10:15:00Z", 88002, "PRIVATE_RESPONSE_2"),
            ("started", None, "2026-10-28T10:30:00Z", 88003, "PRIVATE_RESPONSE_3"),
        ])
        db.execute("INSERT INTO meta VALUES('launch_time',?)", (json.dumps("2026-10-11T16:28:37.883948+00:00"),))
        if halt is not None:
            db.execute("INSERT INTO meta VALUES('cost_guard_halt',?)", (halt,))
        if last_run == "matched":
            record = {
                "at": "2026-10-28T10:40:00Z",
                "operation_started_at": START,
                "workflow_run_id": RUN_ID,
                "workflow_run_attempt": ATTEMPT,
                "failures": [{
                    "question_id": 77123,
                    "error_type": "NoValidatedForecast",
                    "error": "PRIVATE_RAW_EXCEPTION",
                    "diagnostics": {
                        "validation_failures": {"invalid_json": 2, "PRIVATE_CATEGORY": 5},
                        "request_failures": {"transport_unconfirmed": 1},
                        "private_text": "PRIVATE_REASONING_SENTINEL",
                    },
                }],
            }
            db.execute("INSERT INTO meta VALUES('last_run',?)", (json.dumps(record),))
        elif isinstance(last_run, dict):
            db.execute("INSERT INTO meta VALUES('last_run',?)", (json.dumps(last_run),))
        elif isinstance(last_run, str):
            db.execute("INSERT INTO meta VALUES('last_run',?)", (last_run,))


def _run_cli(tmp_path, *, last_run="matched", halt="false", failed_stage=None,
             missing_stage=None, run_id=RUN_ID, attempt=ATTEMPT, started=START):
    project = tmp_path / "project"
    shutil.copytree(ROOT / "futureeval_bot", project / "futureeval_bot")
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    config["state_dir"] = "state"
    (project / "config.json").write_text(json.dumps(config), encoding="utf-8")
    db = project / "state/bot.db"
    _seed_database(db, last_run=last_run, halt=halt)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    env = _base_env(tmp_path, failed_stage=failed_stage, missing_stage=missing_stage,
                    run_id=run_id, attempt=attempt, started=started)
    command = [sys.executable, "-m", "futureeval_bot.operational_summary"]
    completed = subprocess.run(command, cwd=project, env=env, capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    return result, completed, summary, env


def test_real_cli_is_sanitized_and_separates_lifetime_from_run_window(tmp_path):
    result, completed, summary, env = _run_cli(tmp_path)
    assert all(env[name] == "" for name in SECRET_SENTINELS)
    assert result["run_binding_status"] == "matched"
    assert result["failure_count"] == 1
    assert result["failure_type_counts"] == {"NoValidatedForecast": 1}
    assert result["validation_failure_categories"] == {"invalid_json": 2, "other": 5}
    assert result["request_failure_categories"] == {"transport_unconfirmed": 1}
    assert result["lifetime_calls"] == 3 and result["window_calls"] == 2
    assert result["lifetime_calls_missing_cost"] == 1 and result["window_calls_missing_cost"] == 1
    assert result["lifetime_recorded_cost_sum_usd"] == 0.25
    assert result["window_recorded_cost_sum_usd"] == 0.0
    assert result["lifetime_phase_counts"] == {"missed": 1, "verified": 1}
    assert result["window_phase_counts"] == {"missed": 1}
    assert result["cost_halt_status"] == "clear"
    assert result["source_sha"] == "1c9fc16ef8c4752472e712a9d3f3c1707e779da7"
    assert len(result["public_config_fingerprint_sha256"]) == 64
    for forbidden in (*SECRET_SENTINELS.values(), "PRIVATE_", "77123", "88001", "88002", "88003"):
        assert forbidden not in completed.stdout + summary


@pytest.mark.parametrize("case", ["missing", "stale", "run_mismatch", "attempt_mismatch", "malformed"])
def test_early_or_unbound_records_never_become_success_or_zero_calls(tmp_path, case):
    record = None
    if case == "stale":
        record = {"at": "2026-10-28T10:40:00Z", "operation_started_at": "2026-10-28T09:00:00Z",
                  "workflow_run_id": RUN_ID, "workflow_run_attempt": ATTEMPT, "failures": []}
    elif case == "run_mismatch":
        record = {"at": "2026-10-28T10:40:00Z", "operation_started_at": START,
                  "workflow_run_id": "111", "workflow_run_attempt": ATTEMPT, "failures": []}
    elif case == "attempt_mismatch":
        record = {"at": "2026-10-28T10:40:00Z", "operation_started_at": START,
                  "workflow_run_id": RUN_ID, "workflow_run_attempt": "1", "failures": []}
    elif case == "malformed":
        record = "not-json"
    result, _, _, _ = _run_cli(tmp_path, last_run=record if case != "missing" else None,
                               failed_stage="run_selected_operation")
    assert result["operation_step_outcome"] == "failure"
    assert result["run_binding_status"] == "unknown"
    assert result["failure_count"] is None
    assert result["failure_type_counts"] == {}
    assert result["window_calls"] == 2
    assert result["window_aggregate_status"] == "available"


@pytest.mark.parametrize("stage", list(STAGES))
def test_every_workflow_stage_outcome_is_fixed_enum_and_preserved(tmp_path, stage):
    result, _, _, _ = _run_cli(tmp_path, failed_stage=stage)
    assert result["stage_outcomes"][stage] == "failure"
    operation_index = list(STAGES).index("run_selected_operation")
    failed_index = list(STAGES).index(stage)
    assert result["operation_step_outcome"] == ("failure" if stage == "run_selected_operation" else
                                                "skipped" if failed_index < operation_index else "success")
    assert set(result["stage_outcomes"].values()) <= {"success", "failure", "cancelled", "skipped", "unknown"}


def test_missing_baseline_fetch_outcome_is_unknown(tmp_path):
    result, _, _, _ = _run_cli(tmp_path, missing_stage="verify_baseline")
    assert result["stage_outcomes"]["verify_baseline"] == "unknown"


@pytest.mark.parametrize("raw,present,expected", [
    (None, False, "clear"),
    ("false", True, "clear"),
    ('{"call_id":7,"reason":"pending_zero_cost_receipt","at":"2026-10-28T10:20:00Z"}', True, "active"),
    # This marker shape belongs to a different source version; exact deployed
    # 1c records the pending_zero_cost_receipt reason instead.
    ('{"call_id":7,"reported_cost":0.25,"at":"2026-10-28T10:20:00Z"}', True, "unknown"),
    ("null", True, "unknown"),
    ("{malformed", True, "unknown"),
    ('{"call_id":7,"reason":"unrecognized","at":"2026-10-28T10:20:00Z"}', True, "unknown"),
])
def test_cost_halt_tri_state(tmp_path, raw, present, expected):
    # None means omit marker; malformed strings are stored verbatim as fixture data.
    halt = raw if present else None
    result, _, _, _ = _run_cli(tmp_path, halt=halt)
    assert result["cost_halt_status"] == expected


def test_operation_timestamp_is_exported_in_same_shell_before_bot_launch():
    # GITHUB_ENV values become visible to later steps, not to another process
    # in the step that appends them. The bot must receive this value directly.
    workflow = _workflow_text()
    operation = workflow.split("id: run_selected_operation", 1)[1].split(
        "name: Publish sanitized operational summary", 1
    )[0]
    assert "export BOT_OPERATION_STARTED_AT=" in operation or "BOT_OPERATION_STARTED_AT=\"$(date" in operation
    assert "|| BOT_OPERATION_STARTED_AT=''" in operation


def test_workflow_retains_pr12_guards_and_uses_secret_empty_always_summary():
    workflow = _workflow_text()
    assert "cron: '7,27,47 * * * *'" in workflow
    assert "group: futureeval-bot-single-worker" in workflow and "cancel-in-progress: false" in workflow
    assert "baseline_commit='f0a423142e6ccb499ba483fe769b8cfcb8e88b30'" in workflow
    assert "expected_tree='6aab6dd6cb39d5feeebf6cf1f47a9e6f07309fd5'" in workflow
    assert 'python -m futureeval_bot "$REQUESTED_MODE" --max-questions 5' in workflow
    assert "id: verify_baseline" in workflow
    assert "BOT_STEP_BASELINE_OUTCOME: ${{ steps.verify_baseline.outcome || 'unknown' }}" in workflow
    assert "python -m futureeval_bot.operational_summary" in workflow
    assert "if: always()" in workflow and "continue-on-error: true" in workflow
    for name in SECRET_SENTINELS:
        assert f"{name}: ''" in workflow
    for stage in STAGES.values():
        assert stage in workflow
    assert "METACULUS_TOKEN: ${{ secrets.METACULUS_TOKEN }}" in workflow
    assert "OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}" in workflow
    assert "GITHUB_TOKEN: ${{ github.token }}" in workflow


def test_producer_has_only_allowlisted_imports_and_read_only_sqlite_access():
    source = (ROOT / "futureeval_bot/operational_summary.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Syntax-aware import policy avoids false positives such as the local
    # variable `requests`, while rejecting network, dotenv, bot-state and
    # dynamic-import escape routes.
    allowed_imports = {
        "__future__", "hashlib", "json", "os", "re", "sqlite3",
        "collections", "datetime", "pathlib",
    }
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
            assert node.level == 0, "summary must not import package-local bot modules"
        elif isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else "")
            assert name not in {"__import__", "eval", "exec", "urlopen", "create_connection", "request"}
    assert imported <= allowed_imports
    assert "sqlite3" in imported

    sql_literals = [node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and node.value.lstrip().upper().startswith(("SELECT ", "PRAGMA "))]
    assert sql_literals
    normalized_sql = " ".join(sql_literals).upper()
    assert "SELECT *" not in normalized_sql
    assert "RESPONSE" not in normalized_sql and "PAYLOAD" not in normalized_sql
    assert "MODE=RO" in source.upper() and "PRAGMA QUERY_ONLY=ON" in normalized_sql


def test_workflow_run_correlation_hook_is_only_added_to_final_run_record():
    source = (ROOT / "futureeval_bot/__main__.py").read_text(encoding="utf-8")
    assert '"workflow_run_id": workflow_run_id' in source
    assert '"workflow_run_attempt": workflow_run_attempt' in source
    assert '"operation_started_at": operation_started_at' in source
    assert "workflow_run_id=workflow_run_id if workflow_run_id.isdigit() else None" in source
    # Guard anchors are public behavior from the pinned production baseline;
    # avoid depending on a sibling source-copy directory in repository CI.
    for anchor in ("live_gate(config, state)", "configured_cloud()", "state.set_meta(\"last_run\"", "if result[\"failures\"]"):
        assert anchor in source
