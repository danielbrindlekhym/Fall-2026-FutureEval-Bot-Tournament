from __future__ import annotations

import base64
import hashlib
import sqlite3
from types import SimpleNamespace

import pytest

from futureeval_bot.cloud import CloudState, seal, snapshot, signature


TOKEN = "synthetic-test-token-not-a-secret-123456789"
HEAD_A = "a" * 40
HEAD_B = "b" * 40
HEAD_C = "c" * 40


def _database(marker: str) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE jobs(id INTEGER PRIMARY KEY, phase TEXT NOT NULL);
        CREATE TABLE calls(id INTEGER PRIMARY KEY, status TEXT NOT NULL);
        """
    )
    db.execute("INSERT INTO meta VALUES('checkpoint_marker', ?)", (marker,))
    db.commit()
    return db


def _blob_sha(encrypted: bytes) -> str:
    header = f"blob {len(encrypted)}".encode() + b"\0"
    return hashlib.sha1(header + encrypted).hexdigest()


def _cloud_for(heads: list[str], states: dict[str, bytes]):
    cloud = CloudState("synthetic/repo", "synthetic-github-token", TOKEN)
    ref_reads = iter(heads)
    docs = {}
    for index, (head, data) in enumerate(states.items(), start=1):
        tree_sha = f"{index:040x}"
        encrypted = seal(data, TOKEN)
        blob_sha = _blob_sha(encrypted)
        docs[head] = {
            "tree_sha": tree_sha,
            "blob_sha": blob_sha,
            "encrypted": encrypted,
        }
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path))
        if path == "/git/ref/heads/bot-state":
            return {"object": {"sha": next(ref_reads)}}
        if path.startswith("/git/commits/"):
            head = path.rsplit("/", 1)[-1]
            return {"sha": head, "tree": {"sha": docs[head]["tree_sha"]}}
        if path.startswith("/git/trees/"):
            tree_sha = path.rsplit("/", 1)[-1]
            doc = next(value for value in docs.values() if value["tree_sha"] == tree_sha)
            return {
                "sha": tree_sha,
                "truncated": False,
                "tree": [{"path": "progress.enc", "type": "blob", "sha": doc["blob_sha"]}],
            }
        if path.startswith("/git/blobs/"):
            blob_sha = path.rsplit("/", 1)[-1]
            doc = next(value for value in docs.values() if value["blob_sha"] == blob_sha)
            return {
                "sha": blob_sha,
                "encoding": "base64",
                "content": base64.b64encode(doc["encrypted"]).decode("ascii"),
            }
        raise AssertionError(f"unexpected checkpoint request: {method} {path}")

    cloud.request = request
    return cloud, calls, docs


def test_head_change_discards_first_snapshot_and_re_reads_current_head():
    old_db = _database("old")
    new_db = _database("new")
    old_bytes, new_bytes = snapshot(old_db), snapshot(new_db)
    old_db.close()
    new_db.close()
    cloud, calls, docs = _cloud_for(
        [HEAD_A, HEAD_B, HEAD_B, HEAD_B],
        {HEAD_A: old_bytes, HEAD_B: new_bytes},
    )

    assert cloud.read() == new_bytes
    assert cloud.read_commit == HEAD_B
    assert cloud.sha == docs[HEAD_B]["blob_sha"]
    assert len(calls) == 10  # exactly two complete reads, with no blob reuse


def test_repeated_head_change_fails_after_one_complete_re_read():
    db = _database("same")
    data = snapshot(db)
    db.close()
    cloud, calls, _ = _cloud_for(
        [HEAD_A, HEAD_B, HEAD_B, HEAD_C],
        {HEAD_A: data, HEAD_B: data, HEAD_C: data},
    )

    with pytest.raises(RuntimeError, match="Checkpoint head changed during read"):
        cloud.read()
    assert len(calls) == 10  # no third read and no unbounded loop


def test_stable_new_head_with_divergent_restored_state_still_fails_attach():
    local_db = _database("local")
    remote_db = _database("remote")
    remote_bytes = snapshot(remote_db)
    remote_signature = signature(remote_db)
    remote_db.close()
    cloud, calls, _ = _cloud_for(
        [HEAD_B, HEAD_B],
        {HEAD_B: remote_bytes},
    )
    state = SimpleNamespace(db=local_db, checkpoint=None)

    with pytest.raises(RuntimeError, match="Local and remote progress disagree"):
        cloud.attach(state)
    assert signature(local_db) != remote_signature
    assert state.checkpoint is None
    assert len(calls) == 5  # one stable read; no save or overwrite
    local_db.close()


def test_corrupt_blob_is_not_retried_as_a_head_movement():
    db = _database("valid")
    data = snapshot(db)
    db.close()
    cloud, calls, docs = _cloud_for([HEAD_A, HEAD_A], {HEAD_A: data})
    original_request = cloud.request

    def corrupt_blob(method, path, **kwargs):
        result = original_request(method, path, **kwargs)
        if path.startswith("/git/blobs/"):
            result["content"] = base64.b64encode(b"corrupt").decode("ascii")
        return result

    cloud.request = corrupt_blob
    with pytest.raises(RuntimeError, match="blob content mismatch"):
        cloud.read()
    assert len(calls) == 4
    assert cloud.sha is None
    assert docs[HEAD_A]["blob_sha"]


def test_stable_head_keeps_single_read_path():
    db = _database("stable")
    data = snapshot(db)
    db.close()
    cloud, calls, _ = _cloud_for([HEAD_A, HEAD_A], {HEAD_A: data})

    assert cloud.read() == data
    assert len(calls) == 5
