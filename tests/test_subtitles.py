import gzip,json
import pytest
from sqlalchemy import create_engine,select,func
from sqlalchemy.orm import Session
from app import subtitle_db as store
from app import subtitles as svc
from app.routers import subtitles as routes
from app.config import settings
from tests.test_licensing import env,admin_headers,generate,raw_verify,sdk_for
from tools.export_shared_subtitles import language,episode


def record(**changes):
    value=dict(library_id='library1',source_id='srt1',revision='revision1',title='The Rookie',aliases=['菜鸟老警'],year=2018,
               episode='S02E06',language='en',language_source='manual',root='//nas/videos',relative_path='The Rookie/S02E06.en.srt',
               video_id='video1',video_relative_path='The Rookie/S02E06.mkv',pairing='filename_unverified',source_digest='a'*64,
               cues=[dict(start_ms=1000,end_ms=2000,text="You're making a big mistake!"),
                     dict(start_ms=2100,end_ms=3000,text='You killed our boy in cold blood.'),
                     dict(start_ms=3100,end_ms=4500,text='We are not leaving anyone outside this shelter.')])
    value.update(changes);return svc.ImportFile(**value)


@pytest.fixture
def db(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'subtitles.sqlite'))
    store.Base.metadata.create_all(engine)
    with Session(engine) as db: yield db
    engine.dispose()


def search_value(series_id,language='en'):
    return svc.Search(series_id=series_id,sentences=[dict(id=str(i),language=language,text=c.text) for i,c in enumerate(record().cues)])


def test_content_dedup_aliases_and_resume(db):
    first=svc.import_file(db,record());second=svc.import_file(db,record(source_id='duplicate',relative_path='duplicate.en.srt'))
    assert second['reused'] and first['track_id']==second['track_id']
    assert db.scalar(select(func.count()).select_from(store.Source))==2
    assert db.scalar(select(func.count()).select_from(store.Track))==1
    assert db.scalar(select(func.count()).select_from(store.Window))==3
    assert svc.list_series(db,'菜鸟老警')[0]['id']==first['series_id']
    assert len(json.loads(gzip.decompress(db.get(store.Track,first['track_id']).cues_gzip)))==3
    assert svc.import_file(db,record())['reused']


def test_scopes_language_variants_and_generic_lines(db):
    a=svc.import_file(db,record())
    svc.import_file(db,record(source_id='different-show',title='Unrelated Show',aliases=[]))
    value=search_value(a['series_id'])
    result=svc.search(db,value)
    assert result['ranked'][0]['episode']=='S02E06' and result['ranked'][0]['hits']==3
    assert result['reliable'] and not result['needs_visual_fallback']
    missing=svc.search(db,search_value(a['series_id'],'ja'))
    assert not missing['coverage_complete'] and not missing['ranked']
    assert all(x['reason']=='language_missing' for x in missing['skipped'])
    generic=svc.Search(series_id=a['series_id'],sentences=[dict(id='x',text='Get back!',language='en')])
    assert svc.search(db,generic)['skipped'][0]['reason']=='too_generic'
    twice=svc.Search(series_id=a['series_id'],sentences=value.sentences+value.sentences)
    assert svc.search(db,twice)['ranked'][0]['hits']==3


def test_source_change_and_deactivation_do_not_leave_searchable_ghosts(db):
    first=svc.import_file(db,record())
    changed=record(cues=[dict(start_ms=0,end_ms=2000,text='A wholly unrelated subtitle replacement sentence.')])
    svc.import_file(db,changed)
    assert not svc.search(db,search_value(first['series_id']))['ranked']
    row=db.scalar(select(store.Source));row.active=False;db.flush()
    assert svc.search(db,search_value(first['series_id']))['language_coverage']==[]


def test_translation_suffix_and_episode():
    assert language('The Rookie_S02E06.Deutsch.srt-zh-CN-translation.srt',[])[0]=='zh'
    assert language('The.Rookie.S02E06.es.srt',[])[0]=='es'
    assert language('The Rookie_S02E06_en-us.srt',[])[0]=='en'
    assert language('The Rookie _ Le flic de Los Angeles_S06E01.French (Forced Narrative).srt',[])[0]=='fr'
    assert language('It.S01E01.English (CC).srt',[])[0]=='en'
    assert language('unknown.srt',[dict(text='hello')])[0]=='und'
    assert episode('S01E01/The.Rookie.S02E06.mkv')=='S02E06'
    assert episode('第 1 季/新紮師兄 第1季 - 第10集 - 骨肉情深.srt')=='S01E10'
    assert episode('第3季/SRT字幕/3.8.srt')=='S03E08'
    assert episode('第4季/SRT字幕/3.8.srt') is None
    assert episode('第 2 季/0204-CN.srt')=='S02E04'
    assert episode('第 1 季/1-14.srt')=='S01E14'
    assert episode('第 1 季/10.srt')=='S01E10'
    assert episode('第 4 季/ザ・ルーキー４_#5_大騒ぎのハロウィーン.srt')=='S04E05'


def test_on_demand_cache_and_restore(db):
    from app.subtitle_cache import run_one
    from tools.rebuild_subtitle_windows import reset_missing
    from sqlalchemy import delete
    first=svc.import_file(db,record(),build_index=False);db.commit()
    assert db.scalar(select(func.count()).select_from(store.Window))==0
    value=search_value(first['series_id']);pending=svc.search(db,value)
    assert not pending['index_ready'] and not pending['needs_visual_fallback']
    assert pending['index_progress'][0]['total']==1
    assert run_one(db.bind)
    db.expire_all()
    ready=svc.search(db,value)
    assert ready['index_ready'] and ready['reliable']
    db.execute(delete(store.Window));db.commit()
    assert reset_missing(db.bind)==1
    db.expire_all()
    assert not svc.search(db,value)['index_ready']
    assert run_one(db.bind)
    db.expire_all()
    assert svc.search(db,value)['reliable']


def test_eviction_preserves_originals_and_requeues(db,monkeypatch):
    from app.subtitle_cache import make_room,run_one
    first=svc.import_file(db,record());db.commit()
    original=db.get(store.Track,first['track_id']).cues_gzip
    monkeypatch.setattr(settings,'SUBTITLE_CACHE_BYTES',64*1024**2)
    db.rollback()
    make_room(db.bind,64*1024**2,'other','en');db.expire_all()
    track=db.get(store.Track,first['track_id'])
    assert track.indexed_version==0 and track.cues_gzip==original
    assert db.scalar(select(func.count()).select_from(store.Source))==1
    assert db.scalar(select(func.count()).select_from(store.Window))==0
    assert not svc.search(db,search_value(first['series_id']))['index_ready']
    assert run_one(db.bind)


def test_review_archive_and_scoped_tokens(db):
    first=svc.import_file(db,record(),build_index=False)
    value=record().model_dump();value['review_reason']='episode_ambiguous'
    svc.import_review(db,value)
    review=db.scalar(select(store.ReviewFile))
    assert json.loads(gzip.decompress(review.payload_gzip))['cues']==value['cues']
    assert not db.scalar(select(store.Source)).active
    svc.import_file(db,record(),build_index=False)
    assert db.scalar(select(func.count()).select_from(store.ReviewFile))==0
    assert db.scalar(select(store.Source)).active
    assert len({svc.term_token("isn't",'show1','en'),svc.term_token("isn't",'show2','en'),svc.term_token("isn't",'show1','de')})==3


def test_batch_archive_dedup_and_preserves_cached_track(db):
    from app.subtitle_archive import archive_batch
    first=svc.import_file(db,record());db.commit()
    original=db.get(store.Track,first['track_id']);payload=original.cues_gzip
    assert archive_batch(db,[record(),record(source_id='copy')])==0
    db.commit();db.expire_all()
    assert db.get(store.Track,first['track_id']).indexed_version==svc.INDEX_VERSION
    assert db.get(store.Track,first['track_id']).cues_gzip==payload
    assert db.scalar(select(func.count()).select_from(store.Source))==2
    other=record(source_id='other',episode='S02E07')
    assert archive_batch(db,[other,record(source_id='other-copy',episode='S02E07')])==1
    db.commit()
    assert db.scalar(select(func.count()).select_from(store.Track))==2
    assert db.scalar(select(func.count()).select_from(store.Window))==3
    review=record(source_id='other').model_dump();review['review_reason']='ambiguous';svc.import_review(db,review);db.commit()
    archive_batch(db,[record(source_id='other')]);db.commit()
    assert db.scalar(select(func.count()).select_from(store.ReviewFile))==0
    assert db.get(store.Source,svc.ident('library1','other')).track_id==first['track_id']


def test_staged_parallel_import_resume(db,tmp_path,monkeypatch):
    import hashlib,sys
    from tools import import_library_bundle as runner
    folder=tmp_path/'chunks';folder.mkdir();chunks=[]
    for i in range(2):
        raw=gzip.compress((record(source_id='parallel'+str(i)).model_dump_json()+'\n').encode())
        name=f'{i}.jsonl.gz';(folder/name).write_bytes(raw)
        chunks.append(dict(key=str(i),path=name,files=1,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()))
    (folder/'manifest.json').write_text(json.dumps(dict(status='complete',expected_files=2,chunks=chunks)))
    monkeypatch.setattr(runner,'engine',lambda:db.bind)
    monkeypatch.setenv('SUBTITLE_DATABASE_URL',str(db.bind.url))
    monkeypatch.setattr(sys,'argv',['import',str(folder),'--run-id','parallel'])
    runner.main();db.expire_all()
    assert db.get(store.ImportRun,'parallel').processed==2
    assert db.get(store.ImportRun,'parallel').status=='complete'
    assert db.scalar(select(func.count()).select_from(store.Track))==1
    assert db.scalar(select(func.count()).select_from(store.Source))==2
    assert db.scalar(select(func.count()).select_from(store.Window))==0
    db.rollback();runner.main();db.expire_all()
    assert db.scalar(select(func.count()).select_from(store.Source))==2


def test_api_purposes_validation_and_revocation(env,tmp_path,monkeypatch):
    client,factory=env
    engine=create_engine('sqlite:///'+str(tmp_path/'subtitles.sqlite'),connect_args={'check_same_thread':False})
    store.Base.metadata.create_all(engine)
    monkeypatch.setattr(store,'engine',lambda:engine);monkeypatch.setattr(settings,'SUBTITLE_DATABASE_URL','configured')
    auth=admin_headers(client)
    assert client.get('/api/subtitles/status').status_code==401
    imported=client.post('/api/subtitles/files',headers=auth,json=record().model_dump())
    assert imported.status_code==200,imported.text
    sid=imported.json()['series_id']
    assert client.get('/api/subtitles/status',headers=auth).json()['unique_tracks']==1
    card=generate(env);verified=raw_verify(client,card).json()['payload']
    query_headers={'Authorization':'Bearer '+verified['subtitle_token']}
    feedback_headers={'Authorization':'Bearer '+verified['feedback_token']}
    assert client.get('/api/client/subtitles/series?q=Rookie',headers=query_headers).status_code==200
    assert client.get('/api/subtitles/status',headers=query_headers).status_code==401
    assert client.get('/api/client/subtitles/series?q=Rookie',headers=feedback_headers).status_code==401
    assert client.get('/api/client/subtitles/series?q=Rookie',headers=auth).status_code==401
    request=search_value(sid).model_dump()
    from app.subtitle_cache import run_one
    assert not client.post('/api/client/subtitles/search',headers=query_headers,json=request).json()['index_ready']
    assert run_one(engine)
    assert client.post('/api/client/subtitles/search',headers=query_headers,json=request).json()['reliable']
    sdk=sdk_for(client);sdk.verify(card)
    assert sdk.query_subtitles(sid,request['sentences'])['reliable']
    assert client.get('/api/client/subtitles/tracks/'+imported.json()['track_id']+'/sources',headers=query_headers).json()['items'][0]['video_id']=='video1'
    assert client.post('/api/client/subtitles/search',headers=query_headers,content=b'x'*(192*1024+1)).status_code==413
    from app.models import Key
    with factory() as db:
        key=db.query(Key).filter_by(key_code=card).one();key.status='disabled';db.commit()
    assert client.get('/api/client/subtitles/series?q=Rookie',headers=query_headers).status_code==403
    engine.dispose()
