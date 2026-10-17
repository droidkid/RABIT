#!/usr/bin/env python3
"""Plot newly collected Figure 9 measurements with fixed paper axes."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
from datetime import datetime, timezone

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import FixedLocator, FixedFormatter, NullLocator

STYLES = [
    ('GE-C/20', r'GE-$(C/20)$', 'blue', 'o', '-'),
    ('GE-C/10', r'GE-$(C/10)$', 'navy', 's', '-'),
    ('EE', 'EE', 'firebrick', '^', '--'),
    ('RE', 'RE', 'seagreen', 'v', '-.'),
]
Y_TICKS = [0.1, 1, 10, 100]
Y_LABELS = [r'$10^{-1}$', '1', '10', r'$10^2$']
X_TICKS = {'a': [100, 1000, 10000, 100000], 'b': [5, 15, 25, 35, 45]}
X_LABELS = {'a': [r'$10^2$', r'$10^3$', r'$10^4$', r'$10^5$'],
            'b': ['5', '15', '25', '35', '45']}
Y_LIMITS = {'a': [0.1, 500], 'b': [0.1, 100]}
CAPTIONS = {'a': '(a) 15% query range, 20% UDI ratio',
            'b': '(b) 1B entries, 1000 cardinality'}


def points_from_rows(rows, panel, expected_attempts, expected_cpus):
    groups = {}
    identities = set()
    for row in rows:
        key = (row['method'], row['x'], row['trial'])
        if key in identities:
            raise ValueError('Duplicate trial')
        identities.add(key)
        if row['method'] not in {s[0] for s in STYLES} or row['x'] not in X_TICKS[panel]:
            raise ValueError('Unexpected curve or x coordinate')
        if row['total_attempts'] != expected_attempts:
            raise ValueError('Unexpected workload size')
        if 'workers' in row and row['workers'] != 16:
            raise ValueError('Unexpected worker count')
        if 'cpu_affinity' in row and row['cpu_affinity'] != expected_cpus:
            raise ValueError('Unexpected CPU affinity')
        value = row['sum_reported_worker_op_s']
        if not math.isfinite(value) or value <= 0:
            raise ValueError('Invalid throughput')
        groups.setdefault((row['method'], row['x']), []).append(row)
    points = []
    for (curve, x), trials in sorted(groups.items()):
        if len(trials) != 3 or {r['trial'] for r in trials} != {1, 2, 3}:
            raise ValueError('A plotted point must have three complete trials')
        value = statistics.mean(r['sum_reported_worker_op_s'] for r in trials)
        if not Y_LIMITS[panel][0] <= value <= Y_LIMITS[panel][1]:
            raise ValueError('A real value is outside the fixed paper range; no clipping or rescaling allowed')
        points.append({'curve': curve, 'x': x, 'mean_op_s': value, 'trials': 3})
    return points


def draw(axis, panel, points):
    for curve, label, color, marker, linestyle in STYLES:
        available = {p['x']: p['mean_op_s'] for p in points if p['curve'] == curve}
        if not available:
            continue
        xs = X_TICKS[panel]
        ys = [available.get(x, math.nan) for x in xs]
        axis.plot(xs, ys, label=label, color=color, marker=marker,
                  linestyle=linestyle, linewidth=2.0, markersize=9,
                  markerfacecolor='none', markeredgewidth=1.9, clip_on=False)
    axis.set_xscale('log' if panel == 'a' else 'linear')
    axis.set_yscale('log')
    axis.set_xlim(X_TICKS[panel][0], X_TICKS[panel][-1])
    axis.set_ylim(*Y_LIMITS[panel])
    axis.xaxis.set_major_locator(FixedLocator(X_TICKS[panel]))
    axis.xaxis.set_major_formatter(FixedFormatter(X_LABELS[panel]))
    axis.yaxis.set_major_locator(FixedLocator(Y_TICKS))
    axis.yaxis.set_major_formatter(FixedFormatter(Y_LABELS))
    axis.xaxis.set_minor_locator(NullLocator())
    axis.yaxis.set_minor_locator(NullLocator())
    axis.tick_params(direction='in', top=True, right=True, length=8, width=.75,
                     pad=7, color='0.55')
    axis.grid(False)
    axis.set_xlabel('Cardinality' if panel == 'a' else 'Query range (% of cardinality)')
    axis.set_ylabel('Throughput (op/s)')
    assert list(axis.get_xticks()) == X_TICKS[panel]
    assert list(axis.get_yticks()) == Y_TICKS
    assert list(axis.get_ylim()) == Y_LIMITS[panel]
    return {'xscale': axis.get_xscale(), 'yscale': axis.get_yscale(),
            'xlim': list(axis.get_xlim()), 'ylim': list(axis.get_ylim()),
            'xticks': [float(v) for v in axis.get_xticks()], 'yticks': [float(v) for v in axis.get_yticks()],
            'xtick_labels': X_LABELS[panel], 'ytick_labels': Y_LABELS,
            'missing_nodes': [{'curve': curve, 'x': x} for curve, *_ in STYLES
                              for x in X_TICKS[panel]
                              if not any(p['curve'] == curve and p['x'] == x for p in points)],
            'points': points}


def export(fig, output, stem):
    fig.savefig(output / (stem + '.png'), dpi=220)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--require-complete', action='store_true',
                        help='Reject input unless all 36 nodes have three complete trials; never launches experiments')
    parser.add_argument('--cpu-list', default='0,1,2,3,4,5,6,7',
                        help='Recorded benchmark CPU IDs, not a request to start or bind a benchmark')
    parser.add_argument('--attempts-per-worker', type=int, default=1000,
                        help='Recorded local workload protocol; not an established paper requirement')
    args = parser.parse_args()
    try:
        recorded_cpus = [int(value) for value in args.cpu_list.split(',')]
    except ValueError:
        parser.error('--cpu-list must contain comma-separated nonnegative integers')
    if not recorded_cpus or min(recorded_cpus) < 0 or len(set(recorded_cpus)) != len(recorded_cpus):
        parser.error('--cpu-list must contain unique nonnegative CPU IDs')
    if args.attempts_per_worker <= 0:
        parser.error('--attempts-per-worker must be positive')
    if args.source.resolve() == args.output.resolve():
        parser.error('--source and --output must be different directories')
    try:
        allowed_cpus = sorted(os.sched_getaffinity(0))
        os.sched_setaffinity(0, allowed_cpus[:2])
    except (AttributeError, OSError):
        pass
    source_bytes = {}
    point_sets = {}
    for panel in ('a', 'b'):
        filename = f'figure9{panel}-8cpu-samples.json'
        raw = (args.source / filename).read_bytes()
        source_bytes[filename] = raw
        points = points_from_rows(json.loads(raw), panel,
                                  16 * args.attempts_per_worker, recorded_cpus)
        if args.require_complete and len(points) != 4 * len(X_TICKS[panel]):
            raise ValueError(f'Panel {panel} is incomplete: {len(points)}/{4 * len(X_TICKS[panel])} points')
        point_sets[panel] = points
    args.output.mkdir(parents=True, exist_ok=True)
    ordered_cpus = sorted(recorded_cpus)
    cpu_caption = (f'{ordered_cpus[0]}-{ordered_cpus[-1]}'
                   if len(ordered_cpus) > 1 and ordered_cpus == list(range(ordered_cpus[0], ordered_cpus[-1] + 1))
                   else ','.join(str(value) for value in recorded_cpus))
    trial_caption = f'Native paths; mean of 3 trials; 16 workers, CPUs {cpu_caption}. Missing points left blank.'
    plt.rcParams.update({'font.family': 'DejaVu Serif', 'mathtext.fontset': 'dejavuserif',
                         'font.size': 14, 'axes.labelsize': 19,
                         'xtick.labelsize': 16, 'ytick.labelsize': 16})
    metadata = {'generated_at': datetime.now(timezone.utc).isoformat(),
                'source_directory': str(args.source.resolve()),
                'cohort': 'newly collected measurements',
                'workers': 16, 'logical_cpus': recorded_cpus, 'trials_per_point': 3,
                'attempts_per_worker': args.attempts_per_worker,
                'operation_count_basis': 'local protocol; paper Figure 9 mixed-attempt count remains unconfirmed',
                'require_complete': args.require_complete,
                'experimental_values_modified': False, 'missing_points_fabricated': False,
                'source_hashes': {}, 'panels': {}}
    for panel in ('a', 'b'):
        filename = f'figure9{panel}-8cpu-samples.json'
        raw = source_bytes[filename]
        metadata['source_hashes'][filename] = hashlib.sha256(raw).hexdigest()
        points = point_sets[panel]
        fig, axis = plt.subplots(figsize=(7.2, 5.1))
        metadata['panels'][panel] = draw(axis, panel, points)
        axis.legend(loc='lower center', bbox_to_anchor=(.5, 1.01), ncol=2,
                    frameon=False, fontsize=13, handlelength=2.0)
        fig.subplots_adjust(left=.16, right=.94, bottom=.27, top=.78)
        fig.text(.55, .105, CAPTIONS[panel], ha='center', fontsize=13)
        fig.text(.16, .035, trial_caption, fontsize=8)
        export(fig, args.output, f'figure9{panel}-native-paper-axes')
        plt.close(fig)
    fig, axes = plt.subplots(1, 2, figsize=(12.8, 5.1))
    for panel, axis in zip(('a', 'b'), axes):
        draw(axis, panel, point_sets[panel])
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc='upper center', bbox_to_anchor=(.5, .99),
               ncol=4, frameon=False, fontsize=16, handlelength=2.0)
    fig.subplots_adjust(left=.09, right=.97, bottom=.28, top=.79, wspace=.43)
    fig.text(.29, .105, CAPTIONS['a'], ha='center', fontsize=13)
    fig.text(.78, .105, CAPTIONS['b'], ha='center', fontsize=13)
    fig.text(.09, .035, trial_caption, fontsize=9)
    export(fig, args.output, 'figure9-native-paper-axes')
    plt.close(fig)
    for filename, raw in source_bytes.items():
        if (args.source / filename).read_bytes() != raw:
            raise ValueError('Historical source changed during rendering')
    metadata['plot_script_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    metadata['artifact_sha256'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in args.output.iterdir() if p.suffix in ('.png', '.pdf', '.svg')}
    (args.output / 'plot-metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print(args.output / 'figure9a-native-paper-axes.png')
    print(args.output / 'figure9b-native-paper-axes.png')
    print(args.output / 'figure9-native-paper-axes.png')


if __name__ == '__main__':
    main()
