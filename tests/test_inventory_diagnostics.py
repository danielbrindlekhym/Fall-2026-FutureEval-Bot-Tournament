from __future__ import annotations

import sqlite3
import sys
import json
import hashlib
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from futureeval_bot.api import FeedPaginationIncomplete, Metaculus, ServiceError
import futureeval_bot.__main__ as entry
from futureeval_bot.inventory_diagnostics import (
    BINDING_KEYS, FAILURE_SUBTYPES, InventoryDiagnostics, UNKNOWN_DIAGNOSTICS,
    collect_inventory, summary_diagnostics, validate_diagnostics,
)
from futureeval_bot.operational_summary import _summary_inventory_diagnostics, collect_summary


class FakeState:
    def __init__(self):
        self.active_operation_id = None
        self.discovered = []
        self.metadata = {}
        self.operation_stage = None

    def discover(self, contract, config):
        self.discovered.append(contract.question_id)

    def set_meta(self, key, value):
        self.metadata[key] = value

    def set_operation_stage(self, stage):
        self.operation_stage = stage


class InventoryDiagnosticsTests(unittest.TestCase):
    def good_complete(self):
        return {
            "schema_version": 1, "status": "complete", "failure_subtype": "none",
            "pagination_disposition": "terminal_no_next", "pages_completed": 2,
            "items_received": 120, "last_page_items": 20, "pagination_complete": True,
        }

    def test_schema_enum_null_bounds_and_complete_semantics(self):
        good = self.good_complete()
        self.assertEqual(validate_diagnostics(good), good)
        for subtype in FAILURE_SUBTYPES - {"none"}:
            failed = {**good, "status": "failed", "failure_subtype": subtype,
                      "pagination_disposition": "unknown", "pagination_complete": None}
            self.assertIsNotNone(validate_diagnostics(failed), subtype)
        invalid = [
            {**good, "schema_version": True},
            {**good, "status": ["complete"]},
            {**good, "failure_subtype": "raw exception"},
            {**good, "pagination_disposition": "external URL"},
            {**good, "pages_completed": -1},
            {**good, "pages_completed": 31},
            {**good, "items_received": 3001},
            {**good, "last_page_items": 101},
            {**good, "items_received": 10, "last_page_items": 20},
            {**good, "items_received": 300, "pages_completed": 2},
            {**good, "pagination_complete": 1},
            {**good, "extra": "private"},
            {**good, "status": "complete", "failure_subtype": "unknown"},
            {**good, "status": "complete", "pagination_disposition": "continuation_rejected",
             "pagination_complete": False},
            {**good, "status": "partial", "pagination_complete": True},
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(validate_diagnostics(value))
        self.assertEqual(validate_diagnostics(UNKNOWN_DIAGNOSTICS), UNKNOWN_DIAGNOSTICS)
        partial = {**good, "status": "partial", "failure_subtype": "continuation_missing",
                   "pagination_disposition": "continuation_rejected", "pages_completed": 1,
                   "items_received": 10, "last_page_items": 10, "pagination_complete": False}
        self.assertEqual(validate_diagnostics(partial), partial)

    def test_exact_operation_binding_and_unknown_fallback(self):
        binding = {
            "operation_id": "repo/123/1", "repository": "repo", "workflow_path": ".github/workflows/forecast.yml",
            "workflow_run_id": "123", "workflow_run_attempt": "1", "source_sha": "a" * 40,
            "config_fingerprint": "0123456789abcdef", "started_at": "2026-11-18T00:00:00Z",
        }
        self.assertEqual(set(binding), BINDING_KEYS)
        record = {"binding": binding, "diagnostics": self.good_complete()}
        self.assertEqual(summary_diagnostics(record, binding, "completed"), self.good_complete())
        self.assertEqual(_summary_inventory_diagnostics(record, binding, "completed"),
                         summary_diagnostics(record, binding, "completed"))
        self.assertEqual(summary_diagnostics(record, {**binding, "source_sha": "b" * 40}, "completed"),
                         UNKNOWN_DIAGNOSTICS)
        self.assertEqual(_summary_inventory_diagnostics(record, {**binding, "source_sha": "b" * 40}, "completed"),
                         summary_diagnostics(record, {**binding, "source_sha": "b" * 40}, "completed"))
        self.assertEqual(summary_diagnostics(record, binding, "failed"), UNKNOWN_DIAGNOSTICS)
        self.assertEqual(_summary_inventory_diagnostics(record, binding, "failed"),
                         summary_diagnostics(record, binding, "failed"))
        self.assertEqual(summary_diagnostics(None, binding, "completed"), UNKNOWN_DIAGNOSTICS)
        self.assertEqual(_summary_inventory_diagnostics(None, binding, "completed"),
                         summary_diagnostics(None, binding, "completed"))
        self.assertEqual(summary_diagnostics({"binding": binding, "diagnostics": {
            **self.good_complete(), "pages_completed": 31}}, binding, "completed"), UNKNOWN_DIAGNOSTICS)
        malformed = {"binding": binding, "diagnostics": {**self.good_complete(), "pages_completed": 31}}
        self.assertEqual(_summary_inventory_diagnostics(malformed, binding, "completed"),
                         summary_diagnostics(malformed, binding, "completed"))

    def _remote(self, response):
        remote = Metaculus.__new__(Metaculus)
        remote.timeout = 25
        remote.next_request = 0.0
        remote.last_request_failure_subtype = "unknown"
        remote.inventory_failure_subtype = "unknown"
        remote.inventory_pagination_disposition = "not_started"
        remote.feed_pages = []
        remote.request = lambda method, path, **kwargs: response
        return remote

    def _params(self):
        return {"tournaments": 33121, "include_descriptions": "true", "with_cp": "false",
                "limit": 100, "offset": 0, "order_by": "scheduled_close_time"}

    def _continuation(self, offset):
        params = self._params()
        params["offset"] = offset
        return "https://www.metaculus.com/api/posts/?" + urlencode(params)

    def test_every_invalid_continuation_fails_closed_after_one_page(self):
        cases = [
            ("missing-next", {}, "continuation_missing"),
            ("empty-next", {"next": ""}, "continuation_missing"),
            ("fragment", {"next": self._continuation(100) + "#page"}, "continuation_fragment"),
            ("missing-query", {"next": "https://www.metaculus.com/api/posts/"}, "continuation_query_missing"),
            ("wrong-origin", {"next": "https://evil.example/api/posts/?offset=100"}, "continuation_origin_or_path"),
            ("wrong-path", {"next": "https://www.metaculus.com/api/elsewhere/?offset=100"}, "continuation_origin_or_path"),
            ("bad-query-syntax", {"next": "https://www.metaculus.com/api/posts/?offset"}, "continuation_query_syntax"),
            ("invalid-offset", {"next": "https://www.metaculus.com/api/posts/?" + urlencode(
                {**self._params(), "offset": "NaN"})}, "continuation_offset_invalid"),
            ("filter-mismatch", {"next": "https://www.metaculus.com/api/posts/?offset=100"}, "continuation_filter_mismatch"),
            ("repeat", {"next": self._continuation(0)}, "continuation_repeated_or_nonadvancing"),
            ("gap", {"next": self._continuation(50)}, "continuation_offset_invalid"),
        ]
        for name, extra, subtype in cases:
            with self.subTest(name=name):
                page = {"results": [{"question": {"type": "binary"}}], **extra}
                remote = self._remote(page)
                with self.assertRaises(FeedPaginationIncomplete) as caught:
                    remote.posts(33121)
                self.assertEqual(caught.exception.failure_subtype, subtype)
                self.assertEqual(caught.exception.pagination_disposition, "continuation_rejected")
                self.assertEqual(caught.exception.pages_completed, 1)
                self.assertEqual(caught.exception.items_received, 1)
                self.assertEqual(len(remote.feed_pages), 1)

    def test_page_30_valid_continuation_fails_without_request_31(self):
        remote = Metaculus.__new__(Metaculus)
        remote.timeout = 25
        remote.next_request = 0.0
        remote.last_request_failure_subtype = "unknown"
        remote.inventory_failure_subtype = "unknown"
        remote.inventory_pagination_disposition = "not_started"
        remote.feed_pages = []
        calls = []
        def request(method, path, **kwargs):
            calls.append(kwargs["params"]["offset"])
            next_offset = kwargs["params"]["offset"] + 100
            return {"results": [{"question": {"type": "unsupported"}} for _ in range(100)],
                    "next": self._continuation(next_offset)}
        remote.request = request
        with self.assertRaises(FeedPaginationIncomplete) as caught:
            remote.posts(33121)
        self.assertEqual(caught.exception.failure_subtype, "page_limit_exhausted")
        self.assertEqual(caught.exception.pagination_disposition, "page_limit_reached")
        self.assertEqual(caught.exception.pages_completed, 30)
        self.assertEqual(caught.exception.items_received, 3000)
        self.assertEqual(len(calls), 30)

    def test_page_30_terminal_and_multi_page_contiguous_feed_complete_at_existing_cap(self):
        remote = Metaculus.__new__(Metaculus)
        remote.timeout, remote.next_request = 25, 0.0
        remote.last_request_failure_subtype = "unknown"
        calls = []
        def request(method, path, **kwargs):
            offset = kwargs["params"]["offset"]
            calls.append(offset)
            results = [{"question": {"type": "unsupported"}}]
            return {"results": results, "next": self._continuation(offset + 1) if offset == 0 else None}
        remote.request = request
        posts = remote.posts(33121)
        self.assertEqual(len(posts), 2)
        self.assertEqual(calls, [0, 1])
        self.assertEqual(remote.inventory_progress(), {
            "pages_completed": 2, "items_received": 2, "last_page_items": 1,
            "pagination_disposition": "terminal_no_next", "pagination_complete": True})

        remote = Metaculus.__new__(Metaculus)
        remote.timeout, remote.next_request = 25, 0.0
        remote.last_request_failure_subtype = "unknown"
        calls = []
        def terminal_request(method, path, **kwargs):
            offset = kwargs["params"]["offset"]
            calls.append(offset)
            results = [{"question": {"type": "unsupported"}} for _ in range(100)]
            return {"results": results, "next": None if offset == 2900 else self._continuation(offset + 100)}
        remote.request = terminal_request
        posts = remote.posts(33121)
        self.assertEqual(len(posts), 3000)
        self.assertEqual(len(calls), 30)
        self.assertEqual(calls[-1], 2900)
        self.assertTrue(remote.inventory_progress()["pagination_complete"])

    def test_request_failures_map_to_fixed_transport_and_http_enums(self):
        class Session:
            def __init__(self, result):
                self.result = result
                self.calls = 0
            def request(self, *args, **kwargs):
                self.calls += 1
                if isinstance(self.result, BaseException):
                    raise self.result
                return self.result
        class Response:
            def __init__(self, status, headers=None, content=b"{}"):
                self.status_code, self.headers, self.content = status, headers or {}, content
                self.ok = 200 <= status < 300
            def json(self):
                if self.content == b"invalid":
                    raise ValueError("synthetic")
                return {}
        cases = [
            (requests.Timeout(), "transport_timeout"),
            (requests.ConnectionError(), "transport_connection"),
            (requests.RequestException(), "transport_other"),
        ]
        for error, subtype in cases:
            remote = Metaculus.__new__(Metaculus)
            remote.timeout, remote.next_request = 1, 0
            remote.last_request_failure_subtype = "unknown"
            remote.session = Session(error)
            with patch("futureeval_bot.api.time.sleep", lambda _: None):
                with self.assertRaises(ServiceError):
                    remote.request("GET", "/posts/")
            self.assertEqual(remote.last_request_failure_subtype, subtype)
            self.assertEqual(remote.session.calls, 3)
        for status, subtype in [(401, "http_auth"), (403, "http_auth"), (404, "http_not_found"),
                                (429, "http_rate_limited"), (500, "http_server_exhausted"),
                                (503, "http_server_exhausted"), (501, "http_other"), (400, "http_other")]:
            remote = Metaculus.__new__(Metaculus)
            remote.timeout, remote.next_request = 1, 0
            remote.last_request_failure_subtype = "unknown"
            headers = {"Retry-After": "61"} if status == 429 else {}
            remote.session = Session(Response(status, headers))
            with patch("futureeval_bot.api.time.sleep", lambda _: None):
                with self.assertRaises(ServiceError):
                    remote.request("GET", "/posts/")
            self.assertEqual(remote.last_request_failure_subtype, subtype, status)
        remote = Metaculus.__new__(Metaculus)
        remote.timeout, remote.next_request = 1, 0
        remote.last_request_failure_subtype = "unknown"
        remote.session = Session(Response(200, content=b"invalid"))
        with self.assertRaises(ValueError):
            remote.request("GET", "/posts/")
        self.assertEqual(remote.last_request_failure_subtype, "response_top_level_shape")

    def test_rejected_continuation_never_reaches_discovery(self):
        state = FakeState()
        state.active_operation_id = "repo/123/1"
        state.db = SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: {
            "operation_id": "repo/123/1", "repository": "repo",
            "workflow_path": ".github/workflows/forecast.yml", "workflow_run_id": "123",
            "workflow_run_attempt": "1", "source_sha": "a" * 40,
            "config_fingerprint": "0123456789abcdef", "started_at": "2026-11-18T00:00:00Z",
            "status": "started",
        }))
        remote = self._remote({"results": [{"question": {"type": "binary"}}]})
        reached = []
        model = SimpleNamespace(config=object(), rate_limit_circuit_open=lambda config: reached.append("model"))
        config = SimpleNamespace(season_project=33121, practice_project=32977)
        with patch.object(entry, "live_gate", lambda *args: None), \
             patch.object(entry, "research", lambda *args: reached.append("research")), \
             patch.object(entry, "prepare", lambda *args: reached.append("forecast")), \
             patch.object(entry, "reconcile_and_submit", lambda *args, **kwargs: reached.append("submit")):
            with self.assertRaises(FeedPaginationIncomplete):
                entry.run(config, state, remote, model)
        self.assertEqual(state.discovered, [])
        self.assertEqual(reached, [])
        self.assertEqual(state.operation_stage, "inventory")
        saved = state.metadata["inventory_diagnostics:repo/123/1"]
        self.assertEqual(saved["diagnostics"]["status"], "failed")
        self.assertEqual(saved["diagnostics"]["failure_subtype"], "continuation_missing")

    def test_complete_feed_requires_terminal_page_and_successful_discovery(self):
        state = FakeState()
        remote = self._remote({"results": [{"question": {"type": "binary"}}], "next": None})
        diagnostic = InventoryDiagnostics(state)
        _, contracts, skipped = collect_inventory(remote, 33121, state, object(), diagnostic,
            supported={"binary"},
            contract_factory=lambda post, project: SimpleNamespace(question_id=1, status="open", open_time=None),
            is_future=lambda contract: False, service_error=RuntimeError, on_invalid_post=lambda post: None)
        self.assertEqual(list(contracts), [1])
        self.assertEqual(skipped, 0)
        self.assertEqual(state.discovered, [1])
        self.assertEqual(diagnostic.progress["status"], "complete")
        self.assertTrue(diagnostic.progress["pagination_complete"])

    def test_metadata_and_state_write_failures_are_distinct(self):
        state = FakeState()
        remote = self._remote({"results": [{"question": {"type": "binary"}}], "next": None})
        diagnostic = InventoryDiagnostics(state)
        with self.assertRaises(RuntimeError):
            collect_inventory(remote, 33121, state, object(), diagnostic, supported={"binary"},
                contract_factory=lambda post, project: (_ for _ in ()).throw(ValueError()),
                is_future=lambda contract: False, service_error=RuntimeError, on_invalid_post=lambda post: None)
        self.assertEqual(diagnostic.progress["failure_subtype"], "supported_metadata_invalid")
        state = FakeState()
        state.discover = lambda *args: (_ for _ in ()).throw(OSError("synthetic"))
        diagnostic = InventoryDiagnostics(state)
        with self.assertRaises(OSError):
            collect_inventory(remote, 33121, state, object(), diagnostic, supported={"binary"},
                contract_factory=lambda post, project: SimpleNamespace(question_id=2, status="open", open_time=None),
                is_future=lambda contract: False, service_error=RuntimeError, on_invalid_post=lambda post: None)
        self.assertEqual(diagnostic.progress["failure_subtype"], "state_write_failed")

    def test_invalid_post_telemetry_failure_preserves_metadata_error_and_subtype(self):
        state = FakeState()
        remote = self._remote({"results": [{"question": {"type": "binary"}}], "next": None})
        diagnostic = InventoryDiagnostics(state)
        with self.assertRaisesRegex(RuntimeError, "Supported question metadata failed validation"):
            collect_inventory(remote, 33121, state, object(), diagnostic, supported={"binary"},
                contract_factory=lambda post, project: (_ for _ in ()).throw(ValueError("invalid")),
                is_future=lambda contract: False, service_error=RuntimeError,
                on_invalid_post=lambda post: (_ for _ in ()).throw(OSError("telemetry write failed")))
        self.assertEqual(diagnostic.progress["failure_subtype"], "supported_metadata_invalid")
        self.assertEqual(state.discovered, [])

    def test_failed_diagnostic_persistence_does_not_mask_primary_error(self):
        class BoundState(FakeState):
            def __init__(self):
                super().__init__()
                self.active_operation_id = "repo/123/1"
                self.db = sqlite3.connect(":memory:")
                self.db.row_factory = sqlite3.Row
                self.db.execute("CREATE TABLE operations(operation_id,repository,workflow_path,workflow_run_id,"
                    "workflow_run_attempt,source_sha,config_fingerprint,started_at,status)")
                self.db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?)", (
                    "repo/123/1", "repo", ".github/workflows/forecast.yml", "123", "1", "a" * 40,
                    "0123456789abcdef", "2026-11-18T00:00:00Z", "started"))
            def set_meta(self, key, value):
                raise OSError("synthetic persistence fault")
        state = BoundState()
        recorder = InventoryDiagnostics(state)
        primary = RuntimeError("synthetic inventory error")
        class FailingRemote:
            inventory_failure_subtype = "transport_other"
            inventory_pagination_disposition = "request_failed"
            def inventory_progress(self):
                return {"pages_completed": 0, "items_received": 0, "last_page_items": 0,
                        "pagination_disposition": "request_failed", "pagination_complete": False}
            def posts(self, project):
                raise primary
        remote = FailingRemote()
        with self.assertRaises(RuntimeError) as caught:
            collect_inventory(remote, 33121, state, object(), recorder, supported={"binary"},
                contract_factory=lambda post, project: None, is_future=lambda contract: False,
                service_error=RuntimeError, on_invalid_post=lambda post: None)
        self.assertIs(caught.exception, primary)
        self.assertEqual(recorder.progress["failure_subtype"], "transport_other")
        expected = {key: state.db.execute("SELECT " + key + " FROM operations").fetchone()[0]
                    for key in BINDING_KEYS}
        self.assertEqual(summary_diagnostics(None, expected, "completed"), UNKNOWN_DIAGNOSTICS)

    def test_read_only_operational_summary_emits_only_bound_diagnostic_schema(self):
        config = {
            "version": "free-baseline-0.1", "spending_ceiling_usd": 0,
            "registration_confirmed": True, "live_enabled": True, "expected_bot_user_id": 310267,
            "season_project": 33121, "practice_project": 32977,
            "models": ["nvidia/nemotron-3-super-120b-a12b:free", "google/gemma-4-31b-it:free"],
            "samples_per_question": 2, "max_calls_per_question": 3, "max_calls_per_day": 40,
            "max_output_tokens": 4096, "model_timeout_seconds": 150, "question_budget_seconds": 480,
            "deadline_reserve_seconds": 300, "max_questions_per_run": 10,
            "search_results_per_query": 4, "max_pages_to_fetch": 3, "state_dir": "state",
        }
        normalized = dict(config)
        fingerprint = hashlib.sha256(json.dumps(normalized, sort_keys=True,
            ensure_ascii=False, allow_nan=False).encode()).hexdigest()[:16]
        repo = "danielbrindlekhym/Fall-2026-FutureEval-Bot-Tournament"
        op_id = repo + "/123/1"
        started = "2026-11-18T00:00:00Z"
        source = "a" * 40
        binding = {"operation_id": op_id, "repository": repo,
            "workflow_path": ".github/workflows/forecast.yml", "workflow_run_id": "123",
            "workflow_run_attempt": "1", "source_sha": source,
            "config_fingerprint": fingerprint, "started_at": started}
        record = {"binding": binding, "diagnostics": self.good_complete()}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "bot.db"
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            db = sqlite3.connect(database)
            db.executescript("""
                CREATE TABLE jobs(phase TEXT);
                CREATE TABLE calls(id INTEGER PRIMARY KEY,status TEXT,cost REAL);
                CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE operations(operation_id TEXT PRIMARY KEY,repository TEXT,workflow_path TEXT,
                  workflow_run_id TEXT,workflow_run_attempt TEXT,source_sha TEXT,config_fingerprint TEXT,
                  trigger_event TEXT,requested_mode TEXT,started_at TEXT,finished_at TEXT,status TEXT,
                  failure_stage TEXT,failure_category TEXT,failure_stage_counts TEXT,failure_category_counts TEXT,
                  checkpoint_continuity TEXT,observed_questions INTEGER);
                CREATE TABLE call_attribution(call_id INTEGER,operation_id TEXT,workflow_run_id TEXT,
                  workflow_run_attempt TEXT,source_sha TEXT,outcome_status TEXT,cost_receipt_status TEXT,cost_received_at TEXT);
                CREATE TABLE receipt_events(operation_id TEXT,question_id INTEGER,receipt_type TEXT,match_status TEXT,
                  timing_status TEXT,server_at TEXT,deadline_at TEXT,id INTEGER);
                CREATE TABLE delivery_proof(dummy INTEGER);
            """)
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                op_id, repo, binding["workflow_path"], "123", "1", source, fingerprint,
                "schedule", "live", started, "2026-11-18T00:01:00Z", "completed",
                None, None, "{}", "{}", "verified", 0))
            db.execute("INSERT INTO meta VALUES(?,?)",
                ("inventory_diagnostics:" + op_id, json.dumps(record, separators=(",", ":"))))
            db.execute("SELECT c.id,c.status,c.cost,a.workflow_run_id,a.workflow_run_attempt,a.source_sha,"
                "a.outcome_status,a.cost_receipt_status,a.cost_received_at FROM call_attribution a "
                "JOIN calls c ON c.id=a.call_id WHERE a.operation_id=? ORDER BY c.id", (op_id,)).fetchall()
            db.commit()
            db.close()
            env = {"BOT_OPERATION_STARTED_AT": started, "BOT_WORKFLOW_RUN_ID": "123",
                "BOT_WORKFLOW_RUN_ATTEMPT": "1", "BOT_SOURCE_SHA": source,
                "BOT_REPOSITORY": repo, "BOT_WORKFLOW_PATH": binding["workflow_path"],
                "BOT_TRIGGER_EVENT": "schedule", "BOT_OPERATION_MODE": "live"}
            summary = collect_summary(database, config_path=config_path, env=env)
        self.assertEqual(summary["inventory_diagnostics"], self.good_complete())
        self.assertEqual(set(summary["inventory_diagnostics"]), {
            "schema_version", "status", "failure_subtype", "pagination_disposition",
            "pages_completed", "items_received", "last_page_items", "pagination_complete"})
        self.assertNotIn("operation_id", json.dumps(summary["inventory_diagnostics"]))


if __name__ == "__main__":
    unittest.main()

