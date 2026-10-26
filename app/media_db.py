"""The seven business tables for video/audio/subtitle resource queries."""
from sqlalchemy import Column,String,Integer,BigInteger,Text,Float,Boolean,ForeignKey,Index,CheckConstraint
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from app.subtitle_db import engine

LONG_TEXT=Text().with_variant(MEDIUMTEXT(),'mysql')


class Base(DeclarativeBase):pass


class Work(Base):
    __tablename__='works'
    id=Column(String(32),primary_key=True)
    identity_key=Column(String(64),nullable=False,unique=True)
    title=Column(String(255),nullable=False)
    title_zh=Column(String(255))
    title_original=Column(String(255))
    kind=Column(String(16),nullable=False,default='unknown')
    year=Column(Integer)
    status=Column(String(24),nullable=False,default='needs_review')
    confidence=Column(Float,nullable=False,default=0)
    details=Column(LONG_TEXT,nullable=False,default='{}')


class WorkName(Base):
    __tablename__='work_names'
    id=Column(String(32),primary_key=True)
    work_id=Column(String(32),ForeignKey('works.id'),nullable=False,index=True)
    name=Column(String(255),nullable=False)
    normalized=Column(String(255),nullable=False,index=True)
    language=Column(String(32),nullable=False,default='und')
    kind=Column(String(16),nullable=False,default='alias')
    __table_args__=(Index('uq_work_name','work_id','normalized','language',unique=True),)


class Episode(Base):
    __tablename__='episodes'
    id=Column(String(32),primary_key=True)
    work_id=Column(String(32),ForeignKey('works.id'),nullable=False,index=True)
    code=Column(String(40),nullable=False)
    season=Column(Integer)
    number=Column(Integer)
    title=Column(String(255))
    __table_args__=(Index('uq_media_episode','work_id','code',unique=True),)


class File(Base):
    __tablename__='files'
    id=Column(String(64),primary_key=True)
    path_hash=Column(String(64),nullable=False,unique=True)
    path=Column(Text,nullable=False)
    kind=Column(String(16),nullable=False,index=True)
    format=Column(String(32),nullable=False,default='')
    size=Column(BigInteger)
    mtime_ns=Column(BigInteger)
    duration_ms=Column(BigInteger)
    availability=Column(String(24),nullable=False,default='present')
    probe_status=Column(String(24),nullable=False,default='pending')
    error=Column(Text)
    details=Column(LONG_TEXT,nullable=False,default='{}')


class EpisodeFile(Base):
    __tablename__='episode_files'
    episode_id=Column(String(32),ForeignKey('episodes.id'),primary_key=True)
    file_id=Column(String(64),ForeignKey('files.id'),primary_key=True,index=True)
    match_status=Column(String(24),nullable=False,default='model_candidate')
    confidence=Column(Float,nullable=False,default=0)
    evidence=Column(LONG_TEXT,nullable=False,default='[]')


class Track(Base):
    __tablename__='tracks'
    id=Column(String(32),primary_key=True)
    file_id=Column(String(64),ForeignKey('files.id'),nullable=False,index=True)
    kind=Column(String(16),nullable=False)
    source=Column(String(16),nullable=False)
    stream_key=Column(String(32),nullable=False)
    stream_index=Column(Integer)
    language=Column(String(32),nullable=False,default='und')
    raw_language=Column(String(100))
    language_source=Column(String(32),nullable=False,default='unknown')
    title=Column(String(255))
    codec=Column(String(64))
    channels=Column(Integer)
    is_default=Column(Boolean,nullable=False,default=False)
    is_forced=Column(Boolean,nullable=False,default=False)
    bound_video_id=Column(String(64),ForeignKey('files.id'))
    binding_status=Column(String(24),nullable=False,default='unknown')
    content_hash=Column(String(64),index=True)
    content_ref_id=Column(String(32),ForeignKey('tracks.id'),index=True)
    cue_count=Column(Integer,nullable=False,default=0)
    parse_status=Column(String(24),nullable=False,default='pending')
    index_state=Column(String(24),nullable=False,default='none',index=True)
    index_bytes=Column(BigInteger,nullable=False,default=0)
    last_used=Column(BigInteger,nullable=False,default=0)
    error=Column(Text)
    __table_args__=(Index('uq_media_track','file_id','stream_key',unique=True),
                   Index('ix_media_language','kind','language','file_id'))


class SubtitleCue(Base):
    __tablename__='subtitle_cues'
    track_id=Column(String(32),ForeignKey('tracks.id'),primary_key=True)
    ordinal=Column(Integer,primary_key=True)
    start_ms=Column(Integer,nullable=False)
    end_ms=Column(Integer,nullable=False)
    text=Column(Text,nullable=False)
    search_text=Column(LONG_TEXT)
    search_end_ms=Column(Integer)
    search_document=Column(LONG_TEXT)
    __table_args__=(CheckConstraint('start_ms>=0 AND end_ms>start_ms',name='ck_media_cue_time'),
                   Index('ft_media_document','search_document',mysql_prefix='FULLTEXT'))


TABLES=tuple(Base.metadata.tables)
