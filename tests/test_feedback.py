import datetime
import io
import json
import uuid
import zipfile

import pytest
import requests

from app.config import settings
from app.dependencies import create_token
from app.models import Feedback,Key,DeviceBinding
from app.licensing import beijing_now
from test_licensing import env,generate,raw_verify,admin_headers,sdk_for
from tk_license import FeedbackError


@pytest.fixture
def feedback_env(env,tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'FEEDBACK_STORAGE_DIR',str(tmp_path/'attachments'))
    return env


def archive(name='diagnostics.json',value=b'{"synthetic":true}'):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as stream:stream.writestr(name,value)
    return out.getvalue()


def feedback_auth(env):
    card=generate(env)
    token=raw_verify(env[0],card).json()['payload']['feedback_token']
    return card,{'Authorization':'Bearer '+token}


def submit(client,headers,logs=None,**changes):
    fields=dict(submission_id=str(uuid.uuid4()),app_version='0.1.2',description='点击导出后任务报错，请检查日志。',contact='test@example.invalid')
    fields.update(changes)
    files={'metadata':(None,json.dumps(fields,ensure_ascii=False))}
    if logs is not None:files['logs']=('logs.zip',logs,'application/zip')
    return client.post('/api/client/feedback',headers=headers,files=files)


def test_feedback_receipt_admin_list_filter_download_and_review(feedback_env):
    client,factory=feedback_env;_,headers=feedback_auth(feedback_env);data=archive()
    response=submit(client,headers,data);assert response.status_code==201,response.text
    receipt=response.json();assert receipt['has_archive'] and receipt['status']=='new'
    assert client.get('/api/feedback').status_code==401
    assert client.get('/api/feedback/'+receipt['id']+'/logs',headers=headers).status_code==401
    auth=admin_headers(client);listing=client.get('/api/feedback?version=0.1.2&status=new',headers=auth).json()
    assert listing['total']==1 and listing['items'][0]['app_version']=='0.1.2'
    assert 'key_code' not in listing['items'][0]
    attachment=client.get('/api/feedback/'+receipt['id']+'/logs',headers=auth)
    assert attachment.status_code==200 and attachment.content==data
    reviewed=client.patch('/api/feedback/'+receipt['id'],headers=auth,json={'status':'resolved','admin_note':'已修复'}).json()
    assert reviewed['status']=='resolved' and reviewed['admin_note']=='已修复'
    assert client.get('/api/feedback?status=new',headers=auth).json()['total']==0


def test_submission_retry_is_idempotent_and_changed_body_conflicts(feedback_env):
    client,factory=feedback_env;_,headers=feedback_auth(feedback_env);ident=str(uuid.uuid4());logs=archive()
    a=submit(client,headers,logs,submission_id=ident);b=submit(client,headers,logs,submission_id=ident)
    assert a.status_code==b.status_code==201 and a.json()['id']==b.json()['id']
    assert submit(client,headers,logs,submission_id=ident,description='另一条不同的问题现象').status_code==409
    with factory() as db:assert db.query(Feedback).count()==1


def test_auth_tokens_cannot_cross_purposes_and_revocation_is_immediate(feedback_env):
    client,factory=feedback_env;card,headers=feedback_auth(feedback_env)
    assert submit(client,{},archive()).status_code==401
    assert submit(client,admin_headers(client),archive()).status_code==401
    assert client.get('/api/keys',headers=headers).status_code==401
    with factory() as db:key=db.query(Key).filter_by(key_code=card).one();key.status='disabled';db.commit()
    assert submit(client,headers,archive()).status_code==403
    with factory() as db:
        key=db.query(Key).filter_by(key_code=card).one();key.status='used';key.expire_time=beijing_now()-datetime.timedelta(seconds=1);db.commit()
    assert submit(client,headers).status_code==403
    with factory() as db:
        key=db.query(Key).filter_by(key_code=card).one();key.expire_time=None;db.query(DeviceBinding).delete();db.commit()
    assert submit(client,headers).status_code==403
    expired=create_token(dict(type='feedback',key_id=1,machine_code='a'*64),datetime.timedelta(seconds=-1))
    assert submit(client,{'Authorization':'Bearer '+expired}).status_code==401


@pytest.mark.parametrize('logs',[b'not a zip',archive('../secret.log'),archive('tool.exe'),archive('C:/private.log')])
def test_invalid_zip_never_creates_a_feedback_or_file(feedback_env,logs):
    client,factory=feedback_env;_,headers=feedback_auth(feedback_env)
    assert submit(client,headers,logs).status_code==400
    with factory() as db:assert db.query(Feedback).count()==0


def test_size_metadata_and_quota_limits(feedback_env,monkeypatch):
    client,_=feedback_env;_,headers=feedback_auth(feedback_env)
    assert submit(client,headers,description='x').status_code==422
    assert submit(client,headers,app_version='bad/version').status_code==422
    response=client.post('/api/client/feedback',headers={**headers,'Content-Length':str(20*1024*1024)},content=b'x')
    assert response.status_code==413
    monkeypatch.setattr(settings,'FEEDBACK_MAX_ARCHIVE_BYTES',100)
    assert submit(client,headers,archive()).status_code==413
    monkeypatch.setattr(settings,'FEEDBACK_MAX_ARCHIVE_BYTES',15*1024*1024)
    monkeypatch.setattr(settings,'FEEDBACK_TOTAL_STORAGE_BYTES',0)
    assert submit(client,headers,archive()).status_code==507
    assert submit(client,headers).status_code==201


def test_sdk_submits_multipart_with_short_lived_token_and_reuses_id(feedback_env,tmp_path):
    client,factory=feedback_env;card=generate(feedback_env);sdk=sdk_for(client);sdk.verify(card)
    path=tmp_path/'logs.zip';path.write_bytes(archive());ident=str(uuid.uuid4())
    a=sdk.submit_feedback('0.1.2','运行配音任务时发生错误。',path,submission_id=ident)
    b=sdk.submit_feedback('0.1.2','运行配音任务时发生错误。',path,submission_id=ident)
    assert a['id']==b['id'];sdk.require_valid()
    with factory() as db:assert db.query(Feedback).count()==1
    deadline=sdk._valid_until
    def offline(*args,**kwargs):raise requests.ConnectionError('test')
    sdk._session.post=offline
    with pytest.raises(FeedbackError,match='无法提交'):sdk.submit_feedback('0.1.2','另一条测试反馈描述。')
    assert sdk._valid_until==deadline


def test_feedback_page_and_integration_are_available(feedback_env):
    client,_=feedback_env
    assert client.get('/feedback').status_code==200
    data=client.get('/api/integration',headers=admin_headers(client)).json()
    assert data['feedback_endpoint']=='/api/client/feedback'
    assert b'submit_feedback' in client.get('/integration').content
