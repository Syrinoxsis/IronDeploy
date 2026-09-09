"""Stream selected original files to TAR without a second physical driver tree."""
import tarfile
import time
from pathlib import PurePosixPath


def selected_snapshot(root, files, cancel=None):
    entries = []
    seen = set()
    for item in files:
        if cancel is not None and cancel.is_set():
            raise ValueError('Driver archive preparation was cancelled')
        relative = PurePosixPath(item['path'])
        if (relative.is_absolute() or len(relative.parts) < 3
                or any(p in ('.', '..') or p.startswith('.') or any(c in p for c in ':\\*?"<>|') for p in relative.parts)):
            raise ValueError('Unsafe driver selection path')
        if str(relative).casefold() in seen:
            raise ValueError('Duplicate driver selection path')
        seen.add(str(relative).casefold())
        path = root.joinpath(*relative.parts)
        path.resolve().relative_to(root.resolve())
        for ancestor in [path, *path.parents]:
            if ancestor == root:
                break
            if ancestor.is_symlink() or getattr(ancestor.stat(), 'st_file_attributes', 0) & 0x400:
                raise ValueError('Driver selection contains reparse point')
        stat = path.stat()
        if not path.is_file() or stat.st_size != item['size'] or stat.st_mtime_ns != item['mtime_ns']:
            raise ValueError(f'Indexed driver changed; rebuild index: {relative}')
        entries.append(('f', str(relative), stat.st_size, stat.st_mtime_ns))
    return {'entries': sorted(entries), 'size': sum(e[2] for e in entries),
            'fileCount': len(entries), 'infCount': sum(e[1].lower().endswith('.inf') for e in entries),
            'entryCount': len(entries)}


def write_selected_tar(root, files, destination, cancel, timeout):
    before = selected_snapshot(root, files, cancel)
    deadline = time.monotonic() + timeout

    class CheckedReader:
        def __init__(self, handle):
            self.handle = handle

        def read(self, size):
            if cancel.is_set() or time.monotonic() > deadline:
                raise ValueError('Driver TAR cancelled or timed out')
            return self.handle.read(size)

    with tarfile.open(destination, 'w', format=tarfile.PAX_FORMAT) as archive:
        for _, relative, _, _ in before['entries']:
            path = root.joinpath(*PurePosixPath(relative).parts)
            with path.open('rb') as handle:
                info = archive.gettarinfo(str(path), arcname=relative)
                archive.addfile(info, CheckedReader(handle))
    if before != selected_snapshot(root, files, cancel):
        raise ValueError('Driver files changed during TAR creation')
