"""WAL readers and serialized, atomic per-import publication."""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class DriverIndexDB:
    def __init__(self, path: Path):
        self.path = path
        self.writer = threading.RLock()

    @contextmanager
    def connect(self, readonly=False):
        connection = sqlite3.connect(
            self.path.as_uri() + '?mode=ro' if readonly else str(self.path),
            uri=readonly, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA busy_timeout=10000')
        connection.execute('PRAGMA foreign_keys=ON')
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self):
        with self.writer:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS imports (
                        path TEXT PRIMARY KEY COLLATE NOCASE, status TEXT NOT NULL,
                        error TEXT, updated REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS packages (
                        id TEXT PRIMARY KEY, import_path TEXT NOT NULL REFERENCES imports(path) ON DELETE CASCADE,
                        status TEXT NOT NULL, payload TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS infs (
                        id INTEGER PRIMARY KEY, package_id TEXT NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
                        path TEXT NOT NULL, metadata TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS hardware_ids (
                        inf_id INTEGER NOT NULL REFERENCES infs(id) ON DELETE CASCADE,
                        hardware_id TEXT NOT NULL, kind TEXT NOT NULL, decoration TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS hwid_lookup ON hardware_ids(hardware_id);
                    CREATE INDEX IF NOT EXISTS inf_package ON infs(package_id);
                    CREATE INDEX IF NOT EXISTS package_import ON packages(import_path);
                ''')

    def start(self, path):
        with self.writer, self.connect() as db, db:
            db.execute('INSERT INTO imports VALUES (?, ?, NULL, ?) ON CONFLICT(path) '
                       'DO UPDATE SET status=excluded.status,error=NULL,updated=excluded.updated',
                       (path, 'indexing', time.time()))

    def publish(self, path, bundles, errors):
        with self.writer, self.connect() as db, db:
            db.execute('DELETE FROM packages WHERE import_path=?', (path,))
            for bundle in bundles:
                payload = {k: v for k, v in bundle.items() if k != 'infs'}
                db.execute('INSERT INTO packages VALUES (?, ?, ?, ?)',
                           (bundle['package_id'], path, 'ready', json.dumps(payload)))
                for inf in bundle['infs']:
                    cursor = db.execute('INSERT INTO infs(package_id,path,metadata) VALUES (?,?,?)',
                                        (bundle['package_id'], inf['inf'], json.dumps(inf['metadata'])))
                    db.executemany('INSERT INTO hardware_ids VALUES (?,?,?,?)', [
                        (cursor.lastrowid, m['hardware_id'], m['kind'], m['decoration']) for m in inf['mappings']])
            db.execute('UPDATE imports SET status=?,error=?,updated=? WHERE path=?',
                       ('ready', '\n'.join(errors) or None, time.time(), path))

    def fail(self, path, error):
        # Keep the last committed generation available after a failed rebuild.
        with self.writer, self.connect() as db, db:
            db.execute('UPDATE imports SET status=?,error=?,updated=? WHERE path=?',
                       ('error', str(error), time.time(), path))

    def imports(self):
        with self.connect(readonly=True) as db:
            return [dict(row) for row in db.execute('SELECT * FROM imports ORDER BY path')]

    def prune(self, paths):
        with self.writer, self.connect() as db, db:
            for row in db.execute('SELECT path FROM imports').fetchall():
                if row['path'].casefold() not in paths:
                    db.execute('DELETE FROM imports WHERE path=?', (row['path'],))

    def lookup(self, ids):
        result = []
        values = sorted(set(ids))
        with self.connect(readonly=True) as db:
            db.execute('BEGIN')
            for offset in range(0, len(values), 800):
                batch = values[offset:offset + 800]
                result.extend(dict(row) for row in db.execute(
                    'SELECT h.*,p.id AS package_id FROM hardware_ids h JOIN infs i ON i.id=h.inf_id '
                    'JOIN packages p ON p.id=i.package_id WHERE p.status=\'ready\' '
                    'AND h.hardware_id IN (' + ','.join('?' for _ in batch) + ')', batch))
            package_ids = sorted({row['package_id'] for row in result})
            payloads = {}
            for offset in range(0, len(package_ids), 800):
                batch = package_ids[offset:offset + 800]
                payloads.update((row['id'], row['payload']) for row in db.execute(
                    'SELECT id,payload FROM packages WHERE id IN (' + ','.join('?' for _ in batch) + ')', batch))
            for row in result:
                row['payload'] = payloads[row['package_id']]
        return result
