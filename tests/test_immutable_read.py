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
    def fixture(self,head_observations=None,bad_content=False,fail=False):
        cloud=m.CloudState('synthetic/repo','invented-token','invented-private-token-long')
        head_a='a'*40; head_c='c'*40; head_d='d'*40
        tree_a='b'*40; tree_c='e'*40; tree_d='f'*40
        encrypted_a=b'invented encrypted bytes'; encrypted_c=b'invented newer encrypted bytes'; encrypted_d=b'invented latest encrypted bytes'
        def blob_sha(encrypted):return hashlib.sha1(b'blob '+str(len(encrypted)).encode()+b'\0'+encrypted).hexdigest()
        blobs={blob_sha(encrypted_a):encrypted_a,blob_sha(encrypted_c):encrypted_c,blob_sha(encrypted_d):encrypted_d}
        commits={head_a:tree_a,head_c:tree_c,head_d:tree_d}
        trees={tree_a:blob_sha(encrypted_a),tree_c:blob_sha(encrypted_c),tree_d:blob_sha(encrypted_d)}
        observations=head_observations or [head_a,head_a]
        calls=[]; heads=0
        def request(method,path,**kwargs):
            nonlocal heads
            calls.append(path)
            if fail:raise RuntimeError('Invented read failure')
            if path.startswith('/contents/'):raise AssertionError('Mutable content endpoint used')
            if path.startswith('/git/ref/'):
                observed=observations[min(heads,len(observations)-1)]; heads+=1
                return {'object':{'sha':observed}}
            if path.startswith('/git/commits/'):
                requested=path.rsplit('/',1)[-1]
                if requested in commits:return {'sha':requested,'tree':{'sha':commits[requested]}}
            if path.startswith('/git/trees/'):
                requested=path.rsplit('/',1)[-1]
                if requested in trees:return {'sha':requested,'truncated':False,'tree':[{'path':'progress.enc','type':'blob','sha':trees[requested]}]}
            if path.startswith('/git/blobs/'):
                requested=path.rsplit('/',1)[-1]
                if requested in blobs:
                    encrypted=blobs[requested]
                    return {'sha':requested,'encoding':'base64','content':base64.b64encode(b'wrong' if bad_content else encrypted).decode()}
            raise AssertionError(path)
        cloud.request=request
        return cloud,calls,blob_sha(encrypted_a),blob_sha(encrypted_c)
    def test_matching_immutable_content(self):
        cloud,calls,blob,_=self.fixture()
        with patch.object(m,'unseal',return_value=b'database'):self.assertEqual(cloud.read(),b'database')
        self.assertEqual(cloud.sha,blob)
    def test_stale_branch_contents_after_acknowledged_save_not_used(self):
        cloud,calls,blob,_=self.fixture(); cloud.sha='d'*40
        with patch.object(m,'unseal',return_value=b'new database'):self.assertEqual(cloud.read(),b'new database')
        self.assertFalse(any('/contents/' in x for x in calls))
    def test_intervening_progress_retries_complete_immutable_read(self):
        cloud,calls,_,new_blob=self.fixture(head_observations=['a'*40,'c'*40,'c'*40,'c'*40])
        with patch.object(m,'unseal',side_effect=[b'old snapshot',b'new snapshot']):
            self.assertEqual(cloud.read(),b'new snapshot')
        self.assertIn('/git/commits/'+'a'*40,calls)
        self.assertIn('/git/commits/'+'c'*40,calls)
        self.assertEqual(cloud.sha,new_blob)
        self.assertEqual(cloud.read_commit,'c'*40)
    def test_repeated_intervening_progress_stops(self):
        cloud,_,_,_=self.fixture(head_observations=['a'*40,'c'*40,'c'*40,'d'*40])
        with patch.object(m,'unseal',return_value=b'database'),self.assertRaisesRegex(RuntimeError,'head changed'):cloud.read()
        self.assertIsNone(cloud.sha)
    def test_content_identity_mismatch_stops(self):
        cloud,_,_,_=self.fixture(bad_content=True)
        with self.assertRaisesRegex(RuntimeError,'content mismatch'):cloud.read()
    def test_read_failure_stops(self):
        cloud,_,_,_=self.fixture(fail=True)
        with self.assertRaisesRegex(RuntimeError,'read failure'):cloud.read()
    def test_wrong_commit_identity_stops(self):
        cloud,_,_,_=self.fixture(); original=cloud.request
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
        cloud,_,_,_=self.fixture(); cloud.read=lambda:remote.serialize()
        try:
            with self.assertRaisesRegex(RuntimeError,'Local and remote progress disagree'):cloud.attach(state)
            self.assertIsNone(state.checkpoint)
        finally:local.close(); remote.close()

if __name__=='__main__':unittest.main()

