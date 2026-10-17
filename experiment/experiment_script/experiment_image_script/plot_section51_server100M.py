import argparse
import csv
import hashlib
import math
from pathlib import Path
import textwrap

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


STYLES = {
    "GE50": ("GE-50", "#0000dd", "o", "-"),
    "GE100": ("GE-100", "#000088", "s", "-"),
    "EE": ("EE", "#b72b2b", "^", "--"),
    "RE": ("RE", "#298c56", "v", "-."),
}


def read_measurements(path, repeats):
    if not path.is_file():
        raise ValueError(f"CSV file not found: {path}")
    if path.stat().st_size == 0:
        raise ValueError(f"CSV file is empty: {path}. Save the CSV in your editor before running.")
    required = {"algorithm", "udi_pct", "rq_length", "repeats",
                "mean_reported_thread_sum_ops_s", "sample_stdev_ops_s"}
    measurements = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("Missing CSV fields: " + ", ".join(sorted(required - set(reader.fieldnames or []))))
        for row in reader:
            key = (row["algorithm"], int(row["udi_pct"]), int(row["rq_length"]))
            if key in measurements:
                raise ValueError(f"Duplicate CSV configuration: {key}")
            mean = float(row["mean_reported_thread_sum_ops_s"])
            sd = float(row["sample_stdev_ops_s"])
            if not (math.isfinite(mean) and math.isfinite(sd) and 0 <= sd < mean):
                raise ValueError(f"Invalid mean or standard deviation for a log axis: {key}")
            if key[0] in STYLES and int(row["repeats"]) != repeats:
                raise ValueError(f"Repeat count differs from --repeats: {key}")
            measurements[key] = (mean, sd)
    return measurements


def draw_panel(measurements, settings, xs, xlabel, title, stem, args):
    fig, ax = plt.subplots(figsize=(6.6, 4.9))
    try:
        for algorithm, (label, color, marker, line) in STYLES.items():
            values = []
            for udi, length in settings:
                key = (algorithm, udi, length)
                if key not in measurements:
                    raise ValueError(f"Missing plot configuration in CSV: {key}")
                mean, sd = measurements[key]
                if mean - sd < args.ylim[0] or mean + sd > args.ylim[1]:
                    raise ValueError(f"Y-axis limits would clip values or error bars: {key}; adjust --ylim")
                values.append((mean, sd))
            ax.errorbar(xs, [mean for mean, _ in values],
                        yerr=[sd for _, sd in values], label=label,
                        color=color, marker=marker, linestyle=line,
                        markerfacecolor="none", markersize=7, markeredgewidth=1.6,
                        linewidth=1.7, elinewidth=0.9, capsize=2.5)

        ax.set_yscale("log")
        ax.set_ylim(*args.ylim)
        ax.set_xticks(xs)
        ax.set_xlim(xs[0], xs[-1])
        ax.set_xlabel(xlabel)
        ax.set_ylabel("Throughput (op/s)")
        ax.tick_params(which="both", direction="in", top=True, right=True)
        ax.set_title(title)
        handles, labels = ax.get_legend_handles_labels()
        fig.legend(handles, labels, frameon=False, ncol=4,
                   loc="upper center", bbox_to_anchor=(0.5, 0.995), fontsize=11)
        caption = (f"N={args.rows / 1000000:g}M; C={args.cardinality:,}; "
                   f"{args.workers} workers; {args.operations} ops/worker; "
                   f"{args.repeats} repeats")
        note = "Error bars: sample SD; benchmark RDTSC timing."
        if args.note:
            note = args.note + " " + note
        fig.text(0.5, 0.065, caption, ha="center", fontsize=8.5)
        fig.text(0.5, 0.015, textwrap.fill(note, width=105), ha="center", fontsize=8)
        fig.subplots_adjust(left=0.13, right=0.97, bottom=0.22, top=0.86)
        for extension in args.formats:
            path = args.out_dir / f"{stem}.{extension}"
            fig.savefig(path, dpi=300, facecolor="white", bbox_inches="tight")
            print(f"Saved: {path}")
    finally:
        plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description="Plot section 5.1 measurements as two separate figures.")
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=100000000)
    parser.add_argument("--cardinality", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--operations", type=int, default=200)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--ylim", type=float, nargs=2, default=[1, 2000], metavar=("MIN", "MAX"))
    parser.add_argument("--note", default="")
    parser.add_argument("--formats", nargs="+", choices=("png", "pdf", "svg"), default=["png"])
    args = parser.parse_args()
    if min(args.rows, args.cardinality, args.workers, args.operations) < 1 or args.repeats < 2:
        parser.error("Experiment sizes must be positive; repeats must be at least 2")
    if not (all(math.isfinite(v) for v in args.ylim) and 0 < args.ylim[0] < args.ylim[1]):
        parser.error("Log-axis limits must be positive and increasing")
    percentages = (5, 15, 25, 35, 45)
    if any(args.cardinality * pct % 100 for pct in percentages):
        parser.error("Cardinality cannot represent the required query percentages exactly")
    args.csv = args.csv.resolve()
    args.out_dir = args.out_dir.resolve()
    measurements = read_measurements(args.csv, args.repeats)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Input CSV: {args.csv}")
    print(f"CSV SHA256: {hashlib.sha256(args.csv.read_bytes()).hexdigest()}")
    print("rows/workers/operations are caption settings absent from the summary CSV; verify your configuration.")
    plt.rcParams.update({"font.family": "DejaVu Serif", "font.size": 12,
                         "pdf.fonttype": 42, "axes.linewidth": 1.0})
    ratios = [0, 10, 20, 30, 40, 50]
    length15 = args.cardinality * 15 // 100
    draw_panel(measurements, [(udi, length15) for udi in ratios], ratios,
               "UDI ratio (%)", "(a) 15% query range",
               "section51_server100M_fig8_panel1", args)
    draw_panel(measurements, [(20, args.cardinality * pct // 100) for pct in percentages],
               percentages, "Query range (% of cardinality)", "(b) 20% UDI ratio",
               "section51_server100M_fig8_panel2", args)


if __name__ == "__main__":
    main()
