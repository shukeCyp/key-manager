"""Explicit destructive cutover, limited to the dedicated subtitle database."""
import argparse,configparser,gzip,hashlib,json,subprocess,time
from pathlib import Path
from sqlalchemy import create_engine,text,inspect
from sqlalchemy.engine import URL
from app.media_db import Base,TABLES

OLD={'subtitle_series','subtitle_aliases','subtitle_episodes','subtitle_tracks','subtitle_sources',
     'subtitle_windows','subtitle_imports','subtitle_review_files','subtitle_index_jobs'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--delete-old-subtitle-data',action='store_true',required=True)
    parser.add_argument('--backup',type=Path,required=True)
    args=parser.parse_args()
    backup=args.backup.resolve()
    if not backup.is_relative_to('/var/backups/tk-subtitles') or not backup.is_file() or backup.stat().st_size<1024:
        raise RuntimeError('缺少有效的旧字幕库恢复备份')
    subprocess.run(['gzip','-t',str(backup)],check=True)
    credentials=configparser.ConfigParser();credentials.read('/root/.my.cnf')
    url=URL.create('mysql+pymysql',username='root',password=credentials['client']['password'],database='tk_subtitles',
        query={'unix_socket':'/var/lib/mysql/mysql.sock','charset':'utf8mb4'})
    engine=create_engine(url)
    existing=set(inspect(engine).get_table_names())
    if existing==set(TABLES):
        print('Seven-table schema already exists; refusing to erase new data.');return
    if existing!=OLD:raise RuntimeError('云端表集合与预期不符，拒绝删除')
    snapshot={}
    with engine.connect() as conn:
        for table in sorted(OLD):snapshot[table]=conn.scalar(text('SELECT COUNT(*) FROM `'+table+'`'))
    # Validate the exact DDL in an isolated, empty schema before deleting anything.
    temporary='tk_media_schema_check_20261025'
    with engine.begin() as conn:
        if conn.scalar(text('SELECT count(*) FROM information_schema.schemata WHERE schema_name=:s'),{'s':temporary}):
            raise RuntimeError('预检数据库已存在，请先检查来源')
        conn.execute(text('CREATE DATABASE '+temporary+' CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci'))
    check=create_engine(url.set(database=temporary))
    try:
        Base.metadata.create_all(check)
        if set(inspect(check).get_table_names())!=set(TABLES):raise RuntimeError('七表DDL预检失败')
    finally:
        check.dispose()
        with engine.begin() as conn:conn.execute(text('DROP DATABASE '+temporary))
    report=dict(time=time.time(),old_counts=snapshot,backup=str(backup),new_tables=list(TABLES),status='prepared')
    report_file=backup.parent/'seven-table-reset.json';report_file.write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8')
    with engine.connect() as conn:
        conn.execute(text('SET FOREIGN_KEY_CHECKS=0'))
        try:
            for table in sorted(OLD):conn.execute(text('DROP TABLE `'+table+'`'))
        finally:conn.execute(text('SET FOREIGN_KEY_CHECKS=1'))
        conn.commit()
    Base.metadata.create_all(engine)
    if set(inspect(engine).get_table_names())!=set(TABLES):raise RuntimeError('重建后的表集合异常')
    report['status']='complete';report_file.write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8')
    print(json.dumps(dict(status='complete',database='tk_subtitles',tables=list(TABLES),backup=str(backup)),ensure_ascii=False))


if __name__=='__main__':main()
