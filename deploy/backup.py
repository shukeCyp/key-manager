"""Online SQLite snapshot plus configuration and signing key. Root only."""
from datetime import datetime, timedelta
import os
from pathlib import Path
import re
import shutil
import sqlite3

os.umask(0o077)
root = Path("/var/backups/shaohua-license")
root.mkdir(mode=0o700, parents=True, exist_ok=True)
target = root / datetime.now().strftime("%Y%m%d-%H%M%S")
target.mkdir(mode=0o700)
with sqlite3.connect("file:/var/lib/shaohua-license/app.db?mode=ro", uri=True) as source:
    with sqlite3.connect(target / "app.db") as destination:
        source.backup(destination)
shutil.copy2("/var/lib/shaohua-license/signing-key.pem", target / "signing-key.pem")
shutil.copy2("/etc/shaohua-license.env", target / "server.env")
for child in root.iterdir():
    if child.is_dir() and not child.is_symlink() and re.fullmatch(r"\d{8}-\d{6}", child.name):
        if datetime.strptime(child.name, "%Y%m%d-%H%M%S") < datetime.now() - timedelta(days=14):
            if child.resolve().parent == root.resolve():
                shutil.rmtree(child)
print("Backup complete:", target.name)
