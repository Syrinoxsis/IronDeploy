"""Conservative INF metadata/file closure parser. Never modifies source packages."""
import csv
import hashlib
import logging
import re
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)


def fields(value: str) -> list[str]:
    return [part.strip().strip('"') for part in next(csv.reader([value], skipinitialspace=True))]


def read_sections(path: Path) -> dict[str, list[tuple[str, str]]]:
    data = path.read_bytes()
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        text = data.decode('utf-16')
    else:
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = data.decode('cp1252')
    sections: dict[str, list[tuple[str, str]]] = {}
    section = None
    pending = ''
    for raw in text.splitlines():
        line = re.split(r';(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)', raw, maxsplit=1)[0].strip()
        line = pending + line
        if line.endswith('\\'):
            pending = line[:-1]
            continue
        pending = ''
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1].strip().lower()
            sections.setdefault(section, [])
        elif line and section is not None:
            key, _, value = line.partition('=')
            sections[section].append((key.strip(), value.strip()))
    if 'version' not in sections:
        raise ValueError('Missing INF Version section')
    return sections


def parse_inf(path: Path, root: Path, all_files: dict[str, Path]) -> dict:
    sections = read_sections(path)
    strings = {key.lower(): value.strip('"') for key, value in sections.get('strings', [])}

    def expand(value: str) -> str:
        for _ in range(8):
            updated = re.sub(r'%([^%]+)%', lambda m: strings.get(m[1].lower(), m[0]), value)
            if updated == value:
                return updated.strip('"')
            value = updated
        return value.strip('"')

    version = {k.lower(): expand(v) for k, v in sections['version']}
    def expanded_fields(value):
        return [expand(part) for part in fields(value)]
    files = {path.relative_to(root).as_posix()}
    includes: list[str] = []
    components: list[str] = []
    declared_sources: dict[str, list[tuple[str, str]]] = {}
    missing_unreferenced_sources: list[str] = []
    unavailable_arches = set()

    # SourceDisksFiles is a catalog of possible payload locations, not an
    # instruction to copy every listed file for every model. Record the
    # locations first; reachable CopyFiles entries resolve through this map.
    for section, entries in sections.items():
        if not section.startswith('sourcedisksfiles'):
            continue
        suffix = section[len('sourcedisksfiles'):]
        disk_sections = [
            sections.get(
                'sourcedisksnames' + suffix,
                sections.get('sourcedisksnames', []),
            )
        ]
        if not any(disk_sections):
            disk_sections = [
                values
                for key, values in sections.items()
                if key.startswith('sourcedisksnames.')
                and key.rsplit('.', 1)[-1] not in unavailable_arches
            ]
        for name, value in entries:
            parts = [expand(part) for part in fields(value)]
            source_subdir = parts[1] if len(parts) > 1 else ''
            locations: list[tuple[str, str]] = []
            for disks in disk_sections or [[]]:
                disk = [
                    expand(part)
                    for part in fields(dict(disks).get(parts[0], ''))
                ] if parts else []
                disk_path = disk[3] if len(disk) > 3 else ''
                subdir = '/'.join(
                    part
                    for part in (disk_path, source_subdir)
                    if part
                )
                locations.append((expand(name), subdir))
            declared_sources.setdefault(expand(name).casefold(), []).extend(
                locations or [(expand(name), '')]
            )

    def source(name: str, subdir: str = '', required: bool = True) -> bool:
        name = expand(name).replace('\\', '/')
        subdir = expand(subdir).replace('\\', '/').strip('/')
        locations = []
        if not subdir:
            # Prefer the location explicitly declared by this INF. A package
            # can legitimately contain two different files with the same
            # basename (for example root/regamdcomp.exe and
            # B406567/regamdcomp.exe); probing the INF directory first can
            # silently select the other driver's payload.
            locations.extend(declared_sources.get(Path(name).name.casefold(), []))
        locations.append((name, subdir))
        checked: list[Path] = []
        for candidate_name, candidate_subdir in locations:
            relative = (
                path.parent.relative_to(root)
                / candidate_subdir.replace('\\', '/').strip('/')
                / candidate_name.replace('\\', '/')
            )
            if (
                relative.is_absolute()
                or '..' in relative.parts
                or ':' in str(relative)
            ):
                raise ValueError(f'Unsafe INF source path: {candidate_name}')
            checked.append(relative)
            found = all_files.get(relative.as_posix().casefold())
            if found is not None:
                files.add(found.relative_to(root).as_posix())
                return True
        if not required:
            return False
        raise ValueError(f'Missing source file: {checked[-1]}')

    for key, value in version.items():
        if key.startswith('catalogfile') and value:
            try:
                source(value)
            except ValueError:
                arch = key.partition('.')[2].removeprefix('nt')
                if arch not in ('x86', 'amd64', 'arm64', 'arm'):
                    raise
                unavailable_arches.add(arch)
    for section, entries in sections.items():
        if any(re.search(r'(^|\.)(nt)?' + arch + r'(\.|$)', section) for arch in unavailable_arches):
            continue
        for key, value in entries:
            if key.lower() == 'include':
                includes.extend(expanded_fields(value))
            elif key.lower() == 'componentids':
                components.extend('SWC\\' + v.upper() for v in expanded_fields(value))
            elif key.lower() == 'copyfiles':
                for item in expanded_fields(value):
                    if item.startswith('@'):
                        if not any(Path(f).name.casefold() == item[1:].casefold() for f in files):
                            source(item[1:])
                    else:
                        for entry, unused in sections.get(item.lower(), []):
                            parts = expanded_fields(entry)
                            name = parts[1] if len(parts) > 1 and parts[1] else parts[0]
                            # SourceDisksFiles already supplies decorated/subdirectory paths.
                            if not any(Path(f).name.casefold() == name.casefold() for f in files):
                                source(name)
    for name, locations in declared_sources.items():
        if any(
            (
                path.parent.relative_to(root)
                / subdir.replace('\\', '/').strip('/')
                / candidate.replace('\\', '/')
            ).as_posix().casefold() in all_files
            for candidate, subdir in locations
        ):
            continue
        if not any(Path(item).name.casefold() == name for item in files):
            missing_unreferenced_sources.append(locations[0][0])
    mappings = []
    for _, value in sections.get('manufacturer', []):
        parts = expanded_fields(value)
        base = parts[0].lower()
        decorations = parts[1:] or ['']
        for decoration in decorations:
            if decoration.split('.')[0].removeprefix('nt').lower() in unavailable_arches:
                continue
            name = base + ('.' + decoration.lower() if decoration else '')
            for _, entry in sections.get(name, []):
                ids = expanded_fields(entry)[1:]
                for position, hardware_id in enumerate(ids):
                    if hardware_id and '%' not in hardware_id:
                        mappings.append({'hardware_id': hardware_id.upper(),
                                         'kind': 'hardware' if position == 0 else 'compatible',
                                         'decoration': decoration.lower()})
    return {'inf': path.relative_to(root).as_posix(), 'files': sorted(files),
            'mappings': mappings, 'includes': includes, 'components': components,
            'metadata': {'provider': version.get('provider'), 'class': version.get('class'),
                         'driver_ver': version.get('driverver'),
                         'date': version.get('driverver', '').partition(',')[0] or None,
                         'version': version.get('driverver', '').partition(',')[2] or None,
                         'os_decorations': sorted({m['decoration'] for m in mappings}),
                         'includes': includes, 'component_ids': components,
                         'warnings': [
                             f'Unreferenced SourceDisksFiles payload is missing: {name}'
                             for name in sorted(set(missing_unreferenced_sources), key=str.casefold)
                         ],
                         'missing_source_files': sorted(
                             set(missing_unreferenced_sources), key=str.casefold
                         ),
                         'catalogs': [
                             v for k, v in version.items() if k.startswith('catalogfile')]}}


def parse_import(
    root: Path,
    import_path: str,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[list[dict], list[str]]:
    """Group INFs sharing payload/catalogs or explicit Include/component dependencies.

    Retain original relative paths in TAR; never use the OEM/model folder as a bundle.
    Invalid individual INFs are isolated; no successful bundles means import error.
    """
    paths = list(root.rglob('*'))
    if any(p.is_symlink() or bool(getattr(p.stat(), 'st_file_attributes', 0) & 0x400) for p in paths):
        raise ValueError('Reparse points are not supported in driver imports')
    all_files = {p.relative_to(root).as_posix().casefold(): p for p in paths if p.is_file()}
    inf_paths = [p for p in all_files.values() if p.suffix.lower() == '.inf']
    log.info('Driver INF scan package=%s found=%d', import_path, len(inf_paths))
    original = {key: (p.stat().st_size, p.stat().st_mtime_ns) for key, p in all_files.items()}
    parsed, errors = [], []
    failed = set()
    failed_catalogs = set()
    if progress is not None:
        progress(0, len(inf_paths))
    for completed, path in enumerate(inf_paths, start=1):
        try:
            parsed.append(parse_inf(path, root, all_files))
        except (ValueError, OSError, UnicodeError, csv.Error) as exc:
            failed.add(path.name.casefold())
            errors.append(f'{path.relative_to(root)}: {exc}')
            try:
                for key, value in read_sections(path)['version']:
                    if key.lower().startswith('catalogfile') and '%' not in value:
                        failed_catalogs.add((path.parent.relative_to(root) / value.strip('"')).as_posix().casefold())
            except (ValueError, OSError, UnicodeError):
                pass
        finally:
            if progress is not None:
                progress(completed, len(inf_paths))
    parents = list(range(len(parsed)))

    def parent(i):
        while parents[i] != i:
            i = parents[i]
        return i

    owners = {}
    by_name = {}
    by_id = {}
    for i, inf in enumerate(parsed):
        by_name.setdefault(Path(inf['inf']).name.casefold(), []).append(i)
        for mapping in inf['mappings']:
            by_id.setdefault(mapping['hardware_id'], []).append(i)
        for file in inf['files']:
            if file.casefold() in owners:
                parents[parent(i)] = parent(owners[file.casefold()])
            owners[file.casefold()] = i
    invalid = set()
    for i, inf in enumerate(parsed):
        if failed_catalogs.intersection(f.casefold() for f in inf['files']):
            invalid.add(i)
        for include in inf['includes']:
            if include.casefold() in failed:
                invalid.add(i)
            for j in by_name.get(include.casefold(), []):
                parents[parent(i)] = parent(j)
            if include.casefold() not in by_name and include.casefold() not in failed:
                errors.append(f"{inf['inf']}: Include {include} is not local; target Windows must supply it")
        for component in inf['components']:
            for j in by_id.get(component, []):
                parents[parent(i)] = parent(j)
    invalid_roots = {parent(i) for i in invalid}
    groups = {}
    for i, inf in enumerate(parsed):
        if parent(i) not in invalid_roots:
            groups.setdefault(parent(i), []).append(inf)
    bundles = []
    for group in groups.values():
        infs = sorted(inf['inf'] for inf in group)
        file_names = sorted({f for inf in group for f in inf['files']})
        files = []
        for name in file_names:
            stat = all_files[name.casefold()].stat()
            if (stat.st_size, stat.st_mtime_ns) != original[name.casefold()]:
                raise ValueError(f'Driver files changed during indexing: {name}')
            files.append({'path': import_path.replace('\\', '/') + '/' + name,
                          'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns})
        package_id = hashlib.sha256((import_path.casefold() + '\n' + '\n'.join(infs)).encode()).hexdigest()
        bundles.append({'package_id': package_id, 'source': 'local', 'import_path': import_path,
                        'inf_paths': infs, 'files': files, 'metadata': [i['metadata'] for i in group],
                        'infs': group})
    if invalid_roots:
        errors.append('Excluded bundles depending on malformed local Include INFs')
    return bundles, list(dict.fromkeys(errors))
