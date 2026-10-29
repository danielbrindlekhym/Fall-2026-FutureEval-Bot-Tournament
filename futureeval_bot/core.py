from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SUPPORTED = {"binary", "multiple_choice", "numeric", "discrete"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        raise ValueError("A question timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def source_hashes() -> dict:
    root = Path(__file__).resolve().parents[1]
    return {str(path.relative_to(root)).replace('\\', '/'): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((root / 'futureeval_bot').glob('*.py'))}


@dataclass(frozen=True)
class Config:
    version: str = "free-baseline-0.1"
    spending_ceiling_usd: float = 0
    registration_confirmed: bool = False
    live_enabled: bool = False
    expected_bot_user_id: int = 310267
    season_project: int = 33121
    practice_project: int = 32977
    models: list[str] = field(default_factory=lambda: ["nvidia/nemotron-3-super-120b-a12b:free", "google/gemma-4-31b-it:free"])
    samples_per_question: int = 2
    max_calls_per_question: int = 3
    max_calls_per_day: int = 40
    max_output_tokens: int = 4096
    model_timeout_seconds: int = 150
    question_budget_seconds: int = 480
    deadline_reserve_seconds: int = 300
    max_questions_per_run: int = 10
    search_results_per_query: int = 4
    max_pages_to_fetch: int = 3
    state_dir: str = "state"

    def __post_init__(self):
        if self.spending_ceiling_usd != 0:
            raise ValueError("This implementation supports only the user's $0 ceiling")
        if not self.models or any(not model.endswith(":free") for model in self.models):
            raise ValueError("Every model must be an explicit :free catalogue entry")
        if not 1 <= self.samples_per_question <= self.max_calls_per_question <= 5:
            raise ValueError("Invalid sample/call limits")
        if not 1 <= self.max_calls_per_day <= 40:
            raise ValueError("Daily call limit must be between 1 and 40")
        if self.deadline_reserve_seconds < 60:
            raise ValueError("Submission reserve must be at least 60 seconds")
        if self.model_timeout_seconds < 1 or self.question_budget_seconds < 1:
            raise ValueError("Timeouts must be positive")
        if self.season_project != 33121 or self.practice_project != 32977:
            raise ValueError("Unexpected destination: this build is scoped to Fall 2026")

    @classmethod
    def load(cls, path: str | Path = "config.json") -> Config:
        return cls(**json.loads(Path(path).read_text(encoding="utf-8-sig")))

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(canonical(asdict(self)).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Contract:
    question_id: int
    post_id: int
    kind: str
    title: str
    background: str
    resolution: str
    fine_print: str
    close_time: str
    open_time: str | None
    project: int
    options: list[str] = field(default_factory=list)
    scaling: dict = field(default_factory=dict)
    open_lower_bound: bool = False
    open_upper_bound: bool = False
    unit: str | None = None
    status: str = "open"
    staff_clarifications: list[str] = field(default_factory=list)
    # None means this contract predates proof-boundary provenance. Forecast
    # behavior continues to use close_time exactly as before.
    close_boundary_conflict: bool | None = None

    @classmethod
    def from_post(cls, post: dict, project: int) -> Contract:
        q = post.get("question")
        if not q or q.get("type") not in SUPPORTED:
            raise ValueError("Unsupported post/question type")
        actual_close = q.get("actual_close_time")
        question_scheduled_close = q.get("scheduled_close_time")
        post_scheduled_close = post.get("scheduled_close_time")
        scheduled_close = question_scheduled_close or post_scheduled_close
        close = actual_close or scheduled_close
        if not close:
            raise ValueError("Missing question closing timestamp")
        timestamp(close)
        close_conflict = False
        if actual_close is not None:
            if not isinstance(actual_close, str) or not actual_close:
                close_conflict = None
        elif question_scheduled_close and post_scheduled_close:
            # These are duplicate scheduled boundaries in the same fallback
            # tier. A disagreement is ambiguous; actual_close_time, when set,
            # remains authoritative over the scheduled value by contract.
            try:
                close_conflict = timestamp(question_scheduled_close) != timestamp(post_scheduled_close)
            except (TypeError, ValueError, AttributeError):
                close_conflict = True
        return cls(
            question_id=int(q["id"]), post_id=int(post["id"]), kind=q["type"],
            title=q.get("title") or post.get("title", ""),
            background=q.get("description") or post.get("description") or "",
            resolution=q.get("resolution_criteria") or "",
            fine_print=q.get("fine_print") or "", close_time=close,
            open_time=q.get("open_time") or post.get("open_time"), project=project,
            options=q.get("options") or [], scaling=q.get("scaling") or {},
            open_lower_bound=bool(q.get("open_lower_bound", False)),
            open_upper_bound=bool(q.get("open_upper_bound", False)),
            unit=q.get("unit") or q.get("unit_of_measure"),
            status=q.get("status") or post.get("status") or "open",
            close_boundary_conflict=close_conflict,
        )

    def remaining(self, now: datetime | None = None) -> float:
        return (timestamp(self.close_time) - (now or utcnow())).total_seconds()

    def forecastable(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        opens = timestamp(self.open_time)
        return self.status == "open" and self.remaining(now) > 0 and (opens is None or opens <= now)

    def as_dict(self) -> dict:
        return asdict(self)


class State:
    """Crash-safe records; freeze a prepared payload before any submission."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.checkpoint = None
        self.cost_receipt_halted = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(
                question_id INTEGER PRIMARY KEY, project INTEGER NOT NULL,
                contract TEXT NOT NULL, config TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'discovered',
                research TEXT, payload TEXT, explanation TEXT, samples TEXT,
                comment_id INTEGER, error TEXT, created TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS calls(
                id INTEGER PRIMARY KEY, question_id INTEGER NOT NULL, day TEXT NOT NULL,
                model TEXT NOT NULL, status TEXT NOT NULL, response TEXT, cost REAL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS delivery_proof(
                question_id INTEGER PRIMARY KEY, workflow_run_id TEXT, workflow_run_attempt TEXT,
                paired INTEGER NOT NULL CHECK(paired IN (0,1)),
                status TEXT NOT NULL CHECK(status IN ('on_time','late','unknown')));
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def persist(self):
        if self.checkpoint:
            self.checkpoint(self)

    def meta(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key: str, value: Any):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, canonical(value)))
        self.persist()

    def discover(self, contract: Contract, config: Config):
        now = utcnow().isoformat()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO jobs(question_id,project,contract,config,created,updated) VALUES(?,?,?,?,?,?)",
                (contract.question_id, contract.project, canonical(contract.as_dict()), canonical(asdict(config)), now, now))
        self.persist()

    def job(self, question_id: int) -> dict:
        row = self.db.execute("SELECT * FROM jobs WHERE question_id=?", (question_id,)).fetchone()
        if row is None:
            raise KeyError(question_id)
        return dict(row)

    def jobs(self, project: int | None = None) -> list[dict]:
        rows = self.db.execute("SELECT * FROM jobs" + (" WHERE project=?" if project else ""), (project,) if project else ())
        return [dict(row) for row in rows]

    def update(self, question_id: int, **values):
        allowed = {"phase", "research", "payload", "explanation", "samples", "comment_id", "error"}
        if not values.keys() <= allowed:
            raise ValueError("Unexpected job column")
        previous = self.job(question_id)
        for frozen in ("payload", "explanation", "samples"):
            if frozen in values and previous[frozen] is not None and previous[frozen] != values[frozen]:
                raise ValueError(f"Cannot replace frozen {frozen}")
        values["updated"] = utcnow().isoformat()
        with self.db:
            self.db.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in values) + " WHERE question_id=?",
                (*values.values(), question_id))
        self.persist()

    def record_delivery_proof(self, question_id: int, *, workflow_run_id: str | None,
                              workflow_run_attempt: str | None, paired: bool, status: str):
        if type(paired) is not bool or status not in {"on_time", "late", "unknown"}:
            raise ValueError("Invalid delivery proof status")
        if not (isinstance(workflow_run_id, str) and workflow_run_id.isdigit()
                and len(workflow_run_id) <= 24):
            workflow_run_id = None
        if not (isinstance(workflow_run_attempt, str) and workflow_run_attempt.isdigit()
                and len(workflow_run_attempt) <= 8):
            workflow_run_attempt = None
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO delivery_proof "
                "(question_id,workflow_run_id,workflow_run_attempt,paired,status) VALUES(?,?,?,?,?)",
                (question_id, workflow_run_id, workflow_run_attempt, int(paired), status),
            )
        self.persist()

    def reserve_call(self, question_id: int, model: str, config: Config, *, cost_receipt_guard=False) -> int:
        if cost_receipt_guard and (self.cost_receipt_halted or self.meta("cost_guard_halt")):
            raise RuntimeError("Cost receipt guard is halted pending an audit")
        day = utcnow().date().isoformat()
        with self.db:
            daily = self.db.execute("SELECT COUNT(*) FROM calls WHERE day=?", (day,)).fetchone()[0]
            question = self.db.execute("SELECT COUNT(*) FROM calls WHERE question_id=?", (question_id,)).fetchone()[0]
            if daily >= config.max_calls_per_day or question >= config.max_calls_per_question:
                raise RuntimeError("Free call allowance exhausted; waiting for a later eligible run")
            cursor = self.db.execute("INSERT INTO calls(question_id,day,model,status,created) VALUES(?,?,?,?,?)",
                (question_id, day, model, "started", utcnow().isoformat()))
            call_id = cursor.lastrowid
            if cost_receipt_guard:
                self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("cost_guard_halt", canonical(
                    {"call_id": call_id, "reason": "pending_zero_cost_receipt", "at": utcnow().isoformat()})))
        if cost_receipt_guard:
            self.cost_receipt_halted = True
        self.persist()  # A reservation must be durable before the external model call.
        return call_id

    def _check_cost_receipt_guard(self, call_id: int):
        guard = self.meta("cost_guard_halt")
        if not isinstance(guard, dict) or guard.get("call_id") != call_id or guard.get("reason") != "pending_zero_cost_receipt":
            raise RuntimeError("Cost receipt guard changed; audit before further calls")

    def release_cost_receipt_guard(self, call_id: int):
        # Only the same newly reserved request may release its marker after a
        # handled completion transport/non-OK failure. Its reservation remains.
        self._check_cost_receipt_guard(call_id)
        with self.db:
            self.db.execute("DELETE FROM meta WHERE key='cost_guard_halt'")
        self.persist()
        self.cost_receipt_halted = False

    def finish_call(self, call_id: int, response: dict, cost: float | None, *, cost_receipt_guard=False):
        if cost_receipt_guard:
            self._check_cost_receipt_guard(call_id)
        with self.db:
            self.db.execute("UPDATE calls SET status='done',response=?,cost=? WHERE id=?", (canonical(response), cost, call_id))
            if cost_receipt_guard and cost == 0:
                self.db.execute("DELETE FROM meta WHERE key='cost_guard_halt'")
        self.persist()
        if cost_receipt_guard and cost == 0:
            self.cost_receipt_halted = False

    def completed_calls(self, question_id: int) -> list[dict]:
        rows = self.db.execute("SELECT * FROM calls WHERE question_id=? AND status='done' ORDER BY id", (question_id,))
        return [dict(row) for row in rows]

    def call_count(self, question_id: int) -> int:
        return self.db.execute('SELECT COUNT(*) FROM calls WHERE question_id=?', (question_id,)).fetchone()[0]

    def summary(self, project: int | None = None) -> dict:
        jobs = self.jobs(project)
        counts = {}
        for job in jobs:
            counts[job["phase"]] = counts.get(job["phase"], 0) + 1
        launch = self.meta("launch_time")
        eligible = [job for job in jobs if launch and Contract(**json.loads(job["contract"])).open_time
                    and timestamp(Contract(**json.loads(job["contract"])).open_time) >= timestamp(launch)]
        verified = sum(job["phase"] == "verified" for job in eligible)
        costs = self.db.execute("SELECT COALESCE(SUM(cost),0),COUNT(*),SUM(CASE WHEN cost IS NULL THEN 1 ELSE 0 END) FROM calls").fetchone()
        return {"questions_observed": len(jobs), "phases": counts, "post_launch_questions_observed": len(eligible),
            "post_launch_verified": verified, "observed_coverage": verified / len(eligible) if eligible else None,
            "reported_model_cost_usd": costs[0], "reserved_model_calls": costs[1], "calls_without_cost_receipt": costs[2] or 0,
            "coverage_note": "Includes only questions discovered; reconciliation against the full seasonal feed is required."}


@contextmanager
def run_lock(directory: str | Path):
    """OS-released lock survives process crashes without leaving a stale lock."""
    import os
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stream = (directory / "worker.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        stream.close()


def finite_probability(value: Any, *, minimum=0.0, maximum=1.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("Probability must be a number")
    value = float(value)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("Probability outside valid bounds")
    return value
