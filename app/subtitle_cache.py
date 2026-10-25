"""Bounded, rebuildable show/language FULLTEXT cache; originals are never evicted."""
import gzip,json,time,shutil
from contextlib import contextmanager,nullcontext
from sqlalchemy import select,func,text,exists,update,delete
from sqlalchemy.orm import Session
from app.config import settings
from app.subtitle_db import Series,Track,Source,Window,IndexJob
from app.subtitles import INDEX_VERSION,ident,terms,Cue,build_windows,window_rows


@contextmanager
def scope_lock(engine,series_id):
    if engine.dialect.name!='mysql':
        yield True;return
    name='subtitle-scope:'+series_id
    with engine.connect() as connection:
        acquired=connection.scalar(text('SELECT GET_LOCK(:name,0)'),dict(name=name))==1
        try:yield acquired
        finally:
            if acquired:connection.execute(text('SELECT RELEASE_LOCK(:name)'),dict(name=name))


def active_tracks(series_id,language=None):
    statement=select(Track).where(Track.series_id==series_id,exists(select(Source.id).where(Source.track_id==Track.id,Source.active.is_(True))))
    if language:statement=statement.where(Track.language==language)
    return statement


def ensure_cached(db,value):
    if not db.get(Series,value.series_id):raise ValueError('未找到此剧')
    now=int(time.time());pending=[]
    requested={s.language for s in value.sentences if len(terms(s.text))>=3}
    for language in requested:
        rows=list(db.execute(active_tracks(value.series_id,language).with_only_columns(Track.id,Track.indexed_version)))
        missing=sum(version!=INDEX_VERSION for _,version in rows)
        if rows:
            db.execute(update(Track).where(Track.id.in_([r[0] for r in rows])).values(last_used=now))
        if not missing:continue
        key=ident('index',value.series_id,language);job=db.get(IndexJob,key)
        if not job:
            job=IndexJob(id=key,series_id=value.series_id,language=language,status='queued',done=len(rows)-missing,total=len(rows),updated_at=now,message='等待生成本剧字幕索引')
            db.add(job)
        elif job.status=='complete' or (job.status=='failed' and job.updated_at<now-60):
            job.status='queued';job.updated_at=now;job.done=len(rows)-missing;job.total=len(rows);job.message='等待重建缓存'
        pending.append(dict(language=language,status=job.status,done=job.done,total=job.total,message=job.message))
    db.commit()
    if not pending:return None
    return pending_response(value.series_id,pending)


def pending_response(series_id,pending):
    return dict(series_id=series_id,status='indexing',index_ready=False,index_progress=pending,
                reliable=False,candidate_episodes=[],needs_visual_fallback=False,ranked=[],elapsed_ms=0,
                queried_sentences=0,skipped=[],language_coverage=[],coverage_complete=False,total_episodes=0,
                cross_language='explicit_variants_only')


def make_room(engine,required,keep_series,keep_language):
    if engine.dialect.name=='mysql' and shutil.disk_usage('/var/lib/mysql').free<10*1024**3:
        raise RuntimeError('服务器可用磁盘不足 10 GiB，已暂停新增缓存')
    limit=max(64*1024**2,settings.SUBTITLE_CACHE_BYTES)
    with Session(engine) as db:
        used=int(db.scalar(select(func.coalesce(func.sum(Track.index_bytes),0))))
        candidates=list(db.execute(select(Track.series_id,Track.language,func.min(Track.last_used)).where(Track.index_bytes>0).group_by(Track.series_id,Track.language).order_by(func.min(Track.last_used))))
    if required>limit:raise RuntimeError('单份字幕超过缓存预算')
    for series_id,language,_ in candidates:
        if used+required<=limit:return
        if (series_id,language)==(keep_series,keep_language):continue
        lock=nullcontext(True) if series_id==keep_series else scope_lock(engine,series_id)
        with lock as acquired:
            if not acquired:continue
            with Session(engine) as db:
                ids=list(db.scalars(select(Track.id).where(Track.series_id==series_id,Track.language==language,Track.index_bytes>0)))
                for track_id in ids:
                    track=db.get(Track,track_id);size=track.index_bytes
                    db.execute(delete(Window).where(Window.track_id==track_id))
                    track.indexed_version=0;track.index_bytes=0;db.commit();used-=size
    if used+required>limit:raise RuntimeError('字幕索引缓存已满，请减少该剧语言范围或提高缓存预算')


def run_one(engine):
    """One durable job at a time. A crashed worker resumes its running job on restart."""
    with Session(engine) as db:
        job=db.scalar(select(IndexJob).where(IndexJob.status.in_(['queued','running'])).order_by(IndexJob.updated_at).limit(1))
        if not job:return False
        job_id,series_id,language=job.id,job.series_id,job.language
    with scope_lock(engine,series_id) as acquired:
        if not acquired:return False
        try:
            with Session(engine) as db:
                ids=list(db.scalars(active_tracks(series_id,language).with_only_columns(Track.id).order_by(Track.id)))
                job=db.get(IndexJob,job_id);job.status='running';job.total=len(ids);job.done=0;job.updated_at=int(time.time());db.commit()
            for i,track_id in enumerate(ids,1):
                with Session(engine) as db:
                    track=db.get(Track,track_id)
                    if track.indexed_version!=INDEX_VERSION:
                        cues=[Cue(**v) for v in json.loads(gzip.decompress(track.cues_gzip))]
                        rows=window_rows(track.id,track.series_id,track.language,cues)
                        required=sum(len(r['text'].encode())+len(r['document'].encode())+256 for r in rows)*2
                        make_room(engine,max(0,required-track.index_bytes),series_id,language)
                        build_windows(db,track,cues)
                    job=db.get(IndexJob,job_id);job.done=i;job.updated_at=int(time.time());job.message='正在准备本剧字幕索引';db.commit()
            with Session(engine) as db:
                job=db.get(IndexJob,job_id);job.status='complete';job.updated_at=int(time.time());job.message='可查询';db.commit()
        except Exception as exc:
            with Session(engine) as db:
                job=db.get(IndexJob,job_id);job.status='failed';job.updated_at=int(time.time());job.message=str(exc)[:200] if isinstance(exc,RuntimeError) else type(exc).__name__;db.commit()
            print('Subtitle cache job failed:',job_id,type(exc).__name__,flush=True)
    return True
