### Sections 5.1 and 5.2

Requires x86-64 Linux, Git, C++17, CMake, Boost filesystem/program_options/system, liburcu, `taskset` and GNU `/usr/bin/time`. Install the Python packages with `python -m pip install -r experiment/requirements.txt` (Python 3.11 recommended).

#### Run the experiments

From the RABIT repository root:

```sh
./run_experiments_all.py
```

The script prepares the required source versions, applies their patches, builds the programs, generates inputs, runs three trials per point, and produces Table 2 and Figures 8 and 9. Source preparation reuses local Git objects where available; otherwise it downloads the pinned revisions from the droidkid RABIT/CUBIT forks. These revisions support local Boost/liburcu installations via CMAKE_PREFIX_PATH; see the repository README for environment setup. No manual checkout or patch command is needed. Use fresh data/output directories when switching from the upstream pins.

Inputs and prepared sources are stored under `~/rabit-inputs/`; new figures are in `~/rabit-fresh-results/figures/` (`figure8.png`, `figure9.png`, `table2.png` and individual panels). Use `--data-root` and `--output` to change them. Repeating the same command reuses verified inputs, prepared sources and completed trials. After changing scripts or source versions, use a new output directory.

#### Use pre-generated bitmaps

```sh
./run_experiments_with_bitmaps.py bitmaps.zip
```

If the local ZIP is absent, the script downloads a ZIP with the same filename from [Bitmap-dataset](https://github.com/junchangwang/Bitmap-dataset), checking its main branch and published releases. It then prepares the programs, imports the bitmaps, runs the experiments and generates the figures, skipping raw-column and bitmap generation. An existing local ZIP is reused.

To use a different download address:

```sh
./run_experiments_with_bitmaps.py bitmaps.zip --url "https://<host>/<path>/bitmaps.zip"
```

Use a direct ZIP URL. `--sha256 <hash>` optionally checks the archive. The ZIP may contain `section51/N<rows>/C<C>/<EE|RE|GE-G>/` and equivalent `section52/` directories. Legacy `BM_100M_1000_GE_50_32/` names are accepted. GE needs matching EE files; Table 2 also needs Section 5.1 GE-200. For a Section 5.2-only ZIP, add `--sections 52`.

#### Include omitted points

The default selects 40 Section 5.1 and 20 Section 5.2 configurations. Trials use 16 workers, at most eight logical CPUs (`--cpus 0-7`) and three repeats; preparation uses at most two CPUs (`--prep-cpus 0-1`). The process memory budget defaults to 48 GiB (`--memory-gib`). Set allocations for the host; see [resource estimates](RESOURCE_USAGE.md).

No source edit is required. To include all 16 omitted Figure 9 points:

```sh
./run_experiments_all.py plan --sections 52 --scope all
./run_experiments_all.py all --sections 52 --scope all \
  --memory-gib RAM_GIB --data-root /path/to/inputs --output ~/rabit-full-results
```

Replace `RAM_GIB` and the input path using the per-node [RAM/disk requirements](RESOURCE_USAGE.md#requirements-for-the-16-omitted-figure-9-points), including host headroom and temporary space. The default 48 GiB budget excludes these points. Adjust `--timeout` / `--prepare-timeout` if needed.

Add `--nodes 52a-EE-X10000` to select one point; omit `--sections 52` to run both sections. The bitmap entry accepts the same options (`--stage plan` for inspection), but requires the requested indexes in its ZIP; otherwise use the full entry above to generate them.

`experiment_config/benchmark-config.json` defines the grids. Section 5.1 uses 200 attempts per worker; Section 5.2 uses 1000. Throughput averages three sums of worker-reported rates. The implementation uses eager loading without the paper's live-index LRU. Source patches are applied automatically from `source_patches/`. Figures are generated from the measurements collected by the current run.

`experiment_data/` contains previous measurement tables; `experiment_image/generated/` contains their figures. They are retained for reference. New experiments use their own measurements and output directory.
