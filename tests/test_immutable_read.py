import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import sqlite3
import types
import unittest
from unittest.mock import patch

package=types.ModuleType('checkpoint_candidate'); package.__path__=[]
core=types.ModuleType('checkpoint_candidate.core'); core.canonical=lambda x:json.dumps(x,sort_keys=True)
sys.modules['checkpoint_candidate']=package; sys.modules['checkpoint_candidate.core']=core
spec=importlib.util.spec_from_file_location('checkpoint_candidate.cloud',Path(__file__).resolve().parents[1]/'futureeval_bot/cloud.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class ReadTests(unittest.TestCase):
    def fixture(self,changed=False,bad_content=False,fail=False):
        cloud=m.CloudState('synthetic/repo','invented-token','invented-private-token-long')
        encrypted=b'invented encrypted bytes'; blob=hashlib.sha1(b'blob '+str(len(encrypted)).encode()+b'\0'+encrypted).hexdigest()
        head='a'*40; tree='b'*40; calls=[]; heads=0
        def request(method,path,**kwargs):
            nonlocal heads
            calls.append(path)
            if fail:raise RuntimeError('Invented read failure')
            if path.startswith('/contents/'):raise AssertionError('Mutable content endpoint used')
            if path.startswith('/git/ref/'):
                heads+=1; return {'object':{'sha':'c'*40 if changed and heads>1 else head}}
            if path=='/git/commits/'+head:return {'sha':head,'tree':{'sha':tree}}
            if path=='/git/trees/'+tree:return {'sha':tree,'truncated':False,'tree':[{'path':'progress.enc','type':'blob','sha':blob}]}
            if path=='/git/blobs/'+blob:return {'sha':blob,'encoding':'base64','content':base64.b64encode(b'wrong' if bad_content else encrypted).decode()}
            raise AssertionError(path)
        cloud.request=request
        return cloud,calls,blob
    def test_matching_immutable_content(self):
        cloud,calls,blob=self.fixture()
        with patch.object(m,'unseal',return_value=b'database'):self.assertEqual(cloud.read(),b'database')
        self.assertEqual(cloud.sha,blob)
    def test_stale_branch_contents_after_acknowledged_save_not_used(self):
        cloud,calls,blob=self.fixture(); cloud.sha='d'*40
        with patch.object(m,'unseal',return_value=b'new database'):self.assertEqual(cloud.read(),b'new database')
        self.assertFalse(any('/contents/' in x for x in calls))
    def test_intervening_progress_stops(self):
        cloud,_,_=self.fixture(changed=True)
        with patch.object(m,'unseal',return_value=b'database'),self.assertRaisesRegex(RuntimeError,'head changed'):cloud.read()
        self.assertIsNone(cloud.sha)
    def test_content_identity_mismatch_stops(self):
        cloud,_,_=self.fixture(bad_content=True)
        with self.assertRaisesRegex(RuntimeError,'content mismatch'):cloud.read()
    def test_read_failure_stops(self):
        cloud,_,_=self.fixture(fail=True)
        with self.assertRaisesRegex(RuntimeError,'read failure'):cloud.read()
    def test_wrong_commit_identity_stops(self):
        cloud,_,_=self.fixture(); original=cloud.request
        cloud.request=lambda method,path,**kw: {'sha':'e'*40,'tree':{'sha':'b'*40}} if path.startswith('/git/commits/') else original(method,path,**kw)
        with self.assertRaisesRegex(RuntimeError,'commit identity mismatch'):cloud.read()
    def test_genuine_durable_divergence_rejected_without_callback(self):
        def database(value):
            db=sqlite3.connect(':memory:')
            db.executescript('CREATE TABLE jobs(id INTEGER); CREATE TABLE calls(id INTEGER); CREATE TABLE meta(key TEXT,value TEXT);')
            db.execute('INSERT INTO meta VALUES(?,?)',('offline_checks_passed',value)); db.commit()
            return db
        local=database('new'); remote=database('old')
        state=types.SimpleNamespace(db=local,checkpoint=None)
        cloud,_,_=self.fixture(); cloud.read=lambda:remote.serialize()
        try:
            with self.assertRaisesRegex(RuntimeError,'Local and remote progress disagree'):cloud.attach(state)
            self.assertIsNone(state.checkpoint)
        finally:local.close(); remote.close()

if __name__=='__main__':unittest.main()
