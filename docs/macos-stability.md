# macOS shutdown and native pipeline stability

The shutdown report identifies a reproducible application shutdown defect, with
possible additional macFUSE/kernel involvement. The changes in this checkout
remove the unsafe shutdown sequence and add bounded failure handling. Local
live-mount checks passed on macFUSE 5.0.6, as detailed below. Validation of the
built app on the reported macOS 27/macFUSE 5.4.0 combination is still required
before claiming the reboot-level failure is resolved.

## Evidence

The attached `autoortho-ui-log.txt` describes a different session from the
one-mount report: six scenery mounts on an external T7 volume, Darwin 25.6.0,
and a performance session dated October 19. Keep those environment details
separate from the reported internal APFS/macOS 27/macFUSE 5.4.0 case.

In that log, streaming starts at 08:48:32 and stopping begins at 08:48:38.
Africa's `fuse_exit` is followed by cleanup at 09:08:38, exactly 20 minutes
later. South America also takes 20 minutes. Europe receives its poison probe
at 09:28:39 and has not completed shutdown when logging ends around 11:08.
The per-worker tile counts remain zero. This demonstrates that X-Plane,
large tile populations, and flight-time network pressure are not prerequisites.
Two mountpoint cleanup attempts also fail with `Resource busy`.

The supplied sample establishes that the main thread was blocked in `lstat`.
It does not identify the pathname or prove which Python generator issued the
call. The implementation nevertheless contained several independent ways to
produce that state:

1. The parent synchronously `lstat`ed a `.poison` file inside the mount.
2. Its FUSE `getattr` callback stopped the FUSE request loop with `fuse_exit`.
3. Mount polling used `os.path.ismount`, which itself calls `lstat`.
4. Worker cleanup called `lexists` and `rmdir` before checking mount status.
5. macOS readiness detection resolved the mount pathname and invoked `mount`,
   both of which can block against an unresponsive filesystem.

The attachment does not contain the separate `row_offset` traceback, manager
`EOFError`, or native oversubscription messages. Those were assessed from the
written report and the corresponding source code.

## Changes by priority

| Priority | Problem | Change |
| --- | --- | --- |
| P0 | FUSE stops serving before macOS has detached it | macOS shutdown no longer probes `.poison` or calls `fuse_exit` from that callback. The OS unmounts while the worker continues serving requests. |
| P0 | Parent or worker blocks while inspecting its mount | Darwin detection uses `getfsstat64(MNT_NOWAIT)` and a private buffer. Path normalization resolves only parent aliases, never the mount leaf. Worker cleanup is deferred to the parent until detach and worker exit. |
| P0 | `diskutil`/`umount` timeout handling can still wait forever | A dedicated `/sbin/umount` helper gets a deadline. Timeout signals the helper without an unbounded post-kill wait. A blocked helper is retained and prevents repeated helpers against the same path. |
| P0 | Shutdown claims success after losing ownership of stuck workers | Surviving worker handles are retained. Failed detach keeps the workers, broker, stats manager, and logging available; startup refuses to overwrite that unresolved session. |
| P1 | One mount prevents all others from stopping | macOS mounts detach concurrently, with bounded helper waits, rather than accumulating minutes per region. |
| P1 | Long texture/download waits prevent timely unmount | The parent sends a one-time quiesce command over a private inherited pipe. Workers cancel queued work and wake native admission waiters without exiting the FUSE loop. This works with GUI standard streams disabled. |
| P1 | Lost stats manager raises into worker threads | Stats reads/writes/mapping operations fall back to local counters on EOF or broken connections. Missing shared flight state disables speculative work. Rebinding preserves existing imported `STATS` references. |
| P1 | Native budgets sum to more than CPU capacity | Thread reservations enforce a total per-worker limit. CPU capacity is divided across launched scenery workers, with a minimum of one thread each. Explicit per-build settings are capped. |
| P1 | Partial builds/cache reads bypass native accounting | Cache batch reads use reserved thread counts. Partial builds reserve their full OpenMP default because their native ABI has no thread-count argument. Worker environment limits are set before native libraries load. |
| P1 | OpenMP workers spin while idle | Workers default to `OMP_WAIT_POLICY=PASSIVE` and `KMP_BLOCKTIME=0`. User-specified wait policies remain supported. |
| P1 | Periodic threads continue after shutdown | Tile cleaners have a wakeable stop event and are joined before tile state is cleared. Worker reload polling stops on quiescence. Stats batchers join their background thread after flushing. |
| P1 | A read crossing an already-served row returns missing-color pixels for the next row | Found during live DDS testing. Partial commits now atomically preserve sealed rows while installing new rows; only installed rows are queued for persistence. Both native and Python partial builders have regression coverage. |
| P2 | `row_offset` NameError in partial mipmap success path | The current source already uses `startrow * bytes_per_chunk_row` instead of that undefined variable. Regression coverage now includes the first, intermediate, multiple, and last rows, checking actual DDS byte offsets. |
| P2 | `.app` metadata reports 1.0.0 | Bundle version fields derive from release `VERNAME`, or the project version for local builds. |

The old native allocator divided CPUs by the number of active builds without
changing reservations already in flight. On 12 cores, four builds could receive
12 + 6 + 4 + 3 = 25 threads. New admissions reserve remaining capacity under a
condition variable and release it even when a build raises. Across six mounts
on 12 cores, each worker gets a two-thread share. If mounts outnumber cores,
each worker still needs one thread; that minimum can exceed the machine total.
The budget concerns native compute, not the total number of sleeping I/O,
network, and UI threads.

Admission is FIFO so a partial build needing its full reservation cannot be
starved by a stream of smaller builds. Static sharing favors predictable CPU
use; it can leave capacity unused when only one of several mounted regions is
active. Measure DDS latency and throughput during the release validation rather
than assuming a performance improvement from lower thread counts alone.

## Native warnings and limits

The worker is already launched as a fresh executable, and the FUSE options
already specify `foreground=True`. Regression coverage preserves that setting.
The warning about forking a threaded process does not, by itself, establish
that Python multiprocessing forked the worker. A native FUSE library may launch
mount helpers internally. Do not suppress this warning or attribute it to a
specific component without the full worker log and native stack.

The original report's processes remaining in `U`/`?E` after SIGKILL are a kernel
or filesystem-driver state, beyond what Python cancellation can repair. An
absent mount-table entry is not sufficient proof of recovery. In particular,
do not document worker SIGKILL as a reliable recovery procedure. These changes
aim to avoid creating the bad shutdown state and to report failure honestly;
they cannot recover an already wedged macFUSE kernel session.

While running tests locally, loading the installed `/usr/local/lib/libfuse.2.dylib`
without mounting also caused `CFRelease(NULL)` in a process-exit destructor.
That is separate evidence about this machine's native library environment,
not proof of the reported macFUSE kernel failure. Callback unit tests use a
stub driver. The subsequent live checks exercised the normal source worker
entrypoint, which exits without running that native destructor.

## Live local check — October 31, 2026

Environment: macOS 26.7.1, Apple Silicon, 10 logical CPUs, Python 3.14.6,
macFUSE 5.0.6, `/usr/local/lib/libfuse.2.dylib`. The test used temporary backing
directories and actual macFUSE mounts, production worker supervision, shared
statistics and download-broker services, and the production parent shutdown
method. It reused mountpoints across cycles without launching X-Plane.

| Scenario | Successful cycles | Total shutdown time |
| --- | --- | --- |
| One mount, verified file read | 20 | 0.76–1.29 s |
| Six mounts, verified file reads | 3 | 1.17–1.24 s |
| One mount, native partial DDS reads | 3 | 1.18–1.40 s |
| Six mounts, concurrent native partial DDS reads | 3 | 0.96–1.16 s |

The DDS input consisted of 341 cached JPEG chunks across ZL12–16. Reads checked
the DDS header, 4096×4096 dimensions, and mipmap-zero rows 0, 1, 7, and 15.
Identical source rows had identical compressed checksums. No network imagery
was needed. Each successful cycle verified worker exit code zero, reaped PIDs,
and disappearance of every test mount. The final independent process check
found no test workers, brokers, managers, or mounts remaining. No force-kill or
reboot was required for successful cycles, and no oversubscription messages or
native partial exceptions were recorded.

The first DDS attempt exposed a real boundary defect: a 32 KiB kernel read at
offset 507904 crosses the first 524288-byte data row, which begins after the
128-byte DDS header. Row zero was already sealed by an earlier read, so the
combined row-zero/row-one commit was rejected and row one was served missing.
The fix preserves row zero while committing row one. The subsequent DDS cycles
passed, and regression tests reproduce the same read sequence for both native
and Python builders. A harmless `winreg` import error logged on macOS was also
corrected by restricting that import to Windows.

Detailed JSON results, logs, and the isolated test harness are saved locally
under `logs/macos-smoke-20261031/`. These checks exercise source workers, cached
imagery, and an older driver than the report. They do not cover a packaged-app
bootloader, a 30-minute flight, or network stalls. The test used a three-second
tile budget and disabled speculative prefetch during the DDS phase.

## Validation before release

Automated regression groups cover shutdown ordering, timed-out and busy helpers,
retained worker handles, retry/restart guards, GUI-independent control pipes,
cached mount-table reads, cleanup protection, manager disconnects, native CPU
admission/cancellation, and exact partial DDS row placement. Run parent/UI
tests separately from package-based texture tests: the existing suite mixes
`autoortho.py` imports with namespace-package imports.

The expanded local verification passed 311 focused tests in two cleanly exiting
runs: 247 texture/runtime/download/queue/cache tests and 64
mount/supervisor/UI lifecycle tests. Syntax checks passed for implementation/build files, and
`git diff --check` passed. The Darwin mount-table ABI was checked against the
local SDK (2,168 bytes), and the cached API returned the APFS root entry.
The automated regression runs use stubbed mounts; the live checks above were
run separately against the installed driver.

The broader Qt runtime suite aborted inside native `QWizard` code while opening
the setup wizard during settings rendering. The selected asynchronous
start/stop, worker-exit, and failed-close lifecycle tests passed. This is a
remaining validation limitation; the full UI suite is not claimed as passing.

For local tests, load both applicable config modules in worker mode before
starting pytest, then unset `AO_RUN_MODE`. This avoids rewriting the real user
configuration during module import while allowing config-persistence tests to
exercise writes to their temporary configuration files.

On a disposable test session or machine, validate the built `.app` with macFUSE
5.4.0 on Apple Silicon, and separately with FUSE-T if it is supported:

1. Start and stop one mount without launching X-Plane. Repeat at least 20 times;
   every cycle must allow a new streaming session without rebooting.
2. Repeat with all six regions. Record total detach time and worker exit time;
   a helper timeout must become a visible failure, never an indefinite UI wait
   or a successful stop while workers remain alive.
3. Stop during cached reads, active downloads, partial native builds, and after
   a 30-minute flight. Verify cancellation, native CPU peaks, memory plateau,
   and the absence of continuing worker statistics after successful shutdown.
4. Inject a manager disconnect and an unmount-helper timeout. Confirm that
   the application remains responsive, preserves ownership, and refuses restart
   until the unresolved session has actually stopped.
5. Verify no worker processes remain after a successful stop, and verify both
   bundle version fields in the built `Info.plist` against the release version.

If a reboot-level failure persists with this sequence removed, collect the
worker and parent stacks, loaded FUSE library path/version, and a macOS
sysdiagnose for the driver maintainers. Avoid diagnostic commands against the
stuck mount; the report already demonstrates that those commands can wedge too.
