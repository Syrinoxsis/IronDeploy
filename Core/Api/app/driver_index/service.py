"""Bounded parsing queue. Import completion and explicit rescan are the only triggers."""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.config import IRONDEPLOY_ROOT
from app.drivers import DRIVERS_DIR
from app.driver_index.database import DriverIndexDB
from app.driver_index.inf_parser import parse_import

log = logging.getLogger(__name__)


class DriverIndexer:
    def __init__(self, root: Path, database: DriverIndexDB):
        self.root, self.db = root, database
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='driver-index')
        self.lock = threading.RLock()
        self.jobs = {}

    def submit(self, path):
        with self.lock:
            if path.casefold() in self.jobs and not self.jobs[path.casefold()].done():
                return self.jobs[path.casefold()]
            self.db.initialize()
            self.db.start(path)
            future = self.pool.submit(self._index, path)
            self.jobs[path.casefold()] = future
            return future

    def _index(self, path):
        started = time.monotonic()
        log.info('Driver indexing started package=%s', path)
        try:
            package = self.root.joinpath(*path.replace('\\', '/').split('/'))
            package.resolve().relative_to(self.root.resolve())
            bundles, errors = parse_import(package, path)
            if not bundles:
                raise ValueError('; '.join(errors) or 'No valid INF packages found')
            parsed = time.monotonic()
            self.db.publish(path, bundles, errors)
            mappings = [m for b in bundles for i in b['infs'] for m in i['mappings']]
            log.info('Driver indexing ready package=%s infs=%d hwids=%d compatible_ids=%d parse=%.3fs commit=%.3fs warnings=%s',
                     path, sum(len(b['infs']) for b in bundles),
                     sum(m['kind'] == 'hardware' for m in mappings),
                     sum(m['kind'] == 'compatible' for m in mappings),
                     parsed-started, time.monotonic()-parsed, errors)
        except Exception as exc:
            self.db.fail(path, exc)
            log.exception('Driver indexing error package=%s', path)

    def scan(self, rebuild=False):
        self.db.initialize()
        known = {r['path'].casefold(): r['status'] for r in self.db.imports()}
        paths = []
        if self.root.exists():
            for vendor in self.root.iterdir():
                if vendor.name.startswith('.') or not vendor.is_dir() or vendor.is_symlink():
                    continue
                for package in vendor.iterdir():
                    if package.is_dir() and not package.name.startswith('.'):
                        paths.append(vendor.name + '\\' + package.name)
        self.db.prune({p.casefold() for p in paths})
        return [self.submit(p) for p in paths if rebuild or known.get(p.casefold()) != 'ready']

    def shutdown(self):
        self.pool.shutdown(wait=True)


_service = None
_lock = threading.Lock()


def get_indexer():
    global _service
    with _lock:
        if _service is None:
            _service = DriverIndexer(DRIVERS_DIR, DriverIndexDB(IRONDEPLOY_ROOT / 'Data' / 'drivers_index.sqlite'))
        return _service


def shutdown_indexer():
    global _service
    with _lock:
        if _service is not None:
            _service.shutdown()
            _service = None
