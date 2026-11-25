# Read-cost benchmark

What a read of a ready texture costs, and where the time goes. Stage M0 of
[docs/developer/hybrid-read-path-plan.md](../../../docs/developer/hybrid-read-path-plan.md);
the results are recorded there.

A replay ([../replay.py](../replay.py)) measures a whole flight. This measures
one thing closely: the same reads, made the way X-Plane makes them, against the
cache files themselves and against three file systems on the same kernel
driver.

| File | What it does |
|---|---|
| `readbench.c` | The client. Opens each texture, reads the header piece, then from a level's start to the end in 64 KB pieces, and times every call. Stops at the file size, as X-Plane does. |
| `aoprobe.c` | A read callback in C. Replaces the `read` field of mfusepy's `fuse_operations`, so reads never enter Python. |
| `minifs.py` | A minimal read-only passthrough file system over one folder, with its read in Python or in `aoprobe`. `--spin N` adds N busy Python threads. |
| `ao_probe.py` | Starts AutoOrtho with its file-system callbacks counted and timed from the inside. Changes nothing on disk. |
| `read_cost.py` | `build`, `lists`, `run`, `report`. |

The client and the probe build on Windows (Win32 calls, as X-Plane's reads
arrive there), Linux and macOS (`open`, `lseek`, `read`; the probe reads with
`pread`). `read_cost.py build` compiles them for the system it runs on.
`minifs.py` needs `--fuse-lib` on Windows only. `ao_probe.py` and the report
are the same everywhere. Linux and macOS runs have not been recorded yet
(plan item N8): the code builds and the client was smoke-tested on Linux
(WSL).

## Run

Use an isolated home (a copy of `~/.autoortho` with a temporary `cache_dir`),
so the user's cache and settings are untouched. Check the PC is idle first.

1. Build: `python read_cost.py build --out <dir> --gcc <path to gcc>`.
2. Start a lone mount worker through the probe, from the repository root, with
   `USERPROFILE` and `HOME` set to the isolated home:

   ```
   AO_RUN_MODE=mount_worker AO_PROBE_OUT=<dir>/probe.json python tools/bench/read_cost/ao_probe.py --root <scenery root> --mountpoint <mount>
   ```

3. Fill the cache: write the texture names to a file, make the lists
   (`read_cost.py lists --names names.txt --dir <mount>/textures --prefix ao`),
   and read level 0 once (`readbench.exe ao-L0.txt 4 populate.csv`).
4. Stop the worker (open `<mount>/.poison`) and start it again, so the timed
   runs see a fresh process.
5. Keep only the textures AutoOrtho serves straight from disk: after one pass,
   `dds_direct_opens` in the probe must equal the number of textures. Textures
   with chunks the provider has no imagery for are never direct (see the plan).
6. Run each target, five times, at one and eight client threads:

   ```
   python read_cost.py run --readbench <dir>/readbench.exe --list ntfs-L1.txt --label ntfs-L1-t1 --threads 1 --repeat 5 --out runs/ntfs-L1-t1.json
   python read_cost.py run ... --list aod-L1.txt --label ao-L1-t1 --pid <worker pid> --probe <dir>/probe.json --out runs/ao-L1-t1.json
   ```

   For the minimal file system, start `minifs.py --root <cache>/dds_cache
   --mountpoint <mount2> --read python|native --lib <dir>/aoprobe.dll
   --fuse-lib <winfsp-x64.dll or dokanfuse2.dll>` and read the same cache files
   through `<mount2>`.
7. `python read_cost.py report runs/*.json --skip 1`.

## Reading the numbers

- All targets read the same bytes from files already in the system's file
  cache: the disk is not in the measurement.
- `ntfs` is the floor. `mini-native` minus `ntfs` is the kernel round trip.
  `mini-py` minus `mini-native` is the least a Python callback costs.
  `ao` minus `mini-py` is AutoOrtho's own work on a ready read.
- The probe's `us_each` is time inside Python once the callback holds the GIL.
  The client's latency minus it is the round trip plus the wait for the GIL.
- A lone worker has no parent, so it reads in loading-screen mode. That does
  not change a ready read.
