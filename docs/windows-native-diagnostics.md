# Windows native crash diagnostics

For macOS and Linux, see [Native crash diagnostics](native-crash-diagnostics.md).

Windows builds start the bundled `ao-crash-collector.exe` automatically for each
mount worker. It attaches before that worker imports native extensions. The
reporter starts AutoOrtho and flies as usual; no ProcDump installation, registry
edits, administrator privileges or configuration changes are needed under normal
same-user process permissions.

The collector passes first-chance exceptions back to the application. On an
unhandled native exception (including Windows fast-fail `0xC0000409`), it writes
diagnostics while the worker is stopped, then lets Windows terminate the worker
with its original exception code. Normal worker exits do not produce dumps. A
collector failure releases the startup gate, and its exit does not kill a worker.
The gate has a 15-second fallback timeout. Attach failures are recorded in the
worker log and streaming can continue without crash capture.

Files are stored locally in `%USERPROFILE%\.autoortho-data\crashes`:

- `worker-<pid>-<id>.dmp`: native thread stacks, exception context, module list
  and memory referenced by stacks (not a full worker heap dump).
- `.crash.json`: exception code, address, thread ID, exception parameters
  (parameter zero is the fast-fail subcode), and dump/context errors if any.
- `.modules.jsonl`: actual loaded module paths and base addresses.
- `.build.json`: build revision, source hashes and native artifact hashes.
- `.complete`: indicates that the collector finished writing the bundle.

The latest three completed bundles are retained when another worker starts;
active collectors are not pruned. Nothing is uploaded automatically. Dumps can
contain paths and memory holding private data; share a bundle privately with a
maintainer rather than attaching it to a public issue. Existing worker logs are
in `%USERPROFILE%\.autoortho-data\logs`.

## Building and verifying

The `windows-native.yml` workflow cross-compiles the pipeline and collector from
the current checkout, records hashes in `native-build.json`, and runs Windows
checks for fast-fail capture, handled exceptions, normal exit and collector exit.
These checks validate the diagnostics mechanism without reproducing a flight
failure. Native DLL versions are checked against their source declarations.

Windows releases consume that workflow's artifact. Both PyInstaller packaging
and the final packaged directory must pass verification; old checked-in DLLs
cannot silently substitute for the compiled artifact. Native binaries retain
debug information and are excluded from UPX so their recorded hashes survive
packaging. Keep the matching `windows-native` CI artifact for native analysis.

The separate **Windows diagnostic build** workflow produces a portable
`AutoOrtho_windows_native_diagnostics.zip` artifact after native Windows checks
pass. It runs for relevant pull requests and can also be started manually. This
is a build artifact, without creating a GitHub release.

For a local cross-build with a MinGW POSIX toolchain installed:

```sh
make -B -C autoortho/aopipeline -f Makefile.mingw64 windows-bundle \
  CC=x86_64-w64-mingw32-gcc-posix LD=x86_64-w64-mingw32-gcc-posix
python autoortho/native_build.py record --source-root . \
  --directory autoortho/aopipeline/lib/windows --revision <commit-id>
python autoortho/native_build.py verify --source-root . \
  --directory autoortho/aopipeline/lib/windows
```

Frozen workers also verify the bundled artifact hashes before native imports.
An unmanifested development checkout logs that it is unverified; a frozen build
rejects missing or mismatched native artifacts.
