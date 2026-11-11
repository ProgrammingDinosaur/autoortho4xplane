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
    MINIDUMP_EXCEPTION_INFORMATION info;
    info.ThreadId = event->dwThreadId;
    info.ExceptionPointers = &pointers;
    info.ClientPointers = FALSE;  /* Exception and context live in collector. */

    DWORD dump_error = 0, dump_attempts = 0;
    BOOL dumped = FALSE;
    const wchar_t *path = with_suffix(prefix, L".dmp");
    HANDLE dump = path ? CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL)
                       : INVALID_HANDLE_VALUE;
    if (dump != INVALID_HANDLE_VALUE) {
        /* DbgHelp intermittently fails (e.g. ERROR_INVALID_USER_BUFFER) while
         * walking remote memory. Fall back to smaller dumps; every level still
         * holds the thread, module and exception streams. */
        static const MINIDUMP_TYPE types[] = {
            (MINIDUMP_TYPE)(MiniDumpWithThreadInfo | MiniDumpWithUnloadedModules |
                MiniDumpWithIndirectlyReferencedMemory | MiniDumpWithProcessThreadData |
                MiniDumpWithFullMemoryInfo),
            (MINIDUMP_TYPE)(MiniDumpWithThreadInfo | MiniDumpWithUnloadedModules |
                MiniDumpWithProcessThreadData | MiniDumpWithFullMemoryInfo),
            (MINIDUMP_TYPE)(MiniDumpWithThreadInfo | MiniDumpWithUnloadedModules),
            MiniDumpNormal,
        };
        for (size_t i = 0; i < sizeof(types) / sizeof(types[0]) && !dumped; ++i) {
            if (i) {  /* Discard any partial output from the failed attempt. */
                SetFilePointer(dump, 0, NULL, FILE_BEGIN);
                SetEndOfFile(dump);
            }
            ++dump_attempts;
            dumped = MiniDumpWriteDump(process, event->dwProcessId, dump, types[i],
                                      have_context ? &info : NULL, NULL, NULL);
            if (!dumped) dump_error = GetLastError();
        }
        if (dumped) dump_error = 0;
        CloseHandle(dump);
        if (!dumped && path) DeleteFileW(path);
    } else {
        dump_error = GetLastError();
    }
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
        fprintf(report, "],\"dump_written\":%s,\"dump_error\":%lu,\"dump_attempts\":%lu,"
                "\"context_error\":%lu}\n",
                dumped ? "true" : "false", dump_error, dump_attempts, context_error);
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
