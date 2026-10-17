
import argparse
import csv
from decimal import Decimal
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--formats", nargs="+", choices=("png", "pdf", "svg"), default=["png"])
    args = parser.parse_args()
    sizes = {}
    with args.csv.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            algo = row["algorithm"]
            if algo in sizes:
                raise ValueError(f"Duplicate algorithm: {algo}")
            if row.get("serialized_bitmap_bytes"):
                sizes[algo] = Decimal(row["serialized_bitmap_bytes"]) / Decimal(1000000)
            else:
                sizes[algo] = Decimal(row["MB_decimal"])
    algorithms = ("EE", "RE", "GE50", "GE100", "GE200")
    if any(sizes.get(a, 0) <= 0 for a in algorithms):
        raise ValueError("Need positive measured sizes for EE, RE, GE50, GE100, GE200")
    ee = sizes["EE"]
    relative = ["—", f"{sizes['RE'] / ee:.2f}×"] + [
        f"+{(sizes[a] / ee - 1) * 100:.2f}%" for a in algorithms[2:]]
    plt.rcParams.update({"font.family": "serif",
                         "font.serif": ["Times New Roman", "DejaVu Serif"],
                         "pdf.fonttype": 42, "ps.fonttype": 42,
                         "svg.fonttype": "none"})
    fig, ax = plt.subplots(figsize=(8.0, 1.65))
    fig.subplots_adjust(left=0.025, right=0.975, bottom=0.08, top=0.96)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    for y, width in ((0.94, 1.5), (0.65, 0.75), (0.12, 1.5)):
        ax.plot([0.015, 0.985], [y, y], color="black", lw=width, solid_capstyle="butt")
    xs = [0.32, 0.47, 0.62, 0.77, 0.92]
    for x, title in zip(xs, ("EE", "RE", "GE-50", "GE-100", "GE-200")):
        ax.text(x, 0.79, title, ha="center", va="center", fontsize=14)
    ax.text(0.025, 0.48, "Size (MB)", ha="left", va="center", fontsize=14)
    ax.text(0.025, 0.27, "vs. EE", ha="left", va="center", fontsize=14)
    for x, algo, comparison in zip(xs, algorithms, relative):
        ax.text(x, 0.48, f"{sizes[algo]:,.3f}", ha="center", va="center", fontsize=13)
        ax.text(x, 0.27, comparison, ha="center", va="center", fontsize=13)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    print("Input:", args.csv.resolve())
    for a, value in zip(algorithms, relative):
        print(f"{a}: {sizes[a]} MB; vs. EE {value}")
    for ext in args.formats:
        path = args.out.with_suffix("." + ext)
        fig.savefig(path, dpi=300, facecolor="white", bbox_inches="tight", pad_inches=0.04)
        print("Saved:", path.resolve())
    plt.close(fig)


if __name__ == "__main__":
    main()
