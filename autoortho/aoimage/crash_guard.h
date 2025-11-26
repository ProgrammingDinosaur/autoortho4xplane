/**
 * crash_guard.h
 * 
 * Windows Structured Exception Handling (SEH) wrapper for C extension calls.
 * 
 * This provides C-level crash protection that Python cannot provide.
 * When crashes occur in C extensions (aoimage.dll, ispc_texcomp.dll, stb_dxt.dll),
 * Python's exception handling cannot intercept them. This module catches these
 * crashes at the C level using Windows SEH (__try/__except).
 * 
 * Benefits:
 * - Catches access violations (0xC0000005) before process dies
 * - Logs crash information for debugging
 * - Attempts graceful cleanup
 * - Can signal Python layer about crash
 * - Prevents cascading crashes (orphaned virtual filesystem -> X-Plane crash)
 */

#ifndef CRASH_GUARD_H
#define CRASH_GUARD_H

#ifdef _WIN32
#include <windows.h>
#include <stdio.h>
#include <time.h>
#include <stdbool.h>

/* Crash log file path - written to user's autoortho data directory */
#define CRASH_LOG_PATH "c_crash.log"

/* Exception filter codes */
#define EXCEPTION_EXECUTE_HANDLER      1
#define EXCEPTION_CONTINUE_SEARCH      0
#define EXCEPTION_CONTINUE_EXECUTION  -1

/**
 * Initialize crash guard system.
 * Call this once at module initialization.
 * Sets up crash log file and prepares exception handling.
 */
void crash_guard_init(const char* log_dir);

/**
 * Log a crash to the crash log file.
 * Called automatically by exception handlers.
 * 
 * @param exception_code Windows exception code (e.g., 0xC0000005)
 * @param exception_addr Memory address where crash occurred
 * @param function_name Name of function where crash occurred
 * @param additional_info Optional additional information
 */
void crash_guard_log_crash(
    DWORD exception_code,
    void* exception_addr,
    const char* function_name,
    const char* additional_info
);

/**
 * Get human-readable name for Windows exception code.
 */
const char* crash_guard_exception_name(DWORD code);

/**
 * Macro to wrap function calls with SEH exception handling.
 * 
 * Usage:
 *   CRASH_GUARDED_CALL(result, risky_function(arg1, arg2), "risky_function");
 * 
 * On crash:
 * - Logs crash info
 * - Sets result to failure value (0/NULL)
 * - Prevents process termination
 */
#define CRASH_GUARDED_CALL(result, call, func_name) \
    do { \
        __try { \
            result = call; \
        } \
        __except(crash_guard_exception_filter(GetExceptionCode(), GetExceptionInformation(), func_name)) { \
            result = 0; /* Failure value */ \
            fprintf(stderr, "[CRASH_GUARD] Exception caught in %s, returning failure\n", func_name); \
        } \
    } while(0)

/**
 * Exception filter function - called by __except.
 * Logs crash details and decides whether to handle the exception.
 * 
 * @return EXCEPTION_EXECUTE_HANDLER to catch exception
 *         EXCEPTION_CONTINUE_SEARCH to let it propagate
 */
int crash_guard_exception_filter(
    DWORD exception_code,
    EXCEPTION_POINTERS* exception_info,
    const char* function_name
);

/**
 * Signal Python that a C-level crash occurred.
 * This allows Python to attempt cleanup before process dies.
 * 
 * Implementation: Writes to a named pipe or shared memory flag
 * that Python monitors.
 */
void crash_guard_signal_python(void);

/**
 * Cleanup crash guard resources.
 * Call at module shutdown.
 */
void crash_guard_cleanup(void);

#else /* Unix/Linux/macOS */

#include <signal.h>
#include <setjmp.h>
#include <stdbool.h>

/**
 * Unix signal-based crash protection.
 * 
 * Unlike Windows SEH, Unix signals don't provide true exception handling.
 * However, we can:
 * 1. Install signal handlers for SIGSEGV, SIGBUS, etc.
 * 2. Use sigsetjmp/siglongjmp for non-local jumps
 * 3. Log crash details
 * 4. Attempt cleanup (though less safe than Windows)
 * 
 * Limitations:
 * - Signal handlers have restrictions (async-signal-safe functions only)
 * - longjmp from signal handler is undefined behavior (but works in practice)
 * - Can't always recover cleanly
 * - Stack may be corrupted
 */

/* Thread-local jump buffer for crash recovery */
extern __thread sigjmp_buf crash_guard_jmp_buf;
extern __thread volatile sig_atomic_t crash_guard_active;
extern __thread volatile int crash_guard_signal;
extern __thread char crash_guard_function[256];

/**
 * Initialize crash guard system (Unix version).
 */
void crash_guard_init(const char* log_dir);

/**
 * Log a crash (Unix version).
 */
void crash_guard_log_crash_unix(
    int signal_num,
    void* fault_addr,
    const char* function_name
);

/**
 * Signal handler for crashes.
 */
void crash_guard_signal_handler(int sig, siginfo_t *info, void *context);

/**
 * Cleanup crash guard (Unix version).
 */
void crash_guard_cleanup(void);

/**
 * Macro to wrap function calls with signal-based crash protection.
 * 
 * WARNING: This uses setjmp/longjmp which is technically undefined behavior
 * when called from a signal handler, but works in practice on most systems.
 * Use with caution for critical code.
 */
#define CRASH_GUARDED_CALL(result, call, func_name) \
    do { \
        crash_guard_active = 1; \
        crash_guard_signal = 0; \
        strncpy((char*)crash_guard_function, func_name, sizeof(crash_guard_function)-1); \
        crash_guard_function[sizeof(crash_guard_function)-1] = '\0'; \
        \
        if (sigsetjmp(crash_guard_jmp_buf, 1) == 0) { \
            result = call; \
            crash_guard_active = 0; \
        } else { \
            /* Crashed - signal handler jumped here */ \
            result = 0; /* Failure */ \
            crash_guard_active = 0; \
            fprintf(stderr, "[CRASH_GUARD] Signal %d caught in %s, returning failure\n", \
                    crash_guard_signal, func_name); \
        } \
    } while(0)

#endif /* _WIN32 */

#endif /* CRASH_GUARD_H */

