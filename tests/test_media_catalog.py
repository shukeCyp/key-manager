import hashlib,json
import pytest
from sqlalchemy import create_engine,select,func,event
from sqlalchemy.orm import Session
from app import media_db as m,media_catalog as c,media_search as search


@pytest.fixture
def db(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'media.sqlite'))
    @event.listens_for(engine,'connect')
    def fk(conn,record):conn.execute('PRAGMA foreign_keys=ON')
    m.Base.metadata.create_all(engine)
    with Session(engine) as db:yield db
    engine.dispose()


def seed(db):
    files=[]
    for ident,kind,suffix in [('v1','video','mkv'),('s1','subtitle','srt'),('s2','subtitle','srt')]:
        path='//nas/share/mixed/'+ident+'.'+suffix
        files.append(dict(id=ident,path=path,path_hash=hashlib.sha256(path.encode()).hexdigest(),kind=kind,format=suffix))
    tracks=[dict(id='a1',file_id='v1',kind='audio',source='container',stream_key='1',stream_index=1,
        language='de',raw_language='ger',language_source='container',is_default=True),
        dict(id='a2',file_id='v1',kind='audio',source='container',stream_key='2',stream_index=2,
        language='ja',raw_language='jpn',language_source='container',is_default=False),
        *[dict(id=i,file_id=i,kind='subtitle',source='external',stream_key='external',stream_index=None,
        language='zh-Hans',raw_language=None,language_source='filename',is_default=False) for i in ('s1','s2')]]
    c.metadata(db,dict(files=files,tracks=tracks));db.commit()
    result=dict(works=[dict(ref='w1',type='series',title_original='The Rookie',year=2018,confidence=.9,
        original_language='en',titles=[dict(text='菜鸟老警',language='zh-Hans',kind='alias')])],
        files=[dict(file_id=i,work_ref='w1',season=2,episodes=[6],confidence=.9,issues=[]) for i in ('v1','s1','s2')])
    c.classify(db,dict(model='mimo-v2.6-flash',result=result));db.commit()
    return db.scalar(select(m.Work.id))


def test_exact_seven_tables_and_multi_audio_queries(db):
    work=seed(db)
    assert set(m.TABLES)=={'works','work_names','episodes','files','episode_files','tracks','subtitle_cues'}
    assert c.list_works(db,'菜鸟')[0]['id']==work
    files=c.resources(db,work,audio_language='ja')['items']
    assert len(files)==1 and files[0]['id']=='v1'
    assert {t['language'] for t in files[0]['tracks']}=={'de','ja'}
    assert not c.resources(db,work,audio_language='en')['items']
    assert len(c.resources(db,work,subtitle_language='zh-Hans')['items'])==2
    assert len(c.resources(db,work,subtitle_language='zh')['items'])==2


def test_idempotent_ingestion_and_multi_episode_binding(db):
    work=seed(db);seed(db)
    assert db.scalar(select(func.count()).select_from(m.Work))==1
    assert db.scalar(select(func.count()).select_from(m.File))==3
    body=dict(works=[dict(ref='w',type='series',title_original='The Rookie',year=2018,
        titles=[],confidence=.9)],files=[dict(file_id='v1',work_ref='w',season=2,episodes=[6,7],confidence=.8)])
    c.classify(db,dict(result=body));db.commit()
    assert len(c.resources(db,work,audio_language='ja')['items'][0]['episodes'])==2


def test_original_cues_deduplicated_without_losing_file_locations(db):
    work=seed(db)
    cues=[dict(start_ms=1000,end_ms=2000,text='今天我们一起回家'),dict(start_ms=2500,end_ms=3500,text='别担心我会保护你')]
    digest=hashlib.sha256(c.compact(cues).encode()).hexdigest()
    assert not c.subtitles(db,dict(file_id='s1',cues=cues,content_hash=digest))['reused']
    assert c.subtitles(db,dict(file_id='s2',cues=cues,content_hash=digest))['reused']
    assert db.scalar(select(func.count()).select_from(m.SubtitleCue))==2
    assert len(c.resources(db,work,subtitle_language='zh-Hans')['items'])==2
    result=search.phrase(db,work,'一起回家','zh-Hans')
    assert result['index_ready'] and {r['file_id'] for r in result['items']}=={'s1','s2'}
    assert result['items'][0]['episode']=='S02E06'
    assert not search.index_one(db.bind)


def test_reject_foreign_file_refs_and_untrusted_cue_hash(db):
    seed(db)
    with pytest.raises(ValueError):c.classify(db,{'result':{'works':[],'files':[{'file_id':'v1','work_ref':'invented'}]}})
    with pytest.raises(ValueError):c.subtitles(db,dict(file_id='s1',cues=[dict(start_ms=0,end_ms=2,text='Hi')],content_hash='0'*64))


def ingest_cues(db,file_id,cues):
    digest=hashlib.sha256(c.compact(cues).encode()).hexdigest()
    return c.subtitles(db,dict(file_id=file_id,cues=cues,content_hash=digest))


def make_search_ready(db,work,query,language=None):
    search.phrase(db,work,query,language)
    while search.index_one(db.bind):pass
    db.expire_all()
    return search.phrase(db,work,query,language,limit=100)['items']


def test_all_subtitle_languages_for_one_video_are_retained_and_searchable(db):
    work=seed(db)
    db.get(m.Track,'s1').language='de'
    db.get(m.Track,'s2').language='ja'
    db.commit()
    ingest_cues(db,'s1',[dict(start_ms=1000,end_ms=2000,text='Wir gehen gemeinsam nach Hause')])
    ingest_cues(db,'s2',[dict(start_ms=1000,end_ms=2000,text='今日は一緒に家に帰ろう')])
    assert {t.language for t in search.owners_for_work(db,work)}=={'de','ja'}
    assert [r['file_id'] for r in make_search_ready(db,work,'gemeinsam nach Hause','de')]==['s1']
    assert [r['file_id'] for r in make_search_ready(db,work,'一緒に家に帰ろう','ja')]==['s2']
    assert [r['file_id'] for r in search.phrase(db,work,'一緒に家に帰ろう')['items']]==['s2']
    assert not search.phrase(db,work,'一緒に家に帰ろう','de')['items']
    assert db.scalar(select(func.count()).select_from(m.SubtitleCue))==2


def test_repeated_dialogue_keeps_each_time_and_each_episode_location(db):
    work=seed(db)
    body=dict(works=[dict(ref='w',type='series',title_original='The Rookie',year=2018,
        titles=[],confidence=.9)],files=[dict(file_id='s2',work_ref='w',season=2,episodes=[7],confidence=.9)])
    c.classify(db,dict(result=body));db.commit()
    cues=[dict(start_ms=1000,end_ms=2000,text='今天我们一起回家'),
          dict(start_ms=12000,end_ms=13000,text='今天我们一起回家')]
    ingest_cues(db,'s1',cues)
    assert ingest_cues(db,'s2',cues)['reused']
    assert db.scalar(select(func.count()).select_from(m.SubtitleCue))==2
    hits=make_search_ready(db,work,'一起回家','zh')
    assert {(r['file_id'],r['episode'],r['start_ms']) for r in hits}=={
        ('s1','S02E06',1000),('s1','S02E06',12000),
        ('s2','S02E07',1000),('s2','S02E07',12000)}


def test_identical_subtitle_body_does_not_hide_other_work_or_language(db):
    work=seed(db)
    body=dict(works=[dict(ref='w',type='movie',title_original='Another Movie',year=2020,
        titles=[],confidence=.9)],files=[dict(file_id='s2',work_ref='w',season=None,episodes=[],confidence=.9)])
    c.classify(db,dict(result=body));db.get(m.Track,'s2').language='zh-Hant';db.commit()
    other=db.scalar(select(m.Work.id).where(m.Work.id!=work))
    cues=[dict(start_ms=1000,end_ms=2000,text='今天我們一起回家')]
    ingest_cues(db,'s1',cues)
    assert ingest_cues(db,'s2',cues)['reused']
    first=make_search_ready(db,work,'一起回家','zh')
    second=make_search_ready(db,other,'一起回家','zh')
    assert {(r['file_id'],r['language']) for r in first}=={('s1','zh-Hans')}
    assert {(r['file_id'],r['language']) for r in second}=={('s2','zh-Hant')}


def test_legacy_search_accepts_full_language_tags():
    from app.subtitles import Sentence
    assert Sentence(id='1',text='一起回家',language='zh-Hans').language=='zh-Hans'
    assert Sentence(id='1',text='Go home',language='en-US').language=='en-US'


def test_batch_search_directly_searches_all_languages_without_duplicate_votes(db):
    from app.routers.media import SearchRequest
    work=seed(db);db.get(m.Track,'s2').language='de';db.commit()
    chinese=['今天我们一起回家','千万不要打开那扇门','我已经找到了重要线索']
    german='Wir werden morgen gemeinsam nach Hause gehen'
    ingest_cues(db,'s1',[dict(start_ms=i*5000,end_ms=i*5000+2000,text=t) for i,t in enumerate(chinese)])
    ingest_cues(db,'s2',[dict(start_ms=1000,end_ms=2000,text=german)])
    value=SearchRequest(work_ids=[work],sentences=[dict(id=str(i),text=t) for i,t in enumerate(chinese+[german])]
        +[dict(id='repeat',text=chinese[0])])
    ready=search.search_catalog(db,value)
    assert ready['search_mode']=='direct' and ready['scanned_cues']==4
    assert ready['index_ready'] and ready['reliable']
    assert ready['ranked'][0]['episode']=='S02E06' and ready['ranked'][0]['hits']==4
    assert {h['language'] for h in ready['ranked'][0]['evidence']}=={'de','zh-Hans'}
    assert db.scalar(select(func.count()).select_from(m.SubtitleCue))==4
    assert {t.index_state for t in db.scalars(select(m.Track))}=={'none'}
    assert not search.index_one(db.bind)


def test_batch_search_fuzzy_asr_ignores_failed_derived_cache(db):
    from app.routers.media import SearchRequest
    work=seed(db)
    ingest_cues(db,'s1',[dict(start_ms=0,end_ms=2000,text='Your dog barks every morning outside')])
    value=SearchRequest(work_ids=[work],sentences=[dict(id='a',text='Your dog barked every morning outside')])
    ready=search.search_catalog(db,value)
    assert ready['ranked'][0]['hits']==1 and not ready['reliable']
    assert ready['ranked'][0]['evidence'][0]['round']==2
    owner=db.get(m.Track,'s1');owner.index_state='failed';owner.error='cache full';db.commit()
    ready=search.search_catalog(db,value)
    assert ready['index_ready'] and ready['ranked'][0]['hits']==1
    assert search.phrase(db,work,'Your dog barks')['items']
    assert owner.index_state=='failed' and owner.error=='cache full'


def test_direct_batch_cross_cue_gap_scope_and_read_only(db):
    from app.routers.media import SearchRequest
    work=seed(db)
    # Out-of-order source ordinals must not change chronological matching.
    ingest_cues(db,'s1',[
        dict(start_ms=3000,end_ms=4000,text='outside every morning'),
        dict(start_ms=1000,end_ms=2000,text='Your dog barks'),
        dict(start_ms=20000,end_ms=21000,text='please bring the missing documents'),
        dict(start_ms=30000,end_ms=31000,text='to the police station immediately')])
    db.get(m.File,'s2').availability='missing';db.commit()
    ingest_cues(db,'s2',[dict(start_ms=1000,end_ms=2000,text='The unavailable special subtitle dialogue')])
    value=SearchRequest(work_ids=[work],sentences=[
        dict(id='cross',text='Your dog barks outside every morning'),
        dict(id='gap',text='please bring the missing documents to the police station immediately'),
        dict(id='missing',text='The unavailable special subtitle dialogue'),
        dict(id='lang',text='Your dog barks outside every morning',language='ja')])
    statements=[]
    def record(conn,cursor,statement,*args):statements.append(statement.lstrip().split()[0].upper())
    event.listen(db.bind,'before_cursor_execute',record)
    try:result=search.search_catalog(db,value)
    finally:event.remove(db.bind,'before_cursor_execute',record)
    assert result['search_mode']=='direct' and result['scanned_cues']==4
    evidence=result['ranked'][0]['evidence']
    assert [hit['query_id'] for hit in evidence]==['cross']
    assert (evidence[0]['start_ms'],evidence[0]['end_ms'])==(1000,4000)
    assert {'id':'lang','reason':'language_missing'} in result['skipped']
    assert set(statements)=={'SELECT'}
    assert not db.scalar(select(m.SubtitleCue.search_text).where(m.SubtitleCue.track_id=='s1'))


def test_direct_batch_scans_once_and_matches_past_old_candidate_limit(db):
    from app.routers.media import SearchRequest
    work=seed(db)
    lines=['Your dog barks every morning outside '+str(i) for i in range(450)]
    ingest_cues(db,'s1',[dict(start_ms=i*6000,end_ms=i*6000+2000,text=t) for i,t in enumerate(lines)])
    db.get(m.Track,'s1').index_state='building';db.commit()
    value=SearchRequest(work_ids=[work],sentences=[dict(id=str(i),text=lines[i]) for i in (0,200,449)])
    result=search.search_catalog(db,value)
    assert result['scanned_cues']==450 and result['ranked'][0]['hits']==3
    assert result['index_ready'] and db.get(m.Track,'s1').index_state=='building'
    assert result['ranked'][0]['evidence'][-1]['start_ms']==449*6000


def test_direct_legacy_api_also_batches_without_cache(db):
    from app.subtitles import Search,Sentence
    work=seed(db)
    ingest_cues(db,'s1',[dict(start_ms=0,end_ms=2000,text='Your dog barks every morning outside')])
    result=search.legacy_search(db,Search(series_id=work,sentences=[
        Sentence(id='a',text='Your dog barks every morning outside',language='zh')]))
    assert result['index_ready'] and result['scanned_cues']==1
    assert result['ranked'][0]['evidence'][0]['id']=='a'


def test_cloud_resource_pages_can_fetch_only_video_and_expose_aliases(db):
    work=seed(db)
    assert [r['id'] for r in c.resources(db,work,kind='video')['items']]==['v1']
    assert len(c.resources(db,work,kind='subtitle')['items'])==2
    assert '菜鸟老警' in {n['name'] for n in c.list_works(db,'The Rookie')[0]['names']}


def test_known_show_with_unknown_episode_still_exposes_its_video(db):
    work=seed(db)
    body=dict(works=[dict(ref='w',type='series',title_original='The Rookie',year=2018,
        titles=[],confidence=.9)],files=[dict(file_id='v1',work_ref='w',season=None,episodes=[],confidence=.8)])
    c.classify(db,dict(result=body));db.commit()
    files=c.resources(db,work,audio_language='ja')['items']
    assert len(files)==1
    assert files[0]['episodes'][0]['code']=='UNKNOWN'
    assert files[0]['episodes'][0]['season'] is None
    assert files[0]['episodes'][0]['match_status']=='needs_review'


def test_new_model_result_does_not_replace_manually_confirmed_file_binding(db):
    work=seed(db)
    for row in db.scalars(select(m.EpisodeFile).where(m.EpisodeFile.file_id=='v1')):row.match_status='confirmed'
    db.commit()
    c.classify(db,dict(result=dict(works=[],files=[dict(file_id='v1',work_ref=None,season=None,episodes=[],confidence=0)])))
    db.commit()
    assert c.resources(db,work,audio_language='ja')['items'][0]['episodes'][0]['match_status']=='confirmed'
