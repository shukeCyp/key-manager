"""Read existing published SQLite cues without copying/rebuilding the giant FTS shards.

Explicit show scope avoids inventing series/episode identities from arbitrary NAS folders.
An immutable publication and read-only connections leave the shared library unchanged.
"""
import argparse,gzip,hashlib,json,re,sqlite3
from pathlib import Path,PurePosixPath
from app.subtitles import normalized,ImportFile

LANGS={
 'en':['en','eng','english','英语','英文'], 'zh':['zh','zho','chi','chinese','中文','简中','繁中','chs','cht','cn'],
 'de':['de','deu','ger','german','deutsch','德语'], 'fr':['fr','fra','fre','french','français','法语'],
 'es':['es','spa','spanish','español','西班牙语'], 'pt':['pt','por','portuguese','português','葡语'],
 'it':['it','ita','italian','italiano','意大利语'], 'ja':['ja','jpn','japanese','日语','日本語','jp'],
 'ru':['ru','rus','russian','俄语'], 'ko':['ko','kor','korean','韩语'], 'ar':['ar','ara','arabic','阿拉伯语'],
 'tr':['tr','tur','turkish'], 'nl':['nl','nld','dut','dutch'], 'pl':['pl','pol','polish'],
 'sv':['sv','swe','swedish'], 'da':['da','dan','danish'], 'fi':['fi','fin','finnish'],
 'no':['no','nor','norwegian'], 'cs':['cs','ces','cze','czech'], 'hu':['hu','hun','hungarian'],
 'ro':['ro','ron','rum','romanian'], 'he':['he','heb','hebrew'], 'th':['th','tha','thai'],
 'vi':['vi','vie','vietnamese'], 'id':['id','ind','indonesian'], 'hi':['hi','hin','hindi'],
 'uk':['uk','ukr','ukrainian'], 'el':['el','ell','gre','greek'], 'bg':['bg','bul','bulgarian']}


def language(path,cues):
    name=PurePosixPath(path).name.casefold()
    # A translation suffix overrides the source language earlier in the filename.
    translated=re.search(r'[-_.](zh|en|fr|de|ja|es|pt|it|ru)(?:[-_][a-z]{2,4})?[-_]translation\.srt$',name)
    if translated:return translated[1],'filename'
    found=[]
    qualifiers=set('srt subtitles subtitle forced narrative cc sdh closed captions closedcaptions dialog dialogue ut simplified traditional europeo latinoamericano brazil brasil portugal spain canada france us gb ca cn tw br hans hant translation'.split())
    for code,aliases in LANGS.items():
        for alias in aliases:
            for match in re.finditer(r'(?<![a-z0-9])'+re.escape(alias)+r'(?![a-z0-9])',name,re.I):
                tail=set(normalized(name[match.end():]).split())
                if all(t in qualifiers or t.isdigit() for t in tail):found.append((match.end(),code))
    sample=''.join(c['text'] for c in cues[:100])
    if found:return max(found)[1],'filename'
    if re.search(r'[\u3040-\u30ff]',sample):return 'ja','script'
    han=len(re.findall(r'[\u3400-\u9fff]',sample));latin=len(re.findall(r'[a-zA-Z]',sample))
    if han>max(20,latin):return 'zh','script'
    if re.search(r'[\uac00-\ud7af]',sample):return 'ko','script'
    return 'und','unknown'


def episode(path):
    for part in reversed(PurePosixPath(path.replace('\\','/')).parts):
        m=re.search(r'(?i)(?:\b|_)[sS](\d{1,3})[ ._-]*[eE](\d{1,4})(?!\d)',part)
        if not m:m=re.search(r'第\s*(\d{1,3})\s*季.*?第\s*(\d{1,4})\s*集',part)
        if m:return f'S{int(m[1]):02d}E{int(m[2]):02d}'
    season=re.search(r'第\s*(\d{1,3})\s*季',path)
    stem=PurePosixPath(path).stem
    short=re.fullmatch(r'(\d{1,3})[.xX-](\d{1,4})',stem)
    if season and short and int(season[1])==int(short[1]):return f'S{int(short[1]):02d}E{int(short[2]):02d}'
    if season:
        packed=re.fullmatch(r'(\d{2})(\d{2})-(?:CN|JP|EN)',stem,re.I)
        if packed and int(packed[1])==int(season[1]):return f'S{int(season[1]):02d}E{int(packed[2]):02d}'
        single=re.fullmatch(r'\d{1,3}',stem) or re.search(r'[_#](\d{1,3})(?:简体|繁体|[_ ]|$)',stem)
        if single:
            number=int(single[1] if single.lastindex else single[0])
            if 0<number<1000:return f'S{int(season[1]):02d}E{number:02d}'
    return None


def ro(path):
    uri=path.absolute().as_uri()
    if uri.startswith('file://') and not uri.startswith('file:///'):uri='file:////'+uri[7:]
    db=sqlite3.connect(uri+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    return db


def main():
    p=argparse.ArgumentParser();p.add_argument('--library',required=True,type=Path);p.add_argument('--title',required=True)
    p.add_argument('--alias',action='append',default=[]);p.add_argument('--year',type=int);p.add_argument('--output',required=True,type=Path)
    p.add_argument('--reuse-bundle',type=Path)
    args=p.parse_args();root=args.library.resolve();pointer=json.loads((root/'current.json').read_text('utf8'))
    folder=(root/'catalog/versions'/pointer['version']).resolve()
    if not folder.is_relative_to(root/'catalog/versions'):raise ValueError('Invalid publication path')
    raw=(folder/'manifest.json').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=pointer['manifest']['sha256']:raise ValueError('Manifest checksum mismatch')
    manifest=json.loads(raw);aliases=[args.title,*args.alias];needles=[normalized(a) for a in aliases]
    cached={}
    if args.reuse_bundle:
        with gzip.open(args.reuse_bundle,'rt',encoding='utf8') as source:
            for line in source:
                record=json.loads(line)
                if record['library_id']==manifest['libraryId'] and record['revision']==manifest['version']:cached[record['source_id']]=record
    catalog=ro(folder/'catalog.sqlite');roots={r['id']:r['path'] for r in catalog.execute('SELECT * FROM roots')}
    videos={r['id']:dict(r) for r in catalog.execute('SELECT * FROM videos WHERE seen=1')}
    files={r['id']:dict(r) for r in catalog.execute('SELECT * FROM subtitle_files WHERE seen=1')}
    total=0;skipped=[];counts={};args.output.parent.mkdir(parents=True,exist_ok=True)
    temporary=args.output.with_suffix(args.output.suffix+'.tmp')
    with gzip.open(temporary,'wt',encoding='utf8') as out:
        for root_id,shard in manifest['subtitles'].items():
            path=(root/shard['path']).resolve()
            if not path.is_relative_to(root):raise ValueError('Invalid subtitle shard path')
            db=ro(path)
            try:
                for row in db.execute('SELECT * FROM files WHERE available=1 AND error IS NULL'):
                    owned=files.get(row['id'])
                    if not owned or owned['root_id']!=root_id:continue
                    full=roots[root_id]+'/'+row['relative_path'];hay=normalized(full)
                    if not any(n in hay for n in needles):continue
                    ep=episode(row['relative_path']);video=videos.get(row['video_id'])
                    if video:ep=episode(video['relative_path']) or ep
                    if not ep:skipped.append(dict(id=row['id'],reason='episode_unknown'));continue
                    previous=cached.get(row['id'])
                    cues=previous['cues'] if previous and previous['source_digest']==row['digest'] else [dict(start_ms=round(r['start']*1000),end_ms=round(r['end']*1000),text=r['original']) for r in db.execute('SELECT start,end,original FROM cues WHERE file_id=? ORDER BY ordinal',(row['id'],))]
                    lang,origin=language(row['relative_path'],cues)
                    record=dict(library_id=manifest['libraryId'],source_id=row['id'],revision=manifest['version'],title=args.title,aliases=args.alias,year=args.year,
                                episode=ep,language=lang,language_source=origin,root=roots[root_id],relative_path=row['relative_path'],video_id=row['video_id'],
                                video_relative_path=video['relative_path'] if video else None,pairing=row['pairing'] or 'unknown',source_digest=row['digest'],cues=cues)
                    try:ImportFile.model_validate(record)
                    except Exception:skipped.append(dict(id=row['id'],reason='invalid_cues'));continue
                    out.write(json.dumps(record,ensure_ascii=False,separators=(',',':'))+'\n');total+=1;counts[lang]=counts.get(lang,0)+1
                    if total%100==0:print(f'exported={total}',flush=True)
            finally:db.close()
    catalog.close();temporary.replace(args.output)
    report=dict(files=total,languages=counts,skipped=skipped,bytes=args.output.stat().st_size,revision=manifest['version'],sha256=hashlib.sha256(args.output.read_bytes()).hexdigest())
    args.output.with_suffix('.report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8')
    print(json.dumps({**report,'skipped':len(skipped)},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
