import json
import sys
import tempfile
import unittest
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
