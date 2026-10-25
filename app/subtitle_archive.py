"""Batch archival without derived indexes; bounded batches commit atomically."""
import gzip,hashlib,json
from sqlalchemy import select,delete
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from app.subtitle_db import Series,Alias,Episode,Track,Source,ReviewFile
from app.subtitles import ident,normalized


def upsert(db,model,rows,changes=()):
    if not rows:return
    table=model.__table__
    if db.bind.dialect.name=='mysql':
        statement=mysql_insert(table)
        keys=changes or [next(iter(table.primary_key.columns)).name]
        statement=statement.on_duplicate_key_update(**{k:statement.inserted[k] for k in keys})
    else:
        statement=sqlite_insert(table)
        statement=statement.on_conflict_do_update(index_elements=[c.name for c in table.primary_key],set_={k:getattr(statement.excluded,k) for k in changes}) if changes else statement.on_conflict_do_nothing()
    db.execute(statement,rows)


def archive_batch(db,records):
    """All records are validated ImportFile values. Caller owns transaction/progress."""
    series={};aliases={};episodes={};tracks={};sources={}
    for data in records:
        sid=ident(normalized(data.title),data.year);eid=ident(sid,data.episode)
        series[sid]=dict(id=sid,title=data.title,year=data.year)
        for name in [data.title,*data.aliases]:
            norm=normalized(name);aliases[(norm,sid)]=dict(normalized=norm,series_id=sid)
        episodes[eid]=dict(id=eid,series_id=sid,code=data.episode)
        raw=json.dumps([c.model_dump() for c in data.cues],ensure_ascii=False,separators=(',',':')).encode()
        digest=hashlib.sha256(raw).hexdigest();tid=ident(eid,data.language,digest)
        if tid not in tracks:
            tracks[tid]=dict(id=tid,series_id=sid,episode_id=eid,language=data.language,language_source=data.language_source,
                             content_hash=digest,cue_count=len(data.cues),cues_gzip=raw,indexed_version=0,index_bytes=0,last_used=0)
        source_id=ident(data.library_id,data.source_id)
        sources[source_id]=dict(id=source_id,library_id=data.library_id,external_id=data.source_id,track_id=tid,root=data.root,
                               relative_path=data.relative_path,video_id=data.video_id,video_relative_path=data.video_relative_path,
                               pairing=data.pairing,source_digest=data.source_digest,revision=data.revision,active=True)
    existing=set(db.scalars(select(Track.id).where(Track.id.in_(tracks)))) if tracks else set()
    upsert(db,Series,[series[k] for k in sorted(series)]);upsert(db,Alias,[aliases[k] for k in sorted(aliases)]);upsert(db,Episode,[episodes[k] for k in sorted(episodes)])
    new=[]
    for key in sorted(set(tracks)-existing):
        row=tracks[key];row['cues_gzip']=gzip.compress(row['cues_gzip'],compresslevel=4,mtime=0);new.append(row)
    upsert(db,Track,new)
    if sources:
        changes=[key for key in next(iter(sources.values())) if key!='id']
        upsert(db,Source,[sources[k] for k in sorted(sources)],changes)
        db.execute(delete(ReviewFile).where(ReviewFile.id.in_(sources)))
    return len(set(tracks)-existing)
