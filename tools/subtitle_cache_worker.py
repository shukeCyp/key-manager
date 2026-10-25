"""Run under systemd; rebuild only requested subtitle languages and evict derived data."""
import time
from app.subtitle_db import engine
from app.subtitle_cache import run_one

if __name__=='__main__':
    store=engine()
    while True:
        try:
            if not run_one(store):time.sleep(2)
        except Exception as exc:
            print('Subtitle worker retry:',type(exc).__name__,flush=True);time.sleep(5)
