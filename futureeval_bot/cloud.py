"""Encrypted durable checkpoints in this bot's dedicated GitHub state branch."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import zlib
from pathlib import Path

import requests
from cryptography.fernet import Fernet

from .core import canonical

BRANCH = 'bot-state'
FILE = 'progress.enc'
# Release inventory is durable evidence; deduplication must include its snapshot.
VOLATILE = {'service_checks', 'last_run'}
MAX_ENCRYPTED_BYTES = 5_000_000


class CheckpointConflict(RuntimeError):
    """A rejected compare-and-swap write; never permission to overwrite progress."""


def cipher(token: str) -> Fernet:
    if len(token) < 20:
        raise ValueError('A valid private Metaculus token is required for checkpoint encryption')
    key = hashlib.sha256(b'futureeval-33121-checkpoint-v1\0' + token.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def snapshot(db: sqlite3.Connection) -> bytes:
    memory = sqlite3.connect(':memory:')
    try:
        db.backup(memory)
        return memory.serialize()
    finally:
        memory.close()


def seal(data: bytes, token: str) -> bytes:
    encrypted = cipher(token).encrypt(zlib.compress(data, level=9))
    if len(encrypted) > MAX_ENCRYPTED_BYTES:
        raise ValueError('Checkpoint exceeded its storage safety cap; maintenance needed')
    return encrypted


def unseal(encrypted: bytes, token: str) -> bytes:
    if len(encrypted) > MAX_ENCRYPTED_BYTES:
        raise ValueError('Oversized checkpoint rejected')
    compressed = cipher(token).decrypt(encrypted)
    decoder = zlib.decompressobj()
    data = decoder.decompress(compressed, 100_000_000)
    if not decoder.eof or decoder.unconsumed_tail or not data.startswith(b'SQLite format 3\0'):
        raise ValueError('Invalid or oversized checkpoint database')
    memory = sqlite3.connect(':memory:')
    try:
        memory.deserialize(data)
        if memory.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Checkpoint integrity check failed')
    finally:
        memory.close()
    return data


def signature(db: sqlite3.Connection) -> str:
    material = {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY 1')]
                for table in ('jobs', 'calls')}
    material['meta'] = [tuple(row) for row in db.execute('SELECT * FROM meta ORDER BY key') if row[0] not in VOLATILE]
    return hashlib.sha256(canonical(material).encode()).hexdigest()


class CloudState:
    def __init__(self, repository: str, github_token: str, metaculus_token: str):
        if repository.count('/') != 1 or not github_token:
            raise ValueError('GitHub checkpoint configuration is missing')
        self.repository, self.metaculus_token = repository, metaculus_token
        self.base = 'https://api.github.com/repos/' + repository
        self.session = requests.Session()
        self.session.headers.update({'Authorization': 'Bearer ' + github_token,
            'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2026-03-10'})
        self.sha = None
        self.last_signature = None
        self.conflict_halted = False

    def request(self, method, path, **kwargs):
        try:
            response = self.session.request(method, self.base + path, timeout=25, **kwargs)
        except requests.RequestException:
            raise RuntimeError('Durable checkpoint connection failed; operation stopped') from None
        if response.status_code == 409 and method == 'PUT' and path == '/contents/' + FILE:
            raise CheckpointConflict('Durable checkpoint returned HTTP 409; reconcile before retry')
        if not response.ok:
            raise RuntimeError('Durable checkpoint returned HTTP ' + str(response.status_code) + '; operation stopped')
        return response.json()

    def read(self) -> bytes:
        item = self.request('GET', '/contents/' + FILE, params={'ref': BRANCH})
        self.sha = item['sha']
        if item.get('encoding') != 'base64':
            item = self.request('GET', '/git/blobs/' + self.sha)
        if item.get('encoding') != 'base64':
            raise RuntimeError('Unsupported checkpoint encoding')
        return unseal(base64.b64decode(item['content']), self.metaculus_token)

    def restore(self, destination: Path):
        data = self.read()  # Missing/corrupt state fails closed; never silently start over.
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix('.restoring')
        temporary.write_bytes(data)
        temporary.replace(destination)

    def attach(self, state):
        remote = sqlite3.connect(':memory:')
        try:
            remote.deserialize(self.read())
            self.last_signature = signature(remote)
        finally:
            remote.close()
        if self.last_signature != signature(state.db):
            raise RuntimeError('Local and remote progress disagree; restore before forecasting')
        state.checkpoint = self.save

    def save(self, state):
        if self.conflict_halted:
            raise RuntimeError('Unresolved checkpoint conflict; restore before further writes')
        current = signature(state.db)
        if current == self.last_signature:
            return
        encrypted = seal(snapshot(state.db), self.metaculus_token)
        body = {'message':'Save encrypted forecasting progress',
            'branch':BRANCH, 'sha':self.sha, 'content':base64.b64encode(encrypted).decode()}
        try:
            result = self.request('PUT', '/contents/' + FILE, json=body)
        except CheckpointConflict:
            # Latch before any reread: callers can attempt to checkpoint their
            # error handler, which must never turn uncertainty into another PUT.
            self.conflict_halted = True
            remote = sqlite3.connect(':memory:')
            try:
                remote.deserialize(self.read())
                observed = signature(remote)
            finally:
                remote.close()
            if observed == current:
                # The intended durable state is already present. No second PUT.
                self.last_signature = current
                self.conflict_halted = False
                return
            if observed != self.last_signature:
                raise RuntimeError('Remote progress changed during checkpoint conflict; restore before forecasting')
            # Exactly one new CAS, using the same frozen encrypted bytes. A new
            # conflict, unreadable state or transport error leaves the latch set.
            result = self.request('PUT', '/contents/' + FILE, json={**body, 'sha':self.sha})
        self.sha = result['content']['sha']
        self.last_signature = current
        self.conflict_halted = False


def configured_cloud():
    repository = os.getenv('GITHUB_REPOSITORY')
    if os.getenv('GITHUB_ACTIONS') == 'true':
        return CloudState(repository or '', os.getenv('GITHUB_TOKEN', ''), os.getenv('METACULUS_TOKEN', ''))
    return None


if __name__ == '__main__':
    import argparse
    from dotenv import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['restore', 'export'])
    parser.add_argument('--database', default='state/bot.db')
    parser.add_argument('--output', default='state/progress.enc')
    args = parser.parse_args()
    if args.mode == 'restore':
        cloud = configured_cloud()
        if cloud is None:
            raise ValueError('Restore is intended for the hosted workflow')
        cloud.restore(Path(args.database))
    else:
        db = sqlite3.connect(args.database)
        try:
            Path(args.output).write_bytes(seal(snapshot(db), os.environ['METACULUS_TOKEN']))
        finally:
            db.close()
    print('Encrypted progress ' + args.mode + ' completed.')
