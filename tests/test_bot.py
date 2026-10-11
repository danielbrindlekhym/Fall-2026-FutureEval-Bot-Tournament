import json
from dataclasses import replace
from datetime import timedelta

import pytest

from futureeval_bot.api import FreeModel, ServiceError, expected_values, reconcile_and_submit, zero_priced
from futureeval_bot.core import Config, Contract, State, canonical, source_hashes, utcnow
from futureeval_bot.forecast import aggregate, parse_sample, prepare, validate_payload
from futureeval_bot.__main__ import live_gate, run


def contract(kind="binary", *, question_id=100, **kwargs):
    values = dict(question_id=question_id, post_id=200, kind=kind,
        title="Synthetic practice question", background="Synthetic context", resolution="Use the stated synthetic source.",
        fine_print="", close_time=(utcnow() + timedelta(days=1)).isoformat(),
        open_time=(utcnow() - timedelta(days=1)).isoformat(), project=32977)
    if kind == "multiple_choice":
        values["options"] = ["Alpha", "Beta", "Gamma"]
    if kind in {"numeric", "discrete"}:
        values["scaling"] = {"range_min": 0, "range_max": 100, "zero_point": None, "inbound_outcome_count": 200}
    values.update(kwargs)
    return Contract(**values)


def response(**kwargs):
    return {"choices": [{"message": {"content": json.dumps({"reasoning": "Synthetic independent reasoning with uncertainty, base rates and supporting assumptions.", **kwargs})}}], "usage": {"cost": 0}}


@pytest.fixture
def state(tmp_path):
    result = State(tmp_path / "bot.db")
    yield result
    result.close()


def saved(state, q=None):
    q = q or contract()
    state.discover(q, Config())
    payload = {"probability_yes": 0.62}
    state.update(q.question_id, payload=canonical(payload), explanation="FutureEval submission synthetic-unique-marker\n\n" + "Reasoning " * 10, phase="prepared")
    return q, payload


class FakeRemote:
    def __init__(self, q, payload=None, comment=False, lose_pair_receipt=False, fail_comment=False):
        self.q = q
        self.values = expected_values(q, payload) if payload else None
        self.has_comment = comment
        self.lose_pair_receipt = lose_pair_receipt
        self.fail_comment = fail_comment
        self.pair_posts = self.forecast_posts = self.comment_posts = 0

    def forecasts(self, question_id):
        return [{"start_time": 1, "forecast_values": self.values}] if self.values is not None else []

    def comments(self, post_id, staff=False):
        if staff:
            return []
        return [{"id": 500, "text": "FutureEval submission synthetic-unique-marker\n\nReasoning"}] if self.has_comment else []

    def post(self, post_id):
        return {"id": self.q.post_id, "question": {"id": self.q.question_id, "type": self.q.kind,
            "title": self.q.title, "status": self.q.status, "scheduled_close_time": self.q.close_time,
            "open_time": self.q.open_time, "scaling": self.q.scaling, "options": self.q.options}}

    def posts(self, project):
        return [self.post(self.q.post_id)]

    def submit_pair(self, q, payload, explanation):
        self.pair_posts += 1
        self.values, self.has_comment = expected_values(q, payload), True
        if self.lose_pair_receipt:
            self.lose_pair_receipt = False
            raise ServiceError("Simulated connection lost after server commit")

    def submit_forecast(self, q, payload):
        self.forecast_posts += 1
        self.values = expected_values(q, payload)

    def submit_comment(self, q, explanation):
        self.comment_posts += 1
        if self.fail_comment:
            self.fail_comment = False
            raise ServiceError("Simulated comment service outage")
        self.has_comment = True


def test_binary_parse_and_bounds():
    payload, reasoning = parse_sample(contract(), response(probability_yes=0.7))
    assert payload == {"probability_yes": 0.7}
    assert len(reasoning) > 40
    assert parse_sample(contract(), response(probability_yes=1))[0]["probability_yes"] == .999


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1, True, "0.7"])
def test_invalid_probability_rejected(value):
    with pytest.raises(ValueError):
        parse_sample(contract(), response(probability_yes=value))


def test_multiple_choice_labels_normalization_and_floor():
    q = contract("multiple_choice")
    payload, _ = parse_sample(q, response(probabilities={"Alpha": 1, "Beta": 0, "Gamma": 0}))
    values = payload["probability_yes_per_category"]
    assert sum(values.values()) == pytest.approx(1)
    assert values["Beta"] >= .001
    with pytest.raises(ValueError):
        parse_sample(q, response(probabilities={"A": .5, "B": .5}))


@pytest.mark.parametrize("kind,scale", [
    ("numeric", {"range_min": 0, "range_max": 100, "zero_point": None, "inbound_outcome_count": 200}),
    ("numeric", {"range_min": 1, "range_max": 1000, "zero_point": 0, "inbound_outcome_count": 200}),
    ("discrete", {"range_min": -.5, "range_max": 15.5, "zero_point": None, "inbound_outcome_count": 16}),
    ("discrete", {"range_min": .095, "range_max": .805, "zero_point": None, "inbound_outcome_count": 71}),
])
def test_numeric_discrete_sdk_grid_and_units(kind, scale):
    q = contract(kind, scaling=scale)
    low, high = scale["range_min"], scale["range_max"]
    # For the log-scale example, use appropriately spaced quantiles in real units.
    fractions = [.1, .2, .4, .6, .8, .9]
    values = [low + (high-low)*p for p in fractions] if scale["zero_point"] is None else [low*(high/low)**p for p in fractions]
    payload, _ = parse_sample(q, response(percentiles=[{"percentile": p, "value": v} for p, v in zip(fractions, values)]))
    cdf = payload["continuous_cdf"]
    assert len(cdf) == scale["inbound_outcome_count"] + 1
    assert cdf[0] == 0 and cdf[-1] == 1
    assert all(a <= b for a, b in zip(cdf, cdf[1:]))
    validate_payload(q, aggregate(q, [payload, payload]))


def test_open_numeric_tails_and_negative_units():
    q = contract("numeric", scaling={"range_min": -35, "range_max": 0, "zero_point": None, "inbound_outcome_count": 200},
        open_lower_bound=True, open_upper_bound=True)
    fractions = [.1, .2, .4, .6, .8, .9]
    payload, _ = parse_sample(q, response(percentiles=[{"percentile": p, "value": v} for p, v in zip(fractions, [-45,-35,-25,-15,-5,5])]))
    assert payload["continuous_cdf"][0] > 0
    assert payload["continuous_cdf"][-1] < 1


def test_numeric_bad_grid_and_nonmonotonic_rejected():
    q = contract("numeric")
    with pytest.raises(ValueError):
        validate_payload(q, {"continuous_cdf": [0, 1]})
    with pytest.raises(ValueError):
        validate_payload(q, {"continuous_cdf": [0] + [.8, .2] + [.5]*197 + [1]})


def test_atomic_submission_verified_and_duplicate_prevented(state):
    q, payload = saved(state)
    remote = FakeRemote(q)
    reconcile_and_submit(state, remote, q.question_id)
    reconcile_and_submit(state, remote, q.question_id)
    assert state.job(q.question_id)["phase"] == "verified"
    assert remote.pair_posts == 1


def test_lost_post_response_reconciles_without_resubmission(state):
    q, payload = saved(state)
    remote = FakeRemote(q, lose_pair_receipt=True)
    with pytest.raises(ServiceError):
        reconcile_and_submit(state, remote, q.question_id)
    reconcile_and_submit(state, remote, q.question_id)
    assert state.job(q.question_id)["phase"] == "verified"
    assert remote.pair_posts == 1


def test_partial_forecast_recovers_comment_only(state):
    q, payload = saved(state)
    remote = FakeRemote(q, payload=payload, fail_comment=True)
    with pytest.raises(ServiceError):
        reconcile_and_submit(state, remote, q.question_id)
    reconcile_and_submit(state, remote, q.question_id)
    assert state.job(q.question_id)["phase"] == "verified"
    assert remote.comment_posts == 2
    assert remote.forecast_posts == remote.pair_posts == 0


def test_partial_comment_recovers_forecast_only(state):
    q, payload = saved(state)
    remote = FakeRemote(q, comment=True)
    reconcile_and_submit(state, remote, q.question_id)
    assert state.job(q.question_id)["phase"] == "verified"
    assert remote.forecast_posts == 1 and remote.comment_posts == remote.pair_posts == 0


def test_other_forecast_is_never_overwritten(state):
    q, payload = saved(state)
    remote = FakeRemote(q, payload={"probability_yes": .4})
    with pytest.raises(ServiceError):
        reconcile_and_submit(state, remote, q.question_id)
    assert remote.pair_posts == remote.forecast_posts == remote.comment_posts == 0


def test_frozen_payload_and_explanation_cannot_be_replaced(state):
    q, payload = saved(state)
    with pytest.raises(ValueError):
        state.update(q.question_id, payload=canonical({"probability_yes": .9}))
    with pytest.raises(ValueError):
        state.update(q.question_id, explanation="Another answer")


def test_state_recovers_after_reopen(tmp_path):
    first = State(tmp_path / "persist.db")
    q, payload = saved(first)
    first.close()
    second = State(tmp_path / "persist.db")
    assert second.job(q.question_id)["payload"] == canonical(payload)
    second.close()


def test_model_samples_replayed_after_crash(state):
    q = contract()
    config = Config()
    state.discover(q, config)
    for p in [.4, .6]:
        call = state.reserve_call(q.question_id, config.models[0], config)
        state.finish_call(call, response(probability_yes=p), 0)
    class NoMoreCalls:
        def complete(self, *args, **kwargs):
            raise AssertionError("Persisted usable samples must be replayed")
    prepare(state, q, config, NoMoreCalls(), {"sources": []}, float("inf"))
    assert json.loads(state.job(q.question_id)["payload"])["probability_yes"] == .5
    prepare(state, q, config, NoMoreCalls(), {"sources": []}, float("inf"))


def test_free_call_limits_count_uncertain_calls(state):
    config = Config(max_calls_per_question=2, samples_per_question=1, max_calls_per_day=2)
    state.reserve_call(1, config.models[0], config)
    state.reserve_call(1, config.models[0], config)
    with pytest.raises(RuntimeError):
        state.reserve_call(1, config.models[0], config)
    with pytest.raises(RuntimeError):
        state.reserve_call(2, config.models[0], config)


@pytest.mark.parametrize("values", [{"spending_ceiling_usd": 1}, {"models": ["paid/model"]}, {"models": ["paid/model:online"]}])
def test_paid_configuration_rejected(values):
    with pytest.raises(ValueError):
        Config(**values)


def test_zero_pricing_requires_complete_finite_prices():
    assert zero_priced({"id": "test/model:free", "pricing": {"prompt": "0", "completion": "0"}})
    for prices in [{}, {"prompt": "0"}, {"prompt": "0", "completion": "0.01"}, {"prompt": "0", "completion": "NaN"}, {"prompt": "0", "completion": "0", "request": "1"}]:
        assert not zero_priced({"id": "test/model:free", "pricing": prices})


def test_free_model_request_cannot_enable_paid_plugins(state):
    config = Config(models=["test/model:free"], samples_per_question=1)
    model = FreeModel(config, state, "not-a-real-test-key")
    model.catalog = {"test/model:free": {"id": "test/model:free", "pricing": {"prompt": "0", "completion": "0"}}}
    class Reply:
        ok = True
        def json(self):
            return response(probability_yes=.5)
    class Session:
        def post(self, url, **kwargs):
            body = kwargs["json"]
            assert body["model"].endswith(":free")
            assert body["plugins"] == []
            assert set(body["provider"]["max_price"].values()) == {0}
            assert "models" not in body
            return Reply()
    model.session = Session()
    model.complete(1, "synthetic", 0, 10)
    assert state.summary()["reported_model_cost_usd"] == 0


def test_nonzero_receipt_halts_future_calls(state):
    config = Config(models=["test/model:free"], samples_per_question=1)
    model = FreeModel(config, state, "not-a-real-test-key")
    model.catalog = {"test/model:free": {"id": "test/model:free", "pricing": {"prompt": "0", "completion": "0"}}}
    class Reply:
        ok = True
        def json(self):
            return {"usage": {"cost": .01}}
    class Session:
        def post(self, *args, **kwargs):
            return Reply()
    model.session = Session()
    with pytest.raises(ServiceError):
        model.complete(1, "synthetic", 0, 10)
    assert state.meta("cost_guard_halt")
    with pytest.raises(ServiceError):
        model.complete(2, "synthetic", 0, 10)


def test_closed_question_does_not_submit_forecast(state):
    q = contract(close_time=(utcnow() - timedelta(seconds=1)).isoformat(), status="closed")
    saved(state, q)
    remote = FakeRemote(q)
    reconcile_and_submit(state, remote, q.question_id)
    assert state.job(q.question_id)["phase"] == "missed"
    assert remote.pair_posts == 0


def test_submission_reserve_stops_model_calls(state):
    q = contract(close_time=(utcnow() + timedelta(seconds=120)).isoformat())
    class Model:
        config = Config()
        def complete(self, *args, **kwargs):
            raise AssertionError("No model calls inside submission reserve")
    result = run(Config(), state, FakeRemote(q), Model(), practice=True)
    assert result["phases"] == {"missed": 1}


def test_live_gate_requires_registration_practice_and_checks(state):
    with pytest.raises(ServiceError):
        live_gate(Config(), state)
    state.set_meta("practice_verified_formats", ["binary", "multiple_choice", "numeric", "discrete"])
    config = Config(registration_confirmed=True, live_enabled=True)
    state.set_meta("offline_checks_passed", {"passed": True, 'config_fingerprint': config.fingerprint, 'source_hashes': source_hashes()})
    live_gate(config, state)
    state.set_meta('offline_checks_passed', {'passed': True})
    with pytest.raises(ServiceError):
        live_gate(config, state)


def test_quoted_multiple_choice_decimals_preserve_values():
    q = contract('multiple_choice')
    payload, _ = parse_sample(q, response(probabilities={'Alpha':'0.84','Beta':'0.14','Gamma':'0.02'}))
    assert payload['probability_yes_per_category']['Alpha'] == pytest.approx(.001 + .997*.84)
    with pytest.raises(ValueError):
        parse_sample(q, response(probabilities={'Alpha':'eval(1)','Beta':'0','Gamma':'0'}))


def test_contract_requires_timezone_and_full_metadata():
    q = contract()
    remote = FakeRemote(q)
    post = remote.post(q.post_id)
    post["question"].update(description="Background", resolution_criteria="Exact source and date", fine_print="Exceptions")
    parsed = Contract.from_post(post, 32977)
    assert parsed.background == "Background" and parsed.resolution == "Exact source and date" and parsed.fine_print == "Exceptions"
    post["question"]["scheduled_close_time"] = "2027-01-01T00:00:00"
    with pytest.raises(ValueError):
        Contract.from_post(post, 32977)
