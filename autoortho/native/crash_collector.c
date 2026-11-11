/* Out-of-process crash capture for one AO mount worker. No third-party runtime.
 *
 * Attach before the worker loads native libraries. Pass first-chance exceptions
 * to the application; capture an unhandled exception while Windows still holds
 * all worker threads stopped. In particular, fast-fail bypasses in-process SEH.
 *
 * Build: x86_64-w64-mingw32-gcc -g -O2 -Wall -Wextra -municode
 *        -static-libgcc crash_collector.c -o ao-crash-collector.exe -ldbghelp
 */
#define WIN32_LEAN_AND_MEAN
#define _WIN32_WINNT 0x0601
#include <windows.h>
#include <tlhelp32.h>
#include <dbghelp.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <wchar.h>

#define PATH_CHARS 32768

static wchar_t file_path[PATH_CHARS];

static const wchar_t *with_suffix(const wchar_t *prefix, const wchar_t *suffix) {
    if (swprintf(file_path, PATH_CHARS, L"%ls%ls", prefix, suffix) < 0) return NULL;
    return file_path;
}

static FILE *open_report(const wchar_t *prefix, const wchar_t *suffix) {
    const wchar_t *path = with_suffix(prefix, suffix);
    return path ? _wfopen(path, L"wb") : NULL;
}

/* Emit paths as JSON Unicode escapes, preserving non-ASCII Windows filenames. */
static void json_path(FILE *out, const wchar_t *value) {
    fputc('"', out);
    for (; *value; ++value) {
        unsigned int c = (unsigned short)*value;
        if (c == '"' || c == '\\') fprintf(out, "\\%c", c);
        else if (c < 32 || c > 126) fprintf(out, "\\u%04x", c);
        else fputc((int)c, out);
    }
    fputc('"', out);
}

static void record_module(FILE *modules, HANDLE file, const void *base) {
    if (!file || file == INVALID_HANDLE_VALUE) return;
    if (modules) {
        wchar_t path[PATH_CHARS];
        DWORD length = GetFinalPathNameByHandleW(file, path, PATH_CHARS, FILE_NAME_NORMALIZED);
        fprintf(modules, "{\"base\":\"0x%llx\",\"path\":", (unsigned long long)(uintptr_t)base);
        json_path(modules, length && length < PATH_CHARS ? path : L"<unavailable>");
        fputs("}\n", modules);
        fflush(modules);
    }
    CloseHandle(file);  /* LOAD_DLL and CREATE_PROCESS hFile belong to debugger. */
}

static DWORD parent_of(DWORD pid) {
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot == INVALID_HANDLE_VALUE) return 0;
    PROCESSENTRY32W entry;
    ZeroMemory(&entry, sizeof(entry));
    entry.dwSize = sizeof(entry);
    DWORD parent = 0;
    if (Process32FirstW(snapshot, &entry)) {
        do {
            if (entry.th32ProcessID == pid) {
                parent = entry.th32ParentProcessID;
                break;
            }
        } while (Process32NextW(snapshot, &entry));
    }
    CloseHandle(snapshot);
    return parent;
}

#define MAX_DUMP_ATTEMPTS 5
enum { EXCEPTION_NONE, EXCEPTION_LOCAL, EXCEPTION_REMOTE, EXCEPTION_MODES };
#define VERSION_CHARS 600

typedef BOOL (WINAPI *mitigation_fn)(HANDLE, int, PVOID, SIZE_T);
typedef DWORD (WINAPI *version_size_fn)(LPCWSTR, LPDWORD);
typedef BOOL (WINAPI *version_info_fn)(LPCWSTR, DWORD, DWORD, LPVOID);
typedef BOOL (WINAPI *version_query_fn)(LPCVOID, LPCWSTR, LPVOID *, PUINT);

/* Resolve optional APIs at runtime: no extra import libraries or Win10 headers. */
static void (*optional_proc(const wchar_t *module, const char *name))(void) {
    HMODULE handle = GetModuleHandleW(module);
    if (!handle) handle = LoadLibraryExW(module, NULL, LOAD_LIBRARY_SEARCH_SYSTEM32);
    return handle ? (void (*)(void))GetProcAddress(handle, name) : NULL;
}

/* Raw PROCESS_MITIGATION_USER_SHADOW_STACK_POLICY flags (bit 0: CET enabled). */
static const char *shadow_stack_policy(HANDLE process) {
    static char text[16];
    mitigation_fn query = (mitigation_fn)optional_proc(L"kernel32.dll", "GetProcessMitigationPolicy");
    DWORD flags = 0;
    if (!process || !query || !query(process, 15 /* ProcessUserShadowStackPolicy */, &flags, sizeof(flags)))
        return "unavailable";
    snprintf(text, sizeof(text), "0x%08lx", flags);
    return text;
}

/* Path and file version of the DbgHelp actually loaded, for CI diagnostics. */
static void dbghelp_version(wchar_t *out, size_t chars) {
    wchar_t path[MAX_PATH];
    HMODULE dbghelp = GetModuleHandleW(L"dbghelp.dll");
    if (!dbghelp || !GetModuleFileNameW(dbghelp, path, MAX_PATH)) {
        swprintf(out, chars, L"unavailable");
        return;
    }
    swprintf(out, chars, L"%ls", path);
    version_size_fn size_of = (version_size_fn)optional_proc(L"version.dll", "GetFileVersionInfoSizeW");
    version_info_fn info_of = (version_info_fn)optional_proc(L"version.dll", "GetFileVersionInfoW");
    version_query_fn query = (version_query_fn)optional_proc(L"version.dll", "VerQueryValueW");
    DWORD ignored = 0, size = size_of ? size_of(path, &ignored) : 0;
    void *data = size && info_of && query ? malloc(size) : NULL;
    VS_FIXEDFILEINFO *fixed = NULL;
    UINT length = 0;
    if (data && info_of(path, 0, size, data) && query(data, L"\\", (LPVOID *)&fixed, &length) && fixed) {
        swprintf(out, chars, L"%ls %lu.%lu.%lu.%lu", path,
                 HIWORD(fixed->dwFileVersionMS), LOWORD(fixed->dwFileVersionMS),
                 HIWORD(fixed->dwFileVersionLS), LOWORD(fixed->dwFileVersionLS));
    }
    free(data);
}

static void capture(HANDLE process, const DEBUG_EVENT *event, const wchar_t *prefix) {
    EXCEPTION_RECORD record = event->u.Exception.ExceptionRecord;
    record.ExceptionRecord = NULL;  /* The linked record pointer is remote. */
    CONTEXT context;
    ZeroMemory(&context, sizeof(context));
    context.ContextFlags = CONTEXT_ALL;
    HANDLE thread = OpenThread(THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION, FALSE, event->dwThreadId);
    BOOL have_context = thread && GetThreadContext(thread, &context);
    DWORD context_error = have_context ? 0 : GetLastError();
    EXCEPTION_POINTERS pointers = { &record, &context };
    MINIDUMP_EXCEPTION_INFORMATION info[EXCEPTION_MODES];
    ZeroMemory(info, sizeof(info));
    /* Local: exception and context live in the collector (ClientPointers=FALSE).
     * Server 2022's DbgHelp rejects this with ERROR_INVALID_USER_BUFFER. */
    info[EXCEPTION_LOCAL].ThreadId = event->dwThreadId;
    info[EXCEPTION_LOCAL].ExceptionPointers = &pointers;
    info[EXCEPTION_LOCAL].ClientPointers = FALSE;
    /* Remote: copy them into the stopped worker and pass worker addresses,
     * the same form an in-process handler would use (ClientPointers=TRUE). */
    struct remote_exception { EXCEPTION_POINTERS pointers; EXCEPTION_RECORD record; CONTEXT context; };
    struct remote_exception *remote = NULL;
    DWORD remote_error = 0;
    if (have_context) {
        remote = VirtualAllocEx(process, NULL, sizeof(*remote), MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
        struct remote_exception copy;
        ZeroMemory(&copy, sizeof(copy));
        SIZE_T written = 0;
        if (remote) {
            copy.pointers.ExceptionRecord = &remote->record;
            copy.pointers.ContextRecord = &remote->context;
            copy.record = record;
            copy.context = context;
        }
        if (!remote || !WriteProcessMemory(process, remote, &copy, sizeof(copy), &written) ||
            written != sizeof(copy)) {
            remote_error = GetLastError();
            if (remote) VirtualFreeEx(process, remote, 0, MEM_RELEASE);
            remote = NULL;
        } else {
            info[EXCEPTION_REMOTE].ThreadId = event->dwThreadId;
            info[EXCEPTION_REMOTE].ExceptionPointers = &remote->pointers;
            info[EXCEPTION_REMOTE].ClientPointers = TRUE;
        }
    }

    DWORD dump_error = 0, dump_attempts = 0;
    DWORD dump_errors[MAX_DUMP_ATTEMPTS] = { 0 };
    int dump_exception = EXCEPTION_NONE;
    BOOL dumped = FALSE;
    const wchar_t *path = with_suffix(prefix, L".dmp");
    HANDLE dump = path ? CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL)
                       : INVALID_HANDLE_VALUE;
    if (dump != INVALID_HANDLE_VALUE) {
        /* Prefer the remote form, then the local one, then a smaller dump; the
         * last attempt omits the exception stream, which crash.json preserves. */
        static const MINIDUMP_TYPE full = (MINIDUMP_TYPE)(MiniDumpWithThreadInfo |
            MiniDumpWithUnloadedModules | MiniDumpWithIndirectlyReferencedMemory |
            MiniDumpWithProcessThreadData | MiniDumpWithFullMemoryInfo);
        const struct { MINIDUMP_TYPE type; int mode; } attempts[MAX_DUMP_ATTEMPTS] = {
            { full, EXCEPTION_REMOTE }, { full, EXCEPTION_LOCAL },
            { MiniDumpNormal, EXCEPTION_REMOTE }, { MiniDumpNormal, EXCEPTION_LOCAL },
            { MiniDumpNormal, EXCEPTION_NONE },
        };
        for (size_t i = 0; i < MAX_DUMP_ATTEMPTS && !dumped; ++i) {
            int mode = attempts[i].mode;
            if (mode != EXCEPTION_NONE && !have_context) continue;
            if (mode == EXCEPTION_REMOTE && !remote) continue;
            if (dump_attempts) {  /* Discard any partial output from the failed attempt. */
                SetFilePointer(dump, 0, NULL, FILE_BEGIN);
                SetEndOfFile(dump);
            }
            dumped = MiniDumpWriteDump(process, event->dwProcessId, dump, attempts[i].type,
                                      mode == EXCEPTION_NONE ? NULL : &info[mode], NULL, NULL);
            if (dumped) dump_exception = mode;
            else dump_error = dump_errors[dump_attempts] = GetLastError();
            ++dump_attempts;
        }
        if (dumped) dump_error = 0;
        CloseHandle(dump);
        if (!dumped && path) DeleteFileW(path);
    } else {
        dump_error = GetLastError();
    }
    if (remote) VirtualFreeEx(process, remote, 0, MEM_RELEASE);
    if (thread) CloseHandle(thread);

    /* Preserve the exception record even if disk space or DbgHelp prevents a dump. */
    FILE *report = open_report(prefix, L".crash.json");
    if (report) {
        fprintf(report, "{\"pid\":%lu,\"thread_id\":%lu,\"exception_code\":\"0x%08lx\","
                "\"exception_address\":\"0x%llx\",\"first_chance\":%lu,\"parameters\":[",
                event->dwProcessId, event->dwThreadId, record.ExceptionCode,
                (unsigned long long)(uintptr_t)record.ExceptionAddress, event->u.Exception.dwFirstChance);
        for (DWORD i = 0; i < record.NumberParameters && i < EXCEPTION_MAXIMUM_PARAMETERS; ++i) {
            fprintf(report, "%s\"0x%llx\"", i ? "," : "", (unsigned long long)record.ExceptionInformation[i]);
        }
        static const char *const modes[EXCEPTION_MODES] = { "none", "local", "remote" };
        fprintf(report, "],\"dump_written\":%s,\"dump_exception\":\"%s\",\"dump_error\":%lu,"
                "\"remote_exception_error\":%lu,\"context_flags\":\"0x%08lx\","
                "\"dump_attempts\":%lu,\"dump_errors\":[",
                dumped ? "true" : "false", modes[dump_exception], dump_error,
                remote_error, (unsigned long)context.ContextFlags, dump_attempts);
        for (DWORD i = 0; i < dump_attempts; ++i) fprintf(report, "%s%lu", i ? "," : "", dump_errors[i]);
        wchar_t dbghelp[VERSION_CHARS];
        fputs("],\"dbghelp_version\":", report);
        dbghelp_version(dbghelp, VERSION_CHARS);
        json_path(report, dbghelp);
        fprintf(report, ",\"shadow_stack_policy\":\"%s\",\"context_error\":%lu}\n",
                shadow_stack_policy(process), context_error);
        fclose(report);
    }
    fprintf(stderr, "Native crash captured: worker=%lu exception=0x%08lx dump=%s error=%lu\n",
            event->dwProcessId, record.ExceptionCode, dumped ? "written" : "failed", dump_error);
    fflush(stderr);
}

static DWORD parse_pid(const wchar_t *value) {
    wchar_t *end;
    unsigned long result = wcstoul(value, &end, 10);
    return *value && !*end && result && result < MAXDWORD ? (DWORD)result : 0;
}

int wmain(int argc, wchar_t **argv) {
    if (argc != 5) return 2;
    DWORD pid = parse_pid(argv[1]), parent_pid = parse_pid(argv[2]);
    if (!pid || !parent_pid || wcslen(argv[4]) > PATH_CHARS - 32) return 2;
    /* This helper only observes a sibling launched by the same AO parent. */
    if (parent_of(GetCurrentProcessId()) != parent_pid || parent_of(pid) != parent_pid) return 2;
    HANDLE ready = OpenEventW(EVENT_MODIFY_STATE, FALSE, argv[3]);
    HANDLE parent = OpenProcess(SYNCHRONIZE, FALSE, parent_pid);
    if (!ready || !parent) {
        if (ready) { SetEvent(ready); CloseHandle(ready); }
        if (parent) CloseHandle(parent);
        return 3;
    }
    /* Never kill a live worker if the collector fails or exits. */
    if (!DebugActiveProcess(pid)) {
        fprintf(stderr, "Native crash collector attach failed: worker=%lu error=%lu\n", pid, GetLastError());
        SetEvent(ready);
        CloseHandle(ready);
        CloseHandle(parent);
        return 3;
    }
    if (!DebugSetProcessKillOnExit(FALSE)) {
        DebugActiveProcessStop(pid);
        SetEvent(ready);
        CloseHandle(ready);
        CloseHandle(parent);
        return 3;
    }
    FILE *modules = open_report(argv[4], L".modules.jsonl");
    HANDLE process = NULL;
    BOOL crashed = FALSE, initial_breakpoint = TRUE, exited = FALSE;
    fprintf(stderr, "Native crash collector attached: worker=%lu\n", pid);
    fflush(stderr);
    SetEvent(ready);

    for (;;) {
        DEBUG_EVENT event;
        ZeroMemory(&event, sizeof(event));
        if (!WaitForDebugEvent(&event, 250)) {
            if (GetLastError() != ERROR_SEM_TIMEOUT) break;
            if (WaitForSingleObject(parent, 0) == WAIT_OBJECT_0) break;
            continue;
        }
        DWORD disposition = DBG_CONTINUE;
        switch (event.dwDebugEventCode) {
        case CREATE_PROCESS_DEBUG_EVENT:
            process = event.u.CreateProcessInfo.hProcess;
            record_module(modules, event.u.CreateProcessInfo.hFile, event.u.CreateProcessInfo.lpBaseOfImage);
            break;
        case LOAD_DLL_DEBUG_EVENT:
            record_module(modules, event.u.LoadDll.hFile, event.u.LoadDll.lpBaseOfDll);
            break;
        case EXCEPTION_DEBUG_EVENT:
            if (initial_breakpoint && event.u.Exception.dwFirstChance &&
                event.u.Exception.ExceptionRecord.ExceptionCode == EXCEPTION_BREAKPOINT) {
                initial_breakpoint = FALSE;
            } else {
                disposition = DBG_EXCEPTION_NOT_HANDLED;
                if (!event.u.Exception.dwFirstChance && !crashed) {
                    crashed = TRUE;
                    capture(process, &event, argv[4]);
                }
            }
            break;
        case EXIT_PROCESS_DEBUG_EVENT:
            exited = TRUE;
            break;
        }
        /* Always let the original exception terminate the worker normally.
         * The parent must see the actual NTSTATUS, never a collector exit code. */
        if (!ContinueDebugEvent(event.dwProcessId, event.dwThreadId, disposition)) break;
        if (exited) break;
    }
    if (!exited) DebugActiveProcessStop(pid);
    /* Windows closes the process/thread handles delivered by debug events
     * after their exit event is continued; do not double-close those handles. */
    if (modules) fclose(modules);
    if (crashed) {
        FILE *complete = open_report(argv[4], L".complete");
        if (complete) fclose(complete);
    } else {
        const wchar_t *path = with_suffix(argv[4], L".modules.jsonl");
        if (path) DeleteFileW(path);
        path = with_suffix(argv[4], L".build.json");
        if (path) DeleteFileW(path);
    }
    CloseHandle(ready);
    CloseHandle(parent);
    return 0;
}
