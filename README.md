
### Brief history of RABIT

Despite widespread endorsement of bitmap indexing for range queries (RQs) in many classic textbooks, its use has long been restricted to read-only, low-cardinality attributes. For example, Oracle database manuals suggest that "[DBAs] should keep in mind that bitmap indexes are usually easier to destroy and re-create than to maintain" [1], and Sybase IQ's bitmap indexes are "recommended for up to 1,000 distinct values" [2].

We therefore conduct a comprehensive study on existing bitmap indexing techniques. Our analysis reveals three key limitations: (1) the prevailing encoding schemes (equality encoding and range encoding) are ill-suited for RQs; (2) current bitvector merge operations are inefficient for RQs; and (3) the most efficient bitmap indexes for RQs (those based on range encoding) incur prohibitively high update costs.

We thus introduce RABIT, the first bitmap index designed for RQs on frequently updated, high-cardinality attributes in HTAP DBMSs. RABIT significantly expands the applicability of bitmap indexing, making it suitable for attributes of any cardinality in tables ranging from read-only to frequently updated.

[1] https://docs.oracle.com/en/database/oracle/oracle-database/23/dwhsg/index.html

[2] https://infocenter.sybase.com/help/index.jsp?topic=/com.sybase.infocenter.dc00170.1510/html/iqapgv1/CIHECEAI.htm


------


### About this project

#### How is this project organized?

The core code resides in the src/bitmaps folder, which contains the baselines, including In-place (src/naive), UCB (src/ucb), UpBit (src/ub), and our algorithm RABIT (src/bitmaps/rabit).  

The file src/bitmaps/rabit/table.cpp contains the implementation of our algorithms. In particular, Rabit::evaluate() and Rabit::range() implement point and range queries, respectively. Our code currently supports three types of encoding schemes: EE, RE, and our GE. The directory src/GTest contains our unit tests. 


#### How to reproduce the key experiments in the RABIT paper?

RABIT relies on c++17, python3, liburcu(-dev), and the Boost library. 

First, use the following command to compile the entire project. 

```sh
python3 -m pip install -r experiment/requirements.txt
./build.sh 
```

Then, reproduce the key results by using the following command. 

```sh
./run_experiments_all.py
```

This script automatically generates all necessary bitmaps, runs the complete evaluation pipeline, and produces the plots for Table 2 and Figures 8 and 9, which together constitute the key experimental results of the RABIT paper.

To run additional experiments, please find the corresponding script in ./experiment/experiment_script.

Note that the datasets are large, and generating all of them may take hours. If you would like to skip this generation step, you can download a pre-generated copy from our repository at https://github.com/junchangwang/Bitmap-dataset (an optional speed-up resource). You can then skip the bitmap-generation procedure and run the experiments directly using the following command:

```sh
./run_experiments_with_bitmaps.py bitmaps.zip
```

## No-sudo SIGMOD builds

Boost and liburcu may be installed under a user-owned prefix. Before building or launching the experiment runner, set:

```bash
export CMAKE_PREFIX_PATH="$HOME/.local/rabit-deps${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"
export LD_LIBRARY_PATH="$HOME/.local/rabit-deps/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
```

Activate the Python environment before running experiments. Preserve these exports in batch jobs. Configure a fresh standalone build with `cmake -S . -B build-local -DCMAKE_BUILD_TYPE=Release`, then `cmake --build build-local -j2`. Check `ldd build-local/nicolas`: Boost and liburcu should resolve to the local installation without missing libraries.

### Publishing experiment bases

The runner pins the local-dependency fixes in the droidkid forks: RABIT `3a9b116d9d9f67936412019129485b4edb9b9c05` and CUBIT `572bb5d5bcfd835026088e113a48a48acfb702b5`. Relative to the original experiment bases (RABIT `63fb6cd474ee6b8466a6fa6c96e4c92c532f91cc` and CUBIT `7eef13d8342bcec2cfb73f35c80281b663dfc040`), benchmark C++ sources are unchanged; dependency discovery is updated in CMake.

Ensure both pinned commits are pushed to reachable branches in https://github.com/droidkid/RABIT.git and https://github.com/droidkid/CUBIT.git before launching on another machine. The RABIT pin references the earlier dependency-fix commit, not the runner commit itself. SOURCE_VARIANTS and the experiment patches are unchanged; all five patches pass `git apply --check` against their new bases.

### Validation

Use fresh data/output directories after changing pins; do not edit managed .sources checkouts. Run `plan`, then `build` with identical options. Confirm all six roles build: rabit, re52, cubit, rabit51, re51, cubit51. Inspect their dependency resolution and build logs. Record original/replacement SHAs, build-only diffs, and compiler/CMake/dependency versions.

Run small GE, RE and EE/CUBIT workloads, including modifications. Require successful exits, complete worker totals and positive throughput. Then run one full-size point with `all --sections 52 --nodes 52a-GE-C-20-X100`, using a separate validation output directory. Repeat that command to check completed-trial reuse. Finally run `all` using the full-suite build output and unchanged options.

The default scope remains 60 configurations with a 48 GiB process budget; the 1 TB host does not automatically enable omitted Figure 9 points. Set --cpus and --prep-cpus to allowed CPU IDs consistently across stages. Check free disk space and the final report for skipped or failed configurations.
