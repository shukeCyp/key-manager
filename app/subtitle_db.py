"""Independent subtitle store; licensing remains available if this DB is offline."""
from functools import lru_cache
from sqlalchemy import create_engine, Column, String, Integer, BigInteger, Text, Boolean, ForeignKey, Index, LargeBinary
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.dialects.mysql import MEDIUMBLOB
from app.config import settings


class Base(DeclarativeBase):
    pass


class Series(Base):
    __tablename__ = 'subtitle_series'
    id = Column(String(32), primary_key=True)
    title = Column(String(255), nullable=False)
    year = Column(Integer)


class Alias(Base):
    __tablename__ = 'subtitle_aliases'
    normalized = Column(String(255), primary_key=True)
    series_id = Column(String(32), ForeignKey('subtitle_series.id'), primary_key=True)


class Episode(Base):
    __tablename__ = 'subtitle_episodes'
    id = Column(String(32), primary_key=True)
    series_id = Column(String(32), ForeignKey('subtitle_series.id'), nullable=False, index=True)
    code = Column(String(32), nullable=False)
    __table_args__ = (Index('uq_subtitle_episode', 'series_id', 'code', unique=True),)


class Track(Base):
    __tablename__ = 'subtitle_tracks'
    id = Column(String(32), primary_key=True)
    series_id = Column(String(32), ForeignKey('subtitle_series.id'), nullable=False)
    episode_id = Column(String(32), ForeignKey('subtitle_episodes.id'), nullable=False)
    language = Column(String(16), nullable=False)
    language_source = Column(String(32), nullable=False)
    content_hash = Column(String(64), nullable=False)
    cue_count = Column(Integer, nullable=False)
    cues_gzip = Column(LargeBinary().with_variant(MEDIUMBLOB(), 'mysql'), nullable=False)
    indexed_version = Column(Integer, nullable=False, default=0)
    index_bytes = Column(BigInteger, nullable=False, default=0)
    last_used = Column(BigInteger, nullable=False, default=0)
    __table_args__ = (Index('ix_subtitle_scope', 'series_id', 'language', 'episode_id'),
                      Index('uq_subtitle_content', 'episode_id', 'language', 'content_hash', unique=True))


class Source(Base):
    __tablename__ = 'subtitle_sources'
    id = Column(String(64), primary_key=True)
    library_id = Column(String(64), nullable=False, index=True)
    external_id = Column(String(64), nullable=False)
    track_id = Column(String(32), ForeignKey('subtitle_tracks.id'), nullable=False, index=True)
    root = Column(String(1024), nullable=False)
    relative_path = Column(Text, nullable=False)
    video_id = Column(String(64))
    video_relative_path = Column(Text)
    pairing = Column(String(64), nullable=False)
    source_digest = Column(String(64), nullable=False)
    revision = Column(String(128), nullable=False)
    active = Column(Boolean, nullable=False, default=True)


class Window(Base):
    __tablename__ = 'subtitle_windows'
    id = Column(BigInteger().with_variant(Integer, 'sqlite'), primary_key=True, autoincrement=True)
    series_id = Column(String(32), nullable=False)
    track_id = Column(String(32), ForeignKey('subtitle_tracks.id'), nullable=False)
    ordinal = Column(Integer, nullable=False)
    start_ms = Column(Integer, nullable=False)
    end_ms = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    document = Column(Text, nullable=False)
    __table_args__ = (Index('uq_subtitle_window', 'track_id', 'ordinal', unique=True),
                      Index('ix_subtitle_window_series', 'series_id'),
                      Index('ft_subtitle_document', 'document', mysql_prefix='FULLTEXT'))


class ImportRun(Base):
    __tablename__ = 'subtitle_imports'
    id = Column(String(64), primary_key=True)
    status = Column(String(16), nullable=False)
    processed = Column(Integer, nullable=False, default=0)
    failed = Column(Integer, nullable=False, default=0)
    message = Column(Text, nullable=False, default='')


class ReviewFile(Base):
    __tablename__ = 'subtitle_review_files'
    id = Column(String(32), primary_key=True)
    library_id = Column(String(64), nullable=False,index=True)
    external_id = Column(String(64), nullable=False)
    relative_path = Column(Text, nullable=False)
    reason = Column(String(255), nullable=False)
    payload_gzip = Column(LargeBinary().with_variant(MEDIUMBLOB(), 'mysql'), nullable=False)


class IndexJob(Base):
    __tablename__ = 'subtitle_index_jobs'
    id = Column(String(32), primary_key=True)
    series_id = Column(String(32), nullable=False, index=True)
    language = Column(String(16), nullable=False)
    status = Column(String(16), nullable=False,index=True)
    done = Column(Integer, nullable=False, default=0)
    total = Column(Integer, nullable=False, default=0)
    updated_at = Column(BigInteger, nullable=False, default=0)
    message = Column(String(255), nullable=False, default='')


@lru_cache(maxsize=2)
def engine_for(url):
    kwargs = dict(pool_pre_ping=True)
    if not url.startswith('sqlite'):
        kwargs.update(pool_size=4, max_overflow=2, pool_timeout=5,
                      connect_args=dict(connect_timeout=5, read_timeout=30, write_timeout=30))
    return create_engine(url, **kwargs)


def engine():
    if not settings.SUBTITLE_DATABASE_URL:
        raise RuntimeError('字幕数据库尚未配置')
    return engine_for(settings.SUBTITLE_DATABASE_URL)
