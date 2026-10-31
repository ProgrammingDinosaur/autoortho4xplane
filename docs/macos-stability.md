# macOS shutdown and native pipeline stability

The shutdown report identifies a reproducible application shutdown defect, with
possible additional macFUSE/kernel involvement. The changes in this checkout
remove the unsafe shutdown sequence and add bounded failure handling. They
still require validation with a real macFUSE mount before a release can claim
the reboot-level failure is resolved.

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
stub driver; live driver integration remains necessary.

## Validation before release

Automated regression groups cover shutdown ordering, timed-out and busy helpers,
retained worker handles, retry/restart guards, GUI-independent control pipes,
cached mount-table reads, cleanup protection, manager disconnects, native CPU
admission/cancellation, and exact partial DDS row placement. Run parent/UI
tests separately from package-based texture tests: the existing suite mixes
`autoortho.py` imports with namespace-package imports.

Local verification passed 212 focused tests in two cleanly exiting runs:
148 texture/runtime/download/queue tests and 64 mount/supervisor/UI lifecycle
tests. Syntax checks passed for the 11 implementation/build files, and
`git diff --check` passed. The Darwin mount-table ABI was checked against the
local SDK (2,168 bytes), and the cached API returned the APFS root entry.
No live FUSE mounts were created for this verification.

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
