"""Conservative media identity inventory; ambiguous paths remain review items."""
import argparse,json,re,sqlite3
from collections import Counter
from pathlib import Path,PurePosixPath
from tools.export_shared_subtitles import episode
from app.subtitles import normalized

GENERIC={'电影','电视剧','字幕','srt字幕','subs','subtitles','srt','tv','movies','movie','series','videos','video'}
SEASON=re.compile(r'(?i)(?:第\s*[\d一二三四五六七八九十]+\s*季|\bseason\s*\d+|^s\d{1,3}$)')
LANG_NOTE=re.compile(r'[（(][^()（）]*(?:德语|法语|英语|日语|西班牙语|葡语|意大利语|多语|英音|国语|中字|双语)[^()（）]*[）)]')


def clean(value):
    value=LANG_NOTE.sub('',value)
    value=re.sub(r'[（(][德英西法日葡俄意中韩阿语、，, /]+[）)]','',value)
    value=re.sub(r'（[德英西法日葡俄意中韩阿语、，, /]+$', '',value)
    value=re.sub(r'[（(][\d一二三四五六七八九十、，, /-]+季[）)]','',value)
    value=re.sub(r'\[[^\]]+\]','',value)
    value=re.sub(r'[（(](?:19|20)\d{2}[）)]','',value)
    value=re.sub(r'(?i)\b(?:480p|720p|1080p|2160p|4k|webrip|web-dl|bluray|hdtv|x264|x265|h264|h265)\b.*','',value)
    return re.sub(r'[._]+',' ',value).strip(' -_，,')


def infer(relative,video_relative=None):
    path=PurePosixPath((video_relative or relative).replace('\\','/'));parts=list(path.parts)
    ep=episode(str(path)) or episode(relative)
    seasons=[i for i,p in enumerate(parts[:-1]) if SEASON.search(p)]
    filename=path.stem
    prefix=re.split(r'(?i)(?:\b|_)[sS]\d{1,3}[ ._-]*[eE]\d{1,4}|第\s*\d+\s*季',filename)[0].strip(' ._-')
    if seasons:
        pos=seasons[0]
        embedded=SEASON.split(parts[pos])[0].strip(' ._-')
        title=embedded if len(embedded)>=2 else (parts[pos-1] if pos else '')
    elif ep:
        title=prefix
    else:
        candidates=[p for p in parts[:-1] if normalized(p) not in GENERIC and not re.fullmatch(r'[A-Za-z0-9]',p)]
        title=candidates[-1] if candidates else filename
    if seasons and not ep:return dict(review_reason='season_without_episode')
    year_match=re.search(r'[（(]((?:19|20)\d{2})[）)]',title)
    if not year_match and not ep:year_match=re.search(r'[（(]((?:19|20)\d{2})[）)]',filename)
    year=int(year_match[1]) if year_match else None
    title=clean(title);prefix=clean(prefix)
    if len(normalized(title))<2 or normalized(title) in GENERIC or title.isdigit():return dict(review_reason='title_unknown')
    if not ep and not video_relative:return dict(review_reason='unpaired_movie_identity')
    aliases=list(dict.fromkeys(a for a in [title,prefix] if len(normalized(a))>=2 and normalized(a) not in GENERIC)) if ep else [title]
    # Existing verified pilot identity; avoid a second Chinese-name copy of this show.
    if any(normalized(a) in {'菜鸟老警','the rookie','新紮師兄','新扎师兄'} for a in aliases):
        return dict(title='The Rookie',aliases=list(dict.fromkeys(['菜鸟老警',*aliases])),year=2018,episode=ep or 'MOVIE')
    return dict(title=title,aliases=aliases,year=year,episode=ep or 'MOVIE')


def main():
    p=argparse.ArgumentParser();p.add_argument('--catalog',required=True,type=Path);p.add_argument('--output',required=True,type=Path);a=p.parse_args()
    with sqlite3.connect(a.catalog.absolute().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        videos={r['id']:dict(r) for r in db.execute('SELECT * FROM videos WHERE seen=1')}
        # This is a path-only preliminary plan. The exporter uses each shard's existing video_id mapping.
        rows=[]
        for r in db.execute('SELECT * FROM subtitle_files WHERE seen=1'):
            rows.append(dict(source_id=r['id'],root_id=r['root_id'],relative_path=r['relative_path'],**infer(r['relative_path'])))
    counts=Counter('review' if 'review_reason'in r else ('movie' if r['episode']=='MOVIE' else 'series') for r in rows)
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(dict(counts=dict(counts),files=rows),ensure_ascii=False,indent=2),'utf8')
    print(dict(counts));print('Potential titles:',len({r['title'] for r in rows if 'title'in r}))


if __name__=='__main__':main()
