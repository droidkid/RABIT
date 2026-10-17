### Runtime and resource estimates

Figures are approximate references from shared-server runs; another host may differ. Section 5.2 used 16 workers on CPUs 0–7, 1000 attempts per worker and three trials per point.

- Time includes loading/initialization, excluding input generation, waiting and failed attempts.
- Disk covers bitmap payload; GE includes shared EE. Add raw columns, temporary files, metadata and headroom.
- RSS is the highest recorded trial peak, rounded up to 0.1 GiB; it excludes swapped pages. Allow extra host RAM.

#### Section 5.1: 100 million rows, C=1000

| Method | One trial, approx. min | Three trials, approx. min | Index disk, approx. GiB | Peak RSS |
|---|---:|---:|---:|---|
| EE | 0.34–1.63 | 1.03–4.89 | 0.723 | Not recorded |
| RE | 0.59–10.79 | 1.77–32.36 | 11.636 | Not recorded |
| GE50 | 0.11–0.21 | 0.32–0.63 | 0.953 | Not recorded |
| GE100 | 0.18–0.31 | 0.55–0.94 | 0.843 | Not recorded |
| GE200 | — | — | 0.783 | Not recorded |

Ranges cover the mean single-trial time and three-trial total across each method's ten configurations. The 120 trials total about **3.27 hours**, including initialization and excluding input generation. Section 5.1 uses 200 attempts per worker. GE200 is used only in the space comparison; peak RSS and historical CPU affinity were not recorded.

#### Section 5.2, Figure 9(a): 100 million rows

| Cardinality C | Method | One trial, approx. min | Three trials, approx. min | Index disk, approx. GiB | Reference peak RSS, GiB |
|---|---|---:|---:|---:|---:|
| 100 | GE-C/20 | 0.7 | 2.1 | 0.788 | 2.3 |
| 100 | GE-C/10 | 1.3 | 3.9 | 0.677 | 2.0 |
| 100 | EE | 3.4 | 10.3 | 0.557 | 4.8 |
| 100 | RE | 3.1 | 9.2 | 1.162 | 3.9 |
| 1000 | GE-C/20 | 0.8 | 2.3 | 0.953 | 3.6 |
| 1000 | GE-C/10 | 1.8 | 5.4 | 0.843 | 3.4 |
| 1000 | EE | 5.2 | 15.5 | 0.723 | 17.4 |
| 1000 | RE | 26.9 | 80.6 | 11.636 | 30.9 |
| 10000 | GE-C/20 | 1.3 | 4.0 | 0.973 | 13.0 |
| 10000 | GE-C/10 | 6.9 | 20.6 | 0.863 | 12.7 |

Add one 0.373 GiB raw column per cardinality. Repeats and GE variants share applicable initial inputs.

For Figure 9(a) RE at C=1000 with the recorded workload, admission uses the 30.9 GiB reference peak plus 15%, rounded up to 35.6 GiB. With the default 4 GiB host headroom, this requires at least 39.6 GiB available RAM. This is an estimate for admission; runtime memory guards remain active. Other RE workloads retain the analytical estimate.

#### Section 5.2, Figure 9(b): 1 billion rows, C=1000

| Query width | Method | One trial, approx. min | Three trials, approx. min | Index disk, approx. GiB | Reference peak RSS, GiB |
|---|---|---:|---:|---:|---:|
| 5% | GE-C/20 | 5.7 | 17.2 | 9.531 | 34.8 |
| 5% | GE-C/10 | 27.6 | 82.8 | 8.428 | 32.1 |
| 15% | GE-C/20 | 8.9 | 26.6 | 9.531 | 34.8 |
| 15% | GE-C/10 | 29.6 | 88.9 | 8.428 | 32.1 |
| 25% | GE-C/20 | 12.1 | 36.3 | 9.531 | 34.8 |
| 25% | GE-C/10 | 30.5 | 91.6 | 8.428 | 32.1 |
| 35% | GE-C/20 | 15.5 | 46.4 | 9.531 | 34.8 |
| 35% | GE-C/10 | 31.5 | 94.4 | 8.428 | 32.1 |
| 45% | GE-C/20 | 18.5 | 55.5 | 9.531 | 34.8 |
| 45% | GE-C/10 | 32.8 | 98.3 | 8.428 | 32.1 |

Add a 3.725 GiB raw column. Widths/repeats share inputs. Both GE variants together occupy about 10.731 GiB of unique bitmap payload, including EE. A reference run of 60 trials took about 13.2 hours (A: 2.6, B: 10.6), excluding generation, waiting and unsuccessful attempts.

#### Requirements for the 16 omitted Figure 9 points

These are analytical estimates. **RAM values are components, not full-process peaks or sufficient host sizes.** Allow additional query/update state, caches and operating-system memory.

| Missing configuration | Points | Index disk, approx. GiB | Native RAM component, approx. GiB |
|---|---:|---:|---:|
| 9(a), C=10000, EE | 1 | 0.743 | 133.5 retained array capacity |
| 9(a), C=100000, EE | 1 | 0.745 | 1335.1 retained array capacity |
| 9(a), C=100000, GE, G=5000 | 1 | 0.975, including EE | 92.7 |
| 9(a), C=100000, GE, G=10000 | 1 | 0.865, including EE | 92.6 |
| 9(a), C=10000, RE | 1 | 116.4 | 116.4 bitmap payload under eager loading |
| 9(a), C=100000, RE | 1 | 1163.6 | 1163.6 bitmap payload under eager loading |
| 9(b), EE, five query widths | 5 | 7.228 shared | 133.5 retained array capacity |
| 9(b), RE, five query widths | 5 | 116.4 shared | 116.4 bitmap payload under eager loading |

Current runner admission estimates (GiB), rounded up; these are not measured peaks. Host RAM includes the default 4 GiB headroom:

| Missing configurations | Process budget (`--memory-gib`), at least | Available host RAM |
|---|---:|---:|
| 9(a), C=100000, both GE variants | 123.4 | 127.4 |
| 9(a), C=10000 EE; 9(b), EE | 166.2 | 170.2 |
| 9(a), C=100000 EE | 1609.0 | 1613.0 |
| 9(a), C=10000 RE; 9(b), RE | 422.5 | 426.5 |
| 9(a), C=100000 RE | 4194.4 | 4198.4 |

These are per-node admission conditions. `plan` reports the current estimates and blockers; sufficient resources do not guarantee trial completion.

Add raw columns, temporary files and headroom. The 1B-row RE build alone needs about 123.6 GiB for RE plus temporary EE. The builder's sorting component is about 1.9 GiB for 100M rows or 18.7 GiB for 1B rows, before indexes and other overhead. Widths and repeats reuse one initial index per method.

Use the [omitted-point instructions](README.md#include-omitted-points), matching source patches, and a sufficiently large input volume. The default 48 GiB process budget excludes these points. Builds use TMPDIR-aware source. Runtime uses eager loading; it does not implement the paper's 64GB live-index LRU. Missing-node completion times are not measured.
