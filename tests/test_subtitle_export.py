import gzip,hashlib,json,sqlite3,sys
from tools.export_library_bundle import main
from tools.plan_shared_subtitles import infer


def test_media_identity_uses_paired_video():
    assert infer('电视剧/C/菜鸟老警/第2季/法.srt','电视剧/C/菜鸟老警/第2季/The.Rookie.S02E06.mkv')['episode']=='S02E06'
    assert infer('电视剧/C/菜鸟老警/第2季/法.srt')['review_reason']=='season_without_episode'
    movie=infer('电影/A/爱尔兰人/爱尔兰人.de.srt','电影/A/爱尔兰人/爱尔兰人 (2019).mkv')
    assert movie['title']=='爱尔兰人' and movie['year']==2019 and movie['episode']=='MOVIE'
    assert infer('电视剧/黑道家族（德英法日）/第1季/The.Sopranos.S01E01.en.srt')['title']=='黑道家族'


def test_sequential_export_preserves_cues_and_archives_unknown(tmp_path,monkeypatch):
    root=tmp_path/'library';folder=root/'catalog/versions/v1';folder.mkdir(parents=True)
    with sqlite3.connect(folder/'catalog.sqlite') as db:
        db.executescript('CREATE TABLE roots(id TEXT,path TEXT); CREATE TABLE subtitle_files(id TEXT,root_id TEXT,relative_path TEXT,seen INTEGER); CREATE TABLE videos(id TEXT,relative_path TEXT,seen INTEGER);')
        db.execute('INSERT INTO roots VALUES (?,?)',('r1','//nas/media'))
        names=['电视剧/C/菜鸟老警/第2季/The.Rookie.S02E06.en.srt','电视剧/C/菜鸟老警/第2季/法.srt','电视剧/C/菜鸟老警/第2季/The.Rookie.S02E07.en.srt','absent.srt']
        db.executemany('INSERT INTO subtitle_files VALUES (?,?,?,1)',[(str(i),'r1',s) for i,s in enumerate(names)])
    with sqlite3.connect(root/'shard.sqlite') as db:
        db.executescript('CREATE TABLE files(id TEXT,relative_path TEXT,video_id TEXT,pairing TEXT,digest TEXT,error TEXT,available INTEGER); CREATE TABLE cues(id INTEGER PRIMARY KEY,file_id TEXT,ordinal INTEGER,start REAL,end REAL,original TEXT);')
        db.executemany('INSERT INTO files VALUES (?,?,NULL,?,?,NULL,1)',[(str(i),s,'unpaired','a'*64) for i,s in enumerate(names[:3])])
        db.executemany('INSERT INTO cues VALUES (?,?,?,?,?,?)',[(1,'0',0,2,3,'First original cue'),(2,'0',1,1,2,'Second original cue'),(3,'1',0,0,1,'Bonjour tout le monde'),(4,'2',0,2,1,'Invalid duration')])
    manifest=dict(libraryId='lib',version='v1',subtitles={'r1':{'path':'shard.sqlite'}})
    raw=json.dumps(manifest).encode();(folder/'manifest.json').write_bytes(raw)
    (root/'current.json').write_text(json.dumps(dict(version='v1',manifest=dict(sha256=hashlib.sha256(raw).hexdigest()))))
    out=tmp_path/'out';monkeypatch.setattr(sys,'argv',['export','--library',str(root),'--output',str(out),'--chunk-files','10'])
    main()
    result=json.loads((out/'manifest.json').read_text())
    assert result['status']=='complete' and result['files']==4 and result['review']==3
    chunk=out/result['chunks'][0]['path']
    assert hashlib.sha256(chunk.read_bytes()).hexdigest()==result['chunks'][0]['sha256']
    records=[]
    for part in result['chunks']:
        with gzip.open(out/part['path'],'rt',encoding='utf8') as stream:records.extend(json.loads(line) for line in stream)
    assert [c['start_ms'] for c in records[0]['cues']]==[2000,1000]
    assert records[0]['title']=='The Rookie' and records[0]['language']=='en'
    assert records[1]['review_reason']=='season_without_episode'
    assert records[2]['review_reason']=='invalid_subtitle_record'
    assert records[3]['review_reason']=='missing_published_shard_record'
