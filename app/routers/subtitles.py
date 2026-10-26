"""Authenticated admin imports and separate, short-lived licensed query access."""
import threading
from functools import partial
from fastapi import APIRouter, Depends, Request, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy import select,func,case
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from app.config import settings
from app.database import get_db
from app.dependencies import get_current_admin, check_rate_limit
from app.routers.feedback import identity
from app import subtitles as service
from app import subtitle_db as store

admin_router=APIRouter(prefix='/subtitles',dependencies=[Depends(get_current_admin)],tags=['subtitle-admin'])
client_router=APIRouter(prefix='/client/subtitles',tags=['subtitle-query'])
slots=threading.BoundedSemaphore(2)
import_slot=threading.BoundedSemaphore(1)


def access(request:Request,db:Session=Depends(get_db)):
    key,app,claims=identity(request,db,token_type='subtitle_query')
    if app.app_code!=settings.SUITE_APP_CODE: raise HTTPException(403,'产品无字幕查询权限')
    ip=request.client.host if request.client else 'unknown'
    if not check_rate_limit('subtitle-ip:'+ip,120,60) or not check_rate_limit('subtitle-key:'+str(key.id),30,60):
        raise HTTPException(429,'字幕查询过于频繁')
    return claims


def call(fn,*args,write=False):
    semaphore=import_slot if write else slots
    if not semaphore.acquire(blocking=False): raise HTTPException(503,'字幕服务忙，请稍后重试')
    try:
        with Session(store.engine()) as db:
            result=fn(db,*args)
            if write: db.commit()
            return result
    except ValueError as exc: raise HTTPException(404,str(exc)) from exc
    except (SQLAlchemyError,RuntimeError): raise HTTPException(503,'字幕数据库暂不可用') from None
    finally: semaphore.release()


async def payload(request,model,maximum):
    parts=[];size=0
    async for chunk in request.stream():
        size+=len(chunk)
        if size>maximum: raise HTTPException(413,'字幕请求过大')
        parts.append(chunk)
    try: return model.model_validate_json(b''.join(parts))
    except ValidationError: raise HTTPException(422,'字幕请求格式无效') from None


@admin_router.post('/files')
async def import_file(request:Request):
    if settings.MEDIA_SCHEMA_ENABLED:raise HTTPException(409,'旧字幕导入已停用，请使用七表采集接口')
    value=await payload(request,service.ImportFile,8*1024*1024)
    return await run_in_threadpool(call,partial(service.import_file,build_index=False),value,write=True)


@admin_router.get('/series')
def series(q:str=Query('',max_length=255)):
    if settings.MEDIA_SCHEMA_ENABLED:
        from app.media_catalog import list_works
        return dict(items=call(list_works,q))
    return dict(items=call(service.list_series,q))


@client_router.get('/series',dependencies=[Depends(access)])
def client_series(q:str=Query(min_length=2,max_length=255)):
    return series(q)


def sources_for_track(db,track_id):
    if settings.MEDIA_SCHEMA_ENABLED:
        from app import media_db as m
        track=db.get(m.Track,track_id)
        if not track:raise ValueError('字幕轨不存在')
        file=db.get(m.File,track.file_id)
        return [dict(library_id='media',subtitle_id=file.id,video_id=track.bound_video_id,
            root='',relative_path=file.path,video_relative_path=None,pairing=track.binding_status,revision='media-v1')]
    if not db.get(store.Track,track_id):raise ValueError('字幕轨不存在')
    return [dict(library_id=s.library_id,subtitle_id=s.external_id,video_id=s.video_id,
                 root=s.root,relative_path=s.relative_path,video_relative_path=s.video_relative_path,
                 pairing=s.pairing,revision=s.revision) for s in db.scalars(select(store.Source).where(
                     store.Source.track_id==track_id,store.Source.active.is_(True)).limit(100))]


@client_router.get('/tracks/{track_id}/sources',dependencies=[Depends(access)])
def track_sources(track_id:str):
    if len(track_id)!=32:raise HTTPException(404,'字幕轨不存在')
    return dict(items=call(sources_for_track,track_id))


@admin_router.post('/search')
async def search(request:Request):
    value=await payload(request,service.Search,192*1024)
    if settings.MEDIA_SCHEMA_ENABLED:
        from app.media_search import legacy_search
        return await run_in_threadpool(call,legacy_search,value,write=True)
    return await run_in_threadpool(call,service.search,value)


@client_router.post('/search',dependencies=[Depends(access)])
async def client_search(request:Request):
    return await search(request)


def statistics(db):
    if settings.MEDIA_SCHEMA_ENABLED:
        from app import media_db as m
        from app.media_catalog import statistics as media_statistics
        result=media_statistics(db)
        return dict(result,series=result['works'],sources=result['subtitle_files'],unique_tracks=db.scalar(
            select(func.count()).select_from(m.Track).where(m.Track.kind=='subtitle')),
            cues=result['subtitle_cues'],review_files=db.scalar(select(func.count()).select_from(m.File).where(m.File.error.is_not(None))),
            indexed_tracks=db.scalar(select(func.count()).select_from(m.Track).where(m.Track.index_state=='ready')),
            cache_budget_bytes=settings.SUBTITLE_CACHE_BYTES,cache_estimated_bytes=int(db.scalar(select(func.coalesce(func.sum(m.Track.index_bytes),0)))),
            index_jobs=[dict(language=t.language,status=t.index_state,done=0,total=t.cue_count,message=t.error or '') for t in db.scalars(
                select(m.Track).where(m.Track.index_state.in_(['pending','building','failed'])).limit(20))],imports=[],
            languages=[dict(language=lang,tracks=n) for lang,n in db.execute(select(m.Track.language,func.count()).where(
                m.Track.kind=='subtitle').group_by(m.Track.language))])
    return dict(series=db.scalar(select(func.count()).select_from(store.Series)),
                episodes=db.scalar(select(func.count()).select_from(store.Episode)),
                unique_tracks=db.scalar(select(func.count()).select_from(store.Track)),
                sources=db.scalar(select(func.count()).select_from(store.Source).where(store.Source.active.is_(True))),
                cues=db.scalar(select(func.coalesce(func.sum(store.Track.cue_count),0))),
                review_files=db.scalar(select(func.count()).select_from(store.ReviewFile)),
                indexed_tracks=db.scalar(select(func.count()).select_from(store.Track).where(store.Track.indexed_version==service.INDEX_VERSION)),
                cache_budget_bytes=settings.SUBTITLE_CACHE_BYTES,
                cache_estimated_bytes=int(db.scalar(select(func.coalesce(func.sum(store.Track.index_bytes),0)))),
                index_jobs=[dict(language=j.language,status=j.status,done=j.done,total=j.total,message=j.message) for j in db.scalars(select(store.IndexJob).where(store.IndexJob.status!='complete').order_by(store.IndexJob.updated_at).limit(20))],
                languages=[dict(language=lang,tracks=count) for lang,count in db.execute(select(store.Track.language,func.count()).group_by(store.Track.language))],
                imports=[dict(id=r.id,status=r.status,processed=r.processed,failed=r.failed,message=r.message) for r in db.scalars(select(store.ImportRun).order_by(case((store.ImportRun.status.in_(['running','interrupted','partial']),0),else_=1),store.ImportRun.id.desc()).limit(10))])


@admin_router.get('/status')
def status():
    if not settings.SUBTITLE_DATABASE_URL: return dict(enabled=False)
    return dict(enabled=True,**call(statistics))


@admin_router.get('/review')
def review(page:int=Query(1,ge=1)):
    def listing(db):
        if settings.MEDIA_SCHEMA_ENABLED:
            from app import media_db as m
            return dict(items=[dict(id=f.id,path=f.path,reason=f.error) for f in db.scalars(select(m.File).where(
                m.File.error.is_not(None)).order_by(m.File.id).offset((page-1)*30).limit(30))])
        return dict(items=[dict(id=r.id,path=r.relative_path,reason=r.reason) for r in db.scalars(select(store.ReviewFile).order_by(store.ReviewFile.id).offset((page-1)*30).limit(30))])
    return call(listing)
