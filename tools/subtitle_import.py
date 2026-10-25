"""Import exported subtitle JSONL, transaction per file and resumable by content hash.

Run as: python -m tools.subtitle_import bundle.jsonl.gz --run-id NAME
Environment: SUBTITLE_DATABASE_URL. Schema creation is a separate administrative step.
"""
import argparse
import gzip,json
from pathlib import Path
from sqlalchemy.orm import Session
from app.subtitle_db import engine,ImportRun
from app.subtitles import ImportFile,import_file,import_review


def main():
    parser=argparse.ArgumentParser();parser.add_argument('bundle',type=Path);parser.add_argument('--run-id',required=True)
    parser.add_argument('--originals-only',action='store_true',help='Archive all originals; build only requested show/language indexes later')
    args=parser.parse_args()
    if not 1<=len(args.run_id)<=64: parser.error('run-id must be 1–64 characters')
    store=engine()
    with Session(store) as db,db.begin():
        row=db.get(ImportRun,args.run_id)
        if row: row.status='running';row.processed=0;row.failed=0;row.message='Resuming; committed files deduplicate'
        else: db.add(ImportRun(id=args.run_id,status='running',processed=0,failed=0,message=''))
    count=0;failed=0;built=0
    if args.originals_only:
        return import_archive(store,args.bundle,args.run_id)
    try:
        with gzip.open(args.bundle,'rb') as stream:
            while True:
                line=stream.readline(8*1024*1024+1)
                if not line:break
                if len(line)>8*1024*1024: raise ValueError('Import record exceeds 8 MiB')
                try:
                    parsed=json.loads(line)
                    value=parsed if 'review_reason' in parsed else ImportFile.model_validate(parsed)
                    with Session(store) as db,db.begin():
                        result=import_review(db,value) if isinstance(value,dict) else import_file(db,value,build_index=not args.originals_only)
                        row=db.get(ImportRun,args.run_id);row.processed=count+1;row.failed=failed
                        row.message='Last source: '+(value['source_id'] if isinstance(value,dict) else value.source_id)
                    built+=not result['reused']
                except Exception as exc:
                    failed+=1
                    print(f'FAILED record {count+1}: {type(exc).__name__}',flush=True)
                    with Session(store) as db,db.begin():
                        row=db.get(ImportRun,args.run_id);row.failed=failed;row.message=f'Record {count+1}: {type(exc).__name__}'
                count+=1
                if count%25==0:print(f'processed={count} unique_built={built} failed={failed}',flush=True)
        with Session(store) as db,db.begin():
            row=db.get(ImportRun,args.run_id);row.status='partial' if failed else 'complete';row.processed=count;row.failed=failed
            row.message=f'Unique tracks built: {built}; repeated source records safely reused'
    except BaseException:
        with Session(store) as db,db.begin():
            row=db.get(ImportRun,args.run_id);row.status='interrupted';row.processed=count;row.failed=failed
        raise
    print(f'FINISHED processed={count} unique_built={built} failed={failed}',flush=True)
    if failed:raise SystemExit(2)


def import_archive(store,bundle,run_id):
    from app.subtitle_archive import archive_batch
    count=0;built=0;pending=[];reviews=[]
    def flush():
        nonlocal count,built,pending,reviews
        if not pending and not reviews:return
        with Session(store) as db,db.begin():
            new=archive_batch(db,pending)
            for value in reviews:import_review(db,value)
            row=db.get(ImportRun,run_id);row.processed=count+len(pending)+len(reviews)
            row.message='Originals archived in atomic batches; indexes built on demand'
        count+=len(pending)+len(reviews);built+=new;pending=[];reviews=[]
        print(f'processed={count} unique_built={built} failed=0',flush=True)
    try:
        with gzip.open(bundle,'rb') as stream:
            while True:
                line=stream.readline(8*1024*1024+1)
                if not line:break
                if len(line)>8*1024*1024:raise ValueError('Import record exceeds 8 MiB')
                parsed=json.loads(line)
                if 'review_reason' in parsed:reviews.append(parsed)
                else:pending.append(ImportFile.model_validate(parsed))
                if len(pending)+len(reviews)>=50:flush()
        flush()
        with Session(store) as db,db.begin():
            row=db.get(ImportRun,run_id);row.status='complete';row.processed=count;row.failed=0;row.message=f'Archived originals; new tracks: {built}'
    except BaseException as exc:
        with Session(store) as db,db.begin():
            row=db.get(ImportRun,run_id);row.status='interrupted';row.processed=count;row.failed=1;row.message=type(exc).__name__+'; failed batch rolled back, originals retained in bundle'
        raise
    print(f'FINISHED processed={count} unique_built={built} failed=0',flush=True)


if __name__=='__main__':main()
