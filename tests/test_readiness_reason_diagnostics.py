from __future__ import annotations

import hashlib
import ast
import io
import importlib.util
import json
import sys
import tokenize
import types
import unittest
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
BASE_COMMIT = "f0a423142e6ccb499ba483fe769b8cfcb8e88b30"
BASE_TREE = "6aab6dd6cb39d5feeebf6cf1f47a9e6f07309fd5"
BUNDLE_MANIFEST_SHA256 = "e8497622f838ea957bf6e9475fa3c949f594a768f214244aefe0f2d81ab7581b"
MANIFEST_FIXTURE = ROOT / "tests" / "fixtures" / "readiness_release_bundle_manifest.json"
MODEL_A, MODEL_B = "vendor/free-a:free", "vendor/free-b:free"
NOW = datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc)
PRIVATE = "PRIVATE_INPUT_SENTINEL_DO_NOT_LOG"


def git_blob_sha_bytes(body: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()


def git_blob_sha(path: Path) -> str:
    return git_blob_sha_bytes(path.read_bytes())


def authenticated_base_bytes(path: str) -> bytes:
    result = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:{path}"], cwd=ROOT,
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout


def load_package(prefix: str, folder: Path):
    package = types.ModuleType(prefix)
    package.__path__ = [str(folder)]
    sys.modules[prefix] = package
    core = types.ModuleType(prefix + ".core")
    class Config: pass
    class Contract: pass
    class State: pass
    core.Config, core.Contract, core.State = Config, Contract, State
    core.canonical = lambda value: json.dumps(value, sort_keys=True)
    core.finite_probability = lambda value: True
    core.utcnow = lambda: NOW
    sys.modules[prefix + ".core"] = core
    loaded = {}
    for name in ("readiness", "api", "forecast"):
        spec = importlib.util.spec_from_file_location(prefix + "." + name, folder / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        loaded[name] = module
    return loaded


class FakeResponse:
    def __init__(self, status=200, headers=None, chunks=None):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks if chunks is not None else []
        self.closed = False
        self.ok = status < 400
    def iter_content(self, chunk_size=8192):
        yield from self._chunks
    def close(self):
        self.closed = True
    def json(self):
        return {"usage": {"cost": 0}}


def admitted_response(model=MODEL_A, now=None):
    now = now or datetime.now(timezone.utc)
    payload = {"data": {"id": model, "endpoints": [{
        "model_id": model, "status": 0,
        "pricing": {"prompt": "0", "completion": "0"},
    }]}}
    return FakeResponse(200, {"Date": format_datetime(now), "Age": "0"},
                        [json.dumps(payload).encode()])


class ReadinessReasonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = load_package("ready_reason_candidate", ROOT / "futureeval_bot")
        cls.check = staticmethod(cls.pkg["readiness"].check)

    def assert_reason(self, code, response=None, get=None, clock=None):
        result = response
        if get is None:
            get = lambda *a, **kw: result
        with self.assertRaises(self.pkg["readiness"].ReadinessStopped) as caught:
            self.check(get, "https://metadata.invalid", MODEL_A,
                       now=lambda: NOW, clock=clock or (lambda: 0.0))
        diag = caught.exception.diagnostics
        self.assertEqual(diag["check_failure_counts"], {code: 1})
        self.assertIsNone(diag["selector_stop"])
        self.assertNotIn(PRIVATE, str(diag))
        self.assertNotIn(PRIVATE, str(caught.exception))

    def test_fixed_fault_matrix_and_unknown_fail_closed(self):
        self.assert_reason("http_status_non_200", FakeResponse(429))
        self.assert_reason("date_header_invalid", FakeResponse(200, {"Age": "0"}))
        self.assert_reason("cache_age_invalid", FakeResponse(200, {"Date": format_datetime(NOW), "Age": "NaN"}))
        old = format_datetime(NOW - timedelta(seconds=61))
        self.assert_reason("response_age_out_of_bounds", FakeResponse(200, {"Date": old, "Age": "0"}))
        self.assert_reason("response_body_limit", FakeResponse(200, {"Date": format_datetime(NOW), "Age": "0"}, [b"x" * 524289]))
        ticks = iter([0.0, 11.0])
        self.assert_reason("response_read_time_limit", FakeResponse(200, {"Date": format_datetime(NOW), "Age": "0"}, [b"{}"]), clock=lambda: next(ticks))
        self.assert_reason("json_invalid", FakeResponse(200, {"Date": format_datetime(NOW), "Age": "0"}, [b"{"]))
        invalid = {"data": {"id": MODEL_A, "endpoints": [{"model_id": MODEL_A, "status": 1}]}}
        self.assert_reason("metadata_not_admitted", FakeResponse(200, {"Date": format_datetime(NOW), "Age": "0"}, [json.dumps(invalid).encode()]))
        def transport(*a, **kw):
            raise requests.ConnectionError(PRIVATE)
        self.assert_reason("transport_exception", get=transport)
        def unknown(*a, **kw):
            raise RuntimeError(PRIVATE)
        self.assert_reason("unexpected_exception", get=unknown)
        stopped = self.pkg["readiness"].ReadinessStopped({PRIVATE: 2}, PRIVATE)
        self.assertEqual(stopped.diagnostics, {
            "check_failure_counts": {"unexpected_exception": 2},
            "selector_stop": "unknown_selector_stop",
        })
        self.assertNotIn(PRIVATE, str(stopped.diagnostics))

    def test_valid_metadata_remains_admitted(self):
        self.check(lambda *a, **kw: admitted_response(MODEL_A, NOW),
                   "https://metadata.invalid", MODEL_A, now=lambda: NOW, clock=lambda: 0.0)


class FakeState:
    def __init__(self):
        self.reservations = 0
        self.finished = 0
        self.cost_receipt_halted = False
    def meta(self, key):
        return None
    def call_count(self, question_id):
        return self.reservations
    def reserve_call(self, question_id, model, config, cost_receipt_guard=True):
        self.reservations += 1
        return "synthetic-call"
    def finish_call(self, *args, **kwargs):
        self.finished += 1
    def release_cost_receipt_guard(self, *args, **kwargs):
        pass


class FakeSession:
    plan = {}
    trace = []
    instances = []
    def __init__(self):
        self.trust_env = True
        self.auth = None
        self.closed = False
        self.is_main = False
        self.instances.append(self)
    def get(self, url, **kwargs):
        model = url.split("/models/", 1)[1].split("/endpoints", 1)[0]
        self.trace.append(("GET", model))
        response = self.plan.get(model, admitted_response(model))
        return response
    def post(self, url, json=None, **kwargs):
        self.trace.append(("POST", json["model"]))
        return FakeResponse(200, {}, [b"{}"])
    def close(self):
        self.closed = True


def exercise_api(package, all_reject=False, timeout=30, prior=False):
    module = package["api"]
    FakeSession.plan = {
        MODEL_A: FakeResponse(503) if all_reject else FakeResponse(503),
        MODEL_B: FakeResponse(503) if all_reject else admitted_response(MODEL_B),
    }
    FakeSession.trace = []
    FakeSession.instances = []
    previous_session = module.requests.Session
    module.requests.Session = FakeSession
    state = FakeState()
    cfg = types.SimpleNamespace(models=[MODEL_A, MODEL_B], max_calls_per_question=3,
                                max_output_tokens=128, model_timeout_seconds=30)
    obj = module.FreeModel.__new__(module.FreeModel)
    obj.config, obj.state = cfg, state
    obj.catalog = {
        MODEL_A: {"id": MODEL_A, "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": []},
        MODEL_B: {"id": MODEL_B, "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": []},
    }
    obj.session = FakeSession()
    obj.session.is_main = True
    obj._rate_limited_models = set()
    if prior:
        obj._readiness_stopped = {(42, 0)}
    error = None
    try:
        try:
            obj.complete(42, PRIVATE, 0, timeout)
        except module.ReadinessStopped as e:
            error = e
    finally:
        module.requests.Session = previous_session
    return {"trace": list(FakeSession.trace), "reservations": state.reservations,
            "finished": state.finished, "error": error}


class APIAndAggregateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._baseline_temp = tempfile.TemporaryDirectory(
            prefix="readiness-authenticated-base-", dir=ROOT / "tests")
        baseline_dir = Path(cls._baseline_temp.name)
        for name in ("readiness", "api", "forecast"):
            (baseline_dir / f"{name}.py").write_bytes(authenticated_base_bytes(f"futureeval_bot/{name}.py"))
        cls.baseline = load_package("ready_reason_baseline", baseline_dir)
        cls.candidate = load_package("ready_reason_candidate_api", ROOT / "futureeval_bot")

    @classmethod
    def tearDownClass(cls):
        cls._baseline_temp.cleanup()

    def test_admitted_path_preserves_selection_and_network_counts(self):
        before = exercise_api(self.baseline)
        after = exercise_api(self.candidate)
        self.assertEqual(before["trace"], [("GET", MODEL_A), ("GET", MODEL_B), ("POST", MODEL_B)])
        self.assertEqual(after["trace"], before["trace"])
        self.assertEqual((before["reservations"], before["finished"]), (1, 1))
        self.assertEqual((after["reservations"], after["finished"]), (1, 1))
        metadata_sessions = [s for s in FakeSession.instances if not s.is_main]
        self.assertTrue(all(s.closed and s.trust_env is False and s.auth is None for s in metadata_sessions))

    def test_readiness_rejection_and_outer_stops_do_not_reserve_or_post(self):
        for baseline, candidate, kind in [
            (self.baseline, self.candidate, "no_candidate_admitted"),
            (self.baseline, self.candidate, "insufficient_readiness_time"),
            (self.baseline, self.candidate, "prior_stopped_attempt"),
        ]:
            args = {"all_reject": True} if kind == "no_candidate_admitted" else {}
            if kind == "insufficient_readiness_time": args["timeout"] = 1
            if kind == "prior_stopped_attempt": args["prior"] = True
            old = exercise_api(baseline, **args)
            new = exercise_api(candidate, **args)
            self.assertEqual(old["reservations"], 0)
            self.assertEqual(new["reservations"], 0)
            self.assertNotIn(("POST", MODEL_A), new["trace"])
            self.assertNotIn(("POST", MODEL_B), new["trace"])
            self.assertIsNotNone(old["error"])
            self.assertIsNotNone(new["error"])
            self.assertNotIn(PRIVATE, str(new["error"].diagnostics))
            self.assertEqual(new["error"].diagnostics["selector_stop"], kind)

    def test_existing_no_validated_forecast_aggregate_contains_only_fixed_codes(self):
        module = self.candidate["forecast"]
        class State:
            def job(self, question_id): return {"payload": None}
            def completed_calls(self, question_id): return []
            def call_count(self, question_id): return 0
            def meta(self, key): return None
            def update(self, *args, **kwargs): self.fail("unexpected update")
        class Model:
            def rate_limit_circuit_open(self, *args): return False
            def complete(self, *args, **kwargs):
                raise self.pkg["readiness"].ReadinessStopped(
                    {"http_status_non_200": 1}, "no_candidate_admitted")
        model = Model()
        model.pkg = self.candidate
        module.prompt_for = lambda *args: PRIVATE
        state = State()
        with self.assertRaises(module.NoValidatedForecast) as caught:
            module.prepare(state, types.SimpleNamespace(question_id=42),
                           types.SimpleNamespace(samples_per_question=1, max_calls_per_question=3),
                           model, {"private": PRIVATE}, __import__("time").monotonic() + 60)
        diag = caught.exception.diagnostics
        self.assertEqual(diag["request_failures"], {"readiness_stopped": 1})
        self.assertEqual(diag["readiness_diagnostics"], {
            "check_failure_counts": {"http_status_non_200": 1},
            "selector_stop_counts": {"no_candidate_admitted": 1},
        })
        self.assertNotIn(PRIVATE, str(diag))
        self.assertNotIn(PRIVATE, str(caught.exception))
        self.assertEqual(state.call_count(42), 0)


class PinTests(unittest.TestCase):
    def test_authenticated_pr10_base_tree_matches_all_git_blob_pins(self):
        manifest_bytes = MANIFEST_FIXTURE.read_bytes()
        self.assertEqual(hashlib.sha256(manifest_bytes).hexdigest(), BUNDLE_MANIFEST_SHA256)
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        self.assertEqual(manifest["base_commit"], BASE_COMMIT)
        self.assertEqual(manifest["base_tree"], BASE_TREE)
        tree = subprocess.run(
            ["git", "rev-parse", f"{BASE_COMMIT}^{{tree}}"], cwd=ROOT,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ).stdout.strip()
        self.assertEqual(tree, BASE_TREE)
        self.assertEqual(len(manifest["baseline_files"]), 30)
        for item in manifest["baseline_files"]:
            body = authenticated_base_bytes(item["path"])
            self.assertEqual(git_blob_sha_bytes(body), item["sha"], item["path"])

    def test_candidate_prompt_ast_and_comments_match_authenticated_forecast(self):
        baseline = authenticated_base_bytes("futureeval_bot/forecast.py")
        candidate = ROOT / "futureeval_bot" / "forecast.py"
        def prompt_node(source):
            tree = ast.parse(source.decode("utf-8"))
            return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "prompt_for")
        self.assertEqual(ast.dump(prompt_node(baseline), include_attributes=False),
                         ast.dump(prompt_node(candidate.read_bytes()), include_attributes=False))
        def comments(source):
            return [token.string for token in tokenize.tokenize(io.BytesIO(source).readline)
                    if token.type == tokenize.COMMENT]
        self.assertEqual(comments(baseline), comments(candidate.read_bytes()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
