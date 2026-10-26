"""Serve pending subtitle-window cache tasks inside the seven-table schema."""
import time
from app.media_db import engine
from app.media_search import index_one

if __name__=='__main__':
    while True:
        try:worked=index_one(engine())
        except Exception:worked=False
        if not worked:time.sleep(3)
