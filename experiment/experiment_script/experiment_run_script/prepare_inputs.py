#!/usr/bin/env python3
"""Generate a seeded uniform int32 column; no bitmap or performance experiment is run."""
import argparse
import hashlib
import json
import os
from pathlib import Path


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.partial')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def prepare_raw(path, rows, cardinality, seed, adopt=False):
    import numpy as np
    path = Path(path)
    if rows < 1 or not 1 <= cardinality < 2**31:
        raise ValueError('Invalid row count or cardinality')
    metadata = path.with_suffix(path.suffix + '.json')
    if path.exists():
        if path.stat().st_size != 4 * rows:
            raise ValueError('Existing column has the wrong byte size: ' + str(path))
        if metadata.exists():
            old = json.loads(metadata.read_text())
            if (old['rows'], old['cardinality']) != (rows, cardinality) or sha256(path) != old['sha256']:
                raise ValueError('Existing column differs from its manifest')
            if old.get('seed') is not None and old['seed'] != seed:
                raise ValueError('Existing seed differs; choose another data directory')
            return old
        if not adopt:
            raise ValueError('Column has no identity manifest; use --adopt-existing after inspecting it')
    elif metadata.exists():
        raise ValueError('Column metadata exists but the column is missing')
    path.parent.mkdir(parents=True, exist_ok=True)
    counts = np.zeros(cardinality + 1, dtype=np.int64)
    if path.exists():
        with path.open('rb') as stream:
            while True:
                values = np.fromfile(stream, dtype='<i4', count=1000000)
                if not len(values):
                    break
                if int(values.min()) < 1 or int(values.max()) > cardinality:
                    raise ValueError('Existing values fall outside 1..C')
                counts += np.bincount(values, minlength=cardinality + 1)
        origin = 'adopted external input; generation seed unknown'
        recorded_seed = None
    else:
        temporary = path.with_name(path.name + '.partial')
        if temporary.exists():
            raise ValueError('Interrupted column generation exists; preserve or remove it before retrying: ' + str(temporary))
        rng = np.random.Generator(np.random.PCG64(seed))
        with temporary.open('xb') as stream:
            for start in range(0, rows, 1000000):
                values = rng.integers(1, cardinality + 1, size=min(1000000, rows - start), dtype=np.int32)
                values.astype('<i4', copy=False).tofile(stream)
                counts += np.bincount(values, minlength=cardinality + 1)
            stream.flush()
            os.fsync(stream.fileno())
        if not counts[1:].all():
            raise ValueError('Some values are absent; the native builder needs all C values. Partial column retained.')
        temporary.rename(path)
        origin = 'uniform NumPy PCG64, fixed chunks of 1000000 rows'
        recorded_seed = seed
    if not counts[1:].all():
        raise ValueError('Some values are absent; do not use this column with the native builder')
    result = {'rows': rows, 'cardinality': cardinality, 'bytes': path.stat().st_size,
              'format': 'little-endian signed int32, values 1..C', 'seed': recorded_seed,
              'generator': origin, 'numpy_version': np.__version__, 'sha256': sha256(path),
              'min_value_count': int(counts[1:].min()), 'max_value_count': int(counts[1:].max())}
    write_json(metadata, result)
    return result


def expected_names(cardinality, encoding, group=0):
    if encoding == 'GE':
        if group <= 0 or cardinality % group:
            raise ValueError('GE group length must divide cardinality')
        ids = range(0, cardinality, group)
    else:
        ids = range(cardinality - (encoding == 'RE'))
    return {str(i) + '.bm' for i in ids}


def index_files(path, cardinality, encoding, group=0):
    path = Path(path)
    if not (path / 'done').is_file():
        raise ValueError('Incomplete index: ' + str(path))
    names = expected_names(cardinality, encoding, group)
    if {p.name for p in path.glob('*.bm')} != names:
        raise ValueError('Unexpected bitmap count/names: ' + str(path))
    result = {}
    for name in sorted(names):
        p = path / name
        if p.stat().st_size == 0:
            raise ValueError('Empty bitmap: ' + str(p))
        result[name] = {'bytes': p.stat().st_size, 'sha256': sha256(p)}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rows', type=int, required=True)
    parser.add_argument('--cardinality', type=int, required=True)
    parser.add_argument('--seed', type=int, default=20261011)
    parser.add_argument('--adopt-existing', action='store_true')
    args = parser.parse_args()
    if hasattr(os, 'sched_getaffinity'):
        os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
    print(json.dumps(prepare_raw(args.output, args.rows, args.cardinality, args.seed, args.adopt_existing), indent=2))


if __name__ == '__main__':
    main()
