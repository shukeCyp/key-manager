"""Content deduplication and show/language scoped, batch subtitle retrieval."""
import gzip
import hashlib
import html
import json
import math
import re
import time
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, func, insert, text, exists,delete
from sqlalchemy.orm import Session
from app.subtitle_db import Series, Alias, Episode, Track, Source, Window, ReviewFile


def normalized(value):
    value = html.unescape(re.sub(r'<[^>]+>', ' ', value))
    value = ''.join(c for c in unicodedata.normalize('NFKD', value.casefold()) if not unicodedata.combining(c))
    return ' '.join(re.findall(r'[^\W_]+', value, flags=re.UNICODE))


def ident(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:32]


STOP = set('the and that this with from have has had were was are for not but you your our they their them she him her its into onto will can could would should just get got come here there yes okay right dont didnt est une des les dans pour pas vous nous elle elles ils sont suis sur avec que qui oui non der die das ein eine und ist sind ich du wir sie nicht mit von zu auf los las una uno del por para con que como pero usted ellos ellas soy eres'.split())
CJK = re.compile(r'[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+')


def terms(value):
    value = normalized(value)
    result = set()
    for word in value.split():
        if CJK.search(word):
            for run in CJK.findall(word):
                result.update(run[i:i+2] for i in range(len(run)-1))
        elif len(word) >= 2 and word not in STOP:
            result.add(word)
    return result


INDEX_VERSION=2


def term_token(value,series_id='',language=''):
    return 't' + hashlib.sha256((series_id+'\0'+language+'\0'+value).encode()).hexdigest()[:20]


def scope_tokens(series_id, language):
    return 's' + series_id, 'l' + hashlib.sha256(language.encode()).hexdigest()[:12]


class Cue(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start_ms: int = Field(ge=0, le=86400000)
    end_ms: int = Field(gt=0, le=86400000)
    text: str = Field(min_length=1, max_length=4000)

    @model_validator(mode='after')
    def duration(self):
        if self.end_ms <= self.start_ms: raise ValueError('字幕结束时间必须晚于开始时间')
        return self


class ImportFile(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    library_id: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=64)
    revision: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=255)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    year: int | None = Field(default=None, ge=1880, le=2200)
    episode: str = Field(pattern=r'^(S\d{2,3}E\d{2,4}|MOVIE)$')
    language: str = Field(pattern=r'^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$')
    language_source: str = Field(default='filename', pattern=r'^(filename|script|manual|unknown)$')
    root: str = Field(min_length=1, max_length=1024)
    relative_path: str = Field(min_length=1, max_length=4096)
    video_id: str | None = Field(default=None, max_length=64)
    video_relative_path: str | None = Field(default=None, max_length=4096)
    pairing: str = Field(max_length=64)
    source_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    cues: list[Cue] = Field(min_length=1, max_length=10000)

    @model_validator(mode='after')
    def valid_aliases(self):
        if not normalized(self.title) or any(not normalized(a) or len(a)>255 for a in self.aliases):
            raise ValueError('剧名或别名无效')
        return self


def window_rows(track_id,series_id,language,cues):
    ordered=sorted(cues,key=lambda c:(c.start_ms,c.end_ms));rows=[]
    for i,cue in enumerate(ordered):
        parts=[cue.text];end=cue.end_ms
        for following in ordered[i+1:i+3]:
            if following.start_ms-end>3000:break
            parts.append(following.text);end=max(end,following.end_ms)
        original='\n'.join(parts)
        document=' '.join(sorted(term_token(t,series_id,language) for t in terms(original)))
        rows.append(dict(series_id=series_id,track_id=track_id,ordinal=i+1,start_ms=cue.start_ms,end_ms=end,text=original,document=document))
    return rows


def build_windows(db,track,cues):
    rows=window_rows(track.id,track.series_id,track.language,cues)
    db.execute(delete(Window).where(Window.track_id==track.id))
    for begin in range(0,len(rows),250):db.execute(insert(Window),rows[begin:begin+250])
    track.indexed_version=INDEX_VERSION
    # Conservative budget includes table rows, B-tree and full-text posting overhead.
    track.index_bytes=sum(len(r['text'].encode())+len(r['document'].encode())+256 for r in rows)*2
    track.last_used=int(time.time())


def import_file(db, data, build_index=True):
    """One transaction per source: rollback leaves its previous version readable."""
    series_id = ident(normalized(data.title), data.year)
    series = db.get(Series, series_id)
    if not series:
        db.add(Series(id=series_id, title=data.title, year=data.year)); db.flush()
    for alias in {normalized(a) for a in [data.title, *data.aliases]}:
        if not db.get(Alias, (alias, series_id)):
            db.add(Alias(normalized=alias, series_id=series_id))
    episode_id = ident(series_id, data.episode)
    if not db.get(Episode, episode_id):
        db.add(Episode(id=episode_id, series_id=series_id, code=data.episode)); db.flush()
    canonical = json.dumps([c.model_dump() for c in data.cues], ensure_ascii=False, separators=(',', ':')).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    track_id = ident(episode_id, data.language, digest)
    reused = db.get(Track, track_id) is not None
    if not reused:
        db.add(Track(id=track_id, series_id=series_id, episode_id=episode_id,
                     language=data.language, language_source=data.language_source,
                     content_hash=digest, cue_count=len(data.cues), cues_gzip=gzip.compress(canonical, mtime=0)))
        db.flush()
    track=db.get(Track,track_id)
    if build_index and track.indexed_version!=INDEX_VERSION:build_windows(db,track,data.cues)
    source_id = ident(data.library_id, data.source_id)
    row = db.get(Source, source_id)
    values=dict(library_id=data.library_id, external_id=data.source_id, track_id=track_id,
                root=data.root, relative_path=data.relative_path, video_id=data.video_id,
                video_relative_path=data.video_relative_path, pairing=data.pairing,
                source_digest=data.source_digest, revision=data.revision, active=True)
    if row:
        for key,value in values.items(): setattr(row,key,value)
    else: db.add(Source(id=source_id, **values))
    review=db.get(ReviewFile,source_id)
    if review:db.delete(review)
    db.flush()
    return dict(series_id=series_id, track_id=track_id, reused=reused, cues=len(data.cues))


def import_review(db,data):
    """Keep unresolved/invalid source records for review without fabricating episode votes."""
    for key in ['library_id','source_id','relative_path','review_reason']:
        if not isinstance(data.get(key),str) or not data[key]:raise ValueError('Invalid review metadata')
    if len(data['library_id'])>64 or len(data['source_id'])>64 or len(data['review_reason'])>255:raise ValueError('Invalid review metadata')
    key=ident(data['library_id'],data['source_id'])
    payload=gzip.compress(json.dumps(data,ensure_ascii=False,separators=(',',':')).encode(),mtime=0)
    if len(payload)>8*1024*1024:raise ValueError('Review payload too large')
    row=db.get(ReviewFile,key)
    if not row:row=ReviewFile(id=key);db.add(row)
    row.library_id=data['library_id'];row.external_id=data['source_id'];row.relative_path=data['relative_path'];row.reason=data['review_reason'];row.payload_gzip=payload
    old=db.get(Source,key)
    if old:old.active=False
    db.flush()
    return dict(review=True,reused=False,cues=len(data.get('cues',[])))


class Sentence(BaseModel):
    model_config=ConfigDict(extra='forbid', str_strip_whitespace=True)
    id: str = Field(max_length=64)
    text: str = Field(min_length=1, max_length=500)
    narrator: bool = False
    # One original sentence can supply translated variants under the same id.
    language: str = Field(pattern=r'^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$')


class Search(BaseModel):
    model_config=ConfigDict(extra='forbid')
    series_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    sentences: list[Sentence] = Field(min_length=1,max_length=200)


def similarity(query, target):
    q, t = normalized(query), normalized(target)
    qt, tt = terms(q), terms(t)
    if len(q)<10 or len(qt)<3: return 0.
    if q in t: return 1.
    coverage=len(qt & tt)/len(qt)
    if coverage<.6: return 0.
    order=SequenceMatcher(None,q.split(),t.split(),autojunk=False).ratio()
    if CJK.search(q): order=SequenceMatcher(None,q,t,autojunk=False).ratio()
    return .8*coverage+.2*order


def search(db,value):
    from app.subtitle_cache import scope_lock,ensure_cached,pending_response
    from app.subtitle_db import IndexJob
    with scope_lock(db.bind,value.series_id) as acquired:
        if not acquired:
            jobs=db.scalars(select(IndexJob).where(IndexJob.series_id==value.series_id,IndexJob.status=='running')).all()
            if jobs:return pending_response(value.series_id,[dict(language=j.language,status=j.status,done=j.done,total=j.total,message=j.message) for j in jobs])
            raise RuntimeError('本剧正在查询，请稍后重试')
        pending=ensure_cached(db,value)
        if pending:return pending
        return search_ready(db,value)


def search_ready(db, value):
    began=time.perf_counter()
    if not db.get(Series,value.series_id): raise ValueError('未找到此剧')
    active=exists(select(Source.id).where(Source.track_id==Track.id,Source.active.is_(True)))
    coverage=[dict(language=lang,episodes=count,tracks=tracks) for lang,count,tracks in db.execute(
        select(Track.language,func.count(func.distinct(Track.episode_id)),func.count()).where(
            Track.series_id==value.series_id,active).group_by(Track.language))]
    episode_count=db.scalar(select(func.count()).select_from(Episode).where(Episode.series_id==value.series_id))
    languages={r['language'] for r in coverage}
    evidence=defaultdict(dict); skipped=[]; seen=set(); used=0; counts={}
    for sentence in value.sentences:
        key=(sentence.language,normalized(sentence.text))
        if key in seen: continue
        seen.add(key)
        if sentence.language not in languages:
            skipped.append(dict(id=sentence.id,reason='language_missing',language=sentence.language)); continue
        words=terms(sentence.text)
        if len(words)<3 or len(normalized(sentence.text))<10:
            skipped.append(dict(id=sentence.id,reason='too_generic')); continue
        used+=1
        statement=select(Window,Track.episode_id,Episode.code).join(Track,Window.track_id==Track.id).join(Episode,Track.episode_id==Episode.id).where(
            Window.series_id==value.series_id,Track.language==sentence.language,active)
        if db.bind.dialect.name=='mysql':
            # Each word token includes show and language, keeping postings scoped
            # before ranking and limiting the candidate windows.
            expression='+('+' '.join(term_token(w,value.series_id,sentence.language) for w in sorted(words,key=lambda x:(-len(x),x))[:16])+')'
            match=text('MATCH(subtitle_windows.document) AGAINST (:expression IN BOOLEAN MODE)')
            statement=statement.where(match).order_by(text('MATCH(subtitle_windows.document) AGAINST (:expression IN BOOLEAN MODE) DESC')).params(expression=expression).limit(400)
        else:
            # SQLite is a small deterministic test fixture only, not a deployment search backend.
            statement=statement.limit(20000)
        hits={}
        for window,episode_id,code in db.execute(statement):
            score=similarity(sentence.text,window.text)
            if score<.72: continue
            if score>hits.get(code,{}).get('similarity',0):
                hits[code]=dict(query_id=sentence.id,query=sentence.text,track_id=window.track_id,
                               language=sentence.language,start_ms=window.start_ms,end_ms=window.end_ms,
                               text=window.text,similarity=score,narrator=sentence.narrator)
        counts[sentence.id]=max(counts.get(sentence.id,0),len(hits))
        for code,hit in hits.items():
            previous=evidence[code].get(sentence.id)
            if not previous or hit['similarity']>previous['similarity']: evidence[code][sentence.id]=hit
    ranked=[]
    for code,hits in evidence.items():
        rows=list(hits.values())
        # Repeated generic lines across episodes carry little vote weight.
        score=sum(h['similarity']*(.25 if h['narrator'] else 1)/max(1,math.log2(1+counts[h['query_id']])) for h in rows)
        originals=sum(not h['narrator'] and counts[h['query_id']]<=3 for h in rows)
        ranked.append(dict(episode=code,score=round(score,4),hits=len(rows),original_hits=originals,evidence=rows))
    ranked.sort(key=lambda r:(-r['score'],-r['original_hits'],r['episode']))
    reliable=bool(ranked and ranked[0]['original_hits']>=3 and (len(ranked)<2 or ranked[0]['score']>=ranked[1]['score']*1.25))
    requested={s.language for s in value.sentences}
    return dict(series_id=value.series_id,index_ready=True,status='ready',elapsed_ms=round((time.perf_counter()-began)*1000,2),
                queried_sentences=used,skipped=skipped,language_coverage=coverage,total_episodes=episode_count,
                coverage_complete=all(any(r['language']==lang and r['episodes']==episode_count for r in coverage) for lang in requested),
                reliable=reliable,candidate_episodes=[r['episode'] for r in ranked[:5]] if reliable else [],
                needs_visual_fallback=not reliable,ranked=ranked[:20],cross_language='explicit_variants_only')


def list_series(db,query='',limit=30):
    statement=select(Series)
    if query:
        q=normalized(query)
        statement=statement.join(Alias).where(Alias.normalized.contains(q,autoescape=True)).distinct()
    return [dict(id=s.id,title=s.title,year=s.year) for s in db.scalars(statement.order_by(Series.title).limit(limit))]
