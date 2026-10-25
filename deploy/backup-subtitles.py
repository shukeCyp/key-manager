"""Back up original subtitle data; omit regenerable full-text window contents."""
import gzip,os,shutil,subprocess
from datetime import datetime
from pathlib import Path
os.umask(0o077)
root=Path('/var/backups/tk-subtitles');root.mkdir(mode=0o700,parents=True,exist_ok=True)
name=datetime.now().strftime('%Y%m%d-%H%M%S')
folder=root/name;folder.mkdir(mode=0o700)
temporary=folder/'subtitles.sql.gz.tmp';target=folder/'subtitles.sql.gz'
common=['mysqldump','--single-transaction','--set-gtid-purged=OFF','--no-tablespaces','--hex-blob']
with gzip.open(temporary,'wb',compresslevel=4) as output:
    for args in [common+['--ignore-table=tk_subtitles.subtitle_windows','tk_subtitles'],common+['--no-data','tk_subtitles','subtitle_windows']]:
        process=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        shutil.copyfileobj(process.stdout,output,1024*1024)
        stderr=process.stderr.read();code=process.wait()
        if code:raise RuntimeError('Subtitle dump failed: '+stderr.decode(errors='replace')[:500])
temporary.replace(target)
shutil.copy2('/etc/tk-subtitles.env',folder/'database.env')
snapshots=sorted([p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and len(p.name)==15 and p.name.replace('-','').isdigit() and (p/'subtitles.sql.gz').is_file()])
for old in snapshots[:-3]:
    if old.resolve().parent==root.resolve():shutil.rmtree(old)
print('Subtitle original-data backup complete:',name,'bytes=',target.stat().st_size)
