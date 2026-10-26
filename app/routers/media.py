"""Authenticated seven-table catalog APIs; ingestion has a separate scoped key."""
import hashlib,hmac,json,threading
from fastapi import APIRouter,Depends,HTTPException,Request,Query
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from app.config import settings
from app.dependencies import get_current_admin,check_rate_limit
from app.routers.subtitles import access
from app import media_db as store,media_catalog as service,media_search
from pydantic import BaseModel,Field,ConfigDict

admin_router=APIRouter(prefix='/media',dependencies=[Depends(get_current_admin)])
client_router=APIRouter(prefix='/client/media')
slots=threading.BoundedSemaphore(4)


def call(fn,*args,write=False,**kwargs):
    if not settings.MEDIA_SCHEMA_ENABLED:raise HTTPException(503,'影视数据库尚未切换')
    if not slots.acquire(blocking=False):raise HTTPException(503,'影视数据库忙，请重试')
    try:
        with Session(store.engine()) as db:
            value=fn(db,*args,**kwargs)
            if write:db.commit()
            return value
    except ValueError as exc:raise HTTPException(422,str(exc)) from None
    except service.CapacityError as exc:raise HTTPException(507,str(exc)) from None
    except (TypeError,KeyError,AttributeError):raise HTTPException(422,'采集字段格式无效') from None
    except SQLAlchemyError:raise HTTPException(503,'影视数据库暂不可用') from None
    finally:slots.release()


def ingest_access(request:Request):
    value=request.headers.get('X-Media-Ingest-Key','')
    expected=settings.MEDIA_INGEST_TOKEN_SHA256
    if not expected or not hmac.compare_digest(hashlib.sha256(value.encode()).hexdigest(),expected):
        raise HTTPException(401,'采集令牌无效')
    if not check_rate_limit('media-ingest',240,60):raise HTTPException(429,'采集请求过于频繁')


@client_router.post('/ingest/{kind}',dependencies=[Depends(ingest_access)])
async def ingest(kind:str,request:Request):
    methods={'metadata':service.metadata,'classify':service.classify,'subtitles':service.subtitles}
    if kind not in methods:raise HTTPException(404,'未知采集类型')
    chunks=[];size=0
    async for chunk in request.stream():
        size+=len(chunk)
        if size>10*1024**2:raise HTTPException(413,'采集批次过大')
        chunks.append(chunk)
    try:value=json.loads(b''.join(chunks))
    except (ValueError,UnicodeError):raise HTTPException(422,'采集JSON无效') from None
    if not isinstance(value,dict):raise HTTPException(422,'采集JSON无效')
    return await run_in_threadpool(lambda:call(methods[kind],value,write=True))


@admin_router.get('/status')
def status():return call(service.statistics)


@admin_router.get('/works')
def works(q:str=Query('',max_length=255)):return dict(items=call(service.list_works,q))


@admin_router.get('/works/{work_id}/files')
def files(work_id:str,audio_language:str|None=None,subtitle_language:str|None=None,
          limit:int=Query(100,ge=1,le=200),offset:int=Query(0,ge=0),kind:str|None=Query(None,pattern='^(video|subtitle)$')):
    return call(service.resources,work_id,audio_language,subtitle_language,limit,offset,kind)


@admin_router.get('/cues')
def cues(work_id:str,q:str=Query(min_length=2,max_length=500),language:str|None=None):
    return call(media_search.phrase,work_id,q,language)


class SearchSentence(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    id:str=Field(min_length=1,max_length=64)
    text:str=Field(min_length=1,max_length=500)
    narrator:bool=False
    language:str|None=Field(default=None,pattern=r'^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$')


class SearchRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    work_ids:list[str]=Field(min_length=1,max_length=20)
    sentences:list[SearchSentence]=Field(min_length=1,max_length=200)


@admin_router.post('/search')
async def search(request:Request):
    from app.routers.subtitles import payload
    value=await payload(request,SearchRequest,192*1024)
    return await run_in_threadpool(lambda:call(media_search.search_catalog,value))


client_router.add_api_route('/works',works,methods=['GET'],dependencies=[Depends(access)])
client_router.add_api_route('/works/{work_id}/files',files,methods=['GET'],dependencies=[Depends(access)])
client_router.add_api_route('/cues',cues,methods=['GET'],dependencies=[Depends(access)])
client_router.add_api_route('/search',search,methods=['POST'],dependencies=[Depends(access)])
