"""After compact restore, reset absent caches; queries rebuild only needed scopes.

Stop the cache worker and API during restore/reset, then start both again.
This does not expand the entire library into permanent FULLTEXT indexes.
"""
from sqlalchemy import select,func,update
from sqlalchemy.orm import Session
from app.subtitle_db import engine,Track,Window,IndexJob


def reset_missing(store):
    count=0
    with Session(store) as db:
        tracks=list(db.execute(select(Track.id,Track.cue_count).where(Track.indexed_version>0)))
        for track_id,cues in tracks:
            actual=db.scalar(select(func.count()).select_from(Window).where(Window.track_id==track_id))
            if actual!=cues:
                db.execute(update(Track).where(Track.id==track_id).values(indexed_version=0,index_bytes=0));count+=1
        db.execute(update(IndexJob).where(IndexJob.status=='running').values(status='queued'))
        db.commit()
    return count


if __name__=='__main__':print('Reset missing caches:',reset_missing(engine()))
