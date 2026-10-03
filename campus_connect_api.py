"""Session-bound school identity and campus subscription applications."""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from urllib.parse import urlsplit
import requests
from flask import Blueprint, current_app, jsonify, request, session


class Manager:
    def __init__(self, secret):
        self.secret=secret

    def call(self, path, data=None):
        with requests.Session() as client:
            client.trust_env=False
            response=client.request('POST' if data is not None else 'GET', 'http://127.0.0.1:18869/internal/'+path,
                                    headers={'Authorization':'Bearer '+self.secret},json=data,timeout=8)
            return response.status_code,response.json()


def create_campus_connect_blueprint(db_path):
    bp=Blueprint('campus_connect',__name__,url_prefix='/api/campus-connect')

    def config():
        secret=current_app.config.get('CAMPUS_CONNECT_SECRET') or os.getenv('MAXCOURSE_CAMPUS_API_SECRET','')
        client=current_app.config.get('CAMPUS_CONNECT_CLIENT') if current_app.testing else None
        return secret,client or Manager(secret)

    def user():
        uid=session.get('user_id')
        if not uid:return None
        with sqlite3.connect(db_path()) as db:
            db.row_factory=sqlite3.Row
            row=db.execute('SELECT id,username,ispace_username,display_name FROM users WHERE id=?',(uid,)).fetchone()
        if not row:return None
        school=(row['ispace_username'] or '').strip().lower()
        return {'id':row['id'],'display_name':row['display_name'] or row['username'],
                'verified':bool(re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,63}',school)),'school':school}

    def subject(who,secret):
        return hmac.new(secret.encode(),('school:'+who['school']).encode(),hashlib.sha256).hexdigest()

    def csrf(who):
        uid=who['id'] if who else None
        stored=session.get('campus_connect_csrf')
        if not isinstance(stored,dict) or stored.get('uid')!=uid:
            stored={'uid':uid,'token':secrets.token_urlsafe(32)}
            session['campus_connect_csrf']=stored
        return stored['token']

    @bp.after_request
    def private_response(response):
        response.headers['Cache-Control']='no-store'
        response.headers['Referrer-Policy']='no-referrer'
        return response

    @bp.get('/status')
    def status():
        who=user(); secret,manager=config()
        base={'user':{k:who[k] for k in ('id','display_name','verified')} if who else None,'csrf_token':csrf(who)}
        if not secret:
            return jsonify({**base,'available':False,'error':'订阅申请服务暂时不可用。'}),503
        key=subject(who,secret) if who and who['verified'] else None
        try:
            code,data=manager.call('status'+('?subject='+key if key else ''))
            if code!=200:raise ValueError('Manager unavailable')
            return jsonify({**base,**data,'available':True})
        except Exception:
            return jsonify({**base,'available':False,'error':'订阅申请服务暂时不可用，请稍后重试。'}),503

    def mutate(operation):
        request.max_content_length=4096
        who=user();secret,manager=config()
        if not who or not who['verified']:
            return jsonify({'error':'请先验证本人学校账号。','code':'school_login_required'}),401
        origin=request.headers.get('Origin')
        if origin and urlsplit(origin).netloc != request.host:
            return jsonify({'error':'请求来源无效。'}),403
        if not hmac.compare_digest(request.headers.get('X-Campus-CSRF',''),csrf(who)):
            return jsonify({'error':'登录状态已变化，请刷新后重试。'}),403
        if not secret:return jsonify({'error':'订阅申请服务暂时不可用。'}),503
        body=request.get_json(silent=True)
        if not isinstance(body,dict):return jsonify({'error':'请求格式不正确。'}),400
        payload={'subject':subject(who,secret)}
        if operation=='claim':
            if body.get('consent') is not True:return jsonify({'error':'请先确认申请规则与信息处理说明。'}),400
            payload['consent']=True
        else:
            if type(body.get('slot')) is not int or body['slot'] not in (1,2):return jsonify({'error':'设备编号无效。'}),400
            payload['slot']=body['slot']
        try:
            code,data=manager.call(operation,payload)
            return jsonify(data),code
        except Exception:
            return jsonify({'error':'申请结果暂时无法确认，请刷新查看。重复申请不会延长期限。'}),503

    @bp.post('/claim')
    def claim():return mutate('claim')

    @bp.post('/reset-device')
    def reset_device():return mutate('reset')

    return bp
