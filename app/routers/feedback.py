"""License-scoped feedback ingestion, and authenticated admin review."""
import hashlib
import asyncio
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import uuid
import zipfile

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_admin, check_rate_limit
from app.licensing import beijing_now
from app.models import Application, DeviceBinding, Key, Feedback
from starlette.concurrency import run_in_threadpool

client_router = APIRouter(prefix='/client', tags=['feedback'])
admin_router = APIRouter(prefix='/feedback', tags=['feedback-admin'], dependencies=[Depends(get_current_admin)])
_upload_slots=asyncio.Semaphore(2)


class Submission(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    submission_id: uuid.UUID
    app_version: str = Field(min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9][a-zA-Z0-9._+ -]*$')
    description: str = Field(min_length=5, max_length=10000)
    contact: str = Field(default='', max_length=200)


def identity(request, db, token_type='feedback'):
    authorization=request.headers.get('authorization','')
    if not authorization.startswith('Bearer '):raise HTTPException(401,'缺少反馈令牌，请先验证卡密')
    try:
        data=jwt.decode(authorization[7:],settings.JWT_SECRET,algorithms=[settings.JWT_ALGORITHM],options={'require':['exp']})
        if data.get('type')!=token_type or type(data.get('key_id')) is not int:raise ValueError()
        if not isinstance(data.get('machine_code'),str) or not 1<=len(data['machine_code'])<=64:raise ValueError()
    except (jwt.InvalidTokenError,ValueError,TypeError):raise HTTPException(401,'反馈令牌无效或已过期，请重新验证卡密')
    key=db.query(Key).filter_by(id=data['key_id']).first()
    app=db.query(Application).filter_by(app_code=data.get('app_code')).first()
    if not key or not app or key.app_id!=app.id or key.status not in {'active','used'}:
        raise HTTPException(403,'当前授权不可提交反馈')
    if key.expire_time and beijing_now()>=key.expire_time:raise HTTPException(403,'授权已过期')
    if not db.query(DeviceBinding).filter_by(key_id=key.id,machine_code=data['machine_code']).first():
        raise HTTPException(403,'设备授权已解绑')
    return key,app,data


def validate_archive(data):
    if not data or len(data)>settings.FEEDBACK_MAX_ARCHIVE_BYTES:raise HTTPException(413,'日志包大小应在 1 字节至 15 MB 之间')
    # Bound central-directory object creation before constructing ZipFile; ZIP64
    # and multi-disk uploads are unnecessary for diagnostic archives.
    end=data.rfind(b'PK\x05\x06',max(0,len(data)-65557))
    if end<0 or len(data)<end+22:raise HTTPException(400,'日志包不是有效 ZIP')
    disk,central_disk,entries_disk,entries=struct.unpack_from('<4H',data,end+4)
    if struct.unpack_from('<I',data,end+12)[0]>2*1024*1024:raise HTTPException(400,'ZIP 目录过大')
    if disk or central_disk or entries_disk!=entries or not 1<=entries<=1000:raise HTTPException(400,'日志包文件数量无效')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            rows=archive.infolist()
            if len(rows)!=entries or sum(row.file_size for row in rows)>100*1024*1024:raise ValueError('size')
            for row in rows:
                name=row.filename.replace('\\','/');path=PurePosixPath(name)
                if len(name)>1024:raise ValueError('name')
                if path.is_absolute() or '..' in path.parts or ':' in name or '\x00' in name or row.flag_bits&1:raise ValueError('path')
                if stat.S_ISLNK(row.external_attr>>16):raise ValueError('symlink')
                if not row.is_dir() and path.suffix.lower() not in {'.log','.txt','.json','.jsonl'}:raise ValueError('type')
                if row.compress_type not in {zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED}:raise ValueError('compression')
            if archive.testzip() is not None:raise ValueError('crc')
    except (zipfile.BadZipFile,ValueError,RuntimeError,NotImplementedError,EOFError):raise HTTPException(400,'日志包损坏或包含不允许的文件')


def receipt(row):
    return dict(id=row.id,submission_id=row.submission_id,status=row.status,created_at=row.created_at.isoformat(),
                app_version=row.app_version,has_archive=bool(row.archive_name))


@client_router.post('/feedback',status_code=201)
async def submit(request:Request,db:Session=Depends(get_db)):
    async with _upload_slots:
        return await _submit(request,db)


async def _submit(request,db):
    key,app,auth=identity(request,db)
    ip=request.client.host if request.client else 'unknown'
    if not check_rate_limit('feedback-ip:'+ip,60,3600) or not check_rate_limit(f'feedback-device:{key.id}:{auth["machine_code"]}',10,600):
        raise HTTPException(429,'反馈提交过于频繁，请稍后再试')
    maximum=settings.FEEDBACK_MAX_ARCHIVE_BYTES+64*1024
    length=request.headers.get('content-length')
    if length and (not length.isdecimal() or int(length)>maximum):raise HTTPException(413,'反馈请求过大')
    parts=[];size=0
    async for part in request.stream():
        size+=len(part)
        if size>maximum:raise HTTPException(413,'反馈请求过大')
        parts.append(part)
    request._body=b''.join(parts)
    try:
        form=await request.form(max_files=1,max_fields=1,max_part_size=64000)
        if set(form.keys())-{'metadata','logs'} or not isinstance(form.get('metadata'),str):raise ValueError()
        value=Submission.model_validate_json(form['metadata'])
        upload=form.get('logs')
        data=await upload.read(settings.FEEDBACK_MAX_ARCHIVE_BYTES+1) if upload else None
        if upload and not hasattr(upload,'filename'):raise ValueError()
    except (ValueError,TypeError,AttributeError):raise HTTPException(422,'反馈信息无效，请检查版本号和问题描述')
    finally:
        if 'form' in locals():await form.close()
    if data is not None:await run_in_threadpool(validate_archive,data)
    digest=hashlib.sha256(data).hexdigest() if data is not None else None
    fields=value.model_dump(mode='json')
    content_hash=hashlib.sha256(json.dumps(dict(**fields,archive_sha256=digest),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    lookup=dict(key_id=key.id,machine_code=auth['machine_code'],submission_id=str(value.submission_id))
    old=db.query(Feedback).filter_by(**lookup).first()
    if old:
        if old.content_hash!=content_hash:raise HTTPException(409,'此提交编号已用于另一条反馈')
        return receipt(old)
    used=db.query(func.coalesce(func.sum(Feedback.archive_bytes),0)).scalar()
    if used+(len(data) if data else 0)>settings.FEEDBACK_TOTAL_STORAGE_BYTES:raise HTTPException(507,'反馈附件存储已满，请暂不附加日志或联系管理员')
    ident=uuid.uuid4().hex;folder=Path(settings.FEEDBACK_STORAGE_DIR);folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    name=ident+'.zip' if data is not None else None
    file=folder/name if name else None
    if file:
        with file.open('xb') as stream:stream.write(data)
        file.chmod(0o600)
    row=Feedback(id=ident,app_id=app.id,**lookup,client_name=auth.get('client_name','')[:64],app_version=value.app_version,
                 description=value.description,contact=value.contact,content_hash=content_hash,archive_name=name,
                 archive_bytes=len(data) if data else 0,archive_sha256=digest,created_at=beijing_now(),updated_at=beijing_now())
    try:
        db.add(row);db.commit();db.refresh(row)
    except IntegrityError:
        db.rollback()
        if file:file.unlink(missing_ok=True)
        old=db.query(Feedback).filter_by(**lookup).first()
        if old and old.content_hash==content_hash:return receipt(old)
        raise HTTPException(409,'此提交编号已用于另一条反馈')
    except Exception:
        db.rollback()
        if file:file.unlink(missing_ok=True)
        raise
    return receipt(row)


def summary(row):
    return dict(**receipt(row),description=row.description,contact=row.contact,client_name=row.client_name,
                machine_code=row.machine_code,archive_bytes=row.archive_bytes,archive_sha256=row.archive_sha256,
                admin_note=row.admin_note,updated_at=row.updated_at.isoformat())


@admin_router.get('')
def listing(status:str=Query('',max_length=16),version:str=Query('',max_length=64),page:int=Query(1,ge=1),db:Session=Depends(get_db)):
    query=db.query(Feedback)
    if status:query=query.filter_by(status=status)
    if version:query=query.filter_by(app_version=version)
    return dict(total=query.count(),items=[summary(row) for row in query.order_by(Feedback.created_at.desc()).offset((page-1)*30).limit(30)])


class Review(BaseModel):
    model_config=ConfigDict(extra='forbid')
    status:str=Field(pattern='^(new|processing|resolved|closed)$')
    admin_note:str=Field(default='',max_length=5000)


@admin_router.patch('/{feedback_id}')
def review(feedback_id:str,value:Review,db:Session=Depends(get_db)):
    row=db.get(Feedback,feedback_id)
    if not row:raise HTTPException(404,'反馈不存在')
    row.status=value.status;row.admin_note=value.admin_note;row.updated_at=beijing_now();db.commit()
    return summary(row)


@admin_router.get('/{feedback_id}/logs')
def download(feedback_id:str,db:Session=Depends(get_db)):
    row=db.get(Feedback,feedback_id)
    if not row or not row.archive_name or not re.fullmatch(r'[a-f0-9]{32}\.zip',row.archive_name):raise HTTPException(404,'日志包不存在')
    folder=Path(settings.FEEDBACK_STORAGE_DIR).resolve();file=folder/row.archive_name
    if file.is_symlink() or not file.is_file() or file.resolve().parent!=folder:raise HTTPException(404,'日志包不存在')
    return FileResponse(file,media_type='application/zip',filename='feedback-'+row.id+'.zip')
