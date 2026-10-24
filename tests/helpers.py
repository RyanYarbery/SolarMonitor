import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import settings  # noqa: E402


def fresh_db():
    """Create a schema-v3 database in a temp file and point settings at it."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    settings.DB_PATH = Path(path)
    import init_db

    init_db.DB_PATH = settings.DB_PATH
    init_db.main()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn, Path(path)


def cleanup_db(path):
    for suffix in ("", "-wal", "-shm", ".before-v3"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()
