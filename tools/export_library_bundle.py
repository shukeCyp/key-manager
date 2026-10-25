"""Sequential cue-table export into bounded, independently retryable compressed chunks.

The giant trigram FTS tables are never copied. Each source file's original cue
order is preserved. Ambiguous identities become archived review records.
"""
import argparse,gzip,hashlib,json,time
from collections import Counter
from pathlib import Path
from app.subtitles import ImportFile
from tools.export_shared_subtitles import ro,language
from tools.plan_shared_subtitles import infer


def main():
    p=argparse.ArgumentParser();p.add_argument('--library',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    p.add_argument('--root-id',help='Export one independent NAS root; separate roots can run concurrently')
    p.add_argument('--chunk-files',type=int,default=500);a=p.parse_args()
    if not 10<=a.chunk_files<=2000:p.error('chunk-files must be 10–2000')
    root=a.library.resolve();a.output.mkdir(parents=True,exist_ok=True)
    if (a.output/'manifest.json').exists():raise SystemExit('Output manifest already exists; use a new output directory')
    pointer=json.loads((root/'current.json').read_text('utf8'));folder=(root/'catalog/versions'/pointer['version']).resolve()
    if not folder.is_relative_to(root/'catalog/versions'):raise ValueError('Invalid publication')
    raw=(folder/'manifest.json').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=pointer['manifest']['sha256']:raise ValueError('Manifest checksum failed')
    publication=json.loads(raw)
    with ro(folder/'catalog.sqlite') as catalog:
        roots={r['id']:r['path'] for r in catalog.execute('SELECT * FROM roots')}
        files={r['id']:dict(r) for r in catalog.execute('SELECT * FROM subtitle_files WHERE seen=1')}
        videos={r['id']:dict(r) for r in catalog.execute('SELECT * FROM videos WHERE seen=1')}
    if a.root_id:
        if a.root_id not in publication['subtitles']:raise ValueError('Unknown root id')
        files={k:v for k,v in files.items() if v['root_id']==a.root_id}
    manifest=dict(library_id=publication['libraryId'],revision=publication['version'],status='running',chunks=[],files=0,cues=0,review=0,expected_files=len(files))
    counts=Counter();chunk=None;chunk_path=None;chunk_count=0;processed=set();begin=time.monotonic()

    def save():
        temp=a.output/'manifest.json.tmp';temp.write_text(json.dumps({**manifest,'languages':dict(counts)},ensure_ascii=False,indent=2),'utf8');temp.replace(a.output/'manifest.json')

    def finish_chunk():
        nonlocal chunk,chunk_count
        if chunk is None:return
        chunk.close();target=chunk_path.with_suffix('');chunk_path.replace(target)
        manifest['chunks'].append(dict(path=target.name,files=chunk_count,bytes=target.stat().st_size,sha256=hashlib.sha256(target.read_bytes()).hexdigest()))
        chunk=None;chunk_count=0;save()

    def emit(record):
        nonlocal chunk,chunk_path,chunk_count
        ident=record['source_id']
        if ident in processed:raise ValueError('Non-contiguous source cues: '+ident)
        processed.add(ident)
        if chunk is None:
            chunk_path=a.output/f'chunk-{len(manifest["chunks"]):05d}.jsonl.gz.tmp'
            chunk=gzip.open(chunk_path,'wt',encoding='utf8',compresslevel=4)
        chunk.write(json.dumps(record,ensure_ascii=False,separators=(',',':'))+'\n');chunk_count+=1;manifest['files']+=1
        manifest['cues']+=len(record.get('cues',[]));manifest['review']+=int('review_reason'in record)
        counts[record.get('language','und')]+=1
        if chunk_count>=a.chunk_files:
            finish_chunk();print(f'files={manifest["files"]} cues={manifest["cues"]} review={manifest["review"]} elapsed={time.monotonic()-begin:.1f}s',flush=True)

    def record(meta,cues):
        value=dict(meta,cues=cues)
        value['language'],value['language_source']=language(value['relative_path'],cues)
        if 'review_reason' not in value:
            try:ImportFile.model_validate(value)
            except Exception:value['review_reason']='invalid_subtitle_record'
        return value

    save()
    try:
        for root_id,shard in publication['subtitles'].items():
            if a.root_id and root_id!=a.root_id:continue
            path=(root/shard['path']).resolve()
            if not path.is_relative_to(root):raise ValueError('Invalid shard path')
            with ro(path) as db:
                db.execute('PRAGMA cache_size=-32768')
                metadata={}
                for row in db.execute('SELECT * FROM files WHERE available=1'):
                    owned=files.get(row['id'])
                    if not owned or owned['root_id']!=root_id:continue
                    video=videos.get(row['video_id'])
                    media=infer(row['relative_path'],video['relative_path'] if video else None)
                    meta=dict(library_id=publication['libraryId'],source_id=row['id'],revision=publication['version'],
                              root=roots[root_id],relative_path=row['relative_path'],video_id=row['video_id'],
                              video_relative_path=video['relative_path'] if video else None,pairing=row['pairing'] or 'unknown',
                              source_digest=row['digest'] or '0'*64,**media)
                    if row['error']:meta['review_reason']='existing_index_error: '+row['error'][:200]
                    metadata[row['id']]=meta
                current=None;cues=[]
                for row in db.execute('SELECT file_id,ordinal,start,end,original FROM cues ORDER BY id'):
                    ident=row['file_id']
                    if ident!=current:
                        if current in metadata:emit(record(metadata[current],cues))
                        current=ident;cues=[]
                    if ident in metadata:
                        cues.append(dict(start_ms=round(row['start']*1000),end_ms=round(row['end']*1000),text=row['original']))
                if current in metadata:emit(record(metadata[current],cues))
                for ident,meta in metadata.items():
                    if ident not in processed:emit(record({**meta,'review_reason':meta.get('review_reason','missing_cues')},[]))
            finish_chunk()
        for ident,meta in files.items():
            if ident not in processed:
                emit(dict(library_id=publication['libraryId'],revision=publication['version'],source_id=ident,
                          relative_path=meta['relative_path'],root=roots[meta['root_id']],cues=[],review_reason='missing_published_shard_record'))
        finish_chunk()
        assert manifest['files']==manifest['expected_files']
        manifest['status']='complete';save()
    except BaseException:
        if chunk:chunk.close()
        manifest['status']='interrupted';save();raise
    print(json.dumps({k:v for k,v in manifest.items() if k!='chunks'},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
