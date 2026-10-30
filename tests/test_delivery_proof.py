import hashlib
import json
import sys
import tempfile
import unittest
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from futureeval_bot.api import _delivery_proof, reconcile_and_submit
from futureeval_bot.core import Config, Contract, State, canonical
from futureeval_bot.operational_summary import collect_summary


BOT_ID = 310267
RUN_ID = "9001"
ATTEMPT = "1"
MARKER = "[frozen-job:synthetic-proof]"
CLOSE = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
PAYLOAD = {"probability_yes": 0.7}


def contract(*, close_boundary_conflict=False):
    return Contract(
        question_id=501, post_id=600, kind="binary", title="synthetic",
        background="", resolution="", fine_print="",
        close_time=CLOSE.isoformat().replace("+00:00", "Z"), open_time=None,
        project=33121, close_boundary_conflict=close_boundary_conflict,
    )


def forecast(start_time, *, author_id=BOT_ID):
    item = {"forecast_values": [0.3, 0.7]}
    if start_time is not None:
        item["start_time"] = start_time
    if author_id is not ...:
        item["author_id"] = author_id
    return item


def comment(created_at, *, author_id=BOT_ID, on_post=600, is_private=True, text=None):
    result = {"author": {"id": author_id}, "on_post": on_post,
              "is_private": is_private, "text": text or MARKER}
    if created_at is not ...:
        result["created_at"] = created_at
    return result


def unix(value):
    return value.timestamp()


class RollbackContract:
    """Strict constructor shape from rollback commit 1c9fc16 (core.py)."""
    fields = (
        "question_id", "post_id", "kind", "title", "background", "resolution",
        "fine_print", "close_time", "open_time", "project", "options", "scaling",
        "open_lower_bound", "open_upper_bound", "unit", "status", "staff_clarifications",
    )

    def __init__(self, **values):
        unexpected = values.keys() - set(self.fields)
        if unexpected:
            name = sorted(unexpected)[0]
            raise TypeError(f"unexpected keyword argument: {name}")
        missing = set(self.fields[:10]) - values.keys()
        if missing:
            raise TypeError(f"missing required argument: {sorted(missing)[0]}")
        self.values = values


class DeliveryProofTests(unittest.TestCase):
    def classify(self, f, c, *, contract_value=None, expected=BOT_ID):
        return _delivery_proof(contract_value or contract(), PAYLOAD, [f], c, MARKER, expected)

    def test_numeric_unix_timestamps_and_strictly_before_boundary(self):
        before = CLOSE - timedelta(seconds=1)
        self.assertEqual(self.classify(forecast(unix(before)), comment(before.isoformat())),
                         (True, "on_time"))
        self.assertEqual(self.classify(forecast(int(unix(before))), comment(before.isoformat())),
                         (True, "on_time"))

    def test_deadline_equality_is_late_for_either_receipt(self):
        before = CLOSE - timedelta(seconds=1)
        self.assertEqual(self.classify(forecast(unix(CLOSE)), comment(before.isoformat())),
                         (True, "late"))
        self.assertEqual(self.classify(forecast(unix(before)), comment(CLOSE.isoformat())),
                         (True, "late"))

    def test_invalid_forecast_unix_timestamp_is_unknown(self):
        before = unix(CLOSE - timedelta(seconds=1))
        invalid = [None, True, False, "1793361599", float("nan"), float("inf"),
                   float("-inf"), 10**100]
        for value in invalid:
            with self.subTest(value=repr(value)):
                self.assertEqual(self.classify(forecast(value), comment(CLOSE.isoformat())),
                                 (True, "unknown"))

    def test_missing_or_wrong_forecast_author_is_unknown_and_not_paired(self):
        before = CLOSE - timedelta(seconds=1)
        self.assertEqual(self.classify(forecast(unix(before), author_id=... ), comment(before.isoformat())),
                         (False, "unknown"))
        self.assertEqual(self.classify(forecast(unix(before), author_id=BOT_ID + 1), comment(before.isoformat())),
                         (False, "unknown"))
        self.assertEqual(self.classify(forecast(unix(before), author_id=True), comment(before.isoformat())),
                         (False, "unknown"))

    def test_comment_identity_privacy_post_marker_and_timestamp_are_required(self):
        before = CLOSE - timedelta(seconds=1)
        base = forecast(unix(before))
        good_time = before.isoformat()
        malformed_association = [
            comment(good_time, author_id=BOT_ID + 1),
            comment(good_time, on_post=601),
            comment(good_time, is_private=False),
            comment(good_time, text="different marker"),
        ]
        for item in malformed_association:
            with self.subTest(comment=item):
                self.assertEqual(self.classify(base, item), (False, "unknown"))
        malformed_time = [
            comment("2026-10-30T11:59:00"),
            comment("not-a-timestamp"),
            comment(...),
        ]
        for item in malformed_time:
            with self.subTest(comment=item):
                self.assertEqual(self.classify(base, item), (True, "unknown"))

    def test_ambiguous_or_conflicting_close_boundary_is_unknown(self):
        before = CLOSE - timedelta(seconds=1)
        good_comment = comment(before.isoformat())
        for conflict in (True, None):
            self.assertEqual(
                self.classify(forecast(unix(before)), good_comment,
                              contract_value=contract(close_boundary_conflict=conflict)),
                (True, "unknown"),
            )

    def test_contract_uses_actual_then_scheduled_fallback_and_marks_conflict(self):
        scheduled = "2026-10-30T12:00:00Z"
        base_post = {"id": 600, "scheduled_close_time": scheduled,
                     "question": {"id": 501, "type": "binary", "status": "closed"}}
        fallback = Contract.from_post(base_post, 33121)
        self.assertEqual(fallback.close_time, scheduled)
        self.assertFalse(fallback.close_boundary_conflict)
        base_post["question"]["actual_close_time"] = "2026-10-30T12:30:00Z"
        actual = Contract.from_post(base_post, 33121)
        self.assertEqual(actual.close_time, "2026-10-30T12:30:00Z")
        self.assertFalse(actual.close_boundary_conflict)
        del base_post["question"]["actual_close_time"]
        base_post["question"]["scheduled_close_time"] = "2026-10-30T13:00:00Z"
        scheduled_conflict = Contract.from_post(base_post, 33121)
        self.assertTrue(scheduled_conflict.close_boundary_conflict)

    def test_old_contract_without_boundary_provenance_fails_closed(self):
        old = contract().as_dict()
        old.pop("close_boundary_conflict")
        parsed = Contract(**old)
        before = CLOSE - timedelta(seconds=1)
        self.assertEqual(self.classify(forecast(unix(before)), comment(before.isoformat()),
                                       contract_value=parsed), (True, "unknown"))

    def test_frozen_contract_json_stays_compatible_with_pinned_rollback_constructor(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            state = State(Path(directory) / "bot.db")
            c = contract(close_boundary_conflict=False)
            state.discover(c, Config())
            job = state.job(c.question_id)
            saved = json.loads(job["contract"])
            self.assertNotIn("close_boundary_conflict", saved)
            rollback_contract = RollbackContract(**saved)
            # Match the rollback run-loop fallback expression: it succeeds even
            # when the just-discovered contract mapping already has this id.
            selected = {c.question_id: rollback_contract}.get(
                c.question_id, RollbackContract(**saved)
            )
            self.assertIs(selected, rollback_contract)
            self.assertEqual(state.contract(c.question_id).close_boundary_conflict, False)
            key = state._contract_provenance_key(c.question_id)
            provenance = state.meta(key)
            self.assertEqual(provenance["close_boundary_conflict"], False)
            from futureeval_bot.cloud import signature
            signed = signature(state.db)
            state.db.execute("DELETE FROM meta WHERE key=?", (key,))
            state.db.commit()
            self.assertNotEqual(signature(state.db), signed,
                                "contract provenance must participate in checkpoint CAS")
            state.close()

    def test_new_on_time_provenance_survives_reopen_and_reconciliation(self):
        class FakeRemote:
            user_id = BOT_ID
            def __init__(self, forecast_time, comment_time):
                self.forecast_time = forecast_time
                self.comment_time = comment_time
                self.forecast_reads = self.comment_reads = self.post_reads = self.submissions = 0
            def forecasts(self, question_id):
                self.forecast_reads += 1
                return [forecast(unix(self.forecast_time))]
            def comments(self, post_id):
                self.comment_reads += 1
                return [comment(self.comment_time.isoformat())]
            def post(self, post_id):
                self.post_reads += 1
                raise AssertionError("complete receipt pair must not reread post")
            def submit_pair(self, *args): self.submissions += 1
            def submit_comment(self, *args): self.submissions += 1
            def submit_forecast(self, *args): self.submissions += 1

        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            cases = (
                (CLOSE - timedelta(seconds=2), CLOSE - timedelta(seconds=1), "on_time"),
                (CLOSE, CLOSE - timedelta(seconds=1), "late"),
                (CLOSE - timedelta(seconds=2), CLOSE, "late"),
                (CLOSE - timedelta(seconds=2), CLOSE + timedelta(seconds=1), "late"),
            )
            for index, (forecast_time, comment_time, expected_status) in enumerate(cases):
                with self.subTest(expected_status=expected_status, forecast_time=forecast_time,
                                  comment_time=comment_time):
                    path = Path(directory) / f"bot-{index}.db"
                    state = State(path)
                    values = contract(close_boundary_conflict=False).as_dict()
                    values["question_id"] = 501 + index
                    c = Contract(**values)
                    state.discover(c, Config())
                    state.update(c.question_id, phase="forecasted", payload=canonical(PAYLOAD),
                                 explanation=MARKER + "\nsynthetic reasoning")
                    frozen = {key: state.job(c.question_id)[key]
                              for key in ("contract", "config", "payload", "explanation", "samples")}
                    state.close()

                    reopened = State(path)
                    remote = FakeRemote(forecast_time, comment_time)
                    reconcile_and_submit(reopened, remote, c.question_id,
                                         workflow_run_id=RUN_ID, workflow_run_attempt=ATTEMPT)
                    row = reopened.db.execute(
                        "SELECT paired,status FROM delivery_proof WHERE question_id=?", (c.question_id,)
                    ).fetchone()
                    self.assertEqual(tuple(row), (1, expected_status))
                    self.assertEqual((remote.forecast_reads, remote.comment_reads, remote.post_reads,
                                      remote.submissions), (1, 1, 0, 0))
                    self.assertEqual({key: reopened.job(c.question_id)[key] for key in frozen}, frozen)
                    self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM calls").fetchone()[0], 0)
                    reopened.close()

    def test_ambiguous_and_legacy_missing_provenance_stay_unknown_without_submission(self):
        class FakeRemote:
            user_id = BOT_ID
            def __init__(self):
                self.submissions = 0
                self.forecast_reads = self.comment_reads = self.post_reads = 0
                self.forecast_rows = [forecast(unix(CLOSE - timedelta(seconds=2)))]
                self.comment_rows = [comment((CLOSE - timedelta(seconds=1)).isoformat())]
            def forecasts(self, question_id): self.forecast_reads += 1; return self.forecast_rows
            def comments(self, post_id): self.comment_reads += 1; return self.comment_rows
            def post(self, post_id):
                self.post_reads += 1
                raise AssertionError("complete receipt pair must not reread post")
            def submit_pair(self, *args): self.submissions += 1
            def submit_comment(self, *args): self.submissions += 1
            def submit_forecast(self, *args): self.submissions += 1

        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            # New but ambiguous provenance is represented by SQL NULL.
            ambiguous_path = Path(directory) / "ambiguous.db"
            state = State(ambiguous_path)
            c = contract(close_boundary_conflict=None)
            state.discover(c, Config())
            state.update(c.question_id, phase="forecasted", payload=canonical(PAYLOAD),
                         explanation=MARKER + "\nsynthetic reasoning")
            state.close()
            reopened = State(ambiguous_path)
            remote = FakeRemote()
            before = (json.dumps(remote.forecast_rows, sort_keys=True),
                      json.dumps(remote.comment_rows, sort_keys=True))
            reconcile_and_submit(reopened, remote, c.question_id,
                                 workflow_run_id=RUN_ID, workflow_run_attempt=ATTEMPT)
            row = reopened.db.execute(
                "SELECT paired,status FROM delivery_proof WHERE question_id=?", (c.question_id,)
            ).fetchone()
            self.assertEqual(tuple(row), (1, "unknown"))
            self.assertEqual(remote.submissions, 0)
            self.assertEqual((remote.forecast_reads, remote.comment_reads, remote.post_reads), (1, 1, 0))
            self.assertEqual(before, (json.dumps(remote.forecast_rows, sort_keys=True),
                                      json.dumps(remote.comment_rows, sort_keys=True)))
            reopened.close()

            # A database created before this feature has no provenance record;
            # absent evidence is not converted to False.
            legacy_path = Path(directory) / "legacy.db"
            raw = sqlite3.connect(legacy_path)
            raw.execute("CREATE TABLE jobs(question_id INTEGER PRIMARY KEY, project INTEGER NOT NULL, contract TEXT NOT NULL, config TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'discovered', research TEXT, payload TEXT, explanation TEXT, samples TEXT, comment_id INTEGER, error TEXT, created TEXT NOT NULL, updated TEXT NOT NULL)")
            raw.commit()
            raw.close()
            legacy = State(legacy_path)
            saved = c.as_dict()
            saved.pop("close_boundary_conflict")
            legacy.db.execute(
                "INSERT INTO jobs(question_id,project,contract,config,created,updated,payload,explanation,phase) VALUES(?,?,?,?,?,?,?,?,?)",
                (c.question_id, c.project, canonical(saved), canonical({}), "created", "updated",
                 canonical(PAYLOAD), MARKER + "\nsynthetic reasoning", "forecasted"),
            )
            legacy.db.commit()
            legacy.close()
            legacy = State(legacy_path)
            self.assertIsNone(legacy.contract(c.question_id).close_boundary_conflict)
            remote = FakeRemote()
            reconcile_and_submit(legacy, remote, c.question_id,
                                 workflow_run_id=RUN_ID, workflow_run_attempt=ATTEMPT)
            row = legacy.db.execute(
                "SELECT paired,status FROM delivery_proof WHERE question_id=?", (c.question_id,)
            ).fetchone()
            self.assertEqual(tuple(row), (1, "unknown"))
            self.assertEqual(remote.submissions, 0)
            self.assertEqual((remote.forecast_reads, remote.comment_reads, remote.post_reads), (1, 1, 0))
            legacy.close()

    def test_malformed_and_wrong_job_provenance_stay_unknown(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            state = State(Path(directory) / "bot.db")
            first = contract(close_boundary_conflict=False)
            state.discover(first, Config())
            second_values = first.as_dict()
            second_values.update(question_id=502, post_id=601, close_boundary_conflict=None)
            second = Contract(**second_values)
            state.discover(second, Config())
            third_values = first.as_dict()
            third_values.update(question_id=503, post_id=602, close_boundary_conflict=True)
            third = Contract(**third_values)
            state.discover(third, Config())

            first_key = state._contract_provenance_key(first.question_id)
            valid_first_provenance = state.meta(first_key)
            first_contract = json.loads(state.job(first.question_id)["contract"])
            first_digest = hashlib.sha256(canonical(first_contract).encode()).hexdigest()
            state.set_meta(first_key, {"contract_sha256": first_digest,
                                       "close_boundary_conflict": 0})
            self.assertIsNone(state.contract(first.question_id).close_boundary_conflict)

            second_key = state._contract_provenance_key(second.question_id)
            state.set_meta(second_key, valid_first_provenance)
            self.assertIsNone(state.contract(second.question_id).close_boundary_conflict)
            state.close()
            state = State(Path(directory) / "bot.db")
            self.assertIsNone(state.contract(first.question_id).close_boundary_conflict)
            self.assertIsNone(state.contract(second.question_id).close_boundary_conflict)
            self.assertIs(state.contract(third.question_id).close_boundary_conflict, True)

            class FakeRemote:
                user_id = BOT_ID
                def __init__(self):
                    self.forecast_reads = self.comment_reads = self.submissions = 0
                    self.forecast_rows = [forecast(unix(CLOSE - timedelta(seconds=2)))]
                    self.comment_rows = [comment((CLOSE - timedelta(seconds=1)).isoformat())]
                def forecasts(self, question_id):
                    self.forecast_reads += 1
                    return self.forecast_rows
                def comments(self, post_id):
                    self.comment_reads += 1
                    return [comment(self.comment_rows[0]["created_at"], on_post=post_id)]
                def post(self, post_id): raise AssertionError("complete pair must not reread post")
                def submit_pair(self, *args): self.submissions += 1
                def submit_comment(self, *args): self.submissions += 1
                def submit_forecast(self, *args): self.submissions += 1

            for c in (first, second, third):
                state.update(c.question_id, phase="forecasted", payload=canonical(PAYLOAD),
                             explanation=MARKER + "\nsynthetic reasoning")
            remote = FakeRemote()
            before_receipts = (json.dumps(remote.forecast_rows, sort_keys=True),
                               json.dumps(remote.comment_rows, sort_keys=True))
            for c in (first, second, third):
                reconcile_and_submit(state, remote, c.question_id,
                                     workflow_run_id=RUN_ID, workflow_run_attempt=ATTEMPT)
                row = state.db.execute(
                    "SELECT paired,status FROM delivery_proof WHERE question_id=?", (c.question_id,)
                ).fetchone()
                self.assertEqual(tuple(row), (1, "unknown"))
            self.assertEqual((remote.forecast_reads, remote.comment_reads, remote.submissions), (3, 3, 0))
            self.assertEqual(before_receipts, (json.dumps(remote.forecast_rows, sort_keys=True),
                                               json.dumps(remote.comment_rows, sort_keys=True)))
            state.close()

    def test_optional_proof_telemetry_failure_does_not_change_reconciliation(self):
        class NoTelemetryState(State):
            def record_delivery_proof(self, *args, **kwargs):
                raise OSError("synthetic telemetry persistence failure")

        class FakeRemote:
            user_id = BOT_ID
            def __init__(self): self.reads = 0; self.submissions = 0
            def forecasts(self, question_id):
                self.reads += 1
                return [forecast(unix(CLOSE - timedelta(seconds=2)))]
            def comments(self, post_id):
                self.reads += 1
                return [comment((CLOSE - timedelta(seconds=1)).isoformat())]
            def post(self, post_id): raise AssertionError("complete pair must not reread post")
            def submit_pair(self, *args): self.submissions += 1
            def submit_comment(self, *args): self.submissions += 1
            def submit_forecast(self, *args): self.submissions += 1

        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            state = NoTelemetryState(Path(directory) / "bot.db")
            c = contract(close_boundary_conflict=False)
            state.discover(c, Config())
            state.update(c.question_id, phase="forecasted", payload=canonical(PAYLOAD),
                         explanation=MARKER + "\nsynthetic reasoning")
            remote = FakeRemote()
            reconcile_and_submit(state, remote, c.question_id,
                                 workflow_run_id=RUN_ID, workflow_run_attempt=ATTEMPT)
            self.assertEqual((remote.reads, remote.submissions), (2, 0))
            self.assertEqual(state.job(c.question_id)["phase"], "verified")
            state.close()

    def test_optional_contract_provenance_write_failure_preserves_job_and_reconcile(self):
        class FakeRemote:
            user_id = BOT_ID
            def __init__(self):
                self.forecast_reads = self.comment_reads = self.post_reads = self.submissions = 0
            def forecasts(self, question_id):
                self.forecast_reads += 1
                return [forecast(unix(CLOSE - timedelta(seconds=2)))]
            def comments(self, post_id):
                self.comment_reads += 1
                return [comment((CLOSE - timedelta(seconds=1)).isoformat(), on_post=post_id)]
            def post(self, post_id):
                self.post_reads += 1
                raise AssertionError("complete receipt pair must not reread post")
            def submit_pair(self, *args): self.submissions += 1
            def submit_comment(self, *args): self.submissions += 1
            def submit_forecast(self, *args): self.submissions += 1

        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / "bot.db"
            state = State(path)
            c = contract(close_boundary_conflict=False)
            state.db.execute("""
                CREATE TRIGGER reject_contract_provenance
                BEFORE INSERT ON meta
                WHEN NEW.key GLOB 'contract_close_boundary_provenance:*'
                BEGIN SELECT RAISE(ABORT, 'synthetic optional sidecar failure'); END
            """)
            state.db.commit()

            state.discover(c, Config())
            saved = state.job(c.question_id)
            self.assertEqual(saved["phase"], "discovered")
            self.assertNotIn("close_boundary_conflict", json.loads(saved["contract"]))
            self.assertIsNone(state.contract(c.question_id).close_boundary_conflict)
            self.assertIsNone(state.meta(state._contract_provenance_key(c.question_id)))
            state.update(c.question_id, phase="forecasted", payload=canonical(PAYLOAD),
                         explanation=MARKER + "\nsynthetic reasoning")
            state.close()

            reopened = State(path)
            self.assertIsNone(reopened.contract(c.question_id).close_boundary_conflict)
            remote = FakeRemote()
            reconcile_and_submit(reopened, remote, c.question_id,
                                 workflow_run_id=RUN_ID, workflow_run_attempt=ATTEMPT)
            self.assertEqual((remote.forecast_reads, remote.comment_reads, remote.post_reads,
                              remote.submissions), (1, 1, 0, 0))
            self.assertEqual(reopened.job(c.question_id)["phase"], "verified")
            self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM calls").fetchone()[0], 0)
            self.assertIsNone(reopened.contract(c.question_id).close_boundary_conflict)
            reopened.close()

    def test_reconcile_uses_only_existing_two_receipt_reads(self):
        class FakeRemote:
            user_id = BOT_ID
            def __init__(self):
                self.forecast_reads = 0
                self.comment_reads = 0
                self.submissions = 0
            def forecasts(self, question_id):
                self.forecast_reads += 1
                return [forecast(unix(CLOSE - timedelta(seconds=2)))]
            def comments(self, post_id):
                self.comment_reads += 1
                return [comment((CLOSE - timedelta(seconds=1)).isoformat())]
            def post(self, post_id):
                raise AssertionError("post read must not occur for a complete receipt pair")
            def submit_pair(self, *args): self.submissions += 1
            def submit_comment(self, *args): self.submissions += 1
            def submit_forecast(self, *args): self.submissions += 1

        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            state = State(Path(directory) / "bot.db")
            c = contract()
            state.discover(c, Config())
            state.update(c.question_id, phase="forecasted", payload=canonical(PAYLOAD),
                         explanation=MARKER + "\nsynthetic reasoning")
            remote = FakeRemote()
            reconcile_and_submit(state, remote, c.question_id,
                                 workflow_run_id=RUN_ID, workflow_run_attempt=ATTEMPT)
            self.assertEqual((remote.forecast_reads, remote.comment_reads, remote.submissions), (1, 1, 0))
            row = state.db.execute(
                "SELECT paired,status FROM delivery_proof WHERE question_id=?", (c.question_id,)
            ).fetchone()
            self.assertEqual(tuple(row), (1, "on_time"))
            state.close()

    def test_summary_is_run_bound_aggregate_only_and_read_only(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            db_path = Path(directory) / "bot.db"
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({"state_dir": directory}), encoding="utf-8")
            state = State(db_path)
            for question_id, run_id, paired, status in (
                (1, RUN_ID, True, "on_time"),
                (2, RUN_ID, True, "late"),
                (3, RUN_ID, False, "unknown"),
                (4, "other-run", True, "on_time"),
            ):
                state.record_delivery_proof(question_id, workflow_run_id=run_id,
                                            workflow_run_attempt=ATTEMPT, paired=paired, status=status)
            now = datetime.now(timezone.utc)
            start = (now - timedelta(seconds=5)).isoformat()
            state.set_meta("last_run", {"at": now.isoformat(), "project": 33121, "config": "test",
                                         "failures": [], "questions_considered": 3,
                                         "workflow_run_id": RUN_ID,
                                         "workflow_run_attempt": ATTEMPT,
                                         "operation_started_at": start})
            state.close()
            before = db_path.read_bytes()
            env = {"BOT_OPERATION_STARTED_AT": start, "BOT_WORKFLOW_RUN_ID": RUN_ID,
                   "BOT_WORKFLOW_RUN_ATTEMPT": ATTEMPT, "BOT_TRIGGER_EVENT": "schedule"}
            rendered = collect_summary(db_path, config_path=config_path, env=env)
            after = db_path.read_bytes()
            self.assertEqual(before, after, "summary must leave the database unchanged")
            self.assertEqual(rendered["delivery_proof_status"], "available")
            self.assertEqual(rendered["delivery_proof_observed_rows"], 3)
            self.assertEqual(rendered["delivery_proof_scope"],
                             "run_observed_subset_not_released_question_or_seasonal_denominator")
            self.assertEqual(rendered["delivery_proof_counts"],
                             {"paired": 2, "on_time": 1, "late": 1, "unknown": 1})
            public = json.dumps(rendered, sort_keys=True)
            for forbidden in ("synthetic reasoning", MARKER, '"question_id"', '"created_at"',
                              '"start_time"', '"post_id"', '"author_id"'):
                self.assertNotIn(forbidden, public)
            self.assertNotIn("other-run", public)


if __name__ == "__main__":
    unittest.main()
