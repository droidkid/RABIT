#!/usr/bin/env python3
"""Generate native inputs and run Sections 5.1/5.2, Table 2 and Figures 8/9."""
import argparse
import collections
import csv
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile

PACKAGE = Path(__file__).resolve().parent
HELPERS = PACKAGE / 'experiment_script/experiment_run_script'
sys.dont_write_bytecode = True
sys.path.insert(0, str(HELPERS))
from prepare_inputs import index_files, prepare_raw, sha256, write_json

GIB = 1024**3
SOURCE_BASES = {
    'RABIT': ('https://github.com/junchangwang/RABIT.git', '63fb6cd474ee6b8466a6fa6c96e4c92c532f91cc'),
    'CUBIT': ('https://github.com/junchangwang/CUBIT.git', '7eef13d8342bcec2cfb73f35c80281b663dfc040'),
}
SOURCE_VARIANTS = {
    'rabit': ('rabit_root', 'RABIT', 'section52-RABIT-GE.patch'),
    're52': ('re_root', 'RABIT', 'section52-RABIT-RE.patch'),
    'cubit': ('cubit_root', 'CUBIT', 'section52-CUBIT-EE.patch'),
    'rabit51': ('rabit51_root', 'RABIT', 'section51-RABIT-GE.patch'),
    're51': ('re51_root', 'RABIT', 'section51-RABIT.patch'),
    'cubit51': ('cubit51_root', 'CUBIT', None),
}


class IntegrityError(RuntimeError):
    pass


def integrity(ok, message):
    if not ok:
        raise IntegrityError(message)


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def parse_result(text, workers, operations):
    throughputs = [float(x) for x in re.findall(
        r"^Throughput\s+([\d.eE+\-]+)\s+op/s\s*$", text, re.MULTILINE)]
    counts = re.findall(r"Thread\s+(\d+)\s+Queries\s+(\d+)\s+Inserts\s+(\d+)\s+"
                        r"Deletes\s+(\d+)\s+Updates\s+(\d+)\s+Total\s+(\d+)", text)
    require(len(throughputs) == workers and len(counts) == workers,
            "Missing worker statistics; raw output is not a valid completed trial")
    require({int(c[0]) for c in counts} == set(range(workers)), "Unexpected worker IDs")
    require(all(math.isfinite(t) and t > 0 for t in throughputs), "Invalid throughput")
    require(all(sum(map(int, c[1:5])) == int(c[5]) == operations for c in counts),
            "Trial ended before the requested operation count")
    totals = [sum(int(c[i]) for c in counts) for i in range(1, 6)]
    return sum(throughputs), totals


def cpus(text):
    values = []
    for part in text.split(','):
        bounds = part.split('-')
        require(len(bounds) in (1, 2), 'Invalid CPU range')
        values.extend(range(int(bounds[0]), int(bounds[-1]) + 1))
    require(values and min(values) >= 0 and len(values) == len(set(values)), 'Invalid CPU list')
    return values


def nodes(args):
    config = json.loads((PACKAGE / 'experiment_config/benchmark-config.json').read_text())
    result = []
    if '51' in args.sections:
        section = config['section51']
        settings = sorted({(u, section['fixed_query_length']) for u in section['udi_percentages']} |
                          {(section['fixed_udi_percentage'], r) for r in section['query_lengths']})
        for method in section['algorithms']:
            for udi, length in settings:
                encoding = 'GE' if method.startswith('GE') else method
                result.append(dict(id=f'51-{method}-U{udi}-R{length}', section='51', panel='8',
                                   method=method, N=section['rows'], C=section['cardinality'],
                                   G=int(method[2:]) if encoding == 'GE' else 0, encoding=encoding,
                                   udi=udi, length=length, operations=section['attempts_per_worker']))
    if '52' in args.sections:
        grid = config['section52']['nodes']
        for n in grid:
            if args.scope == 'available' and not n['enabled_by_default']:
                continue
            method = n['method']
            result.append(dict(id=f"52{n['panel']}-{method.replace('/', '-')}-X{int(n['x'])}",
                               section='52', panel=n['panel'], method=method, x=int(n['x']),
                               N=n['N'], C=n['C'], G=n['G'], encoding=n['encoding'],
                               udi=20, length=n['range_length'], operations=1000))
    if args.nodes:
        require(set(args.nodes) <= {n['id'] for n in result}, 'Unknown or scope-excluded node ID')
        result = [n for n in result if n['id'] in args.nodes]
    return result


def memory_estimate(n):
    scale = n['N'] / 100000000
    if n['encoding'] == 'EE':
        component = 13.36 * scale * n['C'] / 1000 + 4
    elif n['encoding'] == 'RE':
        if (n['section'], n['panel'], n['N'], n['C'], n['udi'], n['length'], n['operations']) == (
                '52', 'a', 100000000, 1000, 20, 150, 1000):
            # Measured peak plus 15%; host headroom is checked separately.
            return math.ceil(30.9 * 1.15 * 10) / 10
        component = 3 * n['N'] * n['C'] / 8 / GIB + 2
    else:
        known = {(100000000, 100, 5): 2.3, (100000000, 100, 10): 2.0,
                 (100000000, 1000, 50): 3.6, (100000000, 1000, 100): 3.4,
                 (100000000, 10000, 500): 13.0, (100000000, 10000, 1000): 12.7,
                 (1000000000, 1000, 50): 34.8, (1000000000, 1000, 100): 32.1}
        component = known.get((n['N'], n['C'], n['G']), scale * (2 + n['C'] * 0.001))
    return component * 1.2 + 1


def dataset_dir(args, n):
    if getattr(args, 'bitmaps', None):
        from run_experiments_with_bitmaps import base_key
        return args._bitmap_root / base_key(args._bitmap_catalog, n)
    return args.data_root / f"N{n['N']}" / f"C{n['C']}"


def encoding_name(n):
    return f"GE-{n['G']}" if n['encoding'] == 'GE' else n['encoding']


def mem_available():
    values = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        values[key] = int(value.strip().split()[0]) * 1024
    return values['MemAvailable'] / GIB


def ancestor(path):
    path = Path(path)
    while not path.exists():
        path = path.parent
    return path


def source_for(args, n):
    if n['section'] == '51':
        if n['encoding'] == 'EE':
            return 'cubit51', args.cubit51_root
        if n['encoding'] == 'RE':
            return 're51', args.re51_root or args.rabit51_root
        return 'rabit51', args.rabit51_root
    if n['encoding'] == 'EE':
        return 'cubit', args.cubit_root
    if n['encoding'] == 'RE':
        return 're52', args.re_root or args.rabit_root
    return 'rabit', args.rabit_root


def source_issue(args, n):
    role, root = source_for(args, n)
    if args.stage == 'plan' and role in getattr(args, '_managed_sources', {}):
        return None
    if root is None or not (root / 'CMakeLists.txt').is_file():
        return 'Missing source checkout for ' + role
    if n['encoding'] == 'GE':
        text = (root / 'src/nicolas/nicolas.cpp').read_text()
        text = re.sub(r'/\*.*?\*/|//[^\n]*', '', text, flags=re.S)
        marker = 'merge_thread = std::thread(rabit_merge_dispatcher, table);'
        before = text.split(marker)[0].split('void evaluate(')[-1]
        if marker not in text or not re.search(r'__atomic_store_n\s*\(\s*&run_merge_func\s*,\s*true', before):
            return 'RABIT maintenance-startup fix is missing'
    if role == 're51':
        text = (root / 'src/bitmaps/ub/table.cpp').read_text()
        if not all(s in text for s in ('SECTION51_RE_UNUSED_CACHE', 'RABIT_RE_CLEAN_UNUSED_CACHE',
                                       'flushFile(getBtvName(i).c_str())')):
            return 'Section 5.1 RE initialization-cache patch is missing; obtain it from the collaborator'
    return None


def blockers(args, n, preparing=False):
    reasons = []
    if getattr(args, 'bitmaps', None):
        from run_experiments_with_bitmaps import missing_inputs
        missing = missing_inputs(args._bitmap_catalog, n)
        if missing:
            reasons.append('ZIP is missing native indexes: ' + ', '.join(missing))
        preparing = False
    try:
        issue = source_issue(args, n)
        if issue:
            reasons.append(issue)
    except OSError as exc:
        reasons.append(str(exc))
    need = memory_estimate(n)
    if need > args.memory_gib:
        reasons.append(f'Estimated trial RAM {need:.1f} GiB exceeds budget {args.memory_gib:g} GiB')
    if preparing:
        if not (args.rabit_root / 'CMakeLists.txt').is_file() and not (
                args.stage == 'plan' and 'rabit' in getattr(args, '_managed_sources', {})):
            reasons.append('Missing RABIT source for native index construction')
        need = max(need, n['N'] * 20 / GIB + 2)
    if mem_available() < need + args.headroom_gib:
        reasons.append(f'Available RAM below estimate plus {args.headroom_gib:g} GiB headroom')
    if preparing:
        base = dataset_dir(args, n)
        raw = 0 if (base / 'column.i32').exists() else n['N'] * 4
        ee = n['N'] * n['C'] / 8 if n['encoding'] == 'RE' else n['N'] * 12
        if n['encoding'] == 'RE':
            # Include the native builder's temporary EE copy.
            disk = raw + ee * 1.05 + n['N'] * 12 + 1024**2
        else:
            disk = raw + ee * 1.1 + n['N'] * 4
        if (base / encoding_name(n) / 'input-identity.json').exists():
            disk = 0
        if shutil.disk_usage(ancestor(args.data_root)).free < disk + args.disk_headroom_gib * GIB:
            reasons.append('Insufficient free data disk including staging and headroom')
    return reasons


def source_identity(root):
    paths = [root / 'CMakeLists.txt']
    paths += [p for p in (root / 'src').rglob('*') if p.is_file() and
              p.suffix in ('.h', '.hpp', '.c', '.cpp', '.cmake') and not p.is_symlink()]
    paths += [root / name for name in ('get_cpu_ghz.sh', '.gitmodules') if (root / name).is_file()]
    r = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True) if shutil.which('git') else None
    return {'root': str(root), 'commit': r.stdout.strip() if r and r.returncode == 0 else 'unknown',
            'files': {str(p.relative_to(root)): sha256(p) for p in sorted(paths)}}


def configure_sources(args, selected):
    root = PACKAGE if (PACKAGE / 'CMakeLists.txt').is_file() else PACKAGE.parent
    args._source_seeds = {
        'RABIT': [p for p in (args.rabit_root, args.rabit51_root, args.re_root, args.re51_root,
                              root, root / 'RABIT', root / 'RABIT-delivery') if p],
        'CUBIT': [p for p in (args.cubit_root, args.cubit51_root, root / 'CUBIT', root.parent / 'CUBIT') if p],
    }
    roles = {source_for(args, n)[0] for n in selected}
    if not args.bitmaps:
        roles.add('rabit')
    args._managed_sources = {}
    for role in sorted(roles):
        attr, project, patch = SOURCE_VARIANTS[role]
        if getattr(args, attr) is not None:
            continue
        url, base = SOURCE_BASES[project]
        spec = {'project': project, 'url': url, 'base': base, 'patch': patch,
                'patch_sha256': sha256(PACKAGE / 'source_patches' / patch) if patch else None}
        key = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]
        destination = args.data_root / '.sources' / (role + '-' + key)
        setattr(args, attr, destination)
        args._managed_sources[role] = dict(spec, directory=str(destination))


def prepare_sources(args):
    if not args._managed_sources:
        return
    require(shutil.which('git'), 'Install Git before running the experiments')
    def git(*command):
        try:
            result = subprocess.run(['git', *map(str, command)], stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, timeout=args.prepare_timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError('Source preparation timed out') from exc
        require(result.returncode == 0, 'Source preparation failed: ' + result.stdout[-4000:])
        return result.stdout.strip()
    for role, spec in args._managed_sources.items():
        destination = Path(spec['directory'])
        receipt = destination / '.source.json'
        if destination.exists():
            require(not destination.is_symlink() and receipt.is_file(), 'Incomplete source cache: ' + str(destination))
            record = json.loads(receipt.read_text())
            current = source_identity(destination)
            integrity(record['specification'] == spec and current['commit'] == spec['base']
                      and current['files'] == record['files'], 'Prepared source changed: ' + str(destination))
            continue
        require(args.stage != 'plot', 'Prepared sources are missing; run the all stage first')
        print('PREPARE SOURCE', role, flush=True)
        parent = destination.parent
        parent.mkdir(parents=True, exist_ok=True)
        cache = parent / ('.' + spec['project'] + '.git')
        if not cache.exists():
            origin = spec['url']
            for candidate in args._source_seeds[spec['project']]:
                probe = subprocess.run(['git', '-C', str(candidate), 'cat-file', '-e', spec['base'] + '^{commit}'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if probe.returncode == 0:
                    origin = str(candidate)
                    break
            temporary = Path(tempfile.mkdtemp(prefix='.base-', dir=parent))
            try:
                git('clone', '--bare', '--no-hardlinks', origin, temporary)
                git('--git-dir', temporary, 'remote', 'set-url', 'origin', spec['url'])
                git('--git-dir', temporary, 'cat-file', '-e', spec['base'] + '^{commit}')
                temporary.rename(cache)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        require(not cache.is_symlink(), 'Source cache must not be a symlink')
        git('--git-dir', cache, 'cat-file', '-e', spec['base'] + '^{commit}')
        temporary = Path(tempfile.mkdtemp(prefix='.source-', dir=parent))
        try:
            git('clone', '--shared', '--no-checkout', cache, temporary)
            git('-C', temporary, 'checkout', '--detach', spec['base'])
            if spec['patch']:
                patch = PACKAGE / 'source_patches' / spec['patch']
                integrity(sha256(patch) == spec['patch_sha256'], 'Source patch changed during preparation')
                git('-C', temporary, 'apply', '--check', patch)
                git('-C', temporary, 'apply', patch)
            helper = temporary / 'get_cpu_ghz.sh'
            if helper.is_file():
                helper.chmod(helper.stat().st_mode | 0o111)
            identity = source_identity(temporary)
            integrity(identity['commit'] == spec['base'], 'Prepared source base differs')
            write_json(temporary / '.source.json', {'specification': spec, 'files': identity['files']})
            temporary.rename(destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        print('SOURCE READY', role, flush=True)


def group_rss(group):
    total = 0
    for p in Path('/proc').glob('[0-9]*'):
        try:
            fields = (p / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == group:
                m = re.search(r'^VmRSS:\s+(\d+)', (p / 'status').read_text(), re.M)
                total += int(m[1]) * 1024 if m else 0
        except (OSError, ValueError, IndexError):
            pass
    return total / GIB


def execute(args, command, prefix, cpu_list, environment=None, timeout=None):
    prefix.parent.mkdir(parents=True, exist_ok=True)
    outpath = prefix.with_suffix('.stdout')
    errpath = prefix.with_suffix('.stderr')
    meta = prefix.with_suffix('.command.json')
    command = ['taskset', '-c', ','.join(map(str, cpu_list)), *map(str, command)]
    env = os.environ.copy()
    for key in ('RABIT_RE_CLEAN_UNUSED_CACHE', 'RABIT_SECTION53A'):
        env.pop(key, None)
    env.update(environment or {})
    info = {'command': command, 'environment': environment or {}, 'cpu_affinity': cpu_list,
            'started_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'status': 'running'}
    write_json(meta, info)
    print('RUN', prefix.name, flush=True)
    started, observed = time.monotonic(), 0
    with outpath.open('x') as out, errpath.open('x') as err:
        proc = subprocess.Popen(['/usr/bin/time', '-v', *command], stdout=out, stderr=err,
                                env=env, start_new_session=True)
        try:
            while proc.poll() is None:
                elapsed = time.monotonic() - started
                observed = max(observed, group_rss(proc.pid))
                if elapsed > (timeout or args.timeout):
                    raise RuntimeError('Child timeout')
                if mem_available() < args.stop_below_gib or observed > args.memory_gib:
                    raise RuntimeError('Child resource guard: low available RAM or process-group RAM budget exceeded')
                time.sleep(1)
            require(proc.returncode == 0, f'Child exited {proc.returncode}; see {errpath}')
            info.update(status='passed', exit_code=proc.returncode)
        except BaseException as exc:
            info.update(status='failed', reason=str(exc))
            raise
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
            info.update(wall_seconds=time.monotonic() - started, observed_group_rss_gib=observed)
            write_json(meta, info)
    text = outpath.read_text(errors='replace')
    require(not re.search(r'ERROR:|Unknown approach|not implemented|hasn.t been set correctly', text),
            'Child reported an error despite exit zero: ' + str(outpath))
    rss = re.search(r'Maximum resident set size \(kbytes\):\s*(\d+)', errpath.read_text(errors='replace'))
    info['max_rss_kib'] = int(rss[1]) if rss else None
    info['stdout_sha256'], info['stderr_sha256'] = sha256(outpath), sha256(errpath)
    write_json(meta, info)
    return text, info


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def save_csv(path, records, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.partial')
    with temporary.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
    temporary.replace(path)


def trial_command(args, n, binary):
    base = dataset_dir(args, n)
    cmd = [binary, '--mode', 'range', '--approach',
           'cubit-lk' if n['encoding'] == 'EE' else 'rabit' if n['encoding'] == 'GE' else 'ub',
           '--workers', '16', '--number-of-queries', str(n['operations'] * (100 - n['udi']) // 100),
           '--number-of-udis', str(n['operations'] * n['udi'] // 100),
           '--cardinality', str(n['C']), '--number-of-rows', str(n['N']),
           '--index-path', base / ('RE' if n['encoding'] == 'RE' else 'EE'),
           '--encoding-scheme', n['encoding'], '--verbose', 'false']
    if n['encoding'] == 'EE':
        cmd += ['--range', str(n['length']), '--rows-per-seg', '100000']
    else:
        cmd += ['--RQ-length', str(n['length']), '--helper-threads', '8', '--merge-threads', '4']
        if n['encoding'] == 'GE':
            cmd += ['--rows-per-seg', '100000', '--GE-group-len', str(n['G']),
                    '--group-path', base / encoding_name(n)]
    return list(map(str, cmd))


def prepare_index(args, n, encoding, group, builder, origin, state):
    base = dataset_dir(args, n)
    name = f'GE-{group}' if encoding == 'GE' else encoding
    directory = base / name
    identity = directory / 'input-identity.json'
    raw = prepare_raw(base / 'column.i32', n['N'], n['C'], args.seed, args.adopt_existing)
    if identity.exists():
        old = json.loads(identity.read_text())
        require(old['column_sha256'] == raw['sha256'], 'Index was built from another column')
        files = index_files(directory, n['C'], encoding, group)
        require(files == old['files'], 'Index content changed: ' + str(directory))
        return old
    if (directory / 'done').exists():
        require(args.adopt_existing, 'Existing index has no provenance; explicitly --adopt-existing if verified externally')
        provenance = 'adopted external bitmap files; semantic equivalence requires separate verification'
    else:
        require(not directory.exists(), 'Partial/unidentified index retained; inspect or move it before retrying: ' + str(directory))
        if encoding == 'RE':
            text = (args.rabit_root / 'src/nicolas/gen_bitmap.cpp').read_text()
            require('getenv("TMPDIR")' in text,
                    'RE generation requires the reviewed TMPDIR fix; public /tmp allocation is not redirected automatically')
        scratch = base / ('scratch-' + name)
        require(not scratch.exists(), 'Previous scratch data retained: ' + str(scratch))
        scratch.mkdir()
        cmd = [builder, '--mode', 'build', '--cardinality', str(n['C']),
               '--number-of-rows', str(n['N']), '--data-path', base / 'column.i32',
               '--index-path', base / ('EE' if encoding == 'GE' else encoding),
               '--group-path', base / name, '--encoding-scheme', encoding, '--GE-group-len', str(group or 1)]
        prefix = args.output / 'input-logs' / f"N{n['N']}-C{n['C']}-{name}-{time.time_ns()}"
        execute(args, cmd, prefix, args.prep_ids, {'TMPDIR': str(scratch)}, args.prepare_timeout)
        index_files(directory, n['C'], encoding, group)
        shutil.rmtree(scratch)
        provenance = 'native build command; builder SHA256 ' + origin
    files = index_files(directory, n['C'], encoding, group)
    result = {'column_sha256': raw['sha256'], 'encoding': encoding, 'group': group,
              'rows': n['N'], 'cardinality': n['C'], 'provenance': provenance, 'files': files}
    write_json(identity, result)
    return result


def check_existing_inputs(args, n):
    base = dataset_dir(args, n)
    raw = base / 'column.i32'
    prebuilt = bool(getattr(args, 'bitmaps', None))
    rawmeta = None
    if not prebuilt:
        require(raw.is_file() and raw.stat().st_size == n['N'] * 4, 'Missing prepared raw column')
        rawmeta = json.loads(raw.with_suffix('.i32.json').read_text())
        integrity(rawmeta['rows'] == n['N'] and rawmeta['cardinality'] == n['C'], 'Raw column parameters differ')
        integrity(sha256(raw) == rawmeta['sha256'], 'Raw column hash changed')
    names = ['RE'] if n['encoding'] == 'RE' else ['EE']
    if n['encoding'] == 'GE':
        names.append(encoding_name(n))
    identities = {}
    for name in names:
        p = base / name
        old = json.loads((p / 'input-identity.json').read_text())
        integrity(old['rows'] == n['N'] and old['cardinality'] == n['C'], 'Index parameters differ')
        if prebuilt:
            integrity(old['archive_sha256'] == args._archive_sha256, 'Index archive identity differs')
        else:
            integrity(old['column_sha256'] == rawmeta['sha256'], 'Index column identity differs')
        encoding, group = ('GE', n['G']) if name.startswith('GE') else (name, 0)
        actual = index_files(p, n['C'], encoding, group)
        integrity(actual == old['files'], 'Input index hash changed')
        identities[name] = old
    columns = {v.get('column_sha256') for v in identities.values()} - {None}
    integrity(len(columns) <= 1, 'EE and GE indexes belong to different input columns')
    return {'raw': rawmeta, 'indexes': identities}


def aggregate(args, selected, state):
    accepted = collections.defaultdict(list)
    all_trials = []
    for n in selected:
        node = state['nodes'].get(n['id'], {})
        if node.get('integrity_failed'):
            continue
        for trial, old in sorted(node.get('trials', {}).items()):
            raw = args.output / old['raw_log']
            require(sha256(raw) == old['raw_sha256'], 'Saved trial raw log changed')
            rate, counts = parse_result(raw.read_text(errors='replace'), 16, n['operations'])
            require(math.isclose(rate, old['rate'], rel_tol=1e-12) and counts == old['counts'], 'Saved trial record differs')
            require(sha256(args.output / old['stderr_log']) == old['stderr_sha256'], 'Saved trial stderr changed')
            all_trials.append(dict(node=n['id'], trial=int(trial), rate=rate,
                                   queries=counts[0], inserts=counts[1], deletes=counts[2], updates=counts[3],
                                   total=counts[4], wall_seconds=old['wall_seconds'],
                                   max_rss_kib=old['max_rss_kib'], raw_log=old['raw_log']))
            accepted[n['id']].append((int(trial), rate, counts))
    fields = ['node', 'trial', 'rate', 'queries', 'inserts', 'deletes', 'updates', 'total',
              'wall_seconds', 'max_rss_kib', 'raw_log']
    save_csv(args.output / 'trials.csv', all_trials, fields)
    rows51, rows52 = [], {p: [] for p in 'ab'}
    complete = []
    for n in selected:
        rr = accepted[n['id']]
        if len(rr) != 3 or {v[0] for v in rr} != {1, 2, 3}:
            continue
        complete.append(n['id'])
        if n['section'] == '51':
            rates = [v[1] for v in rr]
            actual = 100 * sum(sum(v[2][1:4]) for v in rr) / (3 * 16 * n['operations'])
            rows51.append(dict(algorithm=n['method'], udi_pct=n['udi'], rq_length=n['length'], repeats=3,
                               mean_reported_thread_sum_ops_s=statistics.mean(rates),
                               sample_stdev_ops_s=statistics.stdev(rates), actual_udi_pct=actual))
        else:
            for trial, rate, counts in rr:
                rows52[n['panel']].append(dict(method=n['method'], x=n['x'], trial=trial,
                                               total_attempts=16 * n['operations'], sum_reported_worker_op_s=rate,
                                               workers=16, cpu_affinity=args.cpu_ids))
    save_csv(args.output / 'section51/summary.csv', rows51,
             ['algorithm', 'udi_pct', 'rq_length', 'repeats', 'mean_reported_thread_sum_ops_s',
              'sample_stdev_ops_s', 'actual_udi_pct'])
    for panel in 'ab':
        write_json(args.output / f'section52/figure9{panel}-8cpu-samples.json', rows52[panel])
    return rows51, rows52, complete


def plot_fresh(args, selected, state):
    (args.output / 'report.json').unlink(missing_ok=True)
    rows51, rows52, complete = aggregate(args, selected, state)
    figures = args.output / 'figures'
    figures.mkdir(exist_ok=True)
    images = []
    if rows51:
        module = load_module(PACKAGE / 'experiment_script/experiment_image_script/plot_section51_server100M.py', 'fresh51')
        module.STYLES = {k: v for k, v in module.STYLES.items() if any(r['algorithm'] == k for r in rows51)}
        values = {(r['algorithm'], r['udi_pct'], r['rq_length']):
                  (r['mean_reported_thread_sum_ops_s'], r['sample_stdev_ops_s']) for r in rows51}
        for method in module.STYLES:
            for u, length in sorted({(u, 150) for u in (0, 10, 20, 30, 40, 50)} |
                                    {(20, r) for r in (50, 150, 250, 350, 450)}):
                values.setdefault((method, u, length), (math.nan, math.nan))
        folder = figures / 'section51'
        folder.mkdir(exist_ok=True)
        namespace = argparse.Namespace(rows=100000000, cardinality=1000, workers=16, operations=200,
                                       repeats=3, ylim=[1, 2000], formats=['png'], out_dir=folder,
                                       note='Fresh measurements; missing configurations left blank.')
        module.plt.rcParams.update({'font.family': 'DejaVu Serif', 'font.size': 12, 'axes.linewidth': 1.0})
        styles = module.STYLES.copy()
        module.STYLES = {k: v for k, v in styles.items() if any(r['algorithm'] == k and r['rq_length'] == 150 for r in rows51)}
        module.draw_panel(values, [(u, 150) for u in (0, 10, 20, 30, 40, 50)],
                          [0, 10, 20, 30, 40, 50], 'UDI ratio (%)', '(a) 15% query range', 'panel1', namespace)
        module.STYLES = {k: v for k, v in styles.items() if any(r['algorithm'] == k and r['udi_pct'] == 20 for r in rows51)}
        module.draw_panel(values, [(20, r) for r in (50, 150, 250, 350, 450)],
                          [5, 15, 25, 35, 45], 'Query range (% of cardinality)', '(b) 20% UDI ratio', 'panel2', namespace)
        combine_panels(folder / 'panel1.png', folder / 'panel2.png', figures / 'figure8.png')
        images.append('figure8.png')
    if any(rows52.values()):
        subprocess.run([sys.executable, '-B', str(PACKAGE / 'experiment_script/experiment_image_script/plot_section52.py'),
                        '--source', str(args.output / 'section52'), '--output', str(figures / 'section52'),
                        '--cpu-list', ','.join(map(str, args.cpu_ids))], check=True)
        shutil.copy2(figures / 'section52/figure9-native-paper-axes.png', figures / 'figure9.png')
        images.append('figure9.png')
    table = collect_space_table(args) if '51' in args.sections and not args.nodes else None
    if table and table['complete']:
        images.append('table2.png')
    finished = len(complete) == len(selected) and (table is None or table['complete'])
    report = {'status': 'selected nodes complete' if finished else 'partial',
              'selected_nodes': len(selected), 'complete_nodes': complete,
              'missing_selected_nodes': [n['id'] for n in selected if n['id'] not in complete],
              'figures': images, 'table2': table,
              'input_mode': 'prebuilt ZIP; no generation' if args.bitmaps else 'native generation',
              'cohort': 'fresh measurements only; no bundled values reused',
              'full_paper_claim': False, 'scope': args.scope,
              'limitations': ['Paper live-index LRU absent', 'Paper Section 5.2 mixed-attempt count unconfirmed',
                              'Static input/worker checks do not prove concurrent algorithm correctness']}
    write_json(args.output / 'report.json', report)
    print(json.dumps(report, indent=2), flush=True)
    return report


def combine_panels(left, right, target):
    from PIL import Image
    with Image.open(left) as a, Image.open(right) as b:
        a, b = a.convert('RGB'), b.convert('RGB')
        canvas = Image.new('RGB', (a.width + 24 + b.width, max(a.height, b.height)), 'white')
        canvas.paste(a, (0, (canvas.height - a.height) // 2))
        canvas.paste(b, (a.width + 24, (canvas.height - b.height) // 2))
        canvas.save(target)


def prepare_space_index(args, selected, binaries, state, allowed):
    node = dict(next(n for n in selected if n['section'] == '51'), encoding='GE', method='GE200', G=200)
    reasons = blockers(args, node, preparing=True)
    failures = state.get('table2_preparation_failures', 0)
    if reasons or failures >= 2:
        state['table2_preparation_status'] = '; '.join(reasons) if reasons else 'Two preparation failures retained'
        return
    builder = binaries.get('rabit')
    if builder is None:
        state['table2_preparation_status'] = 'Missing native input builder'
        return
    try:
        os.sched_setaffinity(0, args.prep_ids)
        prepare_index(args, node, 'GE', 200, builder, sha256(builder), state)
        state['table2_preparation_status'] = 'GE200 prepared'
    except (RuntimeError, OSError, ValueError) as exc:
        state['table2_preparation_failures'] = failures + 1
        state['table2_preparation_status'] = str(exc)
    finally:
        os.sched_setaffinity(0, allowed)
        write_json(args.output / 'manifest.json', state)


def collect_space_table(args):
    node = dict(section='51', N=100000000, C=1000)
    base = dataset_dir(args, node)
    sizes, missing, columns = {}, [], set()
    for method in ('EE', 'RE', 'GE50', 'GE100', 'GE200'):
        group = int(method[2:]) if method.startswith('GE') else 0
        name = f'GE-{group}' if group else method
        try:
            files = index_files(base / name, 1000, 'GE' if group else method, group)
            identity = json.loads((base / name / 'input-identity.json').read_text())
            integrity(files == identity['files'], 'Space input hash differs: ' + name)
            if identity.get('column_sha256'):
                columns.add(identity['column_sha256'])
            sizes[method] = sum(v['bytes'] for v in files.values())
        except (RuntimeError, OSError, ValueError) as exc:
            missing.append({'method': method, 'reason': str(exc)})
    if len(columns) > 1:
        missing.append({'method': 'all', 'reason': 'Space indexes belong to different input columns'})
    ee = sizes.get('EE')
    rows = []
    for method, own in sizes.items():
        if method.startswith('GE') and ee is None:
            continue
        total = own + (ee if method.startswith('GE') else 0)
        rows.append({'algorithm': method, 'serialized_bitmap_bytes': total,
                     'MB_decimal': total / 1000000, 'relative_to_EE': total / ee if ee else ''})
    destination = args.output / 'section51/index_sizes.csv'
    save_csv(destination, rows, ['algorithm', 'serialized_bitmap_bytes', 'MB_decimal', 'relative_to_EE'])
    complete = len(rows) == 5 and not missing
    if complete:
        subprocess.run([sys.executable, '-B', str(PACKAGE / 'experiment_script/experiment_image_script/plot_section51_space.py'),
                        '--csv', str(destination), '--out', str(args.output / 'figures/table2'), '--formats', 'png'], check=True)
    return {'complete': complete, 'missing_indexes': missing, 'csv': 'section51/index_sizes.csv'}


def main(argv=None, with_bitmaps=False):
    parser = argparse.ArgumentParser(description=__doc__)
    if with_bitmaps:
        parser.description = 'Run Table 2 and Figures 8/9 using existing native indexes from a ZIP; never generate inputs.'
        parser.add_argument('bitmaps', type=Path, help='Local ZIP path; download a matching filename from Bitmap-dataset if absent')
        parser.add_argument('--stage', choices=['plan', 'build', 'run', 'plot', 'all'], default='all')
        parser.add_argument('--url', help='Override the Bitmap-dataset download with a direct HTTPS ZIP URL')
        parser.add_argument('--sha256', dest='bitmap_sha256', help='Optional expected bitmap ZIP SHA256')
    else:
        parser.add_argument('stage', nargs='?', default='all', choices=['plan', 'build', 'prepare', 'run', 'plot', 'all'])
        parser.set_defaults(bitmaps=None)
    parser.add_argument('--rabit-root', type=Path, help='Override automatic Section 5.2 GE/builder source setup')
    parser.add_argument('--cubit-root', type=Path, help='Override automatic Section 5.2 EE source setup')
    parser.add_argument('--re-root', type=Path, help='Override automatic Section 5.2 RE source setup')
    parser.add_argument('--rabit51-root', type=Path, help='Override automatic Section 5.1 GE source setup')
    parser.add_argument('--cubit51-root', type=Path, help='Override automatic Section 5.1 EE source setup')
    parser.add_argument('--re51-root', type=Path, help='Override automatic Section 5.1 RE source setup')
    parser.add_argument('--rabit-binary', type=Path)
    parser.add_argument('--cubit-binary', type=Path)
    parser.add_argument('--re-binary', type=Path)
    parser.add_argument('--re51-binary', type=Path)
    parser.add_argument('--rabit51-binary', type=Path)
    parser.add_argument('--cubit51-binary', type=Path)
    parser.add_argument('--data-root', type=Path, default=Path.home() / 'rabit-inputs')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--sections', nargs='+', choices=['51', '52'], default=['51', '52'])
    parser.add_argument('--scope', choices=['available', 'all'], default='available')
    parser.add_argument('--nodes', nargs='+', help='Restrict to node IDs printed by plan')
    parser.add_argument('--cpus', default='0-7')
    parser.add_argument('--prep-cpus', default='0-1')
    parser.add_argument('--memory-gib', type=float, default=48)
    parser.add_argument('--headroom-gib', type=float, default=4)
    parser.add_argument('--stop-below-gib', type=float, default=2)
    parser.add_argument('--disk-headroom-gib', type=float, default=5)
    parser.add_argument('--seed', type=int, default=20261011)
    parser.add_argument('--adopt-existing', action='store_true')
    parser.add_argument('--timeout', type=float, default=10800)
    parser.add_argument('--prepare-timeout', type=float, default=21600)
    parser.add_argument('--lock-file', type=Path, help='Shared experiment lock; default DATA_ROOT/.experiment.lock')
    args = parser.parse_args(argv)
    args.output = args.output or Path.home() / 'rabit-fresh-results'
    args.cpu_ids, args.prep_ids = cpus(args.cpus), cpus(args.prep_cpus)
    require(len(args.cpu_ids) <= 8 and len(args.prep_ids) <= 2, 'At most 8 experiment CPUs and 2 preparation CPUs')
    allowed = set(os.sched_getaffinity(0))
    require(set(args.cpu_ids) <= allowed and set(args.prep_ids) <= allowed, 'Requested CPU IDs are unavailable')
    require(args.memory_gib > 0 and args.headroom_gib >= 0 and args.stop_below_gib >= 0 and
            args.disk_headroom_gib >= 0 and args.timeout > 0 and args.prepare_timeout > 0, 'Invalid resource/timeout settings')
    require(args.seed >= 0, 'Seed must be nonnegative')
    for key, value in vars(args).copy().items():
        if isinstance(value, Path):
            setattr(args, key, value.resolve())
    if args.bitmaps:
        from run_experiments_with_bitmaps import BITMAP_REPOSITORY, download_archive, inspect_archive
        require(not args.bitmap_sha256 or re.fullmatch(r'[a-fA-F0-9]{64}', args.bitmap_sha256),
                '--sha256 must be a 64-character hexadecimal hash')
        if args.bitmap_sha256:
            args.bitmap_sha256 = args.bitmap_sha256.lower()
        if not args.bitmaps.is_file() and args.stage == 'plan':
            args._bitmap_catalog = {'files': {}, 'identities': {}, 'expected': {}, 'groups': []}
        else:
            os.sched_setaffinity(0, args.prep_ids)
            try:
                download_archive(args)
                args._bitmap_catalog = inspect_archive(args.bitmaps)
            finally:
                os.sched_setaffinity(0, allowed)
        args._bitmap_root = args.data_root / 'not-imported'
    for p in (args.output, args.data_root):
        require(p != PACKAGE and PACKAGE not in p.parents and p not in PACKAGE.parents,
                'Use dedicated data/output directories outside the delivery package and its ancestors')
    require(args.output != args.data_root and args.output not in args.data_root.parents and
            args.data_root not in args.output.parents, 'Data and output directories must be separate')
    selected = nodes(args)
    require(selected, 'No nodes selected')
    configure_sources(args, selected)
    plan = {'workers': 16, 'repeats': 3, 'cpu_affinity': args.cpu_ids, 'preparation_cpus': args.prep_ids,
            'automatic_sources': args._managed_sources,
            'scope': args.scope, 'nodes': [dict(n, estimated_RAM_GiB=round(memory_estimate(n), 2),
                                             blockers=blockers(args, n, True),
                                             source_role=source_for(args, n)[0],
                                             source_directory=str(source_for(args, n)[1]) if source_for(args, n)[1] else None,
                                             data_directory=str(dataset_dir(args, n))) for n in selected]}
    if args.stage == 'plan':
        if args.bitmaps and not args.bitmaps.is_file():
            plan['bitmap_download'] = 'Deferred until execution; ZIP contents cannot be checked before download'
            plan['bitmap_download_source'] = args.url or BITMAP_REPOSITORY
            plan['bitmap_archive_name'] = args.bitmaps.name
        print(json.dumps(plan, indent=2))
        return 0
    require(shutil.which('taskset') and Path('/usr/bin/time').is_file(), 'Linux taskset and GNU /usr/bin/time are required')
    args.data_root.mkdir(parents=True, exist_ok=True)
    lockfile = args.lock_file or args.data_root / '.experiment.lock'
    require(lockfile.parent.is_dir(), 'Lock directory does not exist')
    with lockfile.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.sched_setaffinity(0, args.prep_ids)
        try:
            prepare_sources(args)
        finally:
            os.sched_setaffinity(0, allowed)
        if args.bitmaps and args.stage in ('all', 'run'):
            from run_experiments_with_bitmaps import import_archive
            os.sched_setaffinity(0, args.prep_ids)
            try:
                import_archive(args)
            finally:
                os.sched_setaffinity(0, allowed)
        elif args.bitmaps:
            args._archive_sha256 = sha256(args.bitmaps)
            args._bitmap_root = args.data_root / ('prebuilt-' + args._archive_sha256[:20])
        args.output.mkdir(parents=True, exist_ok=True)
        configuration = {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()
                         if not k.startswith('_') and k not in ('stage', 'memory_gib', 'headroom_gib', 'stop_below_gib', 'disk_headroom_gib',
                                      'timeout', 'prepare_timeout', 'lock_file')}
        if args.bitmaps:
            configuration['archive_sha256'] = args._archive_sha256
        sources = {}
        for n in selected:
            role, source = source_for(args, n)
            if source and (source / 'CMakeLists.txt').is_file() and role not in sources:
                sources[role] = source_identity(source)
        if not args.bitmaps and (args.rabit_root / 'CMakeLists.txt').is_file() and 'rabit' not in sources:
            sources['rabit'] = source_identity(args.rabit_root)
        driver_files = {str(p.relative_to(PACKAGE)): sha256(p) for p in
                        [Path(__file__), HELPERS / 'prepare_inputs.py',
                         PACKAGE / 'experiment_script/experiment_image_script/plot_section51_server100M.py',
                         PACKAGE / 'experiment_script/experiment_image_script/plot_section52.py',
                         PACKAGE / 'experiment_script/experiment_image_script/plot_section51_space.py',
                         PACKAGE / 'experiment_config/benchmark-config.json', *sorted((PACKAGE / 'source_patches').glob('*.patch'))]}
        if args.bitmaps:
            driver_files['run_experiments_with_bitmaps.py'] = sha256(PACKAGE / 'run_experiments_with_bitmaps.py')
        manifest_path = args.output / 'manifest.json'
        if manifest_path.exists():
            state = json.loads(manifest_path.read_text())
            require(state['configuration'] == configuration and state['sources'] == sources
                    and state['driver_files'] == driver_files,
                    'Configuration or source changed; use a new output directory')
        else:
            require(not any(args.output.iterdir()), 'Output must be empty or contain a compatible manifest')
            state = {'configuration': configuration, 'sources': sources, 'driver_files': driver_files,
                     'nodes': {}, 'binaries': {},
                     'machine': {'platform': sys.platform, 'python': sys.version,
                                 'cpuinfo': Path('/proc/cpuinfo').read_text(),
                                 'meminfo': Path('/proc/meminfo').read_text()},
                     'note': 'Fresh native cohort; initial OS cache is not forcibly flushed; no live-index LRU'}
            write_json(manifest_path, state)
        (args.output / 'report.json').unlink(missing_ok=True)
        if args.stage == 'plot':
            report = plot_fresh(args, selected, state)
            return 0 if report['status'] == 'selected nodes complete' else 2
        binaries = {}
        built_roots = {}
        for role, source in sources.items():
            explicit = getattr(args, {'rabit': 'rabit_binary', 'cubit': 'cubit_binary',
                                      're51': 're51_binary', 're52': 're_binary',
                                      'rabit51': 'rabit51_binary', 'cubit51': 'cubit51_binary'}[role])
            if role != 'rabit' and not any(source_for(args, n)[0] == role and source_issue(args, n) is None for n in selected):
                continue
            build = args.output / 'builds' / role
            binary = explicit or built_roots.get(source['root']) or build / 'nicolas'
            if not binary.is_file() and args.stage in ('all', 'build', 'prepare'):
                require(not explicit, 'Explicit binary is missing: ' + str(binary))
                require(shutil.which('cmake'), 'Install CMake and native dependencies before building')
                execute(args, ['cmake', '-S', source['root'], '-B', build, '-DCMAKE_BUILD_TYPE=Release'],
                        args.output / 'build-logs' / (role + '-configure-' + str(time.time_ns())), args.prep_ids)
                execute(args, ['cmake', '--build', build, '--target', 'nicolas', '-j2'],
                        args.output / 'build-logs' / (role + '-build-' + str(time.time_ns())), args.prep_ids)
            if binary.is_file():
                digest = sha256(binary)
                old = state['binaries'].get(role)
                require(not old or old['sha256'] == digest, 'Binary changed; use a new output directory')
                state['binaries'][role] = {'path': str(binary), 'sha256': digest,
                                           'origin': 'caller supplied; source association asserted by caller' if explicit else 'built by this entry'}
                binaries[role] = binary
                built_roots[source['root']] = binary
        for role, source in sources.items():
            require(source_identity(Path(source['root'])) == source, 'Source changed during build: ' + role)
        write_json(manifest_path, state)
        if args.stage == 'build':
            return 0
        if not args.bitmaps and args.stage in ('all', 'prepare') and '51' in args.sections and not args.nodes:
            prepare_space_index(args, selected, binaries, state, allowed)
        preparation_errors = {}
        for n in selected:
            node = state['nodes'].setdefault(n['id'], {'trials': {}, 'failures': 0})
            if node.get('integrity_failed'):
                print('SKIP', n['id'], 'integrity failure retained; use a new cohort after inspection', flush=True)
                continue
            if node['failures'] >= 2:
                node.update(status='blocked', reason='Two failures recorded; inspect them before starting a new cohort')
                write_json(manifest_path, state)
                continue
            reasons = blockers(args, n, args.stage in ('all', 'prepare'))
            if reasons:
                node.update(status='waiting', reason='; '.join(reasons))
                print('SKIP', n['id'], node['reason'], flush=True)
                write_json(manifest_path, state)
                continue
            role, _ = source_for(args, n)
            data_key = str(dataset_dir(args, n)) + '/' + encoding_name(n)
            preparing_phase = True
            try:
                require(role in binaries, 'No compiled binary for ' + role)
                require(data_key not in preparation_errors, preparation_errors.get(data_key, ''))
                if not args.bitmaps and args.stage in ('all', 'prepare'):
                    require('rabit' in binaries or (args.output / 'builds/rabit/nicolas').exists() or args.rabit_binary,
                            'Provide --rabit-binary for index construction when selecting only EE/RE nodes')
                    builder = binaries.get('rabit') or args.rabit_binary or args.output / 'builds/rabit/nicolas'
                    require(builder.is_file(), 'Native RABIT index builder is unavailable')
                    origin = sha256(builder)
                    os.sched_setaffinity(0, args.prep_ids)
                    try:
                        if n['encoding'] != 'RE':
                            prepare_index(args, n, 'EE', 0, builder, origin, state)
                        if n['encoding'] in ('RE', 'GE'):
                            prepare_index(args, n, n['encoding'], n['G'], builder, origin, state)
                    finally:
                        os.sched_setaffinity(0, allowed)
                inputs = check_existing_inputs(args, n)
                if 'inputs' in node:
                    integrity(node['inputs'] == inputs, 'Input identity changed; use a new cohort')
                else:
                    node['inputs'] = inputs
                preparing_phase = False
                if args.stage == 'prepare':
                    node['status'] = 'prepared'
                    write_json(manifest_path, state)
                    continue
                for trial in (1, 2, 3):
                    key = str(trial)
                    if key in node['trials']:
                        old = node['trials'][key]
                        raw = args.output / old['raw_log']
                        integrity(sha256(raw) == old['raw_sha256'], 'Completed raw log changed')
                        rate, counts = parse_result(raw.read_text(errors='replace'), 16, n['operations'])
                        require(math.isclose(rate, old['rate'], rel_tol=1e-12) and counts == old['counts'], 'Completed trial record differs from raw log')
                        integrity(sha256(args.output / old['stderr_log']) == old['stderr_sha256'], 'Completed trial stderr changed')
                        continue
                    integrity(all(sha256(PACKAGE / rel) == digest for rel, digest in driver_files.items()),
                            'Experiment or plotting script changed during run')
                    integrity(sha256(binaries[role]) == state['binaries'][role]['sha256'], 'Binary changed during run')
                    reasons = blockers(args, n)
                    require(not reasons, '; '.join(reasons))
                    prefix = args.output / 'raw_data' / f"{n['id']}-trial{trial}-{time.time_ns()}"
                    environment = {'RABIT_RE_CLEAN_UNUSED_CACHE': '1'} if n['section'] == '51' and n['encoding'] == 'RE' else {}
                    text, measurement = execute(args, trial_command(args, n, binaries[role]), prefix, args.cpu_ids, environment)
                    rate, counts = parse_result(text, 16, n['operations'])
                    node['trials'][key] = {'rate': rate, 'counts': counts,
                                           'raw_log': str(prefix.with_suffix('.stdout').relative_to(args.output)),
                                           'raw_sha256': measurement['stdout_sha256'],
                                           'stderr_log': str(prefix.with_suffix('.stderr').relative_to(args.output)),
                                           'stderr_sha256': measurement['stderr_sha256'],
                                           'wall_seconds': measurement['wall_seconds'], 'max_rss_kib': measurement['max_rss_kib']}
                    node['status'] = 'running' if trial < 3 else 'complete'
                    write_json(manifest_path, state)
                    print('DONE', n['id'], f'trial {trial}/3', flush=True)
                integrity(check_existing_inputs(args, n) == node['inputs'], 'Benchmark modified immutable inputs')
            except (RuntimeError, OSError, ValueError, KeyError) as exc:
                node.update(status='failed', reason=str(exc), failures=node['failures'] + 1)
                if isinstance(exc, IntegrityError):
                    node['integrity_failed'] = True
                if preparing_phase:
                    preparation_errors[data_key] = str(exc)
                print('FAILED', n['id'], str(exc), flush=True)
            write_json(manifest_path, state)
        if args.stage == 'prepare':
            return 0 if all(state['nodes'].get(n['id'], {}).get('status') == 'prepared' for n in selected) else 2
        report = plot_fresh(args, selected, state)
        return 0 if report['status'] == 'selected nodes complete' else 2


def cli(argv=None, with_bitmaps=False):
    def interrupted(signum, frame):
        raise KeyboardInterrupt('Interrupted by signal ' + str(signum))
    signal.signal(signal.SIGTERM, interrupted)
    try:
        return main(argv, with_bitmaps=with_bitmaps)
    except (RuntimeError, OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print('STOP:', exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('STOP: interrupted; completed recorded trials are preserved.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(cli())
