"""Verify staged chunks and archive originals with two bounded, resumable workers."""
import argparse,hashlib,json,shutil,subprocess,sys,time
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from sqlalchemy.orm import Session
from app.subtitle_db import engine,ImportRun


def main():
    p=argparse.ArgumentParser();p.add_argument('folder',type=Path);p.add_argument('--run-id',required=True);a=p.parse_args()
    folder=a.folder.resolve();store=engine()

    def run_chunk(item):
        run_id=a.run_id+'-'+item['key']
        if len(run_id)>64:raise ValueError('Run id too long')
        source=(folder/item['path']).resolve()
        if not source.is_relative_to(folder):raise ValueError('Invalid chunk path')
        if shutil.disk_usage(folder).free<10*1024**3:raise RuntimeError('Less than 10 GiB free; import paused before exhausting disk')
        if source.stat().st_size!=item['bytes'] or hashlib.sha256(source.read_bytes()).hexdigest()!=item['sha256']:raise ValueError('Chunk integrity failed')
        # A deadlock can occur when two chunks add the same newly discovered show.
        # Replaying the batch is safe; permanent invalid records still stop the run.
        for attempt in range(3):
            result=subprocess.run([sys.executable,'-m','tools.subtitle_import',str(source),'--run-id',run_id,'--originals-only'])
            if result.returncode==0:return item
            time.sleep(2*(attempt+1))
        raise RuntimeError('Chunk '+item['key']+' failed; committed batches can be resumed')

    def progress(processed,status,message,failed=0):
        with Session(store) as db,db.begin():
            run=db.get(ImportRun,a.run_id)
            if not run:run=ImportRun(id=a.run_id);db.add(run)
            run.processed=processed;run.failed=failed;run.status=status;run.message=message

    while True:
        path=folder/'manifest.json'
        if not path.exists():time.sleep(5);continue
        manifest=json.loads(path.read_text('utf8'));processed=0;pending=[]
        with Session(store) as db:
            for item in manifest['chunks']:
                run=db.get(ImportRun,a.run_id+'-'+item['key'])
                if run and run.status=='complete' and run.failed==0 and run.processed==item['files']:processed+=item['files']
                else:pending.append(item)
        progress(processed,'running','Importing staged originals; derived indexes are built on demand')
        try:
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures=[workers.submit(run_chunk,item) for item in pending]
                for future in as_completed(futures):
                    item=future.result();processed+=item['files'];progress(processed,'running','Staged chunks complete; export/transfer: '+manifest['status'])
        except BaseException as exc:
            progress(processed,'interrupted',str(exc)[:200],1);raise
        if manifest['status']=='complete':
            assert processed==manifest['expected_files'],(processed,manifest['expected_files'])
            progress(processed,'complete','All originals archived; search indexes are built on demand')
            print('FULL IMPORT COMPLETE:',processed,flush=True);return
        time.sleep(5)


if __name__=='__main__':main()
