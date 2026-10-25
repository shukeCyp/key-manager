"""Reclassify filename language metadata without reading the NAS again."""
import argparse,gzip,json,hashlib
from collections import Counter
from pathlib import Path
from tools.export_shared_subtitles import language
from app.subtitles import ImportFile

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source',type=Path);p.add_argument('destination',type=Path);a=p.parse_args()
    temp=a.destination.with_suffix(a.destination.suffix+'.tmp');counts=Counter();files=0;cues=0
    with gzip.open(a.source,'rt',encoding='utf8') as src,gzip.open(temp,'wt',encoding='utf8') as dst:
        for line in src:
            row=json.loads(line);row['language'],row['language_source']=language(row['relative_path'],row['cues'])
            ImportFile.model_validate(row);files+=1;cues+=len(row['cues']);counts[row['language']]+=1
            dst.write(json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n')
    temp.replace(a.destination)
    report=dict(files=files,cues=cues,languages=dict(counts),bytes=a.destination.stat().st_size,sha256=hashlib.sha256(a.destination.read_bytes()).hexdigest())
    a.destination.with_suffix('.report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8')
    print(json.dumps(report,ensure_ascii=False))
