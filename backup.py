"""Nightly SQLite backup with verification, compression and rotation.

Uses SQLite's online backup API, so it is safe while the collector is
writing. The copy is checked with PRAGMA quick_check before it is gzipped.
Set SOLAR_BACKUP_DIR to a second drive or a network mount; set
SOLAR_BACKUP_COMMAND to ship it off-site, e.g.
    SOLAR_BACKUP_COMMAND="rclone copy {path} remote:solar-backups"
"""

import gzip
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime

import settings


def make_backup(src_path, dest_dir, keep, command=""):
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    raw = dest_dir / f"{src_path.stem}-{stamp}.db"
    final = raw.with_suffix(".db.gz")

    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=60)
    dst = sqlite3.connect(raw)
    try:
        src.backup(dst, pages=256)  # small steps keep the writer unblocked on a Pi 1
        check = dst.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"backup failed verification: {check}")
    finally:
        dst.close()
        src.close()

    with raw.open("rb") as f_in, gzip.open(final, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out, length=1 << 20)
    raw.unlink()

    removed = prune(dest_dir, src_path.stem, keep)

    if command:
        subprocess.run(command.format(path=final), shell=True, check=True, timeout=1800)

    return final, removed


def prune(dest_dir, stem, keep):
    if keep <= 0:
        return []
    backups = sorted(dest_dir.glob(f"{stem}-*.db.gz"))
    old = backups[:-keep] if len(backups) > keep else []
    for path in old:
        path.unlink()
    return old


def main():
    if not settings.DB_PATH.exists():
        print(f"{settings.DB_PATH} does not exist")
        return 1
    final, removed = make_backup(
        settings.DB_PATH, settings.BACKUP_DIR, settings.BACKUP_KEEP, settings.BACKUP_COMMAND
    )
    size_mb = final.stat().st_size / 1e6
    print(f"Backup written: {final} ({size_mb:.1f} MB); pruned {len(removed)} old backups")
    return 0


if __name__ == "__main__":
    sys.exit(main())
