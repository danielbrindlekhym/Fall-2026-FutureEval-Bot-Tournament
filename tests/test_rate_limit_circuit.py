"""Offline tests for the PR9 rate-limit circuit candidate; all HTTP is mocked."""
from __future__ import annotations

import json
import sys
import time
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import futureeval_bot.api as api
from futureeval_bot.core import Config, Contract, canonical
from futureeval_bot.forecast import NoValidatedForecast, prepare


NVIDIA = "nvidia/nemotron-3-super-120b-a12b:free"
GOOGLE = "google/gemma-4-31b-it:free"
MODELS = [NVIDIA, GOOGLE]


class FakeResponse:
    def __init__(self, status, data=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._data = data or {}

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []
        self.headers = {}
        self.auth = None
        self.trust_env = True

    def post(self, url, *, json, timeout):
        self.posts.append((url, json, timeout))
        return self.responses.pop(0)

    def get(self, *args, **kwargs):
        raise AssertionError("Readiness was stubbed and must not perform HTTP")

    def close(self):
        pass


class FakeCallState:
    cost_receipt_halted = False

    def __init__(self, initial_by_question=None):
        self.calls = []
        self.initial_by_question = dict(initial_by_question or {})
        self.released_guards = []
        self.finished = []

    def meta(self, key, default=None):
        return default

    def call_count(self, question_id):
        return self.initial_by_question.get(question_id, 0) + sum(c["question_id"] == question_id for c in self.calls)

    def reserve_call(self, question_id, model, config, *, cost_receipt_guard=False):
        self.calls.append({"question_id": question_id, "model": model, "guard": cost_receipt_guard})
        return len(self.calls)

    def release_cost_receipt_guard(self, call_id):
        self.released_guards.append(call_id)

    def finish_call(self, call_id, response, cost, *, cost_receipt_guard=False):
        self.finished.append((call_id, cost, cost_receipt_guard))


def configured_model(state, session):
    config = Config(models=MODELS, max_calls_per_question=3, max_calls_per_day=40)
    model = api.FreeModel(config, state, "synthetic-test-key")
    model.catalog = {
        name: {"id": name, "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": []}
        for name in MODELS
    }
    return model


def no_network_readiness(*args, **kwargs):
    return None


class RateLimitApiTests(unittest.TestCase):
    def run_with(self, responses, callback):
        state = FakeCallState()
        session = FakeSession(responses)
        with patch.object(api.requests, "Session", return_value=session), patch.object(api, "check_readiness", no_network_readiness):
            model = configured_model(state, session)
            callback(model, state, session)
        return model, state, session

    def test_first_model_429_keeps_other_configured_free_model_eligible(self):
        success = FakeResponse(200, {"usage": {"cost": 0}})

        def exercise(model, state, session):
            with self.assertRaises(api.ServiceError):
                model.complete(101, "synthetic prompt", 0, timeout=60)
            self.assertEqual(model._rate_limited_models, {NVIDIA})
            self.assertFalse(model.rate_limit_circuit_open())
            model.complete(101, "synthetic prompt", 1, timeout=60)
            self.assertEqual([post[1]["model"] for post in session.posts], [NVIDIA, GOOGLE])
            self.assertEqual([call["model"] for call in state.calls], [NVIDIA, GOOGLE])
            self.assertEqual(session.posts[-1][1]["provider"], {
                "allow_fallbacks": True,
                "max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0},
            })
            self.assertEqual(state.finished[-1][1:], (0.0, True))

        self.run_with([FakeResponse(429), success], exercise)

    def test_all_models_429_blocks_same_and_later_question_without_more_calls(self):
        def exercise(model, state, session):
            for question_id, index in ((101, 0), (101, 1)):
                with self.assertRaises(api.ServiceError):
                    model.complete(question_id, "synthetic prompt", index, timeout=60)
            self.assertTrue(model.rate_limit_circuit_open())
            reservations_before = len(state.calls)
            posts_before = len(session.posts)
            for question_id, index in ((101, 2), (102, 0)):
                with self.assertRaises(api.ServiceError):
                    model.complete(question_id, "synthetic prompt", index, timeout=60)
            self.assertEqual(len(state.calls), reservations_before)
            self.assertEqual(len(session.posts), posts_before)
            self.assertEqual(reservations_before, 2)  # Failed reservations remain consumed.
            self.assertEqual(state.released_guards, [1, 2])

        self.run_with([FakeResponse(429), FakeResponse(429)], exercise)

    def test_unknown_or_empty_catalog_is_ordinary_model_unavailability(self):
        state = FakeCallState()
        session = FakeSession([])
        with patch.object(api.requests, "Session", return_value=session), patch.object(api, "check_readiness", no_network_readiness):
            model = configured_model(state, session)
            model.catalog = None
            self.assertFalse(model.rate_limit_circuit_open())
            model.check = lambda: setattr(model, "catalog", {})
            with self.assertRaisesRegex(api.ServiceError, "Free model unavailable"):
                model.complete(101, "synthetic prompt", 0, timeout=60)
            self.assertFalse(model.rate_limit_circuit_open())
        self.assertEqual(state.calls, [])
        self.assertEqual(session.posts, [])

    def test_new_client_resets_run_scoped_circuit(self):
        state = FakeCallState()
        first = FakeSession([FakeResponse(429)])
        next_run = FakeSession([FakeResponse(200, {"usage": {"cost": 0}})])
        with patch.object(api.requests, "Session", side_effect=[first, first, next_run, next_run]), patch.object(api, "check_readiness", no_network_readiness):
            first_model = configured_model(state, first)
            with self.assertRaises(api.ServiceError):
                first_model.complete(101, "synthetic prompt", 0, timeout=60)
            next_model = configured_model(state, next_run)
            self.assertFalse(next_model._rate_limited_models)
            self.assertFalse(next_model.rate_limit_circuit_open())
            next_model.complete(102, "synthetic prompt", 0, timeout=60)
        self.assertEqual(next_run.posts[0][1]["model"], NVIDIA)

    def test_non_429_error_does_not_block_the_existing_model_order(self):
        def exercise(model, state, session):
            with self.assertRaises(api.ServiceError):
                model.complete(101, "synthetic prompt", 0, timeout=60)
            self.assertFalse(model._rate_limited_models)
            model.complete(101, "synthetic prompt", 2, timeout=60)
            self.assertEqual([post[1]["model"] for post in session.posts], [NVIDIA, NVIDIA])

        self.run_with([FakeResponse(500), FakeResponse(200, {"usage": {"cost": 0}})], exercise)

    def test_call_cap_and_frozen_payload_guard_remain_intact(self):
        state = FakeCallState(initial_by_question={101: 3})
        session = FakeSession([])
        with patch.object(api.requests, "Session", return_value=session), patch.object(api, "check_readiness", no_network_readiness):
            model = configured_model(state, session)
            with self.assertRaisesRegex(api.ServiceError, "allowance exhausted"):
                model.complete(101, "synthetic prompt", 3, timeout=60)
        self.assertEqual(state.calls, [])
        self.assertEqual(session.posts, [])

        class FrozenState:
            def job(self, question_id):
                return {"payload": '{"probability_yes":0.5}'}

        class NeverCall:
            def rate_limit_circuit_open(self, config=None):
                return True

            def complete(self, *args, **kwargs):
                raise AssertionError("A frozen payload must not be regenerated")

        contract = Contract(101, 201, "binary", "synthetic", "", "", "",
            "2027-01-01T00:00:00Z", None, 33121)
        prepare(FrozenState(), contract, Config(models=MODELS), NeverCall(), {}, time.monotonic() + 60)


class RateLimitLoopTests(unittest.TestCase):
    def test_open_circuit_emits_fixed_category_and_defers_unprepared_jobs(self):
        # Stub unrelated imports before importing the command module; no service code runs.
        research_stub = ModuleType("futureeval_bot.research")
        research_stub.research = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("research should be deferred"))
        cloud_stub = ModuleType("futureeval_bot.cloud")
        cloud_stub.configured_cloud = lambda: None
        sys.modules["futureeval_bot.research"] = research_stub
        sys.modules["futureeval_bot.cloud"] = cloud_stub
        from futureeval_bot import __main__ as command
        from futureeval_bot.forecast import NoValidatedForecast

        def make_contract(question_id, post_id, close_hours=3):
            future = (datetime.now(timezone.utc) + timedelta(hours=close_hours)).isoformat()
            past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            return Contract(question_id, post_id, "binary", "synthetic", "", "", "", future, past, 33121)

        first = make_contract(101, 201, 3)
        later = make_contract(102, 202, 4)
        google_only = make_contract(104, 204, 5)
        payload_contract = make_contract(103, 203, 6)
        config = Config(models=MODELS)

        def job(contract, payload=None, job_config=config, research="{}"):
            return {"question_id": contract.question_id, "project": 33121, "phase": "prepared" if payload else "discovered",
                "contract": canonical(contract.as_dict()), "config": canonical(asdict(job_config)), "research": research,
                "payload": payload, "explanation": "frozen explanation" if payload else None}

        nvidia_only = Config(models=[NVIDIA])
        google_config = Config(models=[GOOGLE])
        jobs = [job(first, job_config=config), job(later, job_config=nvidia_only, research=None),
            job(google_only, job_config=google_config), job(payload_contract, '{"probability_yes":0.5}')]

        class PayloadVisibleAfterQueueBuild(dict):
            """Simulate a durable frozen payload becoming visible after work is queued."""
            def __init__(self, values):
                super().__init__(values)
                self.payload_reads = 0

            def __getitem__(self, key):
                if key == "payload":
                    self.payload_reads += 1
                    if self.payload_reads == 1:
                        return None
                return super().__getitem__(key)

        # The frozen payload appears after queue construction. This checks that
        # later reconciliation still wins even after an earlier model opens a circuit.
        jobs[3] = PayloadVisibleAfterQueueBuild(jobs[3])

        class State:
            def __init__(self):
                self.values = {"launch_time": "2026-10-01T00:00:00Z", "full_feed_reconciled": {"release_inventories": {}}}
                self.updates = []

            def meta(self, key, default=None):
                return self.values.get(key, default)

            def set_meta(self, key, value):
                self.values[key] = value

            def discover(self, contract, config):
                pass

            def jobs(self, project):
                return jobs

            def job(self, question_id):
                return next(item for item in jobs if item["question_id"] == question_id)

            def update(self, question_id, **values):
                self.updates.append((question_id, values))
                self.job(question_id).update(values)

            def summary(self, project):
                return {}

        class Remote:
            feed_pages = []

            def __init__(self):
                self.forecast_reads = []

            def posts(self, project):
                return [
                    {"id": c.post_id, "status": "open", "question": {"id": c.question_id, "type": "binary",
                        "actual_close_time": c.close_time, "open_time": c.open_time, "status": "open"}}
                    for c in (first, later, google_only, payload_contract)
                ]

            def forecasts(self, question_id):
                self.forecast_reads.append(question_id)
                return []

        state, remote = State(), Remote()
        class Model:
            def __init__(self):
                self.config = config
                self.catalog = {model: {} for model in MODELS}
                self._rate_limited_models = set()
                self.rate_limit_circuit_opened = False

            def rate_limit_circuit_open(self, candidate_config=None):
                configured = [name for name in (candidate_config or self.config).models if name in self.catalog]
                return bool(configured) and all(name in self._rate_limited_models for name in configured)

        model = Model()
        reconciled = []

        def trip_circuit(*args, **kwargs):
            model._rate_limited_models.add(NVIDIA)
            raise NoValidatedForecast({"validation_failures": {}, "request_failures": {
                "http_rate_limited": 1, "rate_limit_circuit_open": 1}, "reserved_calls": 1})

        def prepare_candidate(state, contract, candidate_config, model, evidence, stop_at):
            if contract.question_id == first.question_id:
                self.assertFalse(model.rate_limit_circuit_open(candidate_config))
                return trip_circuit()
            self.assertEqual(contract.question_id, google_only.question_id)
            self.assertEqual(candidate_config.models, [GOOGLE])
            self.assertFalse(model.rate_limit_circuit_open(candidate_config))
            state.update(contract.question_id, payload='{"probability_yes":0.5}',
                explanation="synthetic frozen reasoning", phase="prepared")

        with patch.object(command, "live_gate"), patch.object(command, "prepare", side_effect=prepare_candidate) as prepare_mock, \
             patch.object(command, "reconcile_and_submit", side_effect=lambda state, remote, qid, **kwargs: reconciled.append(qid)), \
             patch.object(command, "release_inventory", return_value={}), patch.object(command, "inventory_counts", return_value={}), \
             patch.object(command, "research", side_effect=AssertionError("research should not run after circuit opens")):
            result = command.run(config, state, remote, model, max_questions=10)

        self.assertEqual(reconciled, [104, 103])  # Eligible Google-only work and later frozen payload both reconcile.
        self.assertEqual(prepare_mock.call_count, 2)  # NVIDIA-only follow-up is skipped; Google-only work remains eligible.
        self.assertEqual(remote.forecast_reads, [101, 104])
        self.assertEqual([qid for qid, _ in state.updates], [101, 104])
        self.assertEqual(jobs[3]["payload"], '{"probability_yes":0.5}')
        self.assertEqual(jobs[3]["phase"], "prepared")
        self.assertNotIn("error", jobs[1])
        self.assertEqual(jobs[1]["phase"], "discovered")
        self.assertIsNone(jobs[1]["research"])
        self.assertIs(model.config, config)
        self.assertTrue(result["rate_limit_circuit_open"])
        self.assertEqual(result["failures"][0]["diagnostics"]["request_failures"]["rate_limit_circuit_open"], 1)

    def test_fully_blocked_job_skips_research_and_all_state_changes(self):
        research_stub = ModuleType("futureeval_bot.research")
        research_stub.research = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("research must not run"))
        cloud_stub = ModuleType("futureeval_bot.cloud")
        cloud_stub.configured_cloud = lambda: None
        sys.modules["futureeval_bot.research"] = research_stub
        sys.modules["futureeval_bot.cloud"] = cloud_stub
        from futureeval_bot import __main__ as command

        config = Config(models=MODELS)
        contract = Contract(201, 301, "binary", "synthetic", "", "", "",
            (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat(),
            (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), 33121)
        frozen_job = {"question_id": 201, "project": 33121, "phase": "discovered",
            "contract": canonical(contract.as_dict()), "config": canonical(asdict(config)),
            "research": None, "payload": None, "explanation": None}

        class State:
            def __init__(self):
                self.values = {"launch_time": "2026-10-01T00:00:00Z", "full_feed_reconciled": {"release_inventories": {}}}
                self.updates = []

            def meta(self, key, default=None):
                return self.values.get(key, default)

            def set_meta(self, key, value):
                self.values[key] = value

            def discover(self, *args):
                pass

            def jobs(self, project):
                return [frozen_job]

            def job(self, question_id):
                return frozen_job

            def update(self, question_id, **values):
                self.updates.append(values)
                frozen_job.update(values)

            def summary(self, project):
                return {}

        class Remote:
            feed_pages = []

            def __init__(self):
                self.forecast_reads = []

            def posts(self, project):
                return [{"id": contract.post_id, "status": "open", "question": {"id": contract.question_id,
                    "type": "binary", "actual_close_time": contract.close_time, "open_time": contract.open_time, "status": "open"}}]

            def forecasts(self, question_id):
                self.forecast_reads.append(question_id)
                return []

        class Model:
            def __init__(self):
                self.config = config
                self.catalog = {name: {} for name in MODELS}
                self._rate_limited_models = set(MODELS)
                self.rate_limit_circuit_opened = True

            def rate_limit_circuit_open(self, candidate_config=None):
                configured = [name for name in (candidate_config or self.config).models if name in self.catalog]
                return bool(configured) and all(name in self._rate_limited_models for name in configured)

        state, remote, model = State(), Remote(), Model()
        with patch.object(command, "live_gate"), patch.object(command, "prepare") as prepare_mock, \
             patch.object(command, "reconcile_and_submit") as reconcile_mock, \
             patch.object(command, "release_inventory", return_value={}), patch.object(command, "inventory_counts", return_value={}), \
             patch.object(command, "research") as research_mock:
            command.run(config, state, remote, model, max_questions=10)

        prepare_mock.assert_not_called()
        reconcile_mock.assert_not_called()
        research_mock.assert_not_called()
        self.assertEqual(remote.forecast_reads, [])
        self.assertEqual(state.updates, [])
        self.assertEqual(frozen_job["phase"], "discovered")
        self.assertIsNone(frozen_job["payload"])
        self.assertIsNone(frozen_job["research"])

    def test_preopened_circuit_yields_allowlisted_question_diagnostic_without_call(self):
        class State:
            def job(self, question_id):
                return {"payload": None}

            def completed_calls(self, question_id):
                return []

            def call_count(self, question_id):
                return 0

            def meta(self, key, default=None):
                return default

        model = SimpleNamespace(rate_limit_circuit_open=lambda config=None: True)
        contract = Contract(101, 201, "binary", "synthetic", "", "", "",
            "2027-01-01T00:00:00Z", None, 33121)
        with self.assertRaises(NoValidatedForecast) as caught:
            prepare(State(), contract, Config(models=MODELS), model, {}, time.monotonic() + 60)
        self.assertEqual(caught.exception.diagnostics["request_failures"], {"rate_limit_circuit_open": 1})


if __name__ == "__main__":
    unittest.main()
