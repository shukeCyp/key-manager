"""Direct per-show subtitle retrieval; legacy derived-index builder retained for rollback."""
import time
from collections import defaultdict
from sqlalchemy import select,func,update
from app import media_db as m
from app.config import settings
from app.subtitles import normalized,terms,window_rows


def search_catalog(db,value):
    """Scan original per-show subtitles once for every sentence in the batch."""
    import re,math
    from app.media_direct import contexts_for,language_matches,match_windows
    started=time.monotonic();work_ids=list(dict.fromkeys(value.work_ids))
    if any(not re.fullmatch('[a-f0-9]{32}',w) for w in work_ids):raise ValueError('影视编号无效')
    if db.scalar(select(func.count()).select_from(m.Work).where(m.Work.id.in_(work_ids)))!=len(work_ids):
        raise ValueError('影视不存在')
    contexts=contexts_for(db,work_ids);coverage=defaultdict(set)
    for items in contexts.values():
        for item in items:coverage[item['language']].add(item['episode'])
    total_episodes=len(set(db.scalars(select(m.Episode.code).where(m.Episode.work_id.in_(work_ids),m.Episode.code!='UNKNOWN'))))
    common=dict(total_episodes=total_episodes,language_coverage=[dict(language=k,episodes=len(v)) for k,v in sorted(coverage.items())],
        index_total=len(contexts),index_done=len(contexts),index_ready=True,index_progress=[],search_mode='direct')
    evidence=defaultdict(dict);skipped=[];seen=set();queries=[]
    languages=set(coverage)
    for sentence in value.sentences:
        needle=normalized(sentence.text);key=(sentence.language,needle)
        if key in seen:continue
        seen.add(key)
        if len(needle)<8 or len(terms(sentence.text))<2:
            skipped.append(dict(id=sentence.id,reason='too_generic'));continue
        if not any(language_matches(sentence.language,lang) for lang in languages):
            skipped.append(dict(id=sentence.id,reason='language_missing'));continue
        queries.append(dict(id=sentence.id,text=sentence.text,language=sentence.language,narrator=sentence.narrator))
    per_query=defaultdict(dict);stats=dict(scanned_cues=0)
    for i,match,locations in match_windows(db,contexts,queries,stats) if queries else ():
        sentence=queries[i]
        for location in locations:
            code=location['episode'];previous=per_query[i].get(code)
            hit=dict(location,**match,query_id=sentence['id'],query=sentence['text'],narrator=sentence['narrator'])
            if not previous or (hit['similarity'],-(hit['end_ms']-hit['start_ms']))>(previous['similarity'],-(previous['end_ms']-previous['start_ms'])):
                per_query[i][code]=hit
    for i,hits in per_query.items():
        for code,hit in hits.items():
            hit['matched_episodes']=len(hits)
            previous=evidence[code].get(queries[i]['id'])
            if not previous or hit['similarity']>previous['similarity']:evidence[code][queries[i]['id']]=hit
    ranked=[]
    for code,items in evidence.items():
        hits=list(items.values())
        score=sum(h['similarity']*(.25 if h['narrator'] else 1)/max(1,math.log2(1+h['matched_episodes'])) for h in hits)
        original_hits=sum(not h['narrator'] and h['matched_episodes']<=3 for h in hits)
        ranked.append(dict(episode=code,hits=len(hits),original_hits=original_hits,score=round(score,4),evidence=hits))
    ranked.sort(key=lambda r:(-r['score'],-r['hits'],r['episode']))
    reliable=bool(ranked and ranked[0]['original_hits']>=3 and (len(ranked)==1 or ranked[0]['score']>ranked[1]['score']))
    return dict(common,ranked=ranked,skipped=skipped,reliable=reliable,**stats,
        needs_visual_fallback=not reliable,queried_sentences=len(queries),elapsed_ms=round((time.monotonic()-started)*1000))


def owners_for_work(db,work_id,language=None):
    scope=select(m.EpisodeFile.file_id).join(m.Episode,m.Episode.id==m.EpisodeFile.episode_id).where(m.Episode.work_id==work_id)
    query=select(m.Track).where(m.Track.file_id.in_(scope),m.Track.kind=='subtitle',m.Track.parse_status=='ready')
    if language:
        from app.media_catalog import language_filter
        query=query.where(language_filter(m.Track.language,language))
    return list(db.scalars(query))


def phrase(db,work_id,query,language=None,limit=30):
    from app.media_direct import contexts_for,match_windows
    if not db.get(m.Work,work_id):raise ValueError('影视不存在')
    if len(normalized(query))<2:raise ValueError('请输入至少两个字符的字幕')
    contexts=contexts_for(db,[work_id],language)
    stats=dict(scanned_cues=0);result=[]
    matches=match_windows(db,contexts,[dict(text=query,language=language)],stats,phrase=True)
    try:
        for _,hit,locations in matches:
            for location in locations:
                result.append(dict(location,start_ms=hit['start_ms'],end_ms=hit['end_ms'],
                    text=hit['text'],pairing='episode_candidate'))
                if len(result)>=limit:return dict(index_ready=True,items=result,index_progress=[],search_mode='direct',**stats)
    finally:matches.close()
    return dict(index_ready=True,items=result,index_progress=[],search_mode='direct',**stats)


def index_one(engine):
    from sqlalchemy.orm import Session
    with Session(engine) as db:
        track=db.scalar(select(m.Track).where(m.Track.index_state.in_(['pending','building']),
            m.Track.content_ref_id.is_(None)).order_by(m.Track.id).limit(1))
        if not track:return False
        track.index_state='building';db.commit();tid=track.id
        try:
            from shutil import disk_usage
            if engine.dialect.name=='mysql' and disk_usage('/').free<10*1024**3:raise ValueError('服务器剩余空间不足10GiB')
            cues=[dict(start_ms=c.start_ms,end_ms=c.end_ms,text=c.text) for c in db.scalars(
                select(m.SubtitleCue).where(m.SubtitleCue.track_id==tid).order_by(m.SubtitleCue.ordinal))]
            # Existing helper constructs up to three original cues per search window.
            from types import SimpleNamespace
            windows=window_rows(tid,'','',[SimpleNamespace(**c) for c in cues])
            estimate=sum(len(r['text'].encode())+len(r['document'].encode())+256 for r in windows)*2
            if estimate>settings.SUBTITLE_CACHE_BYTES:raise ValueError('单个字幕超过缓存预算')
            used=int(db.scalar(select(func.coalesce(func.sum(m.Track.index_bytes),0))))
            for victim in db.scalars(select(m.Track).where(m.Track.index_state=='ready',m.Track.id!=tid).order_by(m.Track.last_used)):
                if used+estimate<=settings.SUBTITLE_CACHE_BYTES:break
                if victim.last_used>=int(time.time())-120:continue
                db.execute(update(m.SubtitleCue).where(m.SubtitleCue.track_id==victim.id).values(
                    search_text=None,search_document=None,search_end_ms=None))
                used-=victim.index_bytes;victim.index_bytes=0;victim.index_state='none'
            if used+estimate>settings.SUBTITLE_CACHE_BYTES:raise ValueError('字幕缓存繁忙，请稍后重试')
            if engine.dialect.name=='mysql':
                from sqlalchemy.dialects.mysql import insert
                # Populate derived columns in bounded batches. Raw cue text and
                # timestamps are never overwritten on the existing primary key.
                values=[dict(track_id=tid,ordinal=w['ordinal'],**original,search_text=w['text'],
                    search_document=w['document'],search_end_ms=w['end_ms']) for w,original in zip(windows,cues)]
                for offset in range(0,len(values),100):
                    statement=insert(m.SubtitleCue).values(values[offset:offset+100])
                    db.execute(statement.on_duplicate_key_update(search_text=statement.inserted.search_text,
                        search_document=statement.inserted.search_document,search_end_ms=statement.inserted.search_end_ms))
            else:
                for row in windows:
                    db.execute(update(m.SubtitleCue).where(m.SubtitleCue.track_id==tid,m.SubtitleCue.ordinal==row['ordinal']).values(
                        search_text=row['text'],search_document=row['document'],search_end_ms=row['end_ms']))
            track.index_state='ready';track.index_bytes=estimate;track.error=None;db.commit()
        except Exception as exc:
            db.rollback();track=db.get(m.Track,tid);track.index_state='failed';track.error=str(exc)[:1000];db.commit()
        return True


def legacy_search(db,value):
    """Old API contract, same one-pass read-only matcher as the current client."""
    from types import SimpleNamespace
    result=search_catalog(db,SimpleNamespace(work_ids=[value.series_id],sentences=value.sentences))
    for row in result['ranked']:
        for hit in row['evidence']:hit['id']=hit['query_id']
    result['coverage_complete']=bool(result['language_coverage'] and all(
        row['episodes']==result['total_episodes'] for row in result['language_coverage']))
    return result
