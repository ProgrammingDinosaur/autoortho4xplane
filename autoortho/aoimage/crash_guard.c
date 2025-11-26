/**
 * crash_guard.c
 * 
 * Implementation of C-level crash protection using Windows SEH.
 * 
 * This module catches crashes that Python's exception handling cannot intercept,
 * such as access violations in C extensions (aoimage.dll, ispc_texcomp.dll, stb_dxt.dll).
 */

#ifdef _WIN32

#include "crash_guard.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <windows.h>

/* Global state */
static char g_crash_log_path[MAX_PATH] = {0};
static FILE* g_crash_log_file = NULL;
static CRITICAL_SECTION g_crash_log_lock;
static bool g_initialized = false;

/* Helper: Get timestamp string */
static void get_timestamp(char* buffer, size_t size) {
    time_t now = time(NULL);
    struct tm* tm_info = localtime(&now);
    strftime(buffer, size, "%Y-%m-%d %H:%M:%S", tm_info);
}

/**
 * Get human-readable name for Windows exception code.
 */
const char* crash_guard_exception_name(DWORD code) {
    switch (code) {
        case EXCEPTION_ACCESS_VIOLATION:
            return "Access Violation (0xC0000005)";
        case EXCEPTION_ARRAY_BOUNDS_EXCEEDED:
            return "Array Bounds Exceeded (0xC000008C)";
        case EXCEPTION_BREAKPOINT:
            return "Breakpoint (0x80000003)";
        case EXCEPTION_DATATYPE_MISALIGNMENT:
            return "Datatype Misalignment (0x80000002)";
        case EXCEPTION_FLT_DENORMAL_OPERAND:
            return "Float Denormal Operand (0xC000008D)";
        case EXCEPTION_FLT_DIVIDE_BY_ZERO:
            return "Float Divide by Zero (0xC000008E)";
        case EXCEPTION_FLT_INEXACT_RESULT:
            return "Float Inexact Result (0xC000008F)";
        case EXCEPTION_FLT_INVALID_OPERATION:
            return "Float Invalid Operation (0xC0000090)";
        case EXCEPTION_FLT_OVERFLOW:
            return "Float Overflow (0xC0000091)";
        case EXCEPTION_FLT_STACK_CHECK:
            return "Float Stack Check (0xC0000092)";
        case EXCEPTION_FLT_UNDERFLOW:
            return "Float Underflow (0xC0000093)";
        case EXCEPTION_ILLEGAL_INSTRUCTION:
            return "Illegal Instruction (0xC000001D)";
        case EXCEPTION_IN_PAGE_ERROR:
            return "In Page Error (0xC0000006)";
        case EXCEPTION_INT_DIVIDE_BY_ZERO:
            return "Integer Divide by Zero (0xC0000094)";
        case EXCEPTION_INT_OVERFLOW:
            return "Integer Overflow (0xC0000095)";
        case EXCEPTION_INVALID_DISPOSITION:
            return "Invalid Disposition (0xC0000026)";
        case EXCEPTION_NONCONTINUABLE_EXCEPTION:
            return "Noncontinuable Exception (0xC0000025)";
        case EXCEPTION_PRIV_INSTRUCTION:
            return "Privileged Instruction (0xC0000096)";
        case EXCEPTION_SINGLE_STEP:
            return "Single Step (0x80000004)";
        case EXCEPTION_STACK_OVERFLOW:
            return "Stack Overflow (0xC00000FD)";
        default: {
            static char buffer[64];
            snprintf(buffer, sizeof(buffer), "Unknown Exception (0x%08lX)", code);
            return buffer;
        }
    }
}

/**
 * Initialize crash guard system.
 */
void crash_guard_init(const char* log_dir) {
    if (g_initialized) {
        return;
    }
    
    InitializeCriticalSection(&g_crash_log_lock);
    
    /* Build crash log path */
    if (log_dir && strlen(log_dir) > 0) {
        snprintf(g_crash_log_path, sizeof(g_crash_log_path), 
                 "%s\\%s", log_dir, CRASH_LOG_PATH);
    } else {
        /* Fallback to temp directory */
        char temp_path[MAX_PATH];
        GetTempPath(MAX_PATH, temp_path);
        snprintf(g_crash_log_path, sizeof(g_crash_log_path),
                 "%s%s", temp_path, CRASH_LOG_PATH);
    }
    
    /* Open log file in append mode */
    g_crash_log_file = fopen(g_crash_log_path, "a");
    if (g_crash_log_file) {
        char timestamp[64];
        get_timestamp(timestamp, sizeof(timestamp));
        fprintf(g_crash_log_file, "\n=== Crash Guard Initialized: %s ===\n", timestamp);
        fflush(g_crash_log_file);
    } else {
        fprintf(stderr, "[CRASH_GUARD] Warning: Could not open crash log: %s\n", g_crash_log_path);
    }
    
    g_initialized = true;
    fprintf(stderr, "[CRASH_GUARD] Initialized (log: %s)\n", g_crash_log_path);
}

/**
 * Log a crash to the crash log file.
 */
void crash_guard_log_crash(
    DWORD exception_code,
    void* exception_addr,
    const char* function_name,
    const char* additional_info
) {
    if (!g_initialized) {
        crash_guard_init(NULL);
    }
    
    EnterCriticalSection(&g_crash_log_lock);
    
    char timestamp[64];
    get_timestamp(timestamp, sizeof(timestamp));
    
    const char* exc_name = crash_guard_exception_name(exception_code);
    
    /* Log to file */
    if (g_crash_log_file) {
        fprintf(g_crash_log_file, "\n--- CRASH DETECTED ---\n");
        fprintf(g_crash_log_file, "Time:      %s\n", timestamp);
        fprintf(g_crash_log_file, "Function:  %s\n", function_name ? function_name : "Unknown");
        fprintf(g_crash_log_file, "Exception: %s\n", exc_name);
        fprintf(g_crash_log_file, "Address:   0x%p\n", exception_addr);
        if (additional_info) {
            fprintf(g_crash_log_file, "Info:      %s\n", additional_info);
        }
        fprintf(g_crash_log_file, "---\n");
        fflush(g_crash_log_file);
    }
    
    /* Also log to stderr for immediate visibility */
    fprintf(stderr, "\n[CRASH_GUARD] *** CRASH DETECTED ***\n");
    fprintf(stderr, "[CRASH_GUARD] Time:      %s\n", timestamp);
    fprintf(stderr, "[CRASH_GUARD] Function:  %s\n", function_name ? function_name : "Unknown");
    fprintf(stderr, "[CRASH_GUARD] Exception: %s\n", exc_name);
    fprintf(stderr, "[CRASH_GUARD] Address:   0x%p\n", exception_addr);
    if (additional_info) {
        fprintf(stderr, "[CRASH_GUARD] Info:      %s\n", additional_info);
    }
    fprintf(stderr, "[CRASH_GUARD] See crash log: %s\n", g_crash_log_path);
    fflush(stderr);
    
    LeaveCriticalSection(&g_crash_log_lock);
}

/**
 * Exception filter function - called by __except.
 */
int crash_guard_exception_filter(
    DWORD exception_code,
    EXCEPTION_POINTERS* exception_info,
    const char* function_name
) {
    void* exception_addr = NULL;
    
    if (exception_info && exception_info->ExceptionRecord) {
        exception_addr = exception_info->ExceptionRecord->ExceptionAddress;
    }
    
    /* Log the crash */
    crash_guard_log_crash(exception_code, exception_addr, function_name, NULL);
    
    /* Attempt to signal Python (best effort) */
    crash_guard_signal_python();
    
    /* 
     * Decision: Do we handle this exception or let it propagate?
     * 
     * For most exceptions, we want to HANDLE them to prevent process termination.
     * However, for stack overflow, we should let it propagate since we can't recover.
     */
    if (exception_code == EXCEPTION_STACK_OVERFLOW) {
        fprintf(stderr, "[CRASH_GUARD] Stack overflow - cannot handle, propagating\n");
        return EXCEPTION_CONTINUE_SEARCH;
    }
    
    /* Handle all other exceptions */
    return EXCEPTION_EXECUTE_HANDLER;
}

/**
 * Signal Python that a C-level crash occurred.
 * 
 * This is a best-effort mechanism. We write a flag file that Python can monitor.
 */
void crash_guard_signal_python(void) {
    /* Write a flag file that Python can check */
    char flag_path[MAX_PATH];
    char temp_path[MAX_PATH];
    GetTempPath(MAX_PATH, temp_path);
    snprintf(flag_path, sizeof(flag_path), "%s\\autoortho_c_crash.flag", temp_path);
    
    FILE* flag_file = fopen(flag_path, "w");
    if (flag_file) {
        char timestamp[64];
        get_timestamp(timestamp, sizeof(timestamp));
        fprintf(flag_file, "C_CRASH:%s\n", timestamp);
        fclose(flag_file);
    }
}

/**
 * Cleanup crash guard resources.
 */
void crash_guard_cleanup(void) {
    if (!g_initialized) {
        return;
    }
    
    EnterCriticalSection(&g_crash_log_lock);
    
    if (g_crash_log_file) {
        char timestamp[64];
        get_timestamp(timestamp, sizeof(timestamp));
        fprintf(g_crash_log_file, "=== Crash Guard Shutdown: %s ===\n\n", timestamp);
        fclose(g_crash_log_file);
        g_crash_log_file = NULL;
    }
    
    LeaveCriticalSection(&g_crash_log_lock);
    DeleteCriticalSection(&g_crash_log_lock);
    
    g_initialized = false;
}

#else /* Unix/Linux/macOS */

#include "crash_guard.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <signal.h>
#include <setjmp.h>
#include <unistd.h>
#include <pthread.h>
#include <execinfo.h>  // For backtrace
#include <sys/types.h>

/* Thread-local storage for crash recovery */
__thread sigjmp_buf crash_guard_jmp_buf;
__thread volatile sig_atomic_t crash_guard_active = 0;
__thread volatile int crash_guard_signal = 0;
__thread char crash_guard_function[256] = {0};

/* Global state */
static char g_crash_log_path[1024] = {0};
static FILE* g_crash_log_file = NULL;
static pthread_mutex_t g_crash_log_mutex = PTHREAD_MUTEX_INITIALIZER;
static bool g_initialized = false;
static struct sigaction g_old_segv_handler;
static struct sigaction g_old_bus_handler;
static struct sigaction g_old_fpe_handler;
static struct sigaction g_old_ill_handler;
static struct sigaction g_old_abrt_handler;

/* Helper: Get timestamp string */
static void get_timestamp(char* buffer, size_t size) {
    time_t now = time(NULL);
    struct tm* tm_info = localtime(&now);
    strftime(buffer, size, "%Y-%m-%d %H:%M:%S", tm_info);
}

/**
 * Get human-readable signal name.
 */
static const char* signal_name(int sig) {
    switch (sig) {
        case SIGSEGV: return "SIGSEGV (Segmentation Fault)";
        case SIGBUS:  return "SIGBUS (Bus Error)";
        case SIGFPE:  return "SIGFPE (Floating Point Exception)";
        case SIGILL:  return "SIGILL (Illegal Instruction)";
        case SIGABRT: return "SIGABRT (Abort)";
        default: {
            static char buffer[64];
            snprintf(buffer, sizeof(buffer), "Signal %d", sig);
            return buffer;
        }
    }
}

/**
 * Log a crash (Unix version).
 * 
 * Note: This is called from a signal handler, so we must only use
 * async-signal-safe functions. fprintf/write are technically safe for stderr.
 */
void crash_guard_log_crash_unix(
    int signal_num,
    void* fault_addr,
    const char* function_name
) {
    /* Use write() instead of fprintf() for async-signal-safety */
    char msg[2048];
    char timestamp[64];
    get_timestamp(timestamp, sizeof(timestamp));
    
    int len = snprintf(msg, sizeof(msg),
        "\n[CRASH_GUARD] *** CRASH DETECTED ***\n"
        "[CRASH_GUARD] Time:      %s\n"
        "[CRASH_GUARD] Signal:    %s\n"
        "[CRASH_GUARD] Function:  %s\n"
        "[CRASH_GUARD] Address:   %p\n",
        timestamp,
        signal_name(signal_num),
        function_name ? function_name : "Unknown",
        fault_addr
    );
    
    /* Write to stderr (async-signal-safe) */
    write(STDERR_FILENO, msg, len);
    
    /* Try to get backtrace (may not be async-signal-safe) */
#ifdef __GLIBC__
    void* backtrace_buffer[100];
    int nptrs = backtrace(backtrace_buffer, 100);
    if (nptrs > 0) {
        write(STDERR_FILENO, "[CRASH_GUARD] Backtrace:\n", 25);
        backtrace_symbols_fd(backtrace_buffer, nptrs, STDERR_FILENO);
    }
#endif
    
    /* Try to write to log file (less safe) */
    if (g_crash_log_file) {
        /* Note: fwrite/fprintf are NOT async-signal-safe, but we'll try anyway */
        fprintf(g_crash_log_file, "\n--- CRASH DETECTED ---\n");
        fprintf(g_crash_log_file, "Time:      %s\n", timestamp);
        fprintf(g_crash_log_file, "Signal:    %s\n", signal_name(signal_num));
        fprintf(g_crash_log_file, "Function:  %s\n", function_name ? function_name : "Unknown");
        fprintf(g_crash_log_file, "Address:   %p\n", fault_addr);
        fprintf(g_crash_log_file, "---\n");
        fflush(g_crash_log_file);
    }
    
    /* Write flag file for Python monitoring */
    char flag_path[1024];
    snprintf(flag_path, sizeof(flag_path), "/tmp/autoortho_c_crash.flag");
    int fd = open(flag_path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd >= 0) {
        write(fd, timestamp, strlen(timestamp));
        close(fd);
    }
}

/**
 * Signal handler for crashes.
 * 
 * This is called when SIGSEGV, SIGBUS, etc. occur.
 * If crash_guard_active is set, we longjmp back to the guarded call.
 * Otherwise, we log and re-raise the signal to get default behavior.
 */
void crash_guard_signal_handler(int sig, siginfo_t *info, void *context) {
    void* fault_addr = info->si_addr;
    
    /* Log the crash */
    crash_guard_log_crash_unix(sig, fault_addr, crash_guard_function);
    
    /* If we have an active guard, longjmp back to it */
    if (crash_guard_active) {
        crash_guard_signal = sig;
        crash_guard_active = 0;  /* Prevent re-entry */
        
        /* 
         * WARNING: longjmp from signal handler is undefined behavior!
         * However, it works on most systems and is better than crashing.
         * 
         * Alternative: Just log and die gracefully
         */
        siglongjmp(crash_guard_jmp_buf, 1);
        
        /* Never reaches here */
    } else {
        /* No active guard - restore default handler and re-raise */
        struct sigaction sa;
        sa.sa_handler = SIG_DFL;
        sigemptyset(&sa.sa_mask);
        sa.sa_flags = 0;
        sigaction(sig, &sa, NULL);
        
        /* Re-raise signal to get default behavior (core dump, etc.) */
        raise(sig);
    }
}

/**
 * Initialize crash guard system (Unix version).
 */
void crash_guard_init(const char* log_dir) {
    if (g_initialized) {
        return;
    }
    
    pthread_mutex_lock(&g_crash_log_mutex);
    
    /* Build crash log path */
    if (log_dir && strlen(log_dir) > 0) {
        snprintf(g_crash_log_path, sizeof(g_crash_log_path), 
                 "%s/c_crash.log", log_dir);
    } else {
        /* Fallback to /tmp */
        snprintf(g_crash_log_path, sizeof(g_crash_log_path),
                 "/tmp/autoortho_c_crash.log");
    }
    
    /* Open log file in append mode */
    g_crash_log_file = fopen(g_crash_log_path, "a");
    if (g_crash_log_file) {
        char timestamp[64];
        get_timestamp(timestamp, sizeof(timestamp));
        fprintf(g_crash_log_file, "\n=== Crash Guard Initialized: %s ===\n", timestamp);
        fflush(g_crash_log_file);
    } else {
        fprintf(stderr, "[CRASH_GUARD] Warning: Could not open crash log: %s\n", 
                g_crash_log_path);
    }
    
    /* Install signal handlers */
    struct sigaction sa;
    sa.sa_sigaction = crash_guard_signal_handler;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = SA_SIGINFO | SA_NODEFER;  /* SA_NODEFER allows handler recursion */
    
    sigaction(SIGSEGV, &sa, &g_old_segv_handler);
    sigaction(SIGBUS,  &sa, &g_old_bus_handler);
    sigaction(SIGFPE,  &sa, &g_old_fpe_handler);
    sigaction(SIGILL,  &sa, &g_old_ill_handler);
    sigaction(SIGABRT, &sa, &g_old_abrt_handler);
    
    g_initialized = true;
    fprintf(stderr, "[CRASH_GUARD] Initialized (Unix, log: %s)\n", g_crash_log_path);
    
    pthread_mutex_unlock(&g_crash_log_mutex);
}

/**
 * Cleanup crash guard (Unix version).
 */
void crash_guard_cleanup(void) {
    if (!g_initialized) {
        return;
    }
    
    pthread_mutex_lock(&g_crash_log_mutex);
    
    /* Restore original signal handlers */
    sigaction(SIGSEGV, &g_old_segv_handler, NULL);
    sigaction(SIGBUS,  &g_old_bus_handler, NULL);
    sigaction(SIGFPE,  &g_old_fpe_handler, NULL);
    sigaction(SIGILL,  &g_old_ill_handler, NULL);
    sigaction(SIGABRT, &g_old_abrt_handler, NULL);
    
    /* Close log file */
    if (g_crash_log_file) {
        char timestamp[64];
        get_timestamp(timestamp, sizeof(timestamp));
        fprintf(g_crash_log_file, "=== Crash Guard Shutdown: %s ===\n\n", timestamp);
        fclose(g_crash_log_file);
        g_crash_log_file = NULL;
    }
    
    g_initialized = false;
    
    pthread_mutex_unlock(&g_crash_log_mutex);
}

#endif /* _WIN32 */

