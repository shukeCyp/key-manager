"""Package source only; runtime data, environments and credentials never enter releases."""
import hashlib,json,subprocess,tarfile
from pathlib import Path

root=Path(__file__).resolve().parents[1];output=root/'data/deployment';output.mkdir(parents=True,exist_ok=True)
tracked=subprocess.check_output(['git','ls-files'],cwd=root,text=True).splitlines()
original_names=set(subprocess.check_output(['git','ls-tree','-r','--name-only','HEAD'],cwd=root,text=True).splitlines())
manifest={}
for name in tracked:
    if name in original_names and (name.startswith(('app/','sdk/','deploy/')) or name=='requirements.txt'):
        original=subprocess.check_output(['git','show','HEAD:'+name],cwd=root)
        manifest[name]=hashlib.sha256(original).hexdigest()
(output/'baseline.json').write_text(json.dumps(manifest),'utf8')
names=list(dict.fromkeys(tracked+subprocess.check_output(['git','ls-files','--others','--exclude-standard'],cwd=root,text=True).splitlines()))
with tarfile.open(output/'release.tar.gz.tmp','w:gz') as tar:
    for name in names:
        path=root/name
        if path.is_file() and not path.is_symlink():tar.add(path,arcname=name)
(output/'release.tar.gz.tmp').replace(output/'release.tar.gz')
print('Packaged',len(names),'source files; sha256=',hashlib.sha256((output/'release.tar.gz').read_bytes()).hexdigest())
