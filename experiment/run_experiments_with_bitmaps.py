#!/usr/bin/env python3
"""Run the shared evaluation pipeline using native bitmaps from a ZIP archive."""
from pathlib import Path, PurePosixPath
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

sys.dont_write_bytecode = True

BITMAP_REPOSITORY = 'https://github.com/junchangwang/Bitmap-dataset'
BITMAP_API = 'https://api.github.com/repos/junchangwang/Bitmap-dataset'


def repository_archive_url(filename):
    """Find the requested ZIP in the repository's main branch or published releases."""
    from run_experiments_all import require

    def metadata(endpoint):
        request = urllib.request.Request(BITMAP_API + endpoint, headers={
            'User-Agent': 'RABIT-prebuilt-bitmap-runner/1', 'Accept': 'application/vnd.github+json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    print('LOOKUP', BITMAP_REPOSITORY, filename, flush=True)
    tree = metadata('/git/trees/main?recursive=1')
    require(not tree.get('truncated'), 'Repository listing is incomplete; provide --url HTTPS_ZIP_URL')
    matches = [entry['path'] for entry in tree.get('tree', [])
               if entry['type'] == 'blob' and PurePosixPath(entry['path']).name == filename]
    require(len(matches) <= 1, 'Multiple repository ZIPs have this name; provide --url HTTPS_ZIP_URL')
    if matches:
        return 'https://raw.githubusercontent.com/junchangwang/Bitmap-dataset/main/' + urllib.parse.quote(matches[0])
    for release in metadata('/releases'):
        if release.get('draft') or release.get('prerelease'):
            continue
        for asset in release.get('assets', []):
            if asset['name'] == filename:
                return asset['browser_download_url']
    raise RuntimeError(f'{filename} has not been published in {BITMAP_REPOSITORY}; '
                       'upload the bitmap ZIP there first, or provide --url HTTPS_ZIP_URL')


def download_archive(args):
    """Download the repository ZIP or an override URL, then publish it atomically."""
    from run_experiments_all import require, GIB
    target = args.bitmaps
    if target.is_file():
        return
    url = args.url or repository_archive_url(target.name)
    address = urllib.parse.urlsplit(url)
    require(address.scheme == 'https' and address.hostname and not address.username and not address.password,
            '--url must be an HTTPS download URL without embedded credentials')
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'RABIT-prebuilt-bitmap-runner/1'})
        with urllib.request.urlopen(request, timeout=30) as response:
            require(urllib.parse.urlsplit(response.geturl()).scheme == 'https', 'Download redirected to a non-HTTPS URL')
            length = response.headers.get('Content-Length')
            expected = int(length) if length is not None else None
            reserve = args.disk_headroom_gib * GIB
            if expected is not None:
                require(expected > 0 and shutil.disk_usage(target.parent).free >= expected + reserve,
                        'Insufficient disk for bitmap download plus headroom')
            digest, count, last = hashlib.sha256(), 0, 0
            with tempfile.NamedTemporaryFile(prefix=target.name + '.download-', suffix='.partial',
                                             dir=target.parent, delete=False) as output:
                temporary = Path(output.name)
                print('DOWNLOAD', target, flush=True)
                while chunk := response.read(8 * 1024**2):
                    require(shutil.disk_usage(target.parent).free >= len(chunk) + reserve,
                            'Insufficient remaining disk for bitmap download')
                    output.write(chunk)
                    digest.update(chunk)
                    count += len(chunk)
                    now = time.monotonic()
                    if now - last >= 30:
                        total = f' / {expected / GIB:.2f}' if expected is not None else ''
                        print(f'DOWNLOAD {count / GIB:.2f}{total} GiB', flush=True)
                        last = now
        require(expected is None or count == expected, 'Incomplete bitmap download')
        require(not args.bitmap_sha256 or digest.hexdigest() == args.bitmap_sha256,
                'Downloaded ZIP SHA256 differs from --sha256')
        require(zipfile.is_zipfile(temporary), 'Download is not a ZIP archive; provide a direct ZIP URL')
        # Hard linking fails rather than replacing a file another process created.
        os.link(temporary, target)
        print('DOWNLOADED', target, flush=True)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def canonical_path(name):
    """Recognize scoped N/C indexes and the public BM_<rows>_<C> layout."""
    if '\\' in name or name.startswith('/'):
        raise ValueError('Unsafe archive path: ' + name)
    parts = PurePosixPath(name).parts
    if '..' in parts or any(':' in p for p in parts):
        raise ValueError('Unsafe archive path: ' + name)
    section = next((p for p in parts if p in ('section51', 'section52')), 'shared')
    for i, part in enumerate(parts):
        legacy = re.fullmatch(r'BM_(\d+)([MKB]?)_(\d+)_(EE|RE|GE[_-]\d+)(?:_32)?', part, re.I)
        if legacy and len(parts) == i + 2:
            rows = int(legacy[1]) * {'': 1, 'K': 1000, 'M': 1000000, 'B': 1000000000}[legacy[2].upper()]
            cardinality = int(legacy[3])
            encoding = legacy[4].upper().replace('_', '-')
            return f'{section}/N{rows}/C{cardinality}/{encoding}/{parts[-1]}'
        if (re.fullmatch(r'N\d+', part) and len(parts) == i + 4
                and re.fullmatch(r'C\d+', parts[i + 1])
                and re.fullmatch(r'EE|RE|GE-\d+', parts[i + 2])):
            return '/'.join((section, *parts[i:]))
    return None


def read_json(archive, item):
    if item.file_size > 32 * 1024**2:
        raise ValueError('Oversized input metadata: ' + item.filename)
    return json.loads(archive.read(item))


def inspect_archive(path):
    """Inspect ZIP headers and manifests without extracting files."""
    from run_experiments_all import require
    catalog = {'files': {}, 'identities': {}, 'expected': {}}
    with zipfile.ZipFile(path) as archive:
        names = set()
        for item in archive.infolist():
            canonical = canonical_path(item.filename)
            mode = item.external_attr >> 16
            require(not stat.S_ISLNK(mode), 'Archive links are not supported: ' + item.filename)
            require(not item.flag_bits & 1, 'Encrypted archives are not supported')
            require(item.filename not in names, 'Duplicate archive member: ' + item.filename)
            names.add(item.filename)
            if item.is_dir():
                continue
            if Path(item.filename).name == 'INPUT_MANIFEST.json':
                manifest = read_json(archive, item)
                for name, record in manifest.get('files', {}).items():
                    target = canonical_path(str(PurePosixPath(item.filename).parent / name))
                    if target:
                        require(target not in catalog['expected'], 'Duplicate declared input: ' + target)
                        catalog['expected'][target] = record
            if canonical is None:
                require(not item.filename.endswith('.bm'), 'Unrecognized bitmap layout: ' + item.filename)
                continue
            filename = canonical.rsplit('/', 1)[1]
            if filename == 'input-identity.json':
                identity = read_json(archive, item)
                declared = catalog['expected'].get(canonical)
                if declared:
                    payload = archive.read(item)
                    require(len(payload) == declared['bytes'] and hashlib.sha256(payload).hexdigest() == declared['sha256'],
                            'Input identity differs from its manifest: ' + item.filename)
                key = canonical.rsplit('/', 1)[0]
                require(key not in catalog['identities'], 'Duplicate input identity: ' + key)
                catalog['identities'][key] = identity
            elif filename == 'done' or re.fullmatch(r'\d+\.bm', filename):
                require(canonical not in catalog['files'], 'Duplicate normalized input: ' + canonical)
                require(item.file_size > 0, 'Empty index member: ' + item.filename)
                catalog['files'][canonical] = {'member': item.filename, 'bytes': item.file_size}
            else:
                require(not filename.endswith('.bm'), 'Unexpected bitmap filename: ' + item.filename)
    require(any(name.endswith('.bm') for name in catalog['files']), 'The ZIP contains no recognized native bitmap directories')
    from prepare_inputs import expected_names
    groups = {}
    for name in catalog['files']:
        key, filename = name.rsplit('/', 1)
        groups.setdefault(key, set()).add(filename)
    for key, actual in groups.items():
        _, rows, cardinality, index = key.split('/')
        n, c = int(rows[1:]), int(cardinality[1:])
        require(n > 0 and c > 0, 'Invalid input dimensions: ' + key)
        encoding, group = ('GE', int(index[3:])) if index.startswith('GE-') else (index, 0)
        require(encoding != 'GE' or group > 0 and c % group == 0, 'Invalid GE group length: ' + key)
        count = c // group if encoding == 'GE' else c - (encoding == 'RE')
        require(len(actual - {'done'}) == count, 'Missing or unexpected bitmap count: ' + key)
        require(actual - {'done'} == expected_names(c, encoding, group), 'Missing or unexpected bitmap files: ' + key)
        identity = catalog['identities'].get(key)
        if identity:
            require((identity['rows'], identity['cardinality'], identity['encoding'], identity['group']) == (n, c, encoding, group),
                    'Input identity parameters differ: ' + key)
            require(set(identity['files']) == actual - {'done'}, 'Input identity file set differs: ' + key)
    catalog['groups'] = sorted(groups)
    return catalog


def base_key(catalog, node):
    suffix = f"N{node['N']}/C{node['C']}"
    scoped = f"section{node['section']}/{suffix}"
    return scoped if any(g.startswith(scoped + '/') for g in catalog['groups']) else 'shared/' + suffix


def missing_inputs(catalog, node):
    key = base_key(catalog, node)
    names = ['RE'] if node['encoding'] == 'RE' else ['EE']
    if node['encoding'] == 'GE':
        names.append(f"GE-{node['G']}")
    return [key + '/' + name for name in names if key + '/' + name not in catalog['groups']]


def import_archive(args):
    """Import verified native files once; raw columns are neither copied nor built."""
    from run_experiments_all import require, GIB
    from prepare_inputs import sha256, index_files, write_json
    archive_hash = sha256(args.bitmaps)
    require(not args.bitmap_sha256 or archive_hash == args.bitmap_sha256,
            'Bitmap ZIP SHA256 differs from --sha256')
    root = args.data_root / ('prebuilt-' + archive_hash[:20])
    args._archive_sha256 = archive_hash
    args._bitmap_root = root
    receipt = root / 'IMPORT.json'
    if root.exists():
        require(not root.is_symlink() and receipt.is_file(), 'Incomplete import retained for inspection: ' + str(root))
        recorded = json.loads(receipt.read_text())
        require(recorded['archive_sha256'] == archive_hash and recorded['groups'] == args._bitmap_catalog['groups'],
                'Existing import identity differs; choose another data directory')
        return
    need = sum(r['bytes'] for r in args._bitmap_catalog['files'].values())
    require(shutil.disk_usage(args.data_root).free >= need + args.disk_headroom_gib * GIB,
            'Insufficient disk for extracted bitmaps plus headroom')
    before = args.bitmaps.stat()
    temporary = Path(tempfile.mkdtemp(prefix='.bitmap-import-', dir=args.data_root))
    print('IMPORT', args.bitmaps, flush=True)
    try:
        with zipfile.ZipFile(args.bitmaps) as archive:
            for rel, record in args._bitmap_catalog['files'].items():
                target = temporary / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                h = hashlib.sha256()
                with archive.open(record['member']) as source, target.open('xb') as output:
                    for chunk in iter(lambda: source.read(8 * 1024**2), b''):
                        h.update(chunk)
                        output.write(chunk)
                expected = args._bitmap_catalog['expected'].get(rel)
                if expected:
                    require(target.stat().st_size == expected['bytes'] and h.hexdigest() == expected['sha256'],
                            'Archive member differs from declared hash: ' + rel)
            for key in args._bitmap_catalog['groups']:
                directory = temporary / key
                _, rows, cardinality, name = key.split('/')
                n, c = int(rows[1:]), int(cardinality[1:])
                encoding, group = ('GE', int(name[3:])) if name.startswith('GE-') else (name, 0)
                if not (directory / 'done').exists():
                    (directory / 'done').write_text('Complete native bitmap set imported from ZIP.\n')
                files = index_files(directory, c, encoding, group)
                old = args._bitmap_catalog['identities'].get(key)
                if old:
                    require(files == old['files'], 'Imported bitmap hashes differ from identity: ' + key)
                write_json(directory / 'input-identity.json', {
                    'rows': n, 'cardinality': c, 'encoding': encoding, 'group': group,
                    'column_sha256': old.get('column_sha256') if old else None,
                    'files': files, 'archive_sha256': archive_hash,
                    'provenance': 'Imported native index; file integrity checked, semantic equivalence not independently certified'})
        after = args.bitmaps.stat()
        require((before.st_size, before.st_mtime_ns, before.st_ino) == (after.st_size, after.st_mtime_ns, after.st_ino),
                'Input archive changed during import')
        write_json(temporary / 'IMPORT.json', {'archive_sha256': archive_hash, 'groups': args._bitmap_catalog['groups'],
                                             'raw_columns_generated': False, 'bitmaps_generated': False})
        temporary.rename(root)
    except BaseException:
        shutil.rmtree(temporary)
        raise


def main():
    import run_experiments_all as runner
    return runner.cli(with_bitmaps=True)


if __name__ == '__main__':
    raise SystemExit(main())
