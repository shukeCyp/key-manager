"""Read-only, bounded-memory matching of a show's original subtitle cues.

Every request reads its selected canonical tracks once. No persistent search
windows, hashed documents, cache jobs or cache readiness are required.
"""
from collections import Counter, defaultdict, deque
from difflib import SequenceMatcher
from itertools import groupby

from sqlalchemy import select

from app import media_db as m
from app.subtitles import CJK, normalized, terms


def language_matches(target, language):
    return not target or language == target or ('-' not in target and language.startswith(target + '-'))


def contexts_for(db, work_ids, language=None):
    contexts = defaultdict(list)
    rows = db.execute(select(m.Track.id, m.Track.content_ref_id, m.Track.language,
        m.File.id, m.File.path, m.Episode.id, m.Episode.code, m.Episode.work_id)
        .join(m.File, m.File.id == m.Track.file_id)
        .join(m.EpisodeFile, m.EpisodeFile.file_id == m.File.id)
        .join(m.Episode, m.Episode.id == m.EpisodeFile.episode_id)
        .where(m.Episode.work_id.in_(work_ids), m.Track.kind == 'subtitle',
            m.Track.parse_status == 'ready', m.File.availability == 'present', m.Episode.code != 'UNKNOWN'))
    for tid, owner, lang, fid, path, eid, code, wid in rows:
        if language_matches(language, lang):
            contexts[owner or tid].append(dict(work_id=wid, episode_id=eid, episode=code,
                track_id=tid, file_id=fid, path=path, language=lang))
    return contexts


def normalized_terms(value, wanted, cjk):
    """Keep only terms used by this request; no full-show vocabulary is built."""
    result = set()
    for word in value.split():
        if CJK.search(word):
            if cjk:
                for run in CJK.findall(word):
                    result.update(pair for i in range(len(run)-1) if (pair := run[i:i+2]) in wanted)
        elif word in wanted:
            result.add(word)
    return result


def raw_windows(db, owners, stats, wanted):
    """Raw columns only, streamed by track; sort at most one subtitle file."""
    owners = sorted(owners)
    cjk = any(CJK.search(term) for term in wanted)
    for offset in range(0, len(owners), 64):
        statement = select(m.SubtitleCue.track_id, m.SubtitleCue.ordinal,
            m.SubtitleCue.start_ms, m.SubtitleCue.end_ms, m.SubtitleCue.text).where(
            m.SubtitleCue.track_id.in_(owners[offset:offset+64])).order_by(
            m.SubtitleCue.track_id, m.SubtitleCue.ordinal)
        result = db.execute(statement.execution_options(yield_per=1000))
        try:
            for owner, rows in groupby(result, key=lambda row: row.track_id):
                pending = deque()
                # Import preserves original ordinals, including out-of-order SRTs.
                ordered = sorted(rows, key=lambda row: (row.start_ms, row.end_ms, row.ordinal))
                def prepared():
                    for row in ordered:
                        stats['scanned_cues'] += 1
                        body = normalized(row.text)
                        yield (row.start_ms, row.end_ms, row.text, body, normalized_terms(body, wanted, cjk))
                source = iter(prepared())
                pending.extend(next(source, None) for _ in range(3))
                pending = deque(row for row in pending if row is not None)
                while pending:
                    window = [pending[0]]
                    end = pending[0][1]
                    for row in list(pending)[1:]:
                        if row[0] - end > 3000:
                            break
                        window.append(row)
                        end = max(end, row[1])
                    yield owner, window
                    pending.popleft()
                    following = next(source, None)
                    if following is not None:
                        pending.append(following)
        finally:
            result.close()


def match_windows(db, contexts, queries, stats, *, phrase=False):
    """Route overlapping terms to queries, then verify all candidate text windows."""
    inverted = defaultdict(set)
    unfiltered = set()
    for i, query in enumerate(queries):
        query['needle'] = normalized(query['text'])
        query['terms'] = terms(query['text'])
        for term in query['terms']:
            inverted[term].add(i)
        if phrase and (len(query['needle']) < 10 or len(query['terms']) < 3):
            unfiltered.add(i)
    eligible = {owner: {i: [c for c in items if language_matches(q.get('language'), c['language'])]
        for i, q in enumerate(queries)} for owner, items in contexts.items()}
    eligible = {owner: {i: items for i, items in by_query.items() if items}
        for owner, by_query in eligible.items()}
    eligible = {owner: items for owner, items in eligible.items() if items}
    windows = raw_windows(db, eligible, stats, inverted)
    try:
        for owner, window in windows:
            all_terms = set().union(*(row[4] for row in window))
            if not all_terms and not unfiltered:
                continue
            counts = Counter(i for term in all_terms for i in inverted.get(term, ()))
            candidates = {i for i, count in counts.items() if count >= .6 * len(queries[i]['terms'])}
            for i in (candidates | unfiltered) & eligible[owner].keys():
                query = queries[i]
                best = None
                body_terms = set()
                end = window[0][1]
                for size, row in enumerate(window, 1):
                    body_terms.update(row[4]); end = max(end, row[1])
                    body = ' '.join(r[3] for r in window[:size]).strip()
                    if query['needle'] in body:
                        score = 1.
                    elif phrase:
                        score = SequenceMatcher(None, query['needle'], body).ratio()
                    elif len(query['needle']) >= 10 and len(query['terms']) >= 3:
                        coverage = len(query['terms'] & body_terms) / len(query['terms'])
                        if coverage < .6:
                            continue
                        a, b = (query['needle'], body) if CJK.search(query['needle']) else (query['needle'].split(), body.split())
                        score = .8 * coverage + .2 * SequenceMatcher(None, a, b, autojunk=False).ratio()
                    else:
                        continue
                    if score < (.55 if phrase else .65):
                        continue
                    if best is None or score > best['similarity']:
                        best = dict(text='\n'.join(r[2] for r in window[:size]),
                            start_ms=window[0][0], end_ms=end, similarity=round(score, 4),
                            round=1 if score == 1 else 2)
                    if score == 1:
                        break
                if best:
                    yield i, best, eligible[owner][i]
    finally:
        windows.close()
