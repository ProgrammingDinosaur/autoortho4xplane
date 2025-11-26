/**
 * pydds_safe_wrapper.c
 * 
 * Crash-protected wrappers for external compression libraries.
 * 
 * This provides safe wrappers around:
 * - ispc_texcomp (CompressBlocksBC1, CompressBlocksBC3)
 * - stb_dxt (compress_pixels)
 * 
 * These external libraries can crash on malformed data or due to bugs.
 * Since we don't own the source code, we can't modify them directly.
 * Instead, we wrap the calls with crash protection.
 * 
 * Usage from Python (pydds.py):
 *   result = safe_CompressBlocksBC1(src, dst)
 *   if result == 0:
 *       # Crashed - use fallback
 */

#include <stdint.h>
#include <string.h>
#include "aoimage/crash_guard.h"

/* rgba_surface structure (must match pydds.py definition) */
typedef struct {
    char* data;
    uint32_t width;
    uint32_t height;
    uint32_t stride;
} rgba_surface;

/* Forward declarations for external library functions */
/* These are implemented in ispc_texcomp.dll/.so/.dylib */
#ifdef _WIN32
__declspec(dllimport) void CompressBlocksBC1(rgba_surface* src, uint8_t* dst);
__declspec(dllimport) void CompressBlocksBC3(rgba_surface* src, uint8_t* dst);
#else
extern void CompressBlocksBC1(rgba_surface* src, uint8_t* dst);
extern void CompressBlocksBC3(rgba_surface* src, uint8_t* dst);
#endif

/* Export symbols for Python to call */
#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

/**
 * Initialize crash guard.
 * Call this from Python at module load.
 */
EXPORT void pydds_safe_init(const char* log_dir) {
    crash_guard_init(log_dir);
}

/**
 * Cleanup crash guard.
 * Call this from Python at module unload.
 */
EXPORT void pydds_safe_cleanup(void) {
    crash_guard_cleanup();
}

/**
 * Safe wrapper for CompressBlocksBC1.
 * 
 * Returns:
 *   1 on success
 *   0 on crash or invalid parameters
 */
EXPORT int safe_CompressBlocksBC1(rgba_surface* src, uint8_t* dst) {
    /* Validate parameters before calling external library */
    if (!src || !dst || !src->data) {
        return 0;  /* Invalid parameters */
    }
    
    /* Additional validation */
    if (src->width == 0 || src->height == 0) {
        return 0;  /* Invalid dimensions */
    }
    
    if (src->width % 4 != 0 || src->height % 4 != 0) {
        return 0;  /* BC1 requires dimensions divisible by 4 */
    }
    
    /* Call external library with crash protection */
    int result;
    CRASH_GUARDED_CALL(
        result,
        (CompressBlocksBC1(src, dst), 1),  /* Returns 1 if no crash */
        "CompressBlocksBC1"
    );
    
    return result;  /* 0 if crashed, 1 if succeeded */
}

/**
 * Safe wrapper for CompressBlocksBC3.
 * 
 * Returns:
 *   1 on success
 *   0 on crash or invalid parameters
 */
EXPORT int safe_CompressBlocksBC3(rgba_surface* src, uint8_t* dst) {
    /* Validate parameters */
    if (!src || !dst || !src->data) {
        return 0;
    }
    
    if (src->width == 0 || src->height == 0) {
        return 0;
    }
    
    if (src->width % 4 != 0 || src->height % 4 != 0) {
        return 0;  /* BC3 requires dimensions divisible by 4 */
    }
    
    /* Call external library with crash protection */
    int result;
    CRASH_GUARDED_CALL(
        result,
        (CompressBlocksBC3(src, dst), 1),
        "CompressBlocksBC3"
    );
    
    return result;
}

/**
 * Safe wrapper for STB DXT compression (if used).
 * 
 * compress_pixels signature:
 *   void compress_pixels(uint8_t* dst, uint8_t* src, 
 *                        uint64_t width, uint64_t height, bool is_rgba)
 */
#ifdef USE_STB_DXT
#ifdef _WIN32
__declspec(dllimport) void compress_pixels(uint8_t* dst, uint8_t* src, 
                                           uint64_t width, uint64_t height, 
                                           int is_rgba);
#else
extern void compress_pixels(uint8_t* dst, uint8_t* src,
                           uint64_t width, uint64_t height,
                           int is_rgba);
#endif

EXPORT int safe_compress_pixels_stb(uint8_t* dst, uint8_t* src,
                                    uint64_t width, uint64_t height,
                                    int is_rgba) {
    if (!src || !dst) {
        return 0;
    }
    
    if (width == 0 || height == 0) {
        return 0;
    }
    
    int result;
    CRASH_GUARDED_CALL(
        result,
        (compress_pixels(dst, src, width, height, is_rgba), 1),
        "compress_pixels_stb"
    );
    
    return result;
}
#endif /* USE_STB_DXT */

