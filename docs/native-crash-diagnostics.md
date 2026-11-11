# Native crash diagnostics

Mount workers collect crash evidence automatically on Windows, macOS and Linux.
Start AutoOrtho and fly normally. The surviving application process records the
worker's failure, so native faults do not need to reach a Python exception hook.
All evidence stays in `~/.autoortho-data/crashes`; nothing is uploaded.

## Native library rebuilds

The release workflows force a fresh pipeline build on Windows, macOS and Linux.
AoCache, AoDecode and AoDDS are compiled together into `aopipeline.dll`,
`libaopipeline.dylib` or `libaopipeline.so`. Third-party libraries remain build
dependencies; this does not rebuild every bundled third-party library.

On macOS and Linux, `release-bundle` copies the compiler's output into the
platform directory consumed by PyInstaller. The workflow records source and
binary hashes in `native-build.json` and checks all three component versions
against their source declarations. Packaging verifies the staged library and
its source hashes for the target platform. A build failure stops the job.

Packagers may change Mach-O code signatures or ELF loader paths. The manifest
describes the verified compiler output; POSIX crash reports also record hashes
of the installed files after those transformations.

For a local macOS build, use `make -B -C autoortho/aopipeline -f Makefile.macos
release-bundle`, then `python autoortho/native_build.py record --platform macos
--source-root . --directory autoortho/aopipeline/lib/macos --revision <commit>`.
Use `Makefile.linux`, `--platform linux` and `lib/linux` for Linux. The
`verify` action checks hashes; `versions` checks the compiled component versions.

The vendored macOS static dependencies currently carry deployment targets newer
than the Makefile's macOS 11 setting. A successful build on a recent Mac does not
validate compatibility with older macOS versions; check those targets before
publishing a release that claims older OS support.

## macOS and Linux

Before application or native-library imports, the worker enables CPython's C
fault handler with a dedicated file descriptor that stays open until exit. On
`SIGSEGV`, `SIGABRT`, `SIGBUS`, `SIGILL` or `SIGFPE`, it records Python thread
stacks and requests the crashing thread's C stack with Python 3.14. C stack
support depends on the OS and Python build; an unsupported build records that
limitation in the fault log. Free-threaded Python can report only the current
thread. This does not install Python callbacks for fatal signals, or change
FUSE's existing termination and user-signal handling.

Each failed worker produces a bundle with a shared `posix-<id>` prefix:

- `.fault.log`: fatal-signal traceback and supported C stack information.
- `.worker.json`: PID, Python version, executable, native library paths and
  SHA-256 hashes, and existing core-dump settings. These are hashes of the actual
  installed libraries, including any packaging/code-signing transformations.
- `.images.json`: loaded executable image paths and addresses, refreshed after
  the worker's runtime imports and before mounting FUSE.
- `.crash.json`: parent-recorded exit code, signal, and whether shutdown had
  been requested. A native fatal signal during shutdown is still retained.
- `.system.json`: outcome of OS crash-report collection, including why reports
  could not be obtained.
- `.complete`: collection finished; the bundle is eligible for retention.

The parent collects OS reports in the background without delaying streaming
cleanup. On macOS, it waits up to eight seconds for a readable `.ips` or `.crash`
report with the worker's PID and a fresh modification time, and copies only that
matching report. The native image list in that OS report can include libraries
loaded after AutoOrtho's startup snapshot.

On Linux, it asks an existing `/usr/bin/coredumpctl` or `/bin/coredumpctl` for
`info` matching the worker's PID, user, executable and a time window from startup
through a short OS-reporting grace period after exit. It retries for
up to eight seconds to allow the OS collector to finish. Output goes into
`.system.txt`. Availability depends on systemd-coredump and journal permissions;
this command does not export a full core image. Existing core limits and
`/proc/sys/kernel/core_pattern` are recorded without changing them.

This does not install a debugger, request administrator access, change global
crash-report settings, or enable potentially large core files. Native faults
still terminate with their original signal. `SIGKILL` cannot run a fault handler:
the parent records it without claiming that a native stack or an OOM diagnosis
is available. Normal exits and requested non-fatal stops discard their empty
diagnostic files. The latest three completed POSIX bundles are retained when a
new worker starts; active bundles and Windows reports are not pruned.

Crash files can contain private paths and process memory details. Share them
privately with a maintainer. The `POSIX crash diagnostics` workflow checks real
segmentation faults, aborts, normal exits and `SIGKILL` on Linux and macOS without
FUSE or the simulator, and disables core files in its test children only.

## Windows

Windows uses a separate bundled debugger process to capture native fast-fail,
including a minidump and exception subcode. It also checks native DLL build
provenance. See [Windows native diagnostics](windows-native-diagnostics.md) for
its build, packaging and evidence details.
