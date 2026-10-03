import hashlib
import hmac
import os
import sqlite3
import tempfile
import unittest
from flask import Flask
from campus_connect_api import create_campus_connect_blueprint


class FakeManager:
    def __init__(self):self.calls=[];self.fail=False
    def call(self,path,data=None):
        self.calls.append((path,data))
        if self.fail:return 409,{'error':'名额已满','code':'capacity_full'}
        return 200,{'capacity':10,'used':0,'remaining':10,'test_days':60,'device_limit':2,'subscription':None}


class CampusConnectTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=os.path.join(self.temp.name,'test.db')
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,ispace_username TEXT,display_name TEXT)')
            db.execute('INSERT INTO users VALUES(1,"synthetic-local","synthetic-school","Test")')
            db.execute('INSERT INTO users VALUES(2,"unverified",NULL,"Unverified")')
        self.manager=FakeManager()
        self.app=Flask(__name__);self.app.config.update(TESTING=True,SECRET_KEY='fake-session',CAMPUS_CONNECT_SECRET='fake-internal-secret',CAMPUS_CONNECT_CLIENT=self.manager)
        self.app.register_blueprint(create_campus_connect_blueprint(lambda:self.db));self.client=self.app.test_client()

    def login(self,uid=1):
        with self.client.session_transaction() as session:session['user_id']=uid
        return self.client.get('/api/campus-connect/status').get_json()['csrf_token']

    def test_guest_status_has_no_private_subject_or_subscription(self):
        response=self.client.get('/api/campus-connect/status')
        self.assertEqual(response.status_code,200);self.assertIsNone(response.json['user'])
        self.assertEqual(self.manager.calls[-1],('status',None))
        self.assertEqual(response.headers['Cache-Control'],'no-store')

    def test_claim_requires_verified_school_csrf_origin_and_consent(self):
        self.assertEqual(self.client.post('/api/campus-connect/claim',json={'consent':True}).status_code,401)
        token=self.login(2)
        self.assertEqual(self.client.post('/api/campus-connect/claim',json={'consent':True},headers={'X-Campus-CSRF':token}).status_code,401)
        token=self.login()
        for data,headers,expected in [({'consent':True},{},403),({'consent':True},{'X-Campus-CSRF':token,'Origin':'https://evil.example'},403),({'consent':False},{'X-Campus-CSRF':token},400)]:
            before=len(self.manager.calls)
            response=self.client.post('/api/campus-connect/claim',json=data,headers=headers)
            self.assertEqual(response.status_code,expected);self.assertEqual(len(self.manager.calls),before)

    def test_subject_is_derived_from_verified_binding_not_client_input(self):
        token=self.login()
        response=self.client.post('/api/campus-connect/claim',json={'consent':True,'subject':'attacker-selected'},headers={'X-Campus-CSRF':token})
        self.assertEqual(response.status_code,200)
        expected=hmac.new(b'fake-internal-secret',b'school:synthetic-school',hashlib.sha256).hexdigest()
        self.assertEqual(self.manager.calls[-1],('claim',{'subject':expected,'consent':True}))

    def test_quota_error_propagates_and_invalid_reset_is_denied(self):
        token=self.login();self.manager.fail=True
        response=self.client.post('/api/campus-connect/claim',json={'consent':True},headers={'X-Campus-CSRF':token})
        self.assertEqual(response.status_code,409)
        for slot in [0,3,True,'1']:
            self.assertEqual(self.client.post('/api/campus-connect/reset-device',json={'slot':slot},headers={'X-Campus-CSRF':token}).status_code,400)

    def test_changed_session_invalidates_previous_csrf(self):
        token=self.login()
        with self.client.session_transaction() as session:session['user_id']=2
        self.client.get('/api/campus-connect/status')
        with self.client.session_transaction() as session:self.assertNotEqual(session['campus_connect_csrf']['token'],token)


if __name__=='__main__':unittest.main()
