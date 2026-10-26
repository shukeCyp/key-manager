"""Bounded, idempotent seven-table imports and resource queries."""
import gzip,hashlib,json,re,time
from sqlalchemy import select,func,delete,or_,text
from app import media_db as m
from app.subtitle_archive import upsert
from app.subtitles import ident,normalized,terms,term_token


class CapacityError(RuntimeError):pass


def compact(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'))


def checked_string(value,limit=4096):
    if not isinstance(value,str) or not value or len(value)>limit or '\x00' in value:raise ValueError('字符串格式无效')
    return value


def language_filter(column,language):
    language=language.replace('_','-')
    return or_(column==language,column.like(language+'-%')) if '-' not in language else column==language


def metadata(db,data):
    files=data.get('files',[]);tracks=data.get('tracks',[])
    if not isinstance(files,list) or len(files)>200 or not isinstance(tracks,list) or len(tracks)>3000:
        raise ValueError('元数据批次过大')
    allowed_files=set(m.File.__table__.columns.keys());allowed_tracks=set(m.Track.__table__.columns.keys())
    clean=[]
    for row in files:
        if not isinstance(row,dict) or set(row)-allowed_files:raise ValueError('文件字段无效')
        checked_string(row.get('id'),64);path=checked_string(row.get('path'))
        if row.get('kind') not in ('video','subtitle'):raise ValueError('文件类型无效')
        if row.get('path_hash')!=hashlib.sha256(path.replace('\\','/').casefold().encode()).hexdigest():
            raise ValueError('文件路径校验失败')
        clean.append(row)
    file_ids={row['id'] for row in files}
    for row in tracks:
        if not isinstance(row,dict) or set(row)-allowed_tracks:raise ValueError('轨道字段无效')
        checked_string(row.get('id'),32)
        if row.get('file_id') not in file_ids:raise ValueError('轨道引用不在当前批次内')
        if row.get('kind') not in ('audio','subtitle') or row.get('source') not in ('container','external'):
            raise ValueError('轨道类型无效')
        if not re.fullmatch(r'[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*',row.get('language','und')):
            raise ValueError('轨道语言无效')
    if clean:upsert(db,m.File,clean,[k for k in clean[0] if k not in ('id','details')])
    if tracks:
        # Never overwrite parsed text/canonical references with a later probe.
        changes=[k for k in tracks[0] if k not in ('id','content_hash','content_ref_id','cue_count','parse_status','index_state','index_bytes','last_used','error')]
        # SQLAlchemy bulk insert needs a uniform set of keys.
        keys=set.intersection(*(set(r) for r in tracks))
        upsert(db,m.Track,[{k:r[k] for k in keys} for r in tracks],[k for k in changes if k in keys])
    return dict(files=len(files),tracks=len(tracks))


def classify(db,data):
    result=data.get('result',{});works=result.get('works');files=result.get('files')
    if not isinstance(works,list) or not isinstance(files,list) or len(files)>100 or len(works)>100:
        raise ValueError('影视归属批次无效')
    refs={};recognized=[]
    for value in works:
        ref=checked_string(value.get('ref'),100)
        if ref in refs:raise ValueError('作品引用重复')
        names=value.get('titles',[])
        if not isinstance(names,list) or len(names)>60:raise ValueError('别名数量无效')
        original=value.get('title_original')
        display=original or next((n.get('text') for n in names if isinstance(n,dict)),None)
        if not display:
            refs[ref]=None;continue
        checked_string(display,255)
        kind=value.get('type','unknown')
        if kind not in ('series','movie','anime','documentary','other','unknown'):raise ValueError('作品类型无效')
        year=value.get('year')
        if year is not None and (type(year) is not int or not 1800<=year<=2200):raise ValueError('年份无效')
        identity=hashlib.sha256(compact([kind,normalized(display),year]).encode()).hexdigest()
        work_id=identity[:32];refs[ref]=work_id
        confidence=float(value.get('confidence',0))
        if not 0<=confidence<=1:raise ValueError('置信度无效')
        title_zh=next((n.get('text') for n in names if n.get('language') in ('zh','zh-Hans','zh-CN')),None)
        upsert(db,m.Work,[dict(id=work_id,identity_key=identity,title=display,title_original=original,
            title_zh=title_zh,year=year,kind=kind,status='model_candidate',confidence=confidence,details=compact(value))])
        db.flush()
        aliases=[]
        for name in names+[dict(text=display,language=value.get('original_language') or 'und',kind='canonical')]:
            label=checked_string(name.get('text'),255);language=checked_string(name.get('language','und'),32)
            norm=normalized(label)
            if not norm:continue
            aliases.append(dict(id=ident(work_id,norm,language),work_id=work_id,name=label,
                normalized=norm,language=language,kind=name.get('kind','alias')))
        upsert(db,m.WorkName,list({a['id']:a for a in aliases}.values()))
    seen=set()
    for value in files:
        file_id=checked_string(value.get('file_id'),64)
        if file_id in seen:raise ValueError('文件重复')
        seen.add(file_id);file=db.get(m.File,file_id)
        if not file:raise ValueError('必须先上传文件清单')
        if value.get('work_ref') is not None and value.get('work_ref') not in refs:raise ValueError('作品引用不存在')
        work_id=refs.get(value.get('work_ref'));season=value.get('season');numbers=value.get('episodes',[])
        if not isinstance(numbers,list) or len(numbers)>500 or any(type(n) is not int or n<0 or n>100000 for n in numbers):
            raise ValueError('集号无效')
        if season is not None and (type(season) is not int or not 0<=season<=999):raise ValueError('季号无效')
        confidence=float(value.get('confidence',0))
        if not 0<=confidence<=1:raise ValueError('置信度无效')
        previous=json.loads(file.details or '{}');previous['recognition']=value
        previous['model']=data.get('model','unknown');file.details=compact(previous)
        confirmed=db.scalar(select(m.EpisodeFile.file_id).where(m.EpisodeFile.file_id==file_id,
            m.EpisodeFile.match_status=='confirmed').limit(1))
        if confirmed:continue
        db.execute(delete(m.EpisodeFile).where(m.EpisodeFile.file_id==file_id))
        if not work_id:continue
        work=db.get(m.Work,work_id)
        episodes=[(None,None,'MOVIE')] if work.kind=='movie' else [
            (season,n,f'S{season:02d}E{n:02d}') for n in numbers] if season is not None else []
        if not episodes:episodes=[(None,None,'UNKNOWN')]
        for s,n,code in episodes:
            eid=ident(work_id,code)
            upsert(db,m.Episode,[dict(id=eid,work_id=work_id,season=s,number=n,code=code,
                title=(value.get('episode_title') or '')[:255] or None)])
            db.flush()
            upsert(db,m.EpisodeFile,[dict(episode_id=eid,file_id=file_id,match_status='needs_review' if code=='UNKNOWN' else 'model_candidate',
                confidence=confidence,evidence=compact(value.get('issues',[])))],('confidence','evidence','match_status'))
        language=value.get('subtitle_language')
        if file.kind=='subtitle' and language:
            track=db.scalar(select(m.Track).where(m.Track.file_id==file_id,m.Track.source=='external'))
            if track and track.language_source not in ('manual','filename'):
                track.language=checked_string(language,32);track.language_source='model_filename'
        recognized.append(file_id)
    return dict(works=len(refs),files=len(recognized))


def subtitles(db,data):
    if 'items' in data:
        values=data['items']
        if not isinstance(values,list) or not 1<=len(values)<=20:raise ValueError('字幕批次数量无效')
        results=[]
        for value in values:
            try:results.append(dict(file_id=value.get('file_id'),**subtitles(db,value)))
            except ValueError as exc:
                db.rollback();results.append(dict(file_id=value.get('file_id'),status='error',error=str(exc)))
        return dict(items=results)
    file_id=checked_string(data.get('file_id'),64);file=db.get(m.File,file_id)
    if not file or file.kind!='subtitle':raise ValueError('字幕文件不存在')
    track=db.scalar(select(m.Track).where(m.Track.file_id==file_id,m.Track.source=='external'))
    if not track:raise ValueError('外挂字幕轨不存在')
    if data.get('error'):
        track.parse_status='error';track.error=checked_string(data['error'],2000)
        return dict(status='error')
    rows=data.get('cues')
    if not isinstance(rows,list) or not 1<=len(rows)<=15000:raise ValueError('字幕句数无效')
    canonical=[]
    for row in rows:
        start=row.get('start_ms');end=row.get('end_ms');body=checked_string(row.get('text'),10000)
        if type(start) is not int or type(end) is not int or not 0<=start<end<=172800000:raise ValueError('字幕时间无效')
        canonical.append(dict(start_ms=start,end_ms=end,text=body))
    digest=hashlib.sha256(compact(canonical).encode()).hexdigest()
    if data.get('content_hash')!=digest:raise ValueError('字幕内容校验失败')
    if track.content_hash==digest and track.parse_status=='ready':return dict(status='ready',reused=True,cues=track.cue_count)
    lock='media-cues-'+digest[:40];mysql=db.bind.dialect.name=='mysql'
    if mysql:
        from shutil import disk_usage
        if disk_usage('/').free<10*1024**3:raise CapacityError('服务器剩余空间不足10GiB，采集已停止增长')
    lock_conn=db.bind.connect() if mysql else None
    if lock_conn is not None and lock_conn.scalar(text('SELECT GET_LOCK(:key,10)'),{'key':lock})!=1:
        lock_conn.close();raise ValueError('字幕内容正在写入，请重试')
    try:
        owner=db.scalar(select(m.Track).where(m.Track.content_hash==digest,m.Track.content_ref_id.is_(None),
            m.Track.parse_status=='ready',m.Track.id!=track.id).limit(1).with_for_update())
        if track.content_hash and track.content_hash!=digest:
            dependants=db.scalar(select(func.count()).select_from(m.Track).where(m.Track.content_ref_id==track.id))
            if dependants:raise ValueError('字幕正文变更涉及去重副本，需先拆分版本')
        db.execute(delete(m.SubtitleCue).where(m.SubtitleCue.track_id==track.id))
        track.content_hash=digest;track.cue_count=len(canonical);track.parse_status='ready';track.error=None
        track.content_ref_id=owner.id if owner else None;track.index_state='none';track.index_bytes=0
        if not owner:
            db.execute(m.SubtitleCue.__table__.insert(),[dict(track_id=track.id,ordinal=i+1,**r) for i,r in enumerate(canonical)])
        # Commit while the advisory lock is held, so the next importer sees owner.
        db.commit()
        return dict(status='ready',reused=bool(owner),cues=len(canonical))
    finally:
        if lock_conn is not None:
            try:lock_conn.execute(text('SELECT RELEASE_LOCK(:key)'),{'key':lock})
            finally:lock_conn.close()


def statistics(db):
    counts={name:db.scalar(select(func.count()).select_from(model)) for name,model in [
        ('works',m.Work),('episodes',m.Episode),('files',m.File),('tracks',m.Track)]}
    counts['subtitle_cues']=int(db.scalar(select(func.coalesce(func.sum(m.Track.cue_count),0)).where(
        m.Track.kind=='subtitle',m.Track.parse_status=='ready',m.Track.content_ref_id.is_(None))))
    counts['videos']=db.scalar(select(func.count()).select_from(m.File).where(m.File.kind=='video'))
    counts['subtitle_files']=db.scalar(select(func.count()).select_from(m.File).where(m.File.kind=='subtitle'))
    counts['audio_tracks']=db.scalar(select(func.count()).select_from(m.Track).where(m.Track.kind=='audio'))
    counts['parsed_subtitles']=db.scalar(select(func.count()).select_from(m.Track).where(m.Track.parse_status=='ready'))
    return dict(schema_version=1,tables=list(m.TABLES),**counts)


def list_works(db,q='',limit=100):
    statement=select(m.Work)
    if q:
        matching=select(m.WorkName.work_id).where(m.WorkName.normalized.contains(normalized(q),autoescape=True))
        statement=statement.where(or_(m.Work.id.in_(matching),m.Work.title.contains(q,autoescape=True)))
    works=list(db.scalars(statement.order_by(m.Work.title).limit(limit)))
    names={w.id:[] for w in works}
    if works:
        for row in db.scalars(select(m.WorkName).where(m.WorkName.work_id.in_(names))):
            names[row.work_id].append(dict(name=row.name,language=row.language,kind=row.kind))
    return [dict(id=w.id,title=w.title,title_zh=w.title_zh,title_original=w.title_original,
        kind=w.kind,year=w.year,status=w.status,confidence=w.confidence,names=names[w.id]) for w in works]


def resources(db,work_id,audio_language=None,subtitle_language=None,limit=100,offset=0,kind=None):
    if not db.get(m.Work,work_id):raise ValueError('影视不存在')
    eligible=select(m.EpisodeFile.file_id).join(m.Episode,m.Episode.id==m.EpisodeFile.episode_id).where(m.Episode.work_id==work_id)
    query=select(m.File).where(m.File.id.in_(eligible),m.File.availability=='present')
    if kind:query=query.where(m.File.kind==kind)
    if audio_language:query=query.where(m.File.id.in_(select(m.Track.file_id).where(m.Track.kind=='audio',language_filter(m.Track.language,audio_language))))
    if subtitle_language:query=query.where(m.File.id.in_(select(m.Track.file_id).where(m.Track.kind=='subtitle',language_filter(m.Track.language,subtitle_language))))
    result=[]
    for file in db.scalars(query.order_by(m.File.path).offset(offset).limit(limit)):
        try:library_root=json.loads(file.details or '{}').get('root')
        except (ValueError,AttributeError):library_root=None
        tracks=[{k:getattr(t,k) for k in ('id','kind','source','stream_index','language','raw_language','language_source','codec',
            'channels','is_default','is_forced','parse_status','index_state','binding_status')} for t in db.scalars(select(m.Track).where(m.Track.file_id==file.id))]
        episodes=[dict(id=e.id,season=e.season,episode=e.number,code=e.code,title=e.title,match_status=a.match_status,
            confidence=a.confidence) for e,a in db.execute(select(m.Episode,m.EpisodeFile).join(m.EpisodeFile).where(
                m.EpisodeFile.file_id==file.id,m.Episode.work_id==work_id))]
        result.append(dict(id=file.id,path=file.path,kind=file.kind,format=file.format,size=file.size,
            duration_ms=file.duration_ms,mtime_ns=file.mtime_ns,library_root=library_root,
            probe_status=file.probe_status,error=file.error,tracks=tracks,episodes=episodes))
    return dict(items=result,offset=offset,limit=limit)
