/**
 * aodds.c - Native DDS Texture Building Implementation
 * 
 * Complete native pipeline for building DDS textures from cached JPEGs.
 * Includes ISPC texcomp integration for high-performance compression.
 */

/* Enable dladdr() on Linux/glibc */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include "aodds.h"
#include "aodecode.h"
#include "aocache.h"
#include "internal.h"
#include <stdio.h>
#include <string.h>
#include <math.h>
#include <turbojpeg.h>

#ifdef AOPIPELINE_WINDOWS
#include <windows.h>
#include <io.h>  /* For _get_osfhandle, _fileno */
#else
#include <dlfcn.h>
#include <sys/time.h>
#endif

/* ============================================================================
 * SIMD Detection and Headers
 * ============================================================================
 * SSE2 is available on all x86-64 processors. We use it for faster
 * mipmap reduction when the data is appropriately aligned.
 */
#if defined(__SSE2__) || (defined(_MSC_VER) && (defined(_M_X64) || defined(_M_AMD64)))
#include <emmintrin.h>
#define AODDS_HAS_SSE2 1
#else
#define AODDS_HAS_SSE2 0
#endif

/* Version string */
#define AODDS_VERSION "1.0.0"

/* DDS file constants */
#define DDS_MAGIC 0x20534444  /* "DDS " */
#define DDS_HEADER_SIZE 128
#define CHUNK_SIZE 256

/* DDS header flags */
#define DDSD_CAPS        0x00000001
#define DDSD_HEIGHT      0x00000002
#define DDSD_WIDTH       0x00000004
#define DDSD_PITCH       0x00000008
#define DDSD_PIXELFORMAT 0x00001000
#define DDSD_MIPMAPCOUNT 0x00020000
#define DDSD_LINEARSIZE  0x00080000
#define DDSD_DEPTH       0x00800000

#define DDPF_FOURCC      0x00000004

#define DDSCAPS_TEXTURE  0x00001000
#define DDSCAPS_MIPMAP   0x00400000
#define DDSCAPS_COMPLEX  0x00000008

/* FourCC codes */
#define FOURCC_DXT1 0x31545844  /* "DXT1" */
#define FOURCC_DXT5 0x35545844  /* "DXT5" */

/* I/O optimization: 256KB stdio buffer for efficient large writes */
#define AODDS_WRITE_BUFFER_SIZE (256 * 1024)

/*============================================================================
 * ISPC Texture Compression
 *============================================================================*/

/* ISPC function pointer types */
typedef struct {
    uint8_t* ptr;
    int32_t width;
    int32_t height;
    int32_t stride;
} rgba_surface_t;

typedef void (*CompressBlocksBC1_fn)(const rgba_surface_t*, uint8_t*);
typedef void (*CompressBlocksBC3_fn)(const rgba_surface_t*, uint8_t*);

/* Global function pointers */
static CompressBlocksBC1_fn ispc_compress_bc1 = NULL;
static CompressBlocksBC3_fn ispc_compress_bc3 = NULL;
static int ispc_initialized = 0;
static int ispc_available = 0;
static int force_fallback = 0;  /* When set, use fallback even if ISPC is available */

#ifdef AOPIPELINE_WINDOWS
static HMODULE ispc_lib = NULL;
#else
static void* ispc_lib = NULL;
#endif

/* ============================================================================
 * TLS Fallback Mutex Protection
 * ============================================================================
 * When TLS is unavailable, static file write buffers become global. This mutex
 * ensures thread-safe access to those shared buffers in that rare case.
 */
#if !TLS_AVAILABLE
#ifdef AOPIPELINE_WINDOWS
static CRITICAL_SECTION g_write_buffer_cs;
static int g_write_buffer_cs_init = 0;
#define WRITE_BUFFER_LOCK() do { \
    if (!g_write_buffer_cs_init) { InitializeCriticalSection(&g_write_buffer_cs); g_write_buffer_cs_init = 1; } \
    EnterCriticalSection(&g_write_buffer_cs); \
} while(0)
#define WRITE_BUFFER_UNLOCK() LeaveCriticalSection(&g_write_buffer_cs)
#else
static pthread_mutex_t g_write_buffer_mutex = PTHREAD_MUTEX_INITIALIZER;
#define WRITE_BUFFER_LOCK() pthread_mutex_lock(&g_write_buffer_mutex)
#define WRITE_BUFFER_UNLOCK() pthread_mutex_unlock(&g_write_buffer_mutex)
#endif
#else
#define WRITE_BUFFER_LOCK() ((void)0)
#define WRITE_BUFFER_UNLOCK() ((void)0)
#endif

/* Fallback compression warning state */
static int fallback_warning_issued = 0;

/* ============================================================================
 * Thread-Safe TurboJPEG Decoder Pool (Dynamic Size)
 * ============================================================================
 * Problem: Multiple Python threads can call build_from_jpegs simultaneously,
 * each spawning its own OpenMP parallel region. Using omp_get_thread_num()
 * for decoder selection fails because thread IDs collide across regions.
 * 
 * Solution: A global pool of decoders with mutex-protected acquisition/release.
 * - Acquisition is O(n) scan but n is typically small and lock is brief
 * - JPEG decoding happens OUTSIDE the lock (full parallelism)
 * - Decoders are reused across calls (no repeated init/destroy overhead)
 * - Graceful overflow: if all slots are busy, creates temporary decoder
 * 
 * Pool size is configurable at runtime via aodds_init_decoder_pool().
 * Default size is 64 if not explicitly initialized.
 * ============================================================================ */
#define DECODER_POOL_DEFAULT_SIZE 64
/* No max cap - users can request as many decoders as their config demands */

static tjhandle* g_decoder_pool = NULL;
static volatile int* g_decoder_in_use = NULL;
static volatile int g_decoder_pool_size = 0;
static volatile int g_decoder_pool_init = 0;

#ifdef AOPIPELINE_WINDOWS
static CRITICAL_SECTION g_decoder_cs;
static volatile LONG g_decoder_cs_init = 0;

static void decoder_pool_lock(void) {
    if (InterlockedCompareExchange(&g_decoder_cs_init, 1, 0) == 0) {
        InitializeCriticalSection(&g_decoder_cs);
        InterlockedExchange(&g_decoder_cs_init, 2);
    }
    while (g_decoder_cs_init != 2) { Sleep(0); }
    EnterCriticalSection(&g_decoder_cs);
}
static void decoder_pool_unlock(void) {
    LeaveCriticalSection(&g_decoder_cs);
}
#else
static pthread_mutex_t g_decoder_mutex = PTHREAD_MUTEX_INITIALIZER;
static void decoder_pool_lock(void) { pthread_mutex_lock(&g_decoder_mutex); }
static void decoder_pool_unlock(void) { pthread_mutex_unlock(&g_decoder_mutex); }
#endif

/* Initialize pool with default size (called under lock) */
static void decoder_pool_init_if_needed(void) {
    if (!g_decoder_pool_init) {
        /* Allocate with default size if not already initialized */
        if (!g_decoder_pool) {
            g_decoder_pool_size = DECODER_POOL_DEFAULT_SIZE;
            g_decoder_pool = (tjhandle*)calloc(g_decoder_pool_size, sizeof(tjhandle));
            g_decoder_in_use = (volatile int*)calloc(g_decoder_pool_size, sizeof(int));
        }
        if (g_decoder_pool && g_decoder_in_use) {
            for (int i = 0; i < g_decoder_pool_size; i++) {
            g_decoder_pool[i] = NULL;
            g_decoder_in_use[i] = 0;
        }
        g_decoder_pool_init = 1;
    }
    }
}

/**
 * Initialize the decoder pool with a specific size.
 * Must be called before any decoding operations for the size to take effect.
 * If called after decoding has started, returns 0 (failure).
 * 
 * @param pool_size  Number of decoders to pool (minimum 1, no upper limit)
 * @return 1 on success, 0 if pool already in use
 */
AODDS_API int32_t aodds_init_decoder_pool(int32_t pool_size) {
    decoder_pool_lock();
    
    /* Cannot resize if pool is already initialized and in use */
    if (g_decoder_pool_init) {
        decoder_pool_unlock();
        return 0;
    }
    
    /* Minimum pool size of 1 */
    if (pool_size < 1) pool_size = 1;
    
    /* Free any existing allocations */
    if (g_decoder_pool) {
        free(g_decoder_pool);
        g_decoder_pool = NULL;
    }
    if (g_decoder_in_use) {
        free((void*)g_decoder_in_use);
        g_decoder_in_use = NULL;
    }
    
    /* Allocate new pool */
    g_decoder_pool_size = pool_size;
    g_decoder_pool = (tjhandle*)calloc(pool_size, sizeof(tjhandle));
    g_decoder_in_use = (volatile int*)calloc(pool_size, sizeof(int));
    
    if (!g_decoder_pool || !g_decoder_in_use) {
        /* Allocation failed - clean up */
        if (g_decoder_pool) free(g_decoder_pool);
        if (g_decoder_in_use) free((void*)g_decoder_in_use);
        g_decoder_pool = NULL;
        g_decoder_in_use = NULL;
        g_decoder_pool_size = 0;
        decoder_pool_unlock();
        return 0;
    }
    
    g_decoder_pool_init = 1;
    decoder_pool_unlock();
    return 1;
}

/**
 * Get the current decoder pool size.
 * @return Current pool size, or default if not initialized
 */
AODDS_API int32_t aodds_get_decoder_pool_size(void) {
    if (g_decoder_pool_size > 0) {
        return g_decoder_pool_size;
    }
    return DECODER_POOL_DEFAULT_SIZE;
}

/**
 * Get decoder pool statistics.
 * @param out_pool_size     Output: total pool size
 * @param out_in_use        Output: number currently in use
 * @param out_allocated     Output: number of decoders actually created
 */
AODDS_API void aodds_get_decoder_pool_stats(
    int32_t* out_pool_size,
    int32_t* out_in_use,
    int32_t* out_allocated
) {
    decoder_pool_lock();
    decoder_pool_init_if_needed();
    
    int in_use = 0;
    int allocated = 0;
    
    for (int i = 0; i < g_decoder_pool_size; i++) {
        if (g_decoder_in_use[i]) in_use++;
        if (g_decoder_pool[i]) allocated++;
    }
    
    if (out_pool_size) *out_pool_size = g_decoder_pool_size;
    if (out_in_use) *out_in_use = in_use;
    if (out_allocated) *out_allocated = allocated;
    
    decoder_pool_unlock();
}

/* Acquire a decoder from pool - creates on first use, reuses thereafter */
static tjhandle acquire_pooled_decoder(int* out_from_pool) {
    decoder_pool_lock();
    decoder_pool_init_if_needed();
    
    /* Find a free slot */
    if (g_decoder_pool && g_decoder_in_use) {
        for (int i = 0; i < g_decoder_pool_size; i++) {
        if (!g_decoder_in_use[i]) {
            g_decoder_in_use[i] = 1;
            /* Create decoder on first use of this slot */
            if (!g_decoder_pool[i]) {
                g_decoder_pool[i] = tjInitDecompress();
            }
            decoder_pool_unlock();
            if (out_from_pool) *out_from_pool = 1;
            return g_decoder_pool[i];
            }
        }
    }
    
    decoder_pool_unlock();
    
    /* All slots busy - create temporary decoder (overflow case) */
    if (out_from_pool) *out_from_pool = 0;
    return tjInitDecompress();
}

/* Release decoder back to pool (or destroy if it was temporary) */
static void release_pooled_decoder(tjhandle tjh, int from_pool) {
    if (!tjh) return;
    
    if (from_pool && g_decoder_pool) {
        decoder_pool_lock();
        for (int i = 0; i < g_decoder_pool_size; i++) {
            if (g_decoder_pool[i] == tjh) {
                g_decoder_in_use[i] = 0;
                decoder_pool_unlock();
                return;
            }
        }
        decoder_pool_unlock();
    }
    
    /* Either not from pool or not found - destroy it */
    tjDestroy(tjh);
}

/* Helper to get directory containing a path */
static void get_directory(char* dest, size_t dest_size, const char* path) {
    /* Use snprintf for safe copy with guaranteed null-termination */
    snprintf(dest, dest_size, "%s", path);
    
    /* Find last separator */
    char* last_sep = NULL;
    for (char* p = dest; *p; p++) {
#ifdef AOPIPELINE_WINDOWS
        if (*p == '\\' || *p == '/') last_sep = p;
#else
        if (*p == '/') last_sep = p;
#endif
    }
    if (last_sep) {
        *last_sep = '\0';
    }
}

/* Atomic rename helper - uses MoveFileEx on Windows for true atomicity */
static int32_t atomic_rename(const char* temp_path, const char* target_path) {
#ifdef AOPIPELINE_WINDOWS
    /* MoveFileEx with MOVEFILE_REPLACE_EXISTING is atomic and handles existing target */
    if (MoveFileExA(temp_path, target_path, MOVEFILE_REPLACE_EXISTING)) {
        return 1;
    }
    /* Fallback for edge cases (e.g., cross-volume moves) */
    DeleteFileA(target_path);
    if (MoveFileA(temp_path, target_path)) {
        return 1;
    }
    return 0;
#else
    /* POSIX rename is already atomic */
    return (rename(temp_path, target_path) == 0) ? 1 : 0;
#endif
}

AODDS_API int32_t aodds_init_ispc(void) {
    if (ispc_initialized) {
        return ispc_available;
    }
    ispc_initialized = 1;
    
    /* Determine library name and subdirectory */
    const char* lib_name;
    const char* lib_subdir;
#ifdef AOPIPELINE_WINDOWS
    lib_name = "ispc_texcomp.dll";
    lib_subdir = "windows";
#elif defined(AOPIPELINE_MACOS)
    lib_name = "libispc_texcomp.dylib";
    lib_subdir = "macos";
#else
    lib_name = "libispc_texcomp.so";
    lib_subdir = "linux";
#endif
    
    char module_dir[4096] = {0};
    char lib_path[4096] = {0};
    
#ifdef AOPIPELINE_WINDOWS
    /* Get path to this DLL (aopipeline.dll) */
    char module_path[4096] = {0};
    HMODULE self_module = NULL;
    GetModuleHandleExA(
        GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
        (LPCSTR)aodds_init_ispc,
        &self_module
    );
    if (self_module) {
        GetModuleFileNameA(self_module, module_path, sizeof(module_path));
        get_directory(module_dir, sizeof(module_dir), module_path);
        
        /* 
         * aopipeline.dll is in: autoortho/aopipeline/lib/windows/
         * ISPC lib is in:       autoortho/lib/windows/
         * So we need: ../../lib/windows/ispc_texcomp.dll
         */
        snprintf(lib_path, sizeof(lib_path), "%s\\..\\..\\..\\lib\\%s\\%s", 
                 module_dir, lib_subdir, lib_name);
        ispc_lib = LoadLibraryA(lib_path);
        
        /* Also try the aopipeline lib dir (in case user copied it there) */
        if (!ispc_lib) {
            snprintf(lib_path, sizeof(lib_path), "%s\\%s", module_dir, lib_name);
            ispc_lib = LoadLibraryA(lib_path);
        }
    }
    
    /* Fallback: try just the library name (relies on PATH) */
    if (!ispc_lib) {
        ispc_lib = LoadLibraryA(lib_name);
    }
    
    if (ispc_lib) {
        ispc_compress_bc1 = (CompressBlocksBC1_fn)GetProcAddress(ispc_lib, "CompressBlocksBC1");
        ispc_compress_bc3 = (CompressBlocksBC3_fn)GetProcAddress(ispc_lib, "CompressBlocksBC3");
    }
#else
    /* Unix: use dladdr to find this shared library's path */
    Dl_info dl_info;
    if (dladdr((void*)aodds_init_ispc, &dl_info) && dl_info.dli_fname) {
        get_directory(module_dir, sizeof(module_dir), dl_info.dli_fname);
        
        /* 
         * libaopipeline.so is in: autoortho/aopipeline/lib/{linux,macos}/
         * ISPC lib is in:         autoortho/lib/{linux,macos}/
         * So we need: ../../../lib/{platform}/libispc_texcomp.{so,dylib}
         */
        snprintf(lib_path, sizeof(lib_path), "%s/../../../lib/%s/%s", 
                 module_dir, lib_subdir, lib_name);
        ispc_lib = dlopen(lib_path, RTLD_NOW);
        
        /* Also try the aopipeline lib dir (in case user copied it there) */
        if (!ispc_lib) {
            snprintf(lib_path, sizeof(lib_path), "%s/%s", module_dir, lib_name);
            ispc_lib = dlopen(lib_path, RTLD_NOW);
        }
    }
    
    /* Fallback: try standard library paths */
    if (!ispc_lib) {
        ispc_lib = dlopen(lib_name, RTLD_NOW);
    }
    
    if (ispc_lib) {
        ispc_compress_bc1 = (CompressBlocksBC1_fn)dlsym(ispc_lib, "CompressBlocksBC1");
        ispc_compress_bc3 = (CompressBlocksBC3_fn)dlsym(ispc_lib, "CompressBlocksBC3");
    }
#endif
    
    ispc_available = (ispc_compress_bc1 != NULL && ispc_compress_bc3 != NULL);
    return ispc_available;
}

AODDS_API void aodds_set_use_ispc(int32_t use_ispc) {
    force_fallback = !use_ispc;
}

AODDS_API int32_t aodds_get_use_ispc(void) {
    return ispc_available && !force_fallback;
}

AODDS_API int32_t aodds_using_fallback_compressor(void) {
    aodds_init_ispc();
    return (!ispc_available || force_fallback) ? 1 : 0;
}

/*============================================================================
 * Timing Utilities
 *============================================================================*/

static double get_time_ms(void) {
#ifdef AOPIPELINE_WINDOWS
    LARGE_INTEGER freq, counter;
    QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&counter);
    return (double)counter.QuadPart * 1000.0 / (double)freq.QuadPart;
#else
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (double)tv.tv_sec * 1000.0 + (double)tv.tv_usec / 1000.0;
#endif
}

/*============================================================================
 * DDS Header Generation
 *============================================================================*/

AODDS_API int32_t aodds_calc_mipmap_count(int32_t width, int32_t height) {
    int32_t count = 1;
    while (width > 4 && height > 4) {
        width /= 2;
        height /= 2;
        count++;
    }
    return count;
}

AODDS_API size_t aodds_calc_dds_size(
    int32_t width,
    int32_t height,
    int32_t mipmap_count,
    dds_format_t format
) {
    if (mipmap_count <= 0) {
        mipmap_count = aodds_calc_mipmap_count(width, height);
    }
    
    size_t block_size = (format == DDS_FORMAT_BC1) ? 8 : 16;
    size_t total_size = DDS_HEADER_SIZE;
    
    int32_t w = width;
    int32_t h = height;
    
    for (int32_t i = 0; i < mipmap_count; i++) {
        size_t blocks_x = (size_t)((w + 3) / 4);
        size_t blocks_y = (size_t)((h + 3) / 4);
        total_size += blocks_x * blocks_y * block_size;
        w = AOPIPELINE_MAX(w / 2, 1);
        h = AOPIPELINE_MAX(h / 2, 1);
    }
    
    return total_size;
}

/* ============================================================================
 * File I/O Optimization Helpers
 * ============================================================================ */

/* Pre-size file to expected size for contiguous block allocation.
 * This is a best-effort optimization - failure is non-fatal.
 * (Self-contained duplicate from aobundle2.c) */
static int32_t preallocate_file_dds(FILE* f, size_t size) {
    if (!f || size == 0) return 0;
    
#ifdef AOPIPELINE_LINUX
    /* Linux: posix_fallocate allocates physical blocks */
    int fd = fileno(f);
    if (fd >= 0) {
        if (posix_fallocate(fd, 0, (off_t)size) == 0) {
            return 1;
        }
    }
#elif defined(AOPIPELINE_MACOS)
    /* macOS: Use F_PREALLOCATE for physical block allocation */
    int fd = fileno(f);
    if (fd >= 0) {
        fstore_t store = {0};
        store.fst_flags = F_ALLOCATECONTIG;  /* Try contiguous first */
        store.fst_posmode = F_PEOFPOSMODE;
        store.fst_offset = 0;
        store.fst_length = (off_t)size;
        store.fst_bytesalloc = 0;
        
        if (fcntl(fd, F_PREALLOCATE, &store) == -1) {
            /* Fall back to non-contiguous allocation */
            store.fst_flags = F_ALLOCATEALL;
            fcntl(fd, F_PREALLOCATE, &store);
        }
        /* Set the file size to match */
        if (ftruncate(fd, (off_t)size) == 0) {
            return 1;
        }
    }
#elif defined(AOPIPELINE_WINDOWS)
    int fd = _fileno(f);
    if (fd >= 0) {
        HANDLE hFile = (HANDLE)_get_osfhandle(fd);
        if (hFile != INVALID_HANDLE_VALUE) {
            LARGE_INTEGER li;
            li.QuadPart = (LONGLONG)size;
            if (SetFilePointerEx(hFile, li, NULL, FILE_BEGIN) &&
                SetEndOfFile(hFile)) {
                li.QuadPart = 0;
                SetFilePointerEx(hFile, li, NULL, FILE_BEGIN);
                return 1;
            }
        }
    }
#else
    /* Generic POSIX fallback */
    int fd = fileno(f);
    if (fd >= 0) {
        if (ftruncate(fd, (off_t)size) == 0) {
            return 1;
        }
    }
#endif
    
    return 0;
}

/* Calculate expected DDS file size for pre-allocation */
static uint32_t calc_dds_file_size(int32_t tile_size, int32_t mipmap_count, dds_format_t format) {
    uint32_t block_size = (format == DDS_FORMAT_BC1) ? 8 : 16;
    uint32_t total_size = DDS_HEADER_SIZE;
    
    int32_t size = tile_size;
    for (int32_t mip = 0; mip < mipmap_count && size >= 4; mip++) {
        uint32_t blocks_x = (size + 3) / 4;
        uint32_t blocks_y = (size + 3) / 4;
        total_size += blocks_x * blocks_y * block_size;
        size /= 2;
    }
    
    return total_size;
}

AODDS_API int32_t aodds_write_header(
    uint8_t* buffer,
    int32_t width,
    int32_t height,
    int32_t mipmap_count,
    dds_format_t format
) {
    if (!buffer) return 0;
    
    memset(buffer, 0, DDS_HEADER_SIZE);
    
    uint32_t* header = (uint32_t*)buffer;
    
    /* Magic number */
    header[0] = DDS_MAGIC;
    
    /* Header size (always 124 for DDS_HEADER) */
    header[1] = 124;
    
    /* Flags */
    header[2] = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | 
                DDSD_MIPMAPCOUNT | DDSD_LINEARSIZE;
    
    /* Height and width */
    header[3] = height;
    header[4] = width;
    
    /* Pitch/linear size (size of top-level mipmap) */
    uint32_t block_size = (format == DDS_FORMAT_BC1) ? 8 : 16;
    uint32_t blocks_x = (width + 3) / 4;
    uint32_t blocks_y = (height + 3) / 4;
    header[5] = blocks_x * blocks_y * block_size;
    
    /* Depth (unused for 2D textures) */
    header[6] = 0;
    
    /* Mipmap count */
    header[7] = mipmap_count;
    
    /* Reserved (11 DWORDs) - already zeroed */
    
    /* Pixel format starts at offset 76 (DWORD 19) */
    uint32_t* pf = &header[19];
    
    /* Pixel format size */
    pf[0] = 32;
    
    /* Pixel format flags */
    pf[1] = DDPF_FOURCC;
    
    /* FourCC */
    pf[2] = (format == DDS_FORMAT_BC1) ? FOURCC_DXT1 : FOURCC_DXT5;
    
    /* RGB bit counts (unused for compressed) */
    pf[3] = 0;
    pf[4] = 0;
    pf[5] = 0;
    pf[6] = 0;
    pf[7] = 0;
    
    /* Caps at offset 108 (DWORD 27) */
    header[27] = DDSCAPS_TEXTURE | DDSCAPS_MIPMAP | DDSCAPS_COMPLEX;
    
    return DDS_HEADER_SIZE;
}

/*============================================================================
 * Image Operations
 *============================================================================*/

AODDS_API int32_t aodds_compose_chunks(
    aodecode_image_t* chunks,
    int32_t chunks_per_side,
    aodecode_image_t* output
) {
    if (!chunks || !output || !output->data || chunks_per_side <= 0) {
        return 0;
    }
    
    int32_t tile_size = chunks_per_side * CHUNK_SIZE;
    if (output->width != tile_size || output->height != tile_size) {
        return 0;
    }
    
    int32_t chunk_count = chunks_per_side * chunks_per_side;
    
    /* Compose chunks into output - can be parallelized */
#pragma omp parallel for schedule(dynamic, 4)
    for (int32_t i = 0; i < chunk_count; i++) {
        if (!chunks[i].data || chunks[i].width != CHUNK_SIZE || 
            chunks[i].height != CHUNK_SIZE) {
            continue;  /* Skip invalid chunks (will use fill color) */
        }
        
        int32_t chunk_row = i / chunks_per_side;
        int32_t chunk_col = i % chunks_per_side;
        int32_t dest_x = chunk_col * CHUNK_SIZE;
        int32_t dest_y = chunk_row * CHUNK_SIZE;
        
        /* Copy chunk data to output */
        for (int32_t y = 0; y < CHUNK_SIZE; y++) {
            uint8_t* src_row = chunks[i].data + (y * chunks[i].stride);
            uint8_t* dst_row = output->data + 
                               ((dest_y + y) * output->stride) + 
                               (dest_x * 4);
            memcpy(dst_row, src_row, CHUNK_SIZE * 4);
        }
    }
    
    return 1;
}

AODDS_API void aodds_fill_missing(
    aodecode_image_t* chunks,
    int32_t chunks_per_side,
    aodecode_image_t* output,
    uint8_t r, uint8_t g, uint8_t b
) {
    if (!chunks || !output || !output->data) return;
    
    int32_t chunk_count = chunks_per_side * chunks_per_side;
    
    /* Fill missing chunk areas with solid color */
#pragma omp parallel for schedule(dynamic, 4)
    for (int32_t i = 0; i < chunk_count; i++) {
        if (chunks[i].data && chunks[i].width == CHUNK_SIZE && 
            chunks[i].height == CHUNK_SIZE) {
            continue;  /* Chunk is valid, skip */
        }
        
        int32_t chunk_row = i / chunks_per_side;
        int32_t chunk_col = i % chunks_per_side;
        int32_t dest_x = chunk_col * CHUNK_SIZE;
        int32_t dest_y = chunk_row * CHUNK_SIZE;
        
        /* Fill with solid color */
        for (int32_t y = 0; y < CHUNK_SIZE; y++) {
            uint8_t* dst_row = output->data + 
                               ((dest_y + y) * output->stride) + 
                               (dest_x * 4);
            for (int32_t x = 0; x < CHUNK_SIZE; x++) {
                dst_row[x * 4 + 0] = r;
                dst_row[x * 4 + 1] = g;
                dst_row[x * 4 + 2] = b;
                dst_row[x * 4 + 3] = 255;
            }
        }
    }
}

/**
 * Combined fill and compose in a single pass.
 * More efficient than separate fill_missing + compose_chunks as it
 * reduces memory bandwidth by 67% (one pass instead of three).
 */
AODDS_API int32_t aodds_fill_and_compose(
    aodecode_image_t* chunks,
    int32_t chunks_per_side,
    aodecode_image_t* output,
    uint8_t missing_r, uint8_t missing_g, uint8_t missing_b
) {
    if (!chunks || !output || !output->data || chunks_per_side <= 0) {
        return 0;
    }
    
    int32_t chunk_count = chunks_per_side * chunks_per_side;
    
#pragma omp parallel for schedule(dynamic, 4)
    for (int32_t i = 0; i < chunk_count; i++) {
        int32_t chunk_row = i / chunks_per_side;
        int32_t chunk_col = i % chunks_per_side;
        int32_t dest_x = chunk_col * CHUNK_SIZE;
        int32_t dest_y = chunk_row * CHUNK_SIZE;
        
        if (chunks[i].data && chunks[i].width == CHUNK_SIZE && 
            chunks[i].height == CHUNK_SIZE) {
            /* Valid chunk: copy data */
            for (int32_t y = 0; y < CHUNK_SIZE; y++) {
                uint8_t* src_row = chunks[i].data + (y * chunks[i].stride);
                uint8_t* dst_row = output->data + 
                                   ((dest_y + y) * output->stride) + 
                                   (dest_x * 4);
                memcpy(dst_row, src_row, CHUNK_SIZE * 4);
            }
        } else {
            /* Missing chunk: fill with color */
            for (int32_t y = 0; y < CHUNK_SIZE; y++) {
                uint8_t* dst_row = output->data + 
                                   ((dest_y + y) * output->stride) + 
                                   (dest_x * 4);
                for (int32_t x = 0; x < CHUNK_SIZE; x++) {
                    dst_row[x * 4 + 0] = missing_r;
                    dst_row[x * 4 + 1] = missing_g;
                    dst_row[x * 4 + 2] = missing_b;
                    dst_row[x * 4 + 3] = 255;
                }
            }
        }
    }
    
    return 1;
}

/* ============================================================================
 * SSE2-optimized mipmap reduction (2-4x faster than scalar on x86-64)
 * ============================================================================ */
#if AODDS_HAS_SSE2
static void reduce_half_sse2(const aodecode_image_t* input, aodecode_image_t* output) {
    int32_t out_w = input->width / 2;
    int32_t out_h = input->height / 2;
    
#pragma omp parallel for schedule(static)
    for (int32_t y = 0; y < out_h; y++) {
        const uint8_t* src_row0 = input->data + ((y * 2) * input->stride);
        const uint8_t* src_row1 = input->data + ((y * 2 + 1) * input->stride);
        uint8_t* dst_row = output->data + (y * output->stride);
        
        /* Process 2 output pixels (4 input pixels from each row) at a time */
        int32_t x = 0;
        for (; x + 2 <= out_w; x += 2) {
            /* Load 4 RGBA pixels from row 0 (16 bytes = 4 pixels) */
            __m128i row0 = _mm_loadu_si128((const __m128i*)(src_row0 + x * 8));
            /* Load 4 RGBA pixels from row 1 (16 bytes = 4 pixels) */
            __m128i row1 = _mm_loadu_si128((const __m128i*)(src_row1 + x * 8));
            
            /* Unpack to 16-bit for safe addition */
            __m128i zero = _mm_setzero_si128();
            
            /* Low 8 bytes (2 pixels) */
            __m128i row0_lo = _mm_unpacklo_epi8(row0, zero);  /* 8x 16-bit */
            __m128i row1_lo = _mm_unpacklo_epi8(row1, zero);
            
            /* High 8 bytes (2 pixels) */
            __m128i row0_hi = _mm_unpackhi_epi8(row0, zero);
            __m128i row1_hi = _mm_unpackhi_epi8(row1, zero);
            
            /* Sum vertically (row0 + row1) */
            __m128i sum_lo = _mm_add_epi16(row0_lo, row1_lo);
            __m128i sum_hi = _mm_add_epi16(row0_hi, row1_hi);
            
            /* For output pixel 0: average of input pixels 0 and 1 */
            /* sum_lo contains: [p0r, p0g, p0b, p0a, p1r, p1g, p1b, p1a] */
            /* We need: (p0 + p1) / 2, where p0 and p1 are already row0+row1 sums */
            
        /* Extract pixel pairs and add horizontally */
        /* sum_lo = [P0+Q0 (4 components), P1+Q1 (4 components)] as 16-bit */
        /* sum_hi = [P2+Q2 (4 components), P3+Q3 (4 components)] as 16-bit */
        /* We need: out0 = (P0+Q0+P1+Q1)/4, out1 = (P2+Q2+P3+Q3)/4 */
        
        /* Add adjacent pixels horizontally within each sum */
        /* p01_sum = P0+Q0+P1+Q1 for each RGBA component */
        __m128i p01_sum = _mm_add_epi16(
            _mm_and_si128(sum_lo, _mm_set_epi16(0, 0, 0, 0, -1, -1, -1, -1)),
            _mm_srli_si128(sum_lo, 8)
        );
        /* p23_sum = P2+Q2+P3+Q3 for each RGBA component */
        __m128i p23_sum = _mm_add_epi16(
            _mm_and_si128(sum_hi, _mm_set_epi16(0, 0, 0, 0, -1, -1, -1, -1)),
            _mm_srli_si128(sum_hi, 8)
        );
        
        /* Combine both output pixels into one register:
         * combined = [out0_r, out0_g, out0_b, out0_a, out1_r, out1_g, out1_b, out1_a]
         */
        __m128i combined = _mm_unpacklo_epi64(p01_sum, p23_sum);
        
        /* Divide by 4 (add 2 for rounding, then shift right 2) */
        __m128i rounding = _mm_set1_epi16(2);
        combined = _mm_add_epi16(combined, rounding);
        combined = _mm_srli_epi16(combined, 2);
        
        /* Pack 8x 16-bit values to 8x 8-bit values in the low 64 bits */
        __m128i result = _mm_packus_epi16(combined, combined);
        
        /* Store 2 output pixels (8 bytes) */
        _mm_storel_epi64((__m128i*)(dst_row + x * 4), result);
        }
        
        /* Scalar fallback for remaining pixels */
        for (; x < out_w; x++) {
            int32_t sx = x * 2 * 4;
            for (int32_t c = 0; c < 4; c++) {
                uint32_t sum = (uint32_t)src_row0[sx + c] +
                               (uint32_t)src_row0[sx + 4 + c] +
                               (uint32_t)src_row1[sx + c] +
                               (uint32_t)src_row1[sx + 4 + c];
                dst_row[x * 4 + c] = (uint8_t)((sum + 2) / 4);
            }
        }
    }
}
#endif /* AODDS_HAS_SSE2 */

/* Scalar implementation of reduce_half (fallback) */
static void reduce_half_scalar(const aodecode_image_t* input, aodecode_image_t* output) {
    int32_t out_w = input->width / 2;
    int32_t out_h = input->height / 2;
    
    /* 2x2 box filter with averaging */
#pragma omp parallel for schedule(static)
    for (int32_t y = 0; y < out_h; y++) {
        const uint8_t* src_row0 = input->data + ((y * 2) * input->stride);
        const uint8_t* src_row1 = input->data + ((y * 2 + 1) * input->stride);
        uint8_t* dst_row = output->data + (y * output->stride);
        
        for (int32_t x = 0; x < out_w; x++) {
            int32_t sx = x * 2 * 4;
            
            /* Average 4 pixels */
            for (int32_t c = 0; c < 4; c++) {
                uint32_t sum = (uint32_t)src_row0[sx + c] +
                               (uint32_t)src_row0[sx + 4 + c] +
                               (uint32_t)src_row1[sx + c] +
                               (uint32_t)src_row1[sx + 4 + c];
                dst_row[x * 4 + c] = (uint8_t)((sum + 2) / 4);
            }
        }
    }
}

AODDS_API int32_t aodds_reduce_half(
    const aodecode_image_t* input,
    aodecode_image_t* output
) {
    if (!input || !input->data || !output || !output->data) {
        return 0;
    }
    
    int32_t out_w = input->width / 2;
    int32_t out_h = input->height / 2;
    
    if (output->width != out_w || output->height != out_h) {
        return 0;
    }
    
#if AODDS_HAS_SSE2
    /* Use SSE2 path for dimensions that work well with SIMD */
    if (out_w >= 2) {
        reduce_half_sse2(input, output);
        return 1;
    }
#endif
    
    /* Fallback to scalar */
    reduce_half_scalar(input, output);
    return 1;
}

/*============================================================================
 * Compression
 *============================================================================*/

/* Fallback BC1 compression (when ISPC not available) */
static void compress_bc1_block_simple(const uint8_t* rgba, uint8_t* block) {
    /* Simple compression: use min/max colors */
    uint8_t min_r = 255, min_g = 255, min_b = 255;
    uint8_t max_r = 0, max_g = 0, max_b = 0;
    
    for (int i = 0; i < 16; i++) {
        uint8_t r = rgba[i * 4 + 0];
        uint8_t g = rgba[i * 4 + 1];
        uint8_t b = rgba[i * 4 + 2];
        
        if (r < min_r) min_r = r;
        if (g < min_g) min_g = g;
        if (b < min_b) min_b = b;
        if (r > max_r) max_r = r;
        if (g > max_g) max_g = g;
        if (b > max_b) max_b = b;
    }
    
    /* Convert to RGB565 */
    uint16_t c0 = ((max_r >> 3) << 11) | ((max_g >> 2) << 5) | (max_b >> 3);
    uint16_t c1 = ((min_r >> 3) << 11) | ((min_g >> 2) << 5) | (min_b >> 3);
    
    /* Ensure c0 > c1 for 4-color mode */
    if (c0 < c1) {
        uint16_t tmp = c0; c0 = c1; c1 = tmp;
        uint8_t tr = max_r; max_r = min_r; min_r = tr;
        uint8_t tg = max_g; max_g = min_g; min_g = tg;
        uint8_t tb = max_b; max_b = min_b; min_b = tb;
    }
    
    block[0] = c0 & 0xFF;
    block[1] = (c0 >> 8) & 0xFF;
    block[2] = c1 & 0xFF;
    block[3] = (c1 >> 8) & 0xFF;
    
    /* Calculate interpolated colors */
    int32_t colors[4][3] = {
        {max_r, max_g, max_b},
        {min_r, min_g, min_b},
        {(2 * max_r + min_r + 1) / 3, (2 * max_g + min_g + 1) / 3, (2 * max_b + min_b + 1) / 3},
        {(max_r + 2 * min_r + 1) / 3, (max_g + 2 * min_g + 1) / 3, (max_b + 2 * min_b + 1) / 3}
    };
    
    /* Encode indices */
    uint32_t indices = 0;
    for (int i = 0; i < 16; i++) {
        int32_t best_idx = 0;
        int32_t best_dist = INT32_MAX;
        
        int32_t r = rgba[i * 4 + 0];
        int32_t g = rgba[i * 4 + 1];
        int32_t b = rgba[i * 4 + 2];
        
        for (int j = 0; j < 4; j++) {
            int32_t dr = r - colors[j][0];
            int32_t dg = g - colors[j][1];
            int32_t db = b - colors[j][2];
            int32_t dist = dr*dr + dg*dg + db*db;
            if (dist < best_dist) {
                best_dist = dist;
                best_idx = j;
            }
        }
        
        indices |= (best_idx << (i * 2));
    }
    
    block[4] = indices & 0xFF;
    block[5] = (indices >> 8) & 0xFF;
    block[6] = (indices >> 16) & 0xFF;
    block[7] = (indices >> 24) & 0xFF;
}

static void compress_bc1_fallback(const aodecode_image_t* image, uint8_t* output) {
    int32_t blocks_x = (image->width + 3) / 4;
    int32_t blocks_y = (image->height + 3) / 4;
    
#pragma omp parallel for schedule(static)
    for (int32_t by = 0; by < blocks_y; by++) {
        for (int32_t bx = 0; bx < blocks_x; bx++) {
            uint8_t block_rgba[16 * 4];
            
            /* Extract 4x4 block */
            for (int y = 0; y < 4; y++) {
                int32_t src_y = by * 4 + y;
                if (src_y >= image->height) src_y = image->height - 1;
                
                for (int x = 0; x < 4; x++) {
                    int32_t src_x = bx * 4 + x;
                    if (src_x >= image->width) src_x = image->width - 1;
                    
                    const uint8_t* src = image->data + 
                                         (src_y * image->stride) + 
                                         (src_x * 4);
                    uint8_t* dst = block_rgba + ((y * 4 + x) * 4);
                    memcpy(dst, src, 4);
                }
            }
            
            /* Compress block */
            uint8_t* block_out = output + ((by * blocks_x + bx) * 8);
            compress_bc1_block_simple(block_rgba, block_out);
        }
    }
}

/*============================================================================
 * BC3 Fallback Compression (DXT5)
 * 
 * BC3 block layout (16 bytes total):
 *   - Bytes 0-7:  Alpha block (DXT5 alpha compression)
 *   - Bytes 8-15: Color block (same as BC1/DXT1)
 * 
 * Alpha block format:
 *   - Byte 0: alpha0 (reference alpha value)
 *   - Byte 1: alpha1 (reference alpha value)
 *   - Bytes 2-7: 16 3-bit indices (48 bits) selecting from 8 derived alphas
 * 
 * Alpha derivation (when alpha0 > alpha1):
 *   alpha[0] = alpha0
 *   alpha[1] = alpha1
 *   alpha[2] = (6*alpha0 + 1*alpha1) / 7
 *   alpha[3] = (5*alpha0 + 2*alpha1) / 7
 *   alpha[4] = (4*alpha0 + 3*alpha1) / 7
 *   alpha[5] = (3*alpha0 + 4*alpha1) / 7
 *   alpha[6] = (2*alpha0 + 5*alpha1) / 7
 *   alpha[7] = (1*alpha0 + 6*alpha1) / 7
 *============================================================================*/

static void compress_alpha_block(const uint8_t* rgba, uint8_t* block) {
    /* Find min/max alpha values in the 4x4 block */
    uint8_t min_alpha = 255;
    uint8_t max_alpha = 0;
    
    for (int i = 0; i < 16; i++) {
        uint8_t a = rgba[i * 4 + 3];  /* Alpha is 4th component */
        if (a < min_alpha) min_alpha = a;
        if (a > max_alpha) max_alpha = a;
    }
    
    /* Set reference alpha values
     * Use alpha0 > alpha1 mode for full 8-value interpolation */
    uint8_t alpha0, alpha1;
    if (max_alpha == min_alpha) {
        /* Solid alpha - avoid division issues */
        alpha0 = max_alpha;
        alpha1 = max_alpha > 0 ? max_alpha - 1 : 0;
    } else {
        alpha0 = max_alpha;
        alpha1 = min_alpha;
    }
    
    /* Ensure alpha0 > alpha1 for 8-value interpolation mode */
    if (alpha0 <= alpha1) {
        alpha0 = alpha1 + 1;
        if (alpha0 == 0) alpha0 = 1;  /* Handle overflow */
    }
    
    block[0] = alpha0;
    block[1] = alpha1;
    
    /* Calculate 8 interpolated alpha values */
    int32_t alphas[8];
    alphas[0] = alpha0;
    alphas[1] = alpha1;
    alphas[2] = (6 * alpha0 + 1 * alpha1 + 3) / 7;
    alphas[3] = (5 * alpha0 + 2 * alpha1 + 3) / 7;
    alphas[4] = (4 * alpha0 + 3 * alpha1 + 3) / 7;
    alphas[5] = (3 * alpha0 + 4 * alpha1 + 3) / 7;
    alphas[6] = (2 * alpha0 + 5 * alpha1 + 3) / 7;
    alphas[7] = (1 * alpha0 + 6 * alpha1 + 3) / 7;
    
    /* Encode 16 3-bit indices into 48 bits (6 bytes) */
    uint64_t indices = 0;
    for (int i = 0; i < 16; i++) {
        int32_t a = rgba[i * 4 + 3];
        
        /* Find closest alpha value */
        int32_t best_idx = 0;
        int32_t best_dist = INT32_MAX;
        for (int j = 0; j < 8; j++) {
            int32_t dist = (a - alphas[j]) * (a - alphas[j]);
            if (dist < best_dist) {
                best_dist = dist;
                best_idx = j;
            }
        }
        
        indices |= ((uint64_t)best_idx << (i * 3));
    }
    
    /* Write 48 bits (6 bytes) of indices */
    block[2] = (indices >> 0) & 0xFF;
    block[3] = (indices >> 8) & 0xFF;
    block[4] = (indices >> 16) & 0xFF;
    block[5] = (indices >> 24) & 0xFF;
    block[6] = (indices >> 32) & 0xFF;
    block[7] = (indices >> 40) & 0xFF;
}

static void compress_bc3_fallback(const aodecode_image_t* image, uint8_t* output) {
    int32_t blocks_x = (image->width + 3) / 4;
    int32_t blocks_y = (image->height + 3) / 4;
    
#pragma omp parallel for schedule(static)
    for (int32_t by = 0; by < blocks_y; by++) {
        for (int32_t bx = 0; bx < blocks_x; bx++) {
            uint8_t block_rgba[16 * 4];
            
            /* Extract 4x4 block */
            for (int y = 0; y < 4; y++) {
                int32_t src_y = by * 4 + y;
                if (src_y >= image->height) src_y = image->height - 1;
                
                for (int x = 0; x < 4; x++) {
                    int32_t src_x = bx * 4 + x;
                    if (src_x >= image->width) src_x = image->width - 1;
                    
                    const uint8_t* src = image->data + 
                                         (src_y * image->stride) + 
                                         (src_x * 4);
                    uint8_t* dst = block_rgba + ((y * 4 + x) * 4);
                    memcpy(dst, src, 4);
                }
            }
            
            /* BC3 block = 16 bytes: 8 bytes alpha + 8 bytes color */
            uint8_t* block_out = output + ((by * blocks_x + bx) * 16);
            
            /* Compress alpha block (bytes 0-7) */
            compress_alpha_block(block_rgba, block_out);
            
            /* Compress color block (bytes 8-15) - same as BC1 */
            compress_bc1_block_simple(block_rgba, block_out + 8);
        }
    }
}

AODDS_API uint32_t aodds_compress(
    const aodecode_image_t* image,
    dds_format_t format,
    uint8_t* output
) {
    if (!image || !image->data || !output) {
        return 0;
    }
    
    /* Initialize ISPC if not done */
    aodds_init_ispc();
    
    uint32_t block_size = (format == DDS_FORMAT_BC1) ? 8 : 16;
    uint32_t blocks_x = (image->width + 3) / 4;
    uint32_t blocks_y = (image->height + 3) / 4;
    uint32_t output_size = blocks_x * blocks_y * block_size;
    
    if (ispc_available && !force_fallback) {
        /* Use ISPC compression */
        rgba_surface_t surface = {
            .ptr = image->data,
            .width = image->width,
            .height = image->height,
            .stride = image->stride
        };
        
        if (format == DDS_FORMAT_BC1) {
            ispc_compress_bc1(&surface, output);
        } else {
            ispc_compress_bc3(&surface, output);
        }
    } else {
        /* Fallback compression (simple inline implementation) */
        if (format == DDS_FORMAT_BC1) {
            compress_bc1_fallback(image, output);
        } else {
            /* BC3 = alpha block + BC1 color block */
            compress_bc3_fallback(image, output);
        }
    }
    
    return output_size;
}

/*============================================================================
 * Main Build Function
 *============================================================================*/

AODDS_API int32_t aodds_build_tile(
    dds_tile_request_t* request,
    aodecode_pool_t* pool
) {
    if (!request || !request->dds_buffer || !request->cache_dir) {
        if (request) safe_strcpy(request->error, "Invalid parameters", 256);
        return 0;
    }
    
    double start_time = get_time_ms();
    
    /* Initialize output */
    request->dds_written = 0;
    request->success = 0;
    request->stats.chunks_found = 0;
    request->stats.chunks_decoded = 0;
    request->stats.chunks_failed = 0;
    request->stats.mipmaps_generated = 0;
    request->stats.elapsed_ms = 0;
    
    int32_t chunks_per_side = request->chunks_per_side;
    int32_t chunk_count = chunks_per_side * chunks_per_side;
    int32_t tile_size = chunks_per_side * CHUNK_SIZE;
    
    /* Calculate required DDS size */
    int32_t mipmap_count = aodds_calc_mipmap_count(tile_size, tile_size);
    uint32_t required_size = aodds_calc_dds_size(tile_size, tile_size, 
                                                  mipmap_count, request->format);
    
    if (request->dds_buffer_size < required_size) {
        snprintf(request->error, 256, 
                 "Buffer too small: need %u bytes, got %u",
                 required_size, request->dds_buffer_size);
        return 0;
    }
    
    /* ═══════════════════════════════════════════════════════════════════════
     * PATH STRING OPTIMIZATION (Phase 2.1)
     * ═══════════════════════════════════════════════════════════════════════
     * BEFORE: 256 mallocs of 4KB each = 1MB total, 257 allocations
     * AFTER:  1 contiguous buffer with 512-byte slots = 128KB, 2 allocations
     * 
     * Path format: {cache_dir}/{col}_{row}_{zoom}_{maptype}.jpg
     * Typical path: ~60-80 chars, 512 bytes is generous with room for long paths
     * ═══════════════════════════════════════════════════════════════════════*/
    #define PATH_SLOT_SIZE 512
    
    char* path_buffer = (char*)malloc(chunk_count * PATH_SLOT_SIZE);
    char** cache_paths = (char**)malloc(chunk_count * sizeof(char*));
    
    if (!path_buffer || !cache_paths) {
        free(path_buffer);
        free(cache_paths);
        safe_strcpy(request->error, "Memory allocation failed", 256);
        return 0;
    }
    
    for (int32_t i = 0; i < chunk_count; i++) {
        cache_paths[i] = path_buffer + (i * PATH_SLOT_SIZE);
        
        int32_t chunk_row = i / chunks_per_side;
        int32_t chunk_col = i % chunks_per_side;
        int32_t abs_col = request->tile_col * chunks_per_side + chunk_col;
        int32_t abs_row = request->tile_row * chunks_per_side + chunk_row;
        
        snprintf(cache_paths[i], PATH_SLOT_SIZE, "%s" PATH_SEP "%d_%d_%d_%s.jpg",
                 request->cache_dir, abs_col, abs_row, 
                 request->zoom, request->maptype);
    }
    
    /* Decode all chunks from cache */
    aodecode_image_t* chunks = (aodecode_image_t*)calloc(
        chunk_count, sizeof(aodecode_image_t)
    );
    if (!chunks) {
        free(path_buffer);
        free(cache_paths);
        safe_strcpy(request->error, "Memory allocation failed", 256);
        return 0;
    }
    
    int32_t decoded = aodecode_from_cache(
        (const char**)cache_paths, chunk_count, chunks, pool, 0
    );
    
    request->stats.chunks_found = decoded;
    request->stats.chunks_decoded = decoded;
    request->stats.chunks_failed = chunk_count - decoded;
    
    /* Free path buffers (now just 2 frees instead of 257) */
    free(path_buffer);
    free(cache_paths);
    
    #undef PATH_SLOT_SIZE
    
    /* Allocate composed tile image */
    aodecode_image_t tile_image = {0};
    tile_image.width = tile_size;
    tile_image.height = tile_size;
    tile_image.stride = tile_size * 4;
    tile_image.channels = 4;
    tile_image.data = (uint8_t*)malloc(tile_size * tile_size * 4);
    
    if (!tile_image.data) {
        for (int32_t i = 0; i < chunk_count; i++) {
            aodecode_free_image(&chunks[i], pool);
        }
        free(chunks);
        safe_strcpy(request->error, "Memory allocation failed", 256);
        return 0;
    }
    
    /* Fill and compose in a single pass for efficiency */
    aodds_fill_and_compose(chunks, chunks_per_side, &tile_image,
                           request->missing_r, request->missing_g, request->missing_b);
    
    /* Free chunk images */
    for (int32_t i = 0; i < chunk_count; i++) {
        aodecode_free_image(&chunks[i], pool);
    }
    free(chunks);
    
    /* Write DDS header */
    uint32_t offset = aodds_write_header(
        request->dds_buffer, tile_size, tile_size, 
        mipmap_count, request->format
    );
    
    /* ═══════════════════════════════════════════════════════════════════════
     * MIPMAP BUFFER REUSE (Phase 2.2)
     * ═══════════════════════════════════════════════════════════════════════
     * BEFORE: Allocate new buffer for each mipmap level (~12 allocations)
     * AFTER:  Pre-allocate two ping-pong buffers (2 allocations)
     * 
     * For 4096x4096 tile:
     * - mip_buf_a: 16MB (for levels 1, 3, 5, 7, 9, 11 - odd levels)
     * - mip_buf_b: 4MB (for levels 2, 4, 6, 8, 10 - even levels)
     * 
     * Level 0: compress tile_image
     * Level 1: reduce tile→mip_a, compress mip_a
     * Level 2: reduce mip_a→mip_b, compress mip_b
     * Level 3: reduce mip_b→mip_a (fits in 16MB), compress mip_a
     * ... alternating between buffers
     * ═══════════════════════════════════════════════════════════════════════*/
    
    /* Allocate ping-pong buffers for mipmaps */
    size_t mip1_size = ((size_t)tile_size / 2) * ((size_t)tile_size / 2) * 4;  /* Level 1 size */
    size_t mip2_size = ((size_t)tile_size / 4) * ((size_t)tile_size / 4) * 4;  /* Level 2 size */
    
    uint8_t* mip_buf_a = (mipmap_count > 1) ? (uint8_t*)malloc(mip1_size) : NULL;
    uint8_t* mip_buf_b = (mipmap_count > 2) ? (uint8_t*)malloc(mip2_size) : NULL;
    
    aodecode_image_t current = tile_image;
    aodecode_image_t next = {0};
    int use_buf_a = 1;  /* Toggle for ping-pong */
    
    for (int32_t mip = 0; mip < mipmap_count; mip++) {
        /* Compress current mipmap */
        uint32_t compressed_size = aodds_compress(
            &current, request->format, 
            request->dds_buffer + offset
        );
        offset += compressed_size;
        request->stats.mipmaps_generated++;
        
        /* Generate next mipmap level */
        if (mip < mipmap_count - 1 && current.width > 4 && current.height > 4) {
            next.width = current.width / 2;
            next.height = current.height / 2;
            next.stride = next.width * 4;
            next.channels = 4;
            
            /* Use pre-allocated ping-pong buffers */
            if (use_buf_a && mip_buf_a) {
                next.data = mip_buf_a;
            } else if (!use_buf_a && mip_buf_b) {
                next.data = mip_buf_b;
            } else {
                /* Fallback: allocate if ping-pong buffer unavailable */
                next.data = (uint8_t*)malloc(next.width * next.height * 4);
            }
            
            if (next.data) {
                aodds_reduce_half(&current, &next);
                current = next;
                memset(&next, 0, sizeof(next));
                use_buf_a = !use_buf_a;  /* Toggle for next iteration */
            } else {
                break;  /* Out of memory, stop generating mipmaps */
            }
        }
    }
    
    /* Clean up - only free the pre-allocated buffers, not the reused ones */
    free(mip_buf_a);
    free(mip_buf_b);
    free(tile_image.data);
    
    request->dds_written = offset;
    request->success = 1;
    request->stats.elapsed_ms = get_time_ms() - start_time;
    
    return 1;
}

AODDS_API int32_t aodds_build_from_chunks(
    aodecode_image_t* chunks,
    int32_t chunks_per_side,
    dds_format_t format,
    uint8_t missing_color[3],
    uint8_t* dds_output,
    uint32_t output_size,
    uint32_t* bytes_written
) {
    if (!chunks || !dds_output || !bytes_written || chunks_per_side <= 0) {
        return 0;
    }
    
    int32_t tile_size = chunks_per_side * CHUNK_SIZE;
    int32_t mipmap_count = aodds_calc_mipmap_count(tile_size, tile_size);
    uint32_t required = aodds_calc_dds_size(tile_size, tile_size, mipmap_count, format);
    
    if (output_size < required) {
        return 0;
    }
    
    /* Allocate tile image */
    aodecode_image_t tile = {0};
    tile.width = tile_size;
    tile.height = tile_size;
    tile.stride = tile_size * 4;
    tile.channels = 4;
    tile.data = (uint8_t*)malloc(tile_size * tile_size * 4);
    
    if (!tile.data) return 0;
    
    /* Fill and compose in a single pass */
    aodds_fill_and_compose(chunks, chunks_per_side, &tile,
                           missing_color[0], missing_color[1], missing_color[2]);
    
    /* Write header */
    uint32_t offset = aodds_write_header(dds_output, tile_size, tile_size, 
                                          mipmap_count, format);
    
    /* ═══════════════════════════════════════════════════════════════════════
     * MIPMAP BUFFER REUSE (Phase 2.2)
     * Pre-allocate ping-pong buffers instead of allocating per mipmap level
     * ═══════════════════════════════════════════════════════════════════════*/
    size_t mip1_size = ((size_t)tile_size / 2) * ((size_t)tile_size / 2) * 4;
    size_t mip2_size = ((size_t)tile_size / 4) * ((size_t)tile_size / 4) * 4;
    
    uint8_t* mip_buf_a = (mipmap_count > 1) ? (uint8_t*)malloc(mip1_size) : NULL;
    uint8_t* mip_buf_b = (mipmap_count > 2) ? (uint8_t*)malloc(mip2_size) : NULL;
    
    aodecode_image_t current = tile;
    aodecode_image_t next = {0};
    int use_buf_a = 1;
    
    for (int32_t mip = 0; mip < mipmap_count; mip++) {
        offset += aodds_compress(&current, format, dds_output + offset);
        
        if (mip < mipmap_count - 1 && current.width > 4) {
            next.width = current.width / 2;
            next.height = current.height / 2;
            next.stride = next.width * 4;
            next.channels = 4;
            
            /* Use pre-allocated ping-pong buffers */
            if (use_buf_a && mip_buf_a) {
                next.data = mip_buf_a;
            } else if (!use_buf_a && mip_buf_b) {
                next.data = mip_buf_b;
            } else {
                next.data = (uint8_t*)malloc(next.width * next.height * 4);
            }
            
            if (next.data) {
                aodds_reduce_half(&current, &next);
                current = next;
                memset(&next, 0, sizeof(next));
                use_buf_a = !use_buf_a;
            } else {
                break;
            }
        }
    }
    
    free(mip_buf_a);
    free(mip_buf_b);
    free(tile.data);
    
    *bytes_written = offset;
    return 1;
}

/*============================================================================
 * Hybrid Pipeline: Build DDS from pre-read JPEG data
 * 
 * This is the optimal approach:
 * - Python reads cache files (fast for OS-cached files)
 * - Native decodes + composes + compresses (parallelism helps)
 *============================================================================*/

AODDS_API int32_t aodds_build_from_jpegs(
    const uint8_t** jpeg_data,
    const uint32_t* jpeg_sizes,
    int32_t chunk_count,
    dds_format_t format,
    uint8_t missing_r,
    uint8_t missing_g,
    uint8_t missing_b,
    uint8_t* dds_output,
    uint32_t output_size,
    uint32_t* bytes_written,
    aodecode_pool_t* pool,
    int32_t max_threads
) {
    if (!jpeg_data || !jpeg_sizes || !dds_output || !bytes_written || chunk_count <= 0) {
        return 0;
    }
    
    /* Calculate chunks per side (must be perfect square) */
    int32_t chunks_per_side = (int32_t)sqrt((double)chunk_count);
    if (chunks_per_side * chunks_per_side != chunk_count) {
        return 0;  /* Not a perfect square */
    }
    
    int32_t tile_size = chunks_per_side * CHUNK_SIZE;
    int32_t mipmap_count = aodds_calc_mipmap_count(tile_size, tile_size);
    uint32_t required = aodds_calc_dds_size(tile_size, tile_size, mipmap_count, format);
    
    if (output_size < required) {
        return 0;
    }
    
    /* Allocate chunk image array */
    aodecode_image_t* chunks = (aodecode_image_t*)calloc(
        chunk_count, sizeof(aodecode_image_t)
    );
    if (!chunks) {
        return 0;
    }
    
    /* Parallel decode all JPEGs */
    int32_t decoded = 0;

#if AOPIPELINE_HAS_OPENMP
    if (max_threads > 0) {
        omp_set_num_threads(max_threads);
    }
    /* ========================================================================
     * THREAD-SAFE POOLED DECODERS
     *
     * Uses a global pool of TurboJPEG decoders with mutex-protected
     * acquisition/release. This solves the concurrent OpenMP region problem
     * while maintaining decoder reuse for performance.
     *
     * - Acquisition is O(n) scan but lock is held only briefly
     * - JPEG decoding happens OUTSIDE the lock (full parallelism)
     * - Decoders are reused across calls (no repeated init/destroy)
     * ======================================================================== */
    #pragma omp parallel reduction(+:decoded)
    {
        /* Acquire a decoder from the thread-safe pool */
        int decoder_from_pool = 0;
        tjhandle tjh = acquire_pooled_decoder(&decoder_from_pool);
        
        if (tjh) {
            #pragma omp for schedule(static)
            for (int32_t i = 0; i < chunk_count; i++) {
                if (!jpeg_data[i] || jpeg_sizes[i] == 0) {
                    continue;  /* Missing chunk - will use fill color */
                }
                
                /* Get JPEG dimensions */
                int width, height, subsamp, colorspace;
                if (tjDecompressHeader3(tjh, jpeg_data[i], jpeg_sizes[i],
                                        &width, &height, &subsamp, &colorspace) < 0) {
                    continue;
                }
                
                /* Validate dimensions */
                if (width != CHUNK_SIZE || height != CHUNK_SIZE) {
                    continue;  /* Unexpected size */
                }
                
                /* Acquire buffer from pool or malloc */
                uint8_t* buffer;
                int from_pool = 0;
                if (pool) {
                    buffer = aodecode_acquire_buffer(pool);
                    from_pool = (buffer != NULL);
                } else {
                    buffer = (uint8_t*)malloc(CHUNK_SIZE * CHUNK_SIZE * 4);
                }
                
                if (!buffer) continue;
                
                /* Decode JPEG to RGBA */
                if (tjDecompress2(tjh, jpeg_data[i], jpeg_sizes[i],
                                  buffer, width, 0, height,
                                  TJPF_RGBA, TJFLAG_FASTDCT) < 0) {
                    if (from_pool) {
                        aodecode_release_buffer(pool, buffer);
                    } else {
                        free(buffer);
                    }
                    continue;
                }
                
                /* Fill chunk structure */
                chunks[i].data = buffer;
                chunks[i].width = width;
                chunks[i].height = height;
                chunks[i].stride = width * 4;
                chunks[i].channels = 4;
                chunks[i].from_pool = from_pool;
                decoded++;
            }
            /* Release decoder back to pool (or destroy if temporary overflow) */
            release_pooled_decoder(tjh, decoder_from_pool);
        }
    }
#else
    /* Non-OpenMP fallback: sequential decode - safe to use shared decoder */
    tjhandle tjh = (tjhandle)aodecode_get_thread_decoder();
    int is_persistent = aodecode_is_persistent_decoder(tjh);
    
    if (tjh) {
        for (int32_t i = 0; i < chunk_count; i++) {
            if (!jpeg_data[i] || jpeg_sizes[i] == 0) {
                continue;
            }
            
            int width, height, subsamp, colorspace;
            if (tjDecompressHeader3(tjh, jpeg_data[i], jpeg_sizes[i],
                                    &width, &height, &subsamp, &colorspace) < 0) {
                continue;
            }
            
            if (width != CHUNK_SIZE || height != CHUNK_SIZE) {
                continue;
            }
            
            uint8_t* buffer = (uint8_t*)malloc(CHUNK_SIZE * CHUNK_SIZE * 4);
            if (!buffer) continue;
            
            if (tjDecompress2(tjh, jpeg_data[i], jpeg_sizes[i],
                              buffer, width, 0, height,
                              TJPF_RGBA, TJFLAG_FASTDCT) < 0) {
                free(buffer);
                continue;
            }
            
            chunks[i].data = buffer;
            chunks[i].width = width;
            chunks[i].height = height;
            chunks[i].stride = width * 4;
            chunks[i].channels = 4;
            chunks[i].from_pool = 0;
            decoded++;
        }
        /* Only destroy non-persistent handles */
        if (!is_persistent) {
            tjDestroy(tjh);
        }
    }
#endif
    
    /* Allocate tile image */
    aodecode_image_t tile = {0};
    tile.width = tile_size;
    tile.height = tile_size;
    tile.stride = tile_size * 4;
    tile.channels = 4;
    tile.data = (uint8_t*)malloc(tile_size * tile_size * 4);
    
    if (!tile.data) {
        for (int32_t i = 0; i < chunk_count; i++) {
            aodecode_free_image(&chunks[i], pool);
        }
        free(chunks);
        return 0;
    }
    
    /* Fill and compose in a single pass */
    aodds_fill_and_compose(chunks, chunks_per_side, &tile,
                           missing_r, missing_g, missing_b);
    
    /* Free chunk images */
    for (int32_t i = 0; i < chunk_count; i++) {
        aodecode_free_image(&chunks[i], pool);
    }
    free(chunks);
    
    /* Write header and generate mipmaps */
    uint32_t offset = aodds_write_header(dds_output, tile_size, tile_size, 
                                          mipmap_count, format);
    
    /* ═══════════════════════════════════════════════════════════════════════
     * MIPMAP BUFFER REUSE (Phase 2.2)
     * Pre-allocate ping-pong buffers instead of allocating per mipmap level
     * ═══════════════════════════════════════════════════════════════════════*/
    size_t mip1_size = ((size_t)tile_size / 2) * ((size_t)tile_size / 2) * 4;
    size_t mip2_size = ((size_t)tile_size / 4) * ((size_t)tile_size / 4) * 4;
    
    uint8_t* mip_buf_a = (mipmap_count > 1) ? (uint8_t*)malloc(mip1_size) : NULL;
    uint8_t* mip_buf_b = (mipmap_count > 2) ? (uint8_t*)malloc(mip2_size) : NULL;
    
    aodecode_image_t current = tile;
    aodecode_image_t next = {0};
    int use_buf_a = 1;
    
    for (int32_t mip = 0; mip < mipmap_count; mip++) {
        offset += aodds_compress(&current, format, dds_output + offset);
        
        if (mip < mipmap_count - 1 && current.width > 4) {
            next.width = current.width / 2;
            next.height = current.height / 2;
            next.stride = next.width * 4;
            next.channels = 4;
            
            /* Use pre-allocated ping-pong buffers */
            if (use_buf_a && mip_buf_a) {
                next.data = mip_buf_a;
            } else if (!use_buf_a && mip_buf_b) {
                next.data = mip_buf_b;
            } else {
                next.data = (uint8_t*)malloc(next.width * next.height * 4);
            }
            
            if (next.data) {
                aodds_reduce_half(&current, &next);
                current = next;
                memset(&next, 0, sizeof(next));
                use_buf_a = !use_buf_a;
            } else {
                break;
            }
        }
    }
    
    free(mip_buf_a);
    free(mip_buf_b);
    free(tile.data);
    
    *bytes_written = offset;
    return 1;
}

/*============================================================================
 * Single Mipmap Build: Build one mipmap level from JPEG data
 * 
 * PERFORMANCE OPTIMIZATION for on-demand mipmap building:
 * When X-Plane requests a specific mipmap level (e.g., mipmap 2), we don't
 * need to build the entire DDS with all mipmaps. This function builds just
 * the requested level, returning raw DXT bytes without a DDS header.
 * 
 * Benefits:
 * - ~3-4x faster than Python path for 64-256 chunk grids
 * - Parallel JPEG decoding (OpenMP)
 * - ISPC SIMD compression
 * - No mipmap chain generation overhead
 *============================================================================*/

AODDS_API uint32_t aodds_calc_mipmap_size(
    int32_t width,
    int32_t height,
    dds_format_t format
) {
    uint32_t block_size = (format == DDS_FORMAT_BC1) ? 8 : 16;
    uint32_t blocks_x = (width + 3) / 4;
    uint32_t blocks_y = (height + 3) / 4;
    return blocks_x * blocks_y * block_size;
}

AODDS_API int32_t aodds_build_single_mipmap(
    const uint8_t** jpeg_data,
    const uint32_t* jpeg_sizes,
    int32_t chunk_count,
    dds_format_t format,
    uint8_t missing_r,
    uint8_t missing_g,
    uint8_t missing_b,
    uint8_t* output,
    uint32_t output_size,
    uint32_t* bytes_written,
    aodecode_pool_t* pool,
    int32_t max_threads
) {
    if (!jpeg_data || !jpeg_sizes || !output || !bytes_written || chunk_count <= 0) {
        return 0;
    }
    
    /* Calculate chunks per side (must be perfect square) */
    int32_t chunks_per_side = (int32_t)sqrt((double)chunk_count);
    if (chunks_per_side * chunks_per_side != chunk_count) {
        return 0;  /* Not a perfect square */
    }
    
    int32_t tile_size = chunks_per_side * CHUNK_SIZE;
    uint32_t required = aodds_calc_mipmap_size(tile_size, tile_size, format);

    if (output_size < required) {
        return 0;
    }

    /* Allocate chunk image array */
    aodecode_image_t* chunks = (aodecode_image_t*)calloc(
        chunk_count, sizeof(aodecode_image_t)
    );
    if (!chunks) {
        return 0;
    }

    /* Parallel decode all JPEGs - using thread-safe pooled decoders */
    int32_t decoded = 0;

#if AOPIPELINE_HAS_OPENMP
    if (max_threads > 0) {
        omp_set_num_threads(max_threads);
    }
    #pragma omp parallel reduction(+:decoded)
    {
        /* Acquire decoder from thread-safe pool */
        int decoder_from_pool = 0;
        tjhandle tjh = acquire_pooled_decoder(&decoder_from_pool);
        
        if (tjh) {
            #pragma omp for schedule(static)
            for (int32_t i = 0; i < chunk_count; i++) {
                if (!jpeg_data[i] || jpeg_sizes[i] == 0) {
                    continue;
                }
                
                int width, height, subsamp, colorspace;
                if (tjDecompressHeader3(tjh, jpeg_data[i], jpeg_sizes[i],
                                        &width, &height, &subsamp, &colorspace) < 0) {
                    continue;
                }
                
                /* Validate dimensions */
                if (width != CHUNK_SIZE || height != CHUNK_SIZE) {
                    continue;
                }
                
                /* Acquire buffer from pool or malloc */
                uint8_t* buffer;
                int from_pool = 0;
                if (pool) {
                    buffer = aodecode_acquire_buffer(pool);
                    from_pool = (buffer != NULL);
                } else {
                    buffer = (uint8_t*)malloc(CHUNK_SIZE * CHUNK_SIZE * 4);
                }
                
                if (!buffer) continue;
                
                /* Decode JPEG to RGBA */
                if (tjDecompress2(tjh, jpeg_data[i], jpeg_sizes[i],
                                  buffer, width, 0, height,
                                  TJPF_RGBA, TJFLAG_FASTDCT) < 0) {
                    if (from_pool) {
                        aodecode_release_buffer(pool, buffer);
                    } else {
                        free(buffer);
                    }
                    continue;
                }
                
                /* Fill chunk structure */
                chunks[i].data = buffer;
                chunks[i].width = width;
                chunks[i].height = height;
                chunks[i].stride = width * 4;
                chunks[i].channels = 4;
                chunks[i].from_pool = from_pool;
                decoded++;
            }
            /* Release decoder back to pool */
            release_pooled_decoder(tjh, decoder_from_pool);
        }
    }
#else
    /* Non-OpenMP fallback: sequential decode - safe to use shared decoder */
    tjhandle tjh = (tjhandle)aodecode_get_thread_decoder();
    int is_persistent = aodecode_is_persistent_decoder(tjh);
    
    if (tjh) {
        for (int32_t i = 0; i < chunk_count; i++) {
            if (!jpeg_data[i] || jpeg_sizes[i] == 0) {
                continue;
            }
            
            int width, height, subsamp, colorspace;
            if (tjDecompressHeader3(tjh, jpeg_data[i], jpeg_sizes[i],
                                    &width, &height, &subsamp, &colorspace) < 0) {
                continue;
            }
            
            if (width != CHUNK_SIZE || height != CHUNK_SIZE) {
                continue;
            }
            
            uint8_t* buffer = (uint8_t*)malloc(CHUNK_SIZE * CHUNK_SIZE * 4);
            if (!buffer) continue;
            
            if (tjDecompress2(tjh, jpeg_data[i], jpeg_sizes[i],
                              buffer, width, 0, height,
                              TJPF_RGBA, TJFLAG_FASTDCT) < 0) {
                free(buffer);
                continue;
            }
            
            chunks[i].data = buffer;
            chunks[i].width = width;
            chunks[i].height = height;
            chunks[i].stride = width * 4;
            chunks[i].channels = 4;
            chunks[i].from_pool = 0;
            decoded++;
        }
        /* Only destroy non-persistent handles */
        if (!is_persistent) {
            tjDestroy(tjh);
        }
    }
#endif
    
    /* Allocate tile image */
    aodecode_image_t tile = {0};
    tile.width = tile_size;
    tile.height = tile_size;
    tile.stride = tile_size * 4;
    tile.channels = 4;
    tile.data = (uint8_t*)malloc(tile_size * tile_size * 4);
    
    if (!tile.data) {
        for (int32_t i = 0; i < chunk_count; i++) {
            aodecode_free_image(&chunks[i], pool);
        }
        free(chunks);
        return 0;
    }
    
    /* Fill and compose in a single pass */
    aodds_fill_and_compose(chunks, chunks_per_side, &tile,
                           missing_r, missing_g, missing_b);
    
    /* Free chunk images */
    for (int32_t i = 0; i < chunk_count; i++) {
        aodecode_free_image(&chunks[i], pool);
    }
    free(chunks);
    
    /* Compress tile image directly to output (no mipmap chain, no header) */
    uint32_t compressed = aodds_compress(&tile, format, output);
    
    /* Cleanup */
    free(tile.data);
    
    *bytes_written = compressed;
    return (compressed > 0) ? 1 : 0;
}


/*============================================================================
 * Fill and Compose for Rectangular Chunk Layouts
 * 
 * Unlike aodds_fill_and_compose() which requires square chunk grids,
 * this function supports arbitrary width × height chunk layouts.
 * Used for partial mipmap builds where only specific rows are needed.
 *============================================================================*/

static int32_t aodds_fill_and_compose_rect(
    aodecode_image_t* chunks,
    int32_t chunks_width,
    int32_t chunks_height,
    aodecode_image_t* output,
    uint8_t missing_r, uint8_t missing_g, uint8_t missing_b
) {
    if (!chunks || !output || !output->data || 
        chunks_width <= 0 || chunks_height <= 0) {
        return 0;
    }
    
    int32_t chunk_count = chunks_width * chunks_height;
    
#pragma omp parallel for schedule(dynamic, 4)
    for (int32_t i = 0; i < chunk_count; i++) {
        int32_t chunk_row = i / chunks_width;
        int32_t chunk_col = i % chunks_width;
        int32_t dest_x = chunk_col * CHUNK_SIZE;
        int32_t dest_y = chunk_row * CHUNK_SIZE;
        
        if (chunks[i].data && chunks[i].width == CHUNK_SIZE && 
            chunks[i].height == CHUNK_SIZE) {
            /* Valid chunk: copy data */
            for (int32_t y = 0; y < CHUNK_SIZE; y++) {
                uint8_t* src_row = chunks[i].data + (y * chunks[i].stride);
                uint8_t* dst_row = output->data + 
                                   ((dest_y + y) * output->stride) + 
                                   (dest_x * 4);
                memcpy(dst_row, src_row, CHUNK_SIZE * 4);
            }
        } else {
            /* Missing chunk: fill with color */
            for (int32_t y = 0; y < CHUNK_SIZE; y++) {
                uint8_t* dst_row = output->data + 
                                   ((dest_y + y) * output->stride) + 
                                   (dest_x * 4);
                for (int32_t x = 0; x < CHUNK_SIZE; x++) {
                    dst_row[x * 4 + 0] = missing_r;
                    dst_row[x * 4 + 1] = missing_g;
                    dst_row[x * 4 + 2] = missing_b;
                    dst_row[x * 4 + 3] = 255;
                }
            }
        }
    }
    
    return 1;
}


/*============================================================================
 * Build Partial Mipmap: Rectangular section from JPEG chunks
 * 
 * Unlike aodds_build_single_mipmap() which requires square chunk grids,
 * this function supports arbitrary width × height chunk layouts.
 * Outputs ONLY the compressed DXT data (no DDS header, no mipmap chain).
 * 
 * Use case: Building specific rows of mipmap 0 for partial reads,
 * providing 10-20x speedup over Python PIL + DXT compression path.
 *============================================================================*/

AODDS_API int32_t aodds_build_partial_mipmap(
    const uint8_t** jpeg_data,
    const uint32_t* jpeg_sizes,
    int32_t chunks_width,
    int32_t chunks_height,
    dds_format_t format,
    uint8_t missing_r,
    uint8_t missing_g,
    uint8_t missing_b,
    uint8_t* output,
    uint32_t output_size,
    uint32_t* bytes_written,
    aodecode_pool_t* pool
) {
    if (!jpeg_data || !jpeg_sizes || !output || !bytes_written || 
        chunks_width <= 0 || chunks_height <= 0) {
        return 0;
    }
    
    int32_t chunk_count = chunks_width * chunks_height;
    int32_t image_width = chunks_width * CHUNK_SIZE;
    int32_t image_height = chunks_height * CHUNK_SIZE;
    
    /* Calculate required output size */
    uint32_t required = aodds_calc_mipmap_size(image_width, image_height, format);
    if (output_size < required) {
        return 0;
    }
    
    /* Allocate chunk image array */
    aodecode_image_t* chunks = (aodecode_image_t*)calloc(
        chunk_count, sizeof(aodecode_image_t)
    );
    if (!chunks) {
        return 0;
    }
    
    /* Parallel decode all JPEGs - using thread-safe pooled decoders */
    int32_t decoded = 0;
    
#if AOPIPELINE_HAS_OPENMP
    #pragma omp parallel reduction(+:decoded)
    {
        /* Acquire decoder from thread-safe pool */
        int decoder_from_pool = 0;
        tjhandle tjh = acquire_pooled_decoder(&decoder_from_pool);
        
        if (tjh) {
            #pragma omp for schedule(static)
            for (int32_t i = 0; i < chunk_count; i++) {
                if (!jpeg_data[i] || jpeg_sizes[i] == 0) {
                    continue;
                }
                
                int width, height, subsamp, colorspace;
                if (tjDecompressHeader3(tjh, jpeg_data[i], jpeg_sizes[i],
                                        &width, &height, &subsamp, &colorspace) < 0) {
                    continue;
                }
                
                /* Validate dimensions */
                if (width != CHUNK_SIZE || height != CHUNK_SIZE) {
                    continue;
                }
                
                /* Acquire buffer from pool or malloc */
                uint8_t* buffer;
                int from_pool = 0;
                if (pool) {
                    buffer = aodecode_acquire_buffer(pool);
                    from_pool = (buffer != NULL);
                } else {
                    buffer = (uint8_t*)malloc(CHUNK_SIZE * CHUNK_SIZE * 4);
                }
                
                if (!buffer) continue;
                
                /* Decode JPEG to RGBA */
                if (tjDecompress2(tjh, jpeg_data[i], jpeg_sizes[i],
                                  buffer, width, 0, height,
                                  TJPF_RGBA, TJFLAG_FASTDCT) < 0) {
                    if (from_pool) {
                        aodecode_release_buffer(pool, buffer);
                    } else {
                        free(buffer);
                    }
                    continue;
                }
                
                /* Fill chunk structure */
                chunks[i].data = buffer;
                chunks[i].width = width;
                chunks[i].height = height;
                chunks[i].stride = width * 4;
                chunks[i].channels = 4;
                chunks[i].from_pool = from_pool;
                decoded++;
            }
            /* Release decoder back to pool */
            release_pooled_decoder(tjh, decoder_from_pool);
        }
    }
#else
    /* Non-OpenMP fallback: sequential decode */
    tjhandle tjh = (tjhandle)aodecode_get_thread_decoder();
    int is_persistent = aodecode_is_persistent_decoder(tjh);
    
    if (tjh) {
        for (int32_t i = 0; i < chunk_count; i++) {
            if (!jpeg_data[i] || jpeg_sizes[i] == 0) {
                continue;
            }
            
            int width, height, subsamp, colorspace;
            if (tjDecompressHeader3(tjh, jpeg_data[i], jpeg_sizes[i],
                                    &width, &height, &subsamp, &colorspace) < 0) {
                continue;
            }
            
            if (width != CHUNK_SIZE || height != CHUNK_SIZE) {
                continue;
            }
            
            uint8_t* buffer;
            int from_pool = 0;
            if (pool) {
                buffer = aodecode_acquire_buffer(pool);
                from_pool = (buffer != NULL);
            } else {
                buffer = (uint8_t*)malloc(CHUNK_SIZE * CHUNK_SIZE * 4);
            }
            
            if (!buffer) continue;
            
            if (tjDecompress2(tjh, jpeg_data[i], jpeg_sizes[i],
                              buffer, width, 0, height,
                              TJPF_RGBA, TJFLAG_FASTDCT) < 0) {
                if (from_pool) {
                    aodecode_release_buffer(pool, buffer);
                } else {
                    free(buffer);
                }
                continue;
            }
            
            chunks[i].data = buffer;
            chunks[i].width = width;
            chunks[i].height = height;
            chunks[i].stride = width * 4;
            chunks[i].channels = 4;
            chunks[i].from_pool = from_pool;
            decoded++;
        }
        
        if (!is_persistent) {
            aodecode_free_thread_decoder(tjh);
        }
    }
#endif
    
    /* Allocate tile image for rectangular dimensions */
    aodecode_image_t tile = {0};
    tile.width = image_width;
    tile.height = image_height;
    tile.stride = image_width * 4;
    tile.channels = 4;
    tile.data = (uint8_t*)malloc(image_width * image_height * 4);
    
    if (!tile.data) {
        for (int32_t i = 0; i < chunk_count; i++) {
            aodecode_free_image(&chunks[i], pool);
        }
        free(chunks);
        return 0;
    }
    
    /* Fill and compose using rectangular function */
    aodds_fill_and_compose_rect(chunks, chunks_width, chunks_height, &tile,
                                missing_r, missing_g, missing_b);
    
    /* Free chunk images */
    for (int32_t i = 0; i < chunk_count; i++) {
        aodecode_free_image(&chunks[i], pool);
    }
    free(chunks);
    
    /* Compress tile image directly to output (no mipmap chain, no header) */
    uint32_t compressed = aodds_compress(&tile, format, output);
    
    /* Cleanup */
    free(tile.data);
    
    *bytes_written = compressed;
    return (compressed > 0) ? 1 : 0;
}


/*============================================================================
 * Build Mipmap Chain: Generate starting mipmap + all smaller mipmaps
 * 
 * This function builds the requested mipmap level AND all smaller mipmaps
 * down to 4×4, matching the behavior of Python's gen_mipmaps().
 * 
 * Use this for on-demand mipmap building to ensure smaller mipmaps are
 * also populated, preventing NULL buffer warnings when X-Plane reads
 * into smaller mipmap positions.
 *============================================================================*/

AODDS_API int32_t aodds_build_mipmap_chain(
    const uint8_t** jpeg_data,
    const uint32_t* jpeg_sizes,
    int32_t chunk_count,
    dds_format_t format,
    uint8_t missing_r,
    uint8_t missing_g,
    uint8_t missing_b,
    uint8_t* output,
    uint32_t output_size,
    uint32_t* bytes_written,
    int32_t* mipmap_count_out,
    uint32_t* mipmap_offsets,
    uint32_t* mipmap_sizes,
    int32_t max_mipmaps,
    aodecode_pool_t* pool
) {
    if (!jpeg_data || !jpeg_sizes || !output || !bytes_written || 
        !mipmap_count_out || !mipmap_offsets || !mipmap_sizes || chunk_count <= 0) {
        return 0;
    }
    
    *bytes_written = 0;
    *mipmap_count_out = 0;
    
    /* Calculate chunks per side (must be perfect square) */
    int32_t chunks_per_side = (int32_t)sqrt((double)chunk_count);
    if (chunks_per_side * chunks_per_side != chunk_count) {
        return 0;  /* Not a perfect square */
    }
    
    int32_t tile_size = chunks_per_side * CHUNK_SIZE;
    
    /* Calculate total mipmaps needed (down to 4×4) */
    int32_t total_mipmaps = 0;
    int32_t size = tile_size;
    while (size >= 4) {
        total_mipmaps++;
        size /= 2;
    }
    if (max_mipmaps > 0 && total_mipmaps > max_mipmaps) {
        total_mipmaps = max_mipmaps;
    }
    
    /* Calculate total required size */
    uint32_t total_required = 0;
    size = tile_size;
    for (int32_t i = 0; i < total_mipmaps; i++) {
        total_required += aodds_calc_mipmap_size(size, size, format);
        size /= 2;
        if (size < 4) size = 4;  /* Minimum 4×4 */
    }
    
    if (output_size < total_required) {
        return 0;
    }
    
    /* Allocate chunk image array */
    aodecode_image_t* chunks = (aodecode_image_t*)calloc(
        chunk_count, sizeof(aodecode_image_t)
    );
    if (!chunks) {
        return 0;
    }
    
    /* Parallel decode all JPEGs - using thread-safe pooled decoders */
    int32_t decoded = 0;
    
#if AOPIPELINE_HAS_OPENMP
    #pragma omp parallel reduction(+:decoded)
    {
        int decoder_from_pool = 0;
        tjhandle tjh = acquire_pooled_decoder(&decoder_from_pool);
        
        if (tjh) {
            #pragma omp for schedule(static)
            for (int32_t i = 0; i < chunk_count; i++) {
                if (!jpeg_data[i] || jpeg_sizes[i] == 0) {
                    continue;
                }
                
                int width, height, subsamp, colorspace;
                if (tjDecompressHeader3(tjh, jpeg_data[i], jpeg_sizes[i],
                                        &width, &height, &subsamp, &colorspace) < 0) {
                    continue;
                }
                
                if (width != CHUNK_SIZE || height != CHUNK_SIZE) {
                    continue;
                }
                
                uint8_t* buffer;
                int from_pool = 0;
                if (pool) {
                    buffer = aodecode_acquire_buffer(pool);
                    from_pool = (buffer != NULL);
                } else {
                    buffer = (uint8_t*)malloc(CHUNK_SIZE * CHUNK_SIZE * 4);
                }
                
                if (!buffer) continue;
                
                if (tjDecompress2(tjh, jpeg_data[i], jpeg_sizes[i],
                                  buffer, width, 0, height,
                                  TJPF_RGBA, TJFLAG_FASTDCT) < 0) {
                    if (from_pool) {
                        aodecode_release_buffer(pool, buffer);
                    } else {
                        free(buffer);
                    }
                    continue;
                }
                
                chunks[i].data = buffer;
                chunks[i].width = width;
                chunks[i].height = height;
                chunks[i].stride = width * 4;
                chunks[i].channels = 4;
                chunks[i].from_pool = from_pool;
                decoded++;
            }
            release_pooled_decoder(tjh, decoder_from_pool);
        }
    }
#else
    tjhandle tjh = (tjhandle)aodecode_get_thread_decoder();
    int is_persistent = aodecode_is_persistent_decoder(tjh);
    
    if (tjh) {
        for (int32_t i = 0; i < chunk_count; i++) {
            if (!jpeg_data[i] || jpeg_sizes[i] == 0) {
                continue;
            }
            
            int width, height, subsamp, colorspace;
            if (tjDecompressHeader3(tjh, jpeg_data[i], jpeg_sizes[i],
                                    &width, &height, &subsamp, &colorspace) < 0) {
                continue;
            }
            
            if (width != CHUNK_SIZE || height != CHUNK_SIZE) {
                continue;
            }
            
            uint8_t* buffer = (uint8_t*)malloc(CHUNK_SIZE * CHUNK_SIZE * 4);
            if (!buffer) continue;
            
            if (tjDecompress2(tjh, jpeg_data[i], jpeg_sizes[i],
                              buffer, width, 0, height,
                              TJPF_RGBA, TJFLAG_FASTDCT) < 0) {
                free(buffer);
                continue;
            }
            
            chunks[i].data = buffer;
            chunks[i].width = width;
            chunks[i].height = height;
            chunks[i].stride = width * 4;
            chunks[i].channels = 4;
            chunks[i].from_pool = 0;
            decoded++;
        }
        if (!is_persistent) {
            tjDestroy(tjh);
        }
    }
#endif
    
    /* Allocate tile image */
    aodecode_image_t tile = {0};
    tile.width = tile_size;
    tile.height = tile_size;
    tile.stride = tile_size * 4;
    tile.channels = 4;
    tile.data = (uint8_t*)malloc(tile_size * tile_size * 4);
    
    if (!tile.data) {
        for (int32_t i = 0; i < chunk_count; i++) {
            aodecode_free_image(&chunks[i], pool);
        }
        free(chunks);
        return 0;
    }
    
    /* Fill and compose in a single pass */
    aodds_fill_and_compose(chunks, chunks_per_side, &tile,
                           missing_r, missing_g, missing_b);
    
    /* Free chunk images */
    for (int32_t i = 0; i < chunk_count; i++) {
        aodecode_free_image(&chunks[i], pool);
    }
    free(chunks);
    
    /* ═══════════════════════════════════════════════════════════════════════
     * MIPMAP CHAIN GENERATION
     * Generate all mipmaps from starting level down to 4×4
     * ═══════════════════════════════════════════════════════════════════════*/
    
    /* Pre-allocate ping-pong buffers for mipmap reduction */
    size_t mip1_size = ((size_t)tile_size / 2) * ((size_t)tile_size / 2) * 4;
    size_t mip2_size = ((size_t)tile_size / 4) * ((size_t)tile_size / 4) * 4;
    
    uint8_t* mip_buf_a = (total_mipmaps > 1) ? (uint8_t*)malloc(mip1_size) : NULL;
    uint8_t* mip_buf_b = (total_mipmaps > 2) ? (uint8_t*)malloc(mip2_size) : NULL;
    
    aodecode_image_t current = tile;
    aodecode_image_t next = {0};
    int use_buf_a = 1;
    uint32_t offset = 0;
    int32_t mip_count = 0;
    
    for (int32_t mip = 0; mip < total_mipmaps; mip++) {
        /* Record offset for this mipmap */
        mipmap_offsets[mip] = offset;
        
        /* Compress current mipmap */
        uint32_t compressed = aodds_compress(&current, format, output + offset);
        mipmap_sizes[mip] = compressed;
        offset += compressed;
        mip_count++;
        
        /* Generate next mipmap level (if not at minimum size) */
        if (mip < total_mipmaps - 1 && current.width > 4 && current.height > 4) {
            next.width = current.width / 2;
            next.height = current.height / 2;
            next.stride = next.width * 4;
            next.channels = 4;
            
            /* Use pre-allocated ping-pong buffers */
            if (use_buf_a && mip_buf_a) {
                next.data = mip_buf_a;
            } else if (!use_buf_a && mip_buf_b) {
                next.data = mip_buf_b;
            } else {
                next.data = (uint8_t*)malloc(next.width * next.height * 4);
            }
            
            if (next.data) {
                aodds_reduce_half(&current, &next);
                current = next;
                memset(&next, 0, sizeof(next));
                use_buf_a = !use_buf_a;
            } else {
                break;  /* Out of memory, stop generating mipmaps */
            }
        }
    }
    
    /* Cleanup */
    free(mip_buf_a);
    free(mip_buf_b);
    free(tile.data);
    
    *bytes_written = offset;
    *mipmap_count_out = mip_count;
    return (mip_count > 0) ? 1 : 0;
}

/*============================================================================
 * Build All Mipmaps from Native Zoom Level Chunks
 * 
 * QUALITY OPTIMIZATION:
 * Instead of building mipmap 0 and deriving smaller mipmaps via reduce_half,
 * this function builds EACH mipmap from its native zoom level chunks:
 *   - Mipmap 0: ZL16 chunks (256 chunks, 4096x4096)
 *   - Mipmap 1: ZL15 chunks (64 chunks, 2048x2048)
 *   - Mipmap 2: ZL14 chunks (16 chunks, 1024x1024)
 *   - Mipmap 3: ZL13 chunks (4 chunks, 512x512)
 *   - Mipmap 4: ZL12 chunks (1 chunk, 256x256)
 * 
 * FALLBACK:
 * If a zoom level has no chunks (all NULL/empty), falls back to reduce_half
 * from the previous mipmap level. This maintains quality while handling
 * network failures gracefully.
 *============================================================================*/

AODDS_API int32_t aodds_build_all_mipmaps_native(
    const uint8_t*** jpeg_data_per_zoom,
    const uint32_t** jpeg_sizes_per_zoom,
    const int32_t* chunk_counts_per_zoom,
    int32_t zoom_count,
    dds_format_t format,
    uint8_t missing_r,
    uint8_t missing_g,
    uint8_t missing_b,
    uint8_t* output,
    uint32_t output_size,
    uint32_t* bytes_written,
    aodecode_pool_t* pool
) {
    if (!jpeg_data_per_zoom || !jpeg_sizes_per_zoom || !chunk_counts_per_zoom ||
        !output || !bytes_written || zoom_count <= 0) {
        return 0;
    }
    
    *bytes_written = 0;
    
    /* Validate first zoom level has chunks (required for mipmap 0) */
    if (chunk_counts_per_zoom[0] <= 0) {
        return 0;
    }
    
    /* Calculate base tile size from first zoom level */
    int32_t chunks_per_side_0 = (int32_t)sqrt((double)chunk_counts_per_zoom[0]);
    if (chunks_per_side_0 * chunks_per_side_0 != chunk_counts_per_zoom[0]) {
        return 0;  /* First zoom must be perfect square */
    }
    
    int32_t tile_size = chunks_per_side_0 * CHUNK_SIZE;
    int32_t mipmap_count = aodds_calc_mipmap_count(tile_size, tile_size);
    
    /* Limit to available zoom levels (but can fill remaining with reduce_half) */
    if (mipmap_count > zoom_count) {
        /* We have fewer zoom levels than mipmaps - will use reduce_half for remaining */
    }
    
    /* Calculate required output size */
    uint32_t required_size = aodds_calc_dds_size(tile_size, tile_size, mipmap_count, format);
    if (output_size < required_size) {
        return 0;
    }
    
    /* Write DDS header */
    uint32_t offset = aodds_write_header(output, tile_size, tile_size, mipmap_count, format);
    
    /* Initialize ISPC */
    aodds_init_ispc();
    
    /* Track the last successfully built mipmap for reduce_half fallback */
    aodecode_image_t prev_mipmap = {0};
    int prev_mipmap_allocated = 0;
    
    /* Process each mipmap level */
    for (int32_t mip = 0; mip < mipmap_count; mip++) {
        int32_t mip_size = tile_size >> mip;
        if (mip_size < 4) mip_size = 4;
        
        aodecode_image_t mip_image = {0};
        int mip_built_from_native = 0;
        
        /* Check if we have native chunks for this mipmap level */
        if (mip < zoom_count && chunk_counts_per_zoom[mip] > 0 && 
            jpeg_data_per_zoom[mip] != NULL) {
            
            int32_t chunk_count = chunk_counts_per_zoom[mip];
            int32_t chunks_per_side = (int32_t)sqrt((double)chunk_count);
            
            /* Validate chunk count matches expected mipmap size */
            if (chunks_per_side * chunks_per_side == chunk_count &&
                chunks_per_side * CHUNK_SIZE == mip_size) {
                
                /* Allocate chunk image array */
                aodecode_image_t* chunks = (aodecode_image_t*)calloc(
                    chunk_count, sizeof(aodecode_image_t)
                );
                
                if (chunks) {
                    /* Decode all JPEGs for this zoom level */
                    int32_t decoded = 0;
                    
#if AOPIPELINE_HAS_OPENMP
                    #pragma omp parallel reduction(+:decoded)
                    {
                        int decoder_from_pool = 0;
                        tjhandle tjh = acquire_pooled_decoder(&decoder_from_pool);
                        
                        if (tjh) {
                            #pragma omp for schedule(static)
                            for (int32_t i = 0; i < chunk_count; i++) {
                                if (!jpeg_data_per_zoom[mip][i] || jpeg_sizes_per_zoom[mip][i] == 0) {
                                    continue;
                                }
                                
                                int width, height, subsamp, colorspace;
                                if (tjDecompressHeader3(tjh, jpeg_data_per_zoom[mip][i], 
                                                        jpeg_sizes_per_zoom[mip][i],
                                                        &width, &height, &subsamp, &colorspace) < 0) {
                                    continue;
                                }
                                
                                if (width != CHUNK_SIZE || height != CHUNK_SIZE) {
                                    continue;
                                }
                                
                                uint8_t* buffer;
                                int from_pool = 0;
                                if (pool) {
                                    buffer = aodecode_acquire_buffer(pool);
                                    from_pool = (buffer != NULL);
                                } else {
                                    buffer = (uint8_t*)malloc(CHUNK_SIZE * CHUNK_SIZE * 4);
                                }
                                
                                if (!buffer) continue;
                                
                                if (tjDecompress2(tjh, jpeg_data_per_zoom[mip][i], 
                                                  jpeg_sizes_per_zoom[mip][i],
                                                  buffer, width, 0, height,
                                                  TJPF_RGBA, TJFLAG_FASTDCT) < 0) {
                                    if (from_pool) {
                                        aodecode_release_buffer(pool, buffer);
                                    } else {
                                        free(buffer);
                                    }
                                    continue;
                                }
                                
                                chunks[i].data = buffer;
                                chunks[i].width = width;
                                chunks[i].height = height;
                                chunks[i].stride = width * 4;
                                chunks[i].channels = 4;
                                chunks[i].from_pool = from_pool;
                                decoded++;
                            }
                            release_pooled_decoder(tjh, decoder_from_pool);
                        }
                    }
#else
                    tjhandle tjh = (tjhandle)aodecode_get_thread_decoder();
                    int is_persistent = aodecode_is_persistent_decoder(tjh);
                    
                    if (tjh) {
                        for (int32_t i = 0; i < chunk_count; i++) {
                            if (!jpeg_data_per_zoom[mip][i] || jpeg_sizes_per_zoom[mip][i] == 0) {
                                continue;
                            }
                            
                            int width, height, subsamp, colorspace;
                            if (tjDecompressHeader3(tjh, jpeg_data_per_zoom[mip][i], 
                                                    jpeg_sizes_per_zoom[mip][i],
                                                    &width, &height, &subsamp, &colorspace) < 0) {
                                continue;
                            }
                            
                            if (width != CHUNK_SIZE || height != CHUNK_SIZE) {
                                continue;
                            }
                            
                            uint8_t* buffer = (uint8_t*)malloc(CHUNK_SIZE * CHUNK_SIZE * 4);
                            if (!buffer) continue;
                            
                            if (tjDecompress2(tjh, jpeg_data_per_zoom[mip][i], 
                                              jpeg_sizes_per_zoom[mip][i],
                                              buffer, width, 0, height,
                                              TJPF_RGBA, TJFLAG_FASTDCT) < 0) {
                                free(buffer);
                                continue;
                            }
                            
                            chunks[i].data = buffer;
                            chunks[i].width = width;
                            chunks[i].height = height;
                            chunks[i].stride = width * 4;
                            chunks[i].channels = 4;
                            chunks[i].from_pool = 0;
                            decoded++;
                        }
                        
                        if (!is_persistent) {
                            tjDestroy(tjh);
                        }
                    }
#endif
                    
                    /* If we decoded at least some chunks, compose the mipmap */
                    if (decoded > 0) {
                        mip_image.width = mip_size;
                        mip_image.height = mip_size;
                        mip_image.stride = mip_size * 4;
                        mip_image.channels = 4;
                        mip_image.data = (uint8_t*)malloc(mip_size * mip_size * 4);
                        
                        if (mip_image.data) {
                            /* Fill and compose */
                            aodds_fill_and_compose(chunks, chunks_per_side, &mip_image,
                                                  missing_r, missing_g, missing_b);
                            mip_built_from_native = 1;
                        }
                    }
                    
                    /* Free chunk images */
                    for (int32_t i = 0; i < chunk_count; i++) {
                        if (chunks[i].data) {
                            if (chunks[i].from_pool && pool) {
                                aodecode_release_buffer(pool, chunks[i].data);
                            } else {
                                free(chunks[i].data);
                            }
                        }
                    }
                    free(chunks);
                }
            }
        }
        
        /* Fallback: If native build failed, use reduce_half from previous mipmap */
        if (!mip_built_from_native && prev_mipmap.data && mip > 0) {
            mip_image.width = mip_size;
            mip_image.height = mip_size;
            mip_image.stride = mip_size * 4;
            mip_image.channels = 4;
            mip_image.data = (uint8_t*)malloc(mip_size * mip_size * 4);
            
            if (mip_image.data) {
                aodds_reduce_half(&prev_mipmap, &mip_image);
                mip_built_from_native = 0;  /* Mark as fallback-built */
            }
        }
        
        /* If we have a mipmap image, compress it */
        if (mip_image.data) {
            uint32_t compressed_size = aodds_compress(&mip_image, format, output + offset);
            offset += compressed_size;
            
            /* Free previous mipmap and store current for next iteration's fallback */
            if (prev_mipmap_allocated && prev_mipmap.data) {
                free(prev_mipmap.data);
            }
            prev_mipmap = mip_image;
            prev_mipmap_allocated = 1;
        } else if (mip == 0) {
            /* Failed to build mipmap 0 - fatal error */
            return 0;
        }
        /* If mip > 0 and no image, just continue (mipmap will be missing) */
    }
    
    /* Final cleanup */
    if (prev_mipmap_allocated && prev_mipmap.data) {
        free(prev_mipmap.data);
    }
    
    *bytes_written = offset;
    return 1;
}

/*============================================================================
 * Direct-to-File Pipeline: Build DDS and write directly to disk
 * 
 * PERFORMANCE OPTIMIZATION:
 * This eliminates the ~65ms Python copy overhead by writing DDS data
 * directly to the disk cache file. The flow becomes:
 *   1. Decode JPEGs to RGBA (parallel, in C)
 *   2. Compose tile image
 *   3. For each mipmap: compress and fwrite to file
 *   4. No Python involvement, no memory copy
 * 
 * ATOMICITY:
 * Uses temp file + rename pattern to prevent corrupt files on crash.
 *============================================================================*/

AODDS_API int32_t aodds_build_from_jpegs_to_file(
    const uint8_t** jpeg_data,
    const uint32_t* jpeg_sizes,
    int32_t chunk_count,
    dds_format_t format,
    uint8_t missing_r,
    uint8_t missing_g,
    uint8_t missing_b,
    const char* output_path,
    uint32_t* bytes_written,
    aodecode_pool_t* pool,
    int32_t max_threads
) {
    if (!jpeg_data || !jpeg_sizes || !output_path || !bytes_written || chunk_count <= 0) {
        return 0;
    }
    
    *bytes_written = 0;
    
    /* Calculate chunks per side (must be perfect square) */
    int32_t chunks_per_side = (int32_t)sqrt((double)chunk_count);
    if (chunks_per_side * chunks_per_side != chunk_count) {
        return 0;
    }
    
    int32_t tile_size = chunks_per_side * CHUNK_SIZE;
    int32_t mipmap_count = aodds_calc_mipmap_count(tile_size, tile_size);

    /* Allocate chunk image array */
    aodecode_image_t* chunks = (aodecode_image_t*)calloc(
        chunk_count, sizeof(aodecode_image_t)
    );
    if (!chunks) {
        return 0;
    }

    /* Parallel decode all JPEGs - using thread-safe pooled decoders */
    int32_t decoded = 0;

#if AOPIPELINE_HAS_OPENMP
    if (max_threads > 0) {
        omp_set_num_threads(max_threads);
    }
    #pragma omp parallel reduction(+:decoded)
    {
        /* Acquire decoder from thread-safe pool */
        int decoder_from_pool = 0;
        tjhandle tjh = acquire_pooled_decoder(&decoder_from_pool);
        
        if (tjh) {
            #pragma omp for schedule(static)
            for (int32_t i = 0; i < chunk_count; i++) {
                if (!jpeg_data[i] || jpeg_sizes[i] == 0) {
                    continue;
                }
                
                int width, height, subsamp, colorspace;
                if (tjDecompressHeader3(tjh, jpeg_data[i], jpeg_sizes[i],
                                        &width, &height, &subsamp, &colorspace) < 0) {
                    continue;
                }
                
                if (width != CHUNK_SIZE || height != CHUNK_SIZE) {
                    continue;
                }
                
                uint8_t* buffer;
                int from_pool = 0;
                if (pool) {
                    buffer = aodecode_acquire_buffer(pool);
                    from_pool = (buffer != NULL);
                }
                if (!from_pool) {
                    buffer = (uint8_t*)malloc(CHUNK_SIZE * CHUNK_SIZE * 4);
                }
                
                if (!buffer) continue;
                
                if (tjDecompress2(tjh, jpeg_data[i], jpeg_sizes[i],
                                  buffer, width, 0, height,
                                  TJPF_RGBA, TJFLAG_FASTDCT) < 0) {
                    if (from_pool) {
                        aodecode_release_buffer(pool, buffer);
                    } else {
                        free(buffer);
                    }
                    continue;
                }
                
                chunks[i].data = buffer;
                chunks[i].width = width;
                chunks[i].height = height;
                chunks[i].stride = width * 4;
                chunks[i].channels = 4;
                chunks[i].from_pool = from_pool;
                decoded++;
            }
            /* Release decoder back to pool */
            release_pooled_decoder(tjh, decoder_from_pool);
        }
    }
#else
    /* Sequential fallback - safe to use shared decoder */
    tjhandle tjh = (tjhandle)aodecode_get_thread_decoder();
    int is_persistent = aodecode_is_persistent_decoder(tjh);
    
    if (tjh) {
        for (int32_t i = 0; i < chunk_count; i++) {
            if (!jpeg_data[i] || jpeg_sizes[i] == 0) continue;
            
            int width, height, subsamp, colorspace;
            if (tjDecompressHeader3(tjh, jpeg_data[i], jpeg_sizes[i],
                                    &width, &height, &subsamp, &colorspace) < 0) {
                continue;
            }
            
            if (width != CHUNK_SIZE || height != CHUNK_SIZE) continue;
            
            uint8_t* buffer = (uint8_t*)malloc(CHUNK_SIZE * CHUNK_SIZE * 4);
            if (!buffer) continue;
            
            if (tjDecompress2(tjh, jpeg_data[i], jpeg_sizes[i],
                              buffer, width, 0, height,
                              TJPF_RGBA, TJFLAG_FASTDCT) < 0) {
                free(buffer);
                continue;
            }
            
            chunks[i].data = buffer;
            chunks[i].width = width;
            chunks[i].height = height;
            chunks[i].stride = width * 4;
            chunks[i].channels = 4;
            chunks[i].from_pool = 0;
            decoded++;
        }
        /* Only destroy non-persistent handles */
        if (!is_persistent) {
            tjDestroy(tjh);
        }
    }
#endif
    
    /* Allocate tile image */
    aodecode_image_t tile = {0};
    tile.width = tile_size;
    tile.height = tile_size;
    tile.stride = tile_size * 4;
    tile.channels = 4;
    tile.data = (uint8_t*)malloc(tile_size * tile_size * 4);
    
    if (!tile.data) {
        for (int32_t i = 0; i < chunk_count; i++) {
            aodecode_free_image(&chunks[i], pool);
        }
        free(chunks);
        return 0;
    }
    
    /* Fill and compose in a single pass */
    aodds_fill_and_compose(chunks, chunks_per_side, &tile,
                           missing_r, missing_g, missing_b);
    
    /* Free chunk images */
    for (int32_t i = 0; i < chunk_count; i++) {
        aodecode_free_image(&chunks[i], pool);
    }
    free(chunks);
    
    /* ═══════════════════════════════════════════════════════════════════════
     * FILE OUTPUT: Write DDS directly to disk
     * Uses temp file + rename for atomicity
     * ═══════════════════════════════════════════════════════════════════════*/
    
    /* Create temp file path */
    char temp_path[4096];
    snprintf(temp_path, sizeof(temp_path), "%s.tmp", output_path);
    
    /* Open temp file for writing */
    FILE* fp = fopen(temp_path, "wb");
    if (!fp) {
        free(tile.data);
        return 0;
    }
    
    /* Optimized I/O: 256KB buffer + file pre-allocation */
    WRITE_BUFFER_LOCK();
    static TLS_VAR char dds_write_buffer1[AODDS_WRITE_BUFFER_SIZE];
    setvbuf(fp, dds_write_buffer1, _IOFBF, sizeof(dds_write_buffer1));
    preallocate_file_dds(fp, calc_dds_file_size(tile_size, mipmap_count, format));
    
    /* Write DDS header */
    uint8_t header[DDS_HEADER_SIZE];
    aodds_write_header(header, tile_size, tile_size, mipmap_count, format);
    if (fwrite(header, 1, DDS_HEADER_SIZE, fp) != DDS_HEADER_SIZE) {
        fclose(fp);
        WRITE_BUFFER_UNLOCK();
        remove(temp_path);
        free(tile.data);
        return 0;
    }
    
    uint32_t total_written = DDS_HEADER_SIZE;
    
    /* Allocate compression buffer (largest mipmap size) */
    uint32_t block_size = (format == DDS_FORMAT_BC1) ? 8 : 16;
    uint32_t max_blocks_x = (tile_size + 3) / 4;
    uint32_t max_blocks_y = (tile_size + 3) / 4;
    uint32_t max_compressed_size = max_blocks_x * max_blocks_y * block_size;
    uint8_t* compress_buffer = (uint8_t*)malloc(max_compressed_size);
    
    if (!compress_buffer) {
        fclose(fp);
        WRITE_BUFFER_UNLOCK();
        remove(temp_path);
        free(tile.data);
        return 0;
    }
    
    /* ═══════════════════════════════════════════════════════════════════════
     * MIPMAP BUFFER REUSE (Phase 2.2)
     * Pre-allocate ping-pong buffers instead of allocating per mipmap level
     * ═══════════════════════════════════════════════════════════════════════*/
    size_t mip1_size = ((size_t)tile_size / 2) * ((size_t)tile_size / 2) * 4;
    size_t mip2_size = ((size_t)tile_size / 4) * ((size_t)tile_size / 4) * 4;
    
    uint8_t* mip_buf_a = (mipmap_count > 1) ? (uint8_t*)malloc(mip1_size) : NULL;
    uint8_t* mip_buf_b = (mipmap_count > 2) ? (uint8_t*)malloc(mip2_size) : NULL;
    
    /* Generate and write mipmaps */
    aodecode_image_t current = tile;
    aodecode_image_t next = {0};
    int success = 1;
    int use_buf_a = 1;
    
    for (int32_t mip = 0; mip < mipmap_count && success; mip++) {
        /* Compress current mipmap to buffer */
        uint32_t compressed_size = aodds_compress(&current, format, compress_buffer);
        
        /* Write compressed data to file */
        if (fwrite(compress_buffer, 1, compressed_size, fp) != compressed_size) {
            success = 0;
            break;
        }
        total_written += compressed_size;
        
        /* Generate next mipmap level */
        if (mip < mipmap_count - 1 && current.width > 4) {
            next.width = current.width / 2;
            next.height = current.height / 2;
            next.stride = next.width * 4;
            next.channels = 4;
            
            /* Use pre-allocated ping-pong buffers */
            if (use_buf_a && mip_buf_a) {
                next.data = mip_buf_a;
            } else if (!use_buf_a && mip_buf_b) {
                next.data = mip_buf_b;
            } else {
                next.data = (uint8_t*)malloc(next.width * next.height * 4);
            }
            
            if (next.data) {
                aodds_reduce_half(&current, &next);
                current = next;
                memset(&next, 0, sizeof(next));
                use_buf_a = !use_buf_a;
            } else {
                success = 0;
            }
        }
    }
    
    /* Cleanup - free ping-pong buffers and tile data */
    free(compress_buffer);
    free(mip_buf_a);
    free(mip_buf_b);
    free(tile.data);
    fclose(fp);
    WRITE_BUFFER_UNLOCK();
    
    if (!success) {
        remove(temp_path);
        return 0;
    }
    
    /* Atomic rename: temp file -> final path */
    if (!atomic_rename(temp_path, output_path)) {
        remove(temp_path);
        return 0;
    }
    
    *bytes_written = total_written;
    return 1;
}

/*============================================================================
 * Native Direct-to-File: Build DDS from cache files and write to disk
 * 
 * PERFORMANCE OPTIMIZATION:
 * Same as aodds_build_tile() but writes directly to disk instead of buffer.
 * Eliminates ~65ms Python copy overhead for native mode predictive builds.
 *============================================================================*/

AODDS_API int32_t aodds_build_tile_to_file(
    const char* cache_dir,
    int32_t tile_row,
    int32_t tile_col,
    const char* maptype,
    int32_t zoom,
    int32_t chunks_per_side,
    dds_format_t format,
    uint8_t missing_r,
    uint8_t missing_g,
    uint8_t missing_b,
    const char* output_path,
    uint32_t* bytes_written,
    aodecode_pool_t* pool
) {
    if (!cache_dir || !maptype || !output_path || !bytes_written || chunks_per_side <= 0) {
        return 0;
    }
    
    *bytes_written = 0;
    
    int32_t chunk_count = chunks_per_side * chunks_per_side;
    int32_t tile_size = chunks_per_side * CHUNK_SIZE;
    int32_t mipmap_count = aodds_calc_mipmap_count(tile_size, tile_size);
    
    /* ═══════════════════════════════════════════════════════════════════════
     * PATH STRING OPTIMIZATION (Phase 2.1)
     * Contiguous buffer instead of individual allocations
     * ═══════════════════════════════════════════════════════════════════════*/
    #define PATH_SLOT_SIZE 512
    
    char* path_buffer = (char*)malloc(chunk_count * PATH_SLOT_SIZE);
    char** cache_paths = (char**)malloc(chunk_count * sizeof(char*));
    
    if (!path_buffer || !cache_paths) {
        free(path_buffer);
        free(cache_paths);
        return 0;
    }
    
    for (int32_t i = 0; i < chunk_count; i++) {
        cache_paths[i] = path_buffer + (i * PATH_SLOT_SIZE);
        
        int32_t chunk_row = i / chunks_per_side;
        int32_t chunk_col = i % chunks_per_side;
        int32_t abs_col = tile_col * chunks_per_side + chunk_col;
        int32_t abs_row = tile_row * chunks_per_side + chunk_row;
        
        snprintf(cache_paths[i], PATH_SLOT_SIZE, "%s" PATH_SEP "%d_%d_%d_%s.jpg",
                 cache_dir, abs_col, abs_row, zoom, maptype);
    }
    
    /* Decode all chunks from cache */
    aodecode_image_t* chunks = (aodecode_image_t*)calloc(
        chunk_count, sizeof(aodecode_image_t)
    );
    if (!chunks) {
        free(path_buffer);
        free(cache_paths);
        return 0;
    }
    
    aodecode_from_cache((const char**)cache_paths, chunk_count, chunks, pool, 0);
    
    /* Free path buffers */
    free(path_buffer);
    free(cache_paths);
    
    #undef PATH_SLOT_SIZE
    
    /* Allocate composed tile image */
    aodecode_image_t tile = {0};
    tile.width = tile_size;
    tile.height = tile_size;
    tile.stride = tile_size * 4;
    tile.channels = 4;
    tile.data = (uint8_t*)malloc(tile_size * tile_size * 4);
    
    if (!tile.data) {
        for (int32_t i = 0; i < chunk_count; i++) {
            aodecode_free_image(&chunks[i], pool);
        }
        free(chunks);
        return 0;
    }
    
    /* Fill and compose in a single pass */
    aodds_fill_and_compose(chunks, chunks_per_side, &tile,
                           missing_r, missing_g, missing_b);

    /* Free chunk images */
    for (int32_t i = 0; i < chunk_count; i++) {
        aodecode_free_image(&chunks[i], pool);
    }
    free(chunks);
    
    /* ═══════════════════════════════════════════════════════════════════════
     * FILE OUTPUT: Write DDS directly to disk with temp file + rename
     * ═══════════════════════════════════════════════════════════════════════*/
    
    char temp_path[4096];
    snprintf(temp_path, sizeof(temp_path), "%s.tmp", output_path);
    
    FILE* fp = fopen(temp_path, "wb");
    if (!fp) {
        free(tile.data);
        return 0;
    }
    
    /* Optimized I/O: 256KB buffer + file pre-allocation */
    WRITE_BUFFER_LOCK();
    static TLS_VAR char dds_write_buffer2[AODDS_WRITE_BUFFER_SIZE];
    setvbuf(fp, dds_write_buffer2, _IOFBF, sizeof(dds_write_buffer2));
    preallocate_file_dds(fp, calc_dds_file_size(tile_size, mipmap_count, format));
    
    /* Write DDS header */
    uint8_t header[DDS_HEADER_SIZE];
    aodds_write_header(header, tile_size, tile_size, mipmap_count, format);
    if (fwrite(header, 1, DDS_HEADER_SIZE, fp) != DDS_HEADER_SIZE) {
        fclose(fp);
        WRITE_BUFFER_UNLOCK();
        remove(temp_path);
        free(tile.data);
        return 0;
    }
    
    uint32_t total_written = DDS_HEADER_SIZE;
    
    /* Allocate compression buffer */
    uint32_t block_size = (format == DDS_FORMAT_BC1) ? 8 : 16;
    uint32_t max_blocks_x = (tile_size + 3) / 4;
    uint32_t max_blocks_y = (tile_size + 3) / 4;
    uint32_t max_compressed_size = max_blocks_x * max_blocks_y * block_size;
    uint8_t* compress_buffer = (uint8_t*)malloc(max_compressed_size);
    
    if (!compress_buffer) {
        fclose(fp);
        WRITE_BUFFER_UNLOCK();
        remove(temp_path);
        free(tile.data);
        return 0;
    }
    
    /* ═══════════════════════════════════════════════════════════════════════
     * MIPMAP BUFFER REUSE (Phase 2.2)
     * ═══════════════════════════════════════════════════════════════════════*/
    size_t mip1_size = ((size_t)tile_size / 2) * ((size_t)tile_size / 2) * 4;
    size_t mip2_size = ((size_t)tile_size / 4) * ((size_t)tile_size / 4) * 4;
    
    uint8_t* mip_buf_a = (mipmap_count > 1) ? (uint8_t*)malloc(mip1_size) : NULL;
    uint8_t* mip_buf_b = (mipmap_count > 2) ? (uint8_t*)malloc(mip2_size) : NULL;
    
    aodecode_image_t current = tile;
    aodecode_image_t next = {0};
    int success = 1;
    int use_buf_a = 1;
    
    for (int32_t mip = 0; mip < mipmap_count && success; mip++) {
        uint32_t compressed_size = aodds_compress(&current, format, compress_buffer);
        
        if (fwrite(compress_buffer, 1, compressed_size, fp) != compressed_size) {
            success = 0;
            break;
        }
        total_written += compressed_size;
        
        if (mip < mipmap_count - 1 && current.width > 4) {
            next.width = current.width / 2;
            next.height = current.height / 2;
            next.stride = next.width * 4;
            next.channels = 4;
            
            if (use_buf_a && mip_buf_a) {
                next.data = mip_buf_a;
            } else if (!use_buf_a && mip_buf_b) {
                next.data = mip_buf_b;
            } else {
                next.data = (uint8_t*)malloc(next.width * next.height * 4);
            }
            
            if (next.data) {
                aodds_reduce_half(&current, &next);
                current = next;
                memset(&next, 0, sizeof(next));
                use_buf_a = !use_buf_a;
            } else {
                success = 0;
            }
        }
    }
    
    /* Cleanup */
    free(compress_buffer);
    free(mip_buf_a);
    free(mip_buf_b);
    free(tile.data);
    fclose(fp);
    WRITE_BUFFER_UNLOCK();
    
    if (!success) {
        remove(temp_path);
        return 0;
    }
    
    /* Atomic rename */
    if (!atomic_rename(temp_path, output_path)) {
        remove(temp_path);
        return 0;
    }
    
    *bytes_written = total_written;
    return 1;
}

/*============================================================================
 * STREAMING TILE BUILDER IMPLEMENTATION
 *============================================================================*/

/**
 * Internal structure for streaming tile builder.
 */
struct aodds_builder_s {
    /* Configuration */
    aodds_builder_config_t config;
    int32_t chunk_count;  /* chunks_per_side^2 */
    
    /* Chunk storage - decoded images */
    aodecode_image_t* chunks;  /* Array of chunk_count images */
    uint8_t* chunk_status;     /* Array of aodds_chunk_status_t values */
    
    /* Deferred JPEG storage for parallel decode at finalize */
    uint8_t** jpeg_buffers;     /* Array of JPEG byte buffers (NULL if decoded or missing) */
    uint32_t* jpeg_sizes;       /* Array of JPEG sizes */
    uint8_t* jpeg_owned;        /* Array: 1 = C owns (must free), 0 = Python owns (don't free) */
    uint32_t jpeg_storage_size; /* Allocated size of jpeg arrays */
    
    /* Thread safety */
#ifdef AOPIPELINE_WINDOWS
    CRITICAL_SECTION lock;
#else
    pthread_mutex_t lock;
#endif
    int lock_initialized;
    
    /* Tile composition buffer (allocated on first finalize) */
    aodecode_image_t tile_image;
    int tile_allocated;
    
    /* Compression work buffers (allocated on first finalize, reused) */
    uint8_t* compress_buffer;       /* BC1/BC3 compression output */
    uint32_t compress_buffer_size;  /* Current allocated size */
    
    /* Mipmap reduction buffers (ping-pong pattern) */
    uint8_t* mip_buf_a;             /* First mipmap buffer */
    uint8_t* mip_buf_b;             /* Second mipmap buffer */
    uint32_t mip_buf_a_size;        /* Allocated size of mip_buf_a */
    uint32_t mip_buf_b_size;        /* Allocated size of mip_buf_b */
    
    /* Decode pool reference */
    aodecode_pool_t* pool;
    
    /* Statistics */
    aodds_builder_status_t status;
};

/* Helper: Initialize lock */
static void builder_init_lock(aodds_builder_t* builder) {
    if (builder->lock_initialized) return;
#ifdef AOPIPELINE_WINDOWS
    InitializeCriticalSection(&builder->lock);
#else
    pthread_mutex_init(&builder->lock, NULL);
#endif
    builder->lock_initialized = 1;
}

/* Helper: Lock the builder */
static void builder_lock(aodds_builder_t* builder) {
#ifdef AOPIPELINE_WINDOWS
    EnterCriticalSection(&builder->lock);
#else
    pthread_mutex_lock(&builder->lock);
#endif
}

/* Helper: Unlock the builder */
static void builder_unlock(aodds_builder_t* builder) {
#ifdef AOPIPELINE_WINDOWS
    LeaveCriticalSection(&builder->lock);
#else
    pthread_mutex_unlock(&builder->lock);
#endif
}

/* Helper: Destroy lock */
static void builder_destroy_lock(aodds_builder_t* builder) {
    if (!builder->lock_initialized) return;
#ifdef AOPIPELINE_WINDOWS
    DeleteCriticalSection(&builder->lock);
#else
    pthread_mutex_destroy(&builder->lock);
#endif
    builder->lock_initialized = 0;
}

AODDS_API aodds_builder_t* aodds_builder_create(
    const aodds_builder_config_t* config,
    aodecode_pool_t* decode_pool
) {
    if (!config || config->chunks_per_side <= 0) {
        return NULL;
    }
    
    aodds_builder_t* builder = (aodds_builder_t*)calloc(1, sizeof(aodds_builder_t));
    if (!builder) {
        return NULL;
    }
    
    /* Copy configuration */
    builder->config = *config;
    builder->chunk_count = config->chunks_per_side * config->chunks_per_side;
    builder->pool = decode_pool;
    
    /* Allocate chunk storage */
    builder->chunks = (aodecode_image_t*)calloc(builder->chunk_count, sizeof(aodecode_image_t));
    builder->chunk_status = (uint8_t*)calloc(builder->chunk_count, sizeof(uint8_t));
    
    if (!builder->chunks || !builder->chunk_status) {
        free(builder->chunks);
        free(builder->chunk_status);
        free(builder);
        return NULL;
    }
    
    /* Allocate JPEG storage arrays for deferred decode */
    builder->jpeg_buffers = (uint8_t**)calloc(builder->chunk_count, sizeof(uint8_t*));
    builder->jpeg_sizes = (uint32_t*)calloc(builder->chunk_count, sizeof(uint32_t));
    builder->jpeg_owned = (uint8_t*)calloc(builder->chunk_count, sizeof(uint8_t));
    builder->jpeg_storage_size = builder->chunk_count;
    
    if (!builder->jpeg_buffers || !builder->jpeg_sizes || !builder->jpeg_owned) {
        free(builder->jpeg_buffers);
        free(builder->jpeg_sizes);
        free(builder->jpeg_owned);
        free(builder->chunks);
        free(builder->chunk_status);
        free(builder);
        return NULL;
    }
    
    /* Initialize lock */
    builder_init_lock(builder);
    
    /* Initialize work buffers (lazy allocation on first finalize) */
    builder->compress_buffer = NULL;
    builder->compress_buffer_size = 0;
    builder->mip_buf_a = NULL;
    builder->mip_buf_b = NULL;
    builder->mip_buf_a_size = 0;
    builder->mip_buf_b_size = 0;
    
    /* Initialize status */
    builder->status.chunks_total = builder->chunk_count;
    builder->status.chunks_received = 0;
    builder->status.chunks_decoded = 0;
    builder->status.chunks_failed = 0;
    builder->status.chunks_fallback = 0;
    builder->status.chunks_missing = 0;
    
    return builder;
}

AODDS_API void aodds_builder_reset(
    aodds_builder_t* builder,
    const aodds_builder_config_t* config
) {
    if (!builder || !config) {
        return;
    }
    
    builder_lock(builder);
    
    int32_t new_chunk_count = config->chunks_per_side * config->chunks_per_side;
    int32_t new_tile_size = config->chunks_per_side * CHUNK_SIZE;
    int32_t old_tile_size = builder->config.chunks_per_side * CHUNK_SIZE;
    
    /* Free existing chunk data and any pending JPEG buffers */
    for (int32_t i = 0; i < builder->chunk_count; i++) {
        if (builder->chunks[i].data) {
            aodecode_free_image(&builder->chunks[i], builder->pool);
        }
        /* Free any deferred JPEG data - ONLY if C owns it (not zero-copy) */
        if (builder->jpeg_buffers && builder->jpeg_buffers[i]) {
            if (builder->jpeg_owned && builder->jpeg_owned[i]) {
                free(builder->jpeg_buffers[i]);
            }
            builder->jpeg_buffers[i] = NULL;
        }
        if (builder->jpeg_sizes) {
            builder->jpeg_sizes[i] = 0;
        }
        if (builder->jpeg_owned) {
            builder->jpeg_owned[i] = 0;
        }
    }
    
    /* Free tile image if tile size changed */
    if (builder->tile_allocated && new_tile_size != old_tile_size) {
        if (builder->tile_image.data) {
            free(builder->tile_image.data);
            builder->tile_image.data = NULL;
        }
        builder->tile_allocated = 0;
        memset(&builder->tile_image, 0, sizeof(builder->tile_image));
    }
    
    /* Free work buffers if tile size increased (they'll be reallocated in finalize)
     * If tile size decreased or unchanged, keep existing buffers for reuse */
    if (new_tile_size > old_tile_size) {
        free(builder->compress_buffer);
        builder->compress_buffer = NULL;
        builder->compress_buffer_size = 0;
        
        free(builder->mip_buf_a);
        free(builder->mip_buf_b);
        builder->mip_buf_a = NULL;
        builder->mip_buf_b = NULL;
        builder->mip_buf_a_size = 0;
        builder->mip_buf_b_size = 0;
    }
    
    /* Reallocate if chunk count changed */
    if (new_chunk_count != builder->chunk_count) {
        free(builder->chunks);
        free(builder->chunk_status);
        free(builder->jpeg_buffers);
        free(builder->jpeg_sizes);
        free(builder->jpeg_owned);
        
        builder->chunks = (aodecode_image_t*)calloc(new_chunk_count, sizeof(aodecode_image_t));
        builder->chunk_status = (uint8_t*)calloc(new_chunk_count, sizeof(uint8_t));
        builder->jpeg_buffers = (uint8_t**)calloc(new_chunk_count, sizeof(uint8_t*));
        builder->jpeg_sizes = (uint32_t*)calloc(new_chunk_count, sizeof(uint32_t));
        builder->jpeg_owned = (uint8_t*)calloc(new_chunk_count, sizeof(uint8_t));
        builder->jpeg_storage_size = new_chunk_count;
        
        if (!builder->chunks || !builder->chunk_status || 
            !builder->jpeg_buffers || !builder->jpeg_sizes || !builder->jpeg_owned) {
            /* Allocation failed - leave builder in invalid state */
            builder->chunk_count = 0;
            builder_unlock(builder);
            return;
        }
    } else {
        /* Just clear existing arrays */
        memset(builder->chunks, 0, new_chunk_count * sizeof(aodecode_image_t));
        memset(builder->chunk_status, 0, new_chunk_count * sizeof(uint8_t));
        memset(builder->jpeg_buffers, 0, new_chunk_count * sizeof(uint8_t*));
        memset(builder->jpeg_sizes, 0, new_chunk_count * sizeof(uint32_t));
        memset(builder->jpeg_owned, 0, new_chunk_count * sizeof(uint8_t));
    }
    
    /* Update configuration */
    builder->config = *config;
    builder->chunk_count = new_chunk_count;
    
    /* Reset status */
    builder->status.chunks_total = new_chunk_count;
    builder->status.chunks_received = 0;
    builder->status.chunks_decoded = 0;
    builder->status.chunks_failed = 0;
    builder->status.chunks_fallback = 0;
    builder->status.chunks_missing = 0;
    
    builder_unlock(builder);
}

AODDS_API void aodds_builder_destroy(aodds_builder_t* builder) {
    if (!builder) {
        return;
    }
    
    /* Free chunk data and any pending JPEG buffers */
    for (int32_t i = 0; i < builder->chunk_count; i++) {
        if (builder->chunks[i].data) {
            aodecode_free_image(&builder->chunks[i], builder->pool);
        }
        /* Only free JPEG buffers if C owns them (not zero-copy) */
        if (builder->jpeg_buffers && builder->jpeg_buffers[i]) {
            if (builder->jpeg_owned && builder->jpeg_owned[i]) {
                free(builder->jpeg_buffers[i]);
            }
        }
    }
    
    free(builder->chunks);
    free(builder->chunk_status);
    free(builder->jpeg_buffers);
    free(builder->jpeg_sizes);
    free(builder->jpeg_owned);
    
    /* Free tile image if allocated */
    if (builder->tile_allocated && builder->tile_image.data) {
        free(builder->tile_image.data);
    }
    
    /* Free work buffers */
    free(builder->compress_buffer);
    free(builder->mip_buf_a);
    free(builder->mip_buf_b);
    
    /* Destroy lock */
    builder_destroy_lock(builder);
    
    free(builder);
}

AODDS_API int32_t aodds_builder_add_chunk(
    aodds_builder_t* builder,
    int32_t chunk_index,
    const uint8_t* jpeg_data,
    uint32_t jpeg_size
) {
    if (!builder || !jpeg_data || jpeg_size == 0) {
        return 0;
    }
    
    if (chunk_index < 0 || chunk_index >= builder->chunk_count) {
        return 0;
    }
    
    builder_lock(builder);
    
    /* Check if already set */
    if (builder->chunk_status[chunk_index] != CHUNK_STATUS_EMPTY) {
        builder_unlock(builder);
        return 0;
    }
    
    /* Allocate JPEG buffer and copy data for deferred decode */
    uint8_t* jpeg_copy = (uint8_t*)malloc(jpeg_size);
    if (!jpeg_copy) {
        builder_unlock(builder);
        return 0;
    }
    memcpy(jpeg_copy, jpeg_data, jpeg_size);
    
    /* Store for parallel decode at finalize time */
    builder->jpeg_buffers[chunk_index] = jpeg_copy;
    builder->jpeg_sizes[chunk_index] = jpeg_size;
    builder->jpeg_owned[chunk_index] = 1;  /* C owns this buffer - must free */
    builder->chunk_status[chunk_index] = CHUNK_STATUS_PENDING_DECODE;
    builder->status.chunks_received++;
    
    builder_unlock(builder);
    return 1;
}

AODDS_API int32_t aodds_builder_add_chunks_batch(
    aodds_builder_t* builder,
    int32_t count,
    const int32_t* indices,
    const uint8_t** jpeg_data,
    const uint32_t* jpeg_sizes
) {
    if (!builder || !indices || !jpeg_data || !jpeg_sizes || count <= 0) {
        return 0;
    }
    
    int32_t added = 0;
    
    builder_lock(builder);
    
    for (int32_t i = 0; i < count; i++) {
        int32_t idx = indices[i];
        
        /* Validate index */
        if (idx < 0 || idx >= builder->chunk_count) {
            continue;
        }
        
        /* Skip if already set */
        if (builder->chunk_status[idx] != CHUNK_STATUS_EMPTY) {
            continue;
        }
        
        /* Skip empty data */
        if (!jpeg_data[i] || jpeg_sizes[i] == 0) {
            continue;
        }
        
        /* Allocate and copy JPEG for deferred decode */
        uint8_t* jpeg_copy = (uint8_t*)malloc(jpeg_sizes[i]);
        if (!jpeg_copy) {
            continue;
        }
        memcpy(jpeg_copy, jpeg_data[i], jpeg_sizes[i]);
        
        /* Store for parallel decode at finalize */
        builder->jpeg_buffers[idx] = jpeg_copy;
        builder->jpeg_sizes[idx] = jpeg_sizes[i];
        builder->jpeg_owned[idx] = 1;  /* C owns this buffer - must free */
        builder->chunk_status[idx] = CHUNK_STATUS_PENDING_DECODE;
        builder->status.chunks_received++;
        added++;
    }
    
    builder_unlock(builder);
    return added;
}

/**
 * ZERO-COPY: Add multiple JPEG chunks without copying data.
 * Stores pointers directly - caller must guarantee data lifetime until finalize().
 */
AODDS_API int32_t aodds_builder_add_chunks_batch_nocopy(
    aodds_builder_t* builder,
    int32_t count,
    const int32_t* indices,
    const uint8_t** jpeg_data,
    const uint32_t* jpeg_sizes
) {
    if (!builder || !indices || !jpeg_data || !jpeg_sizes || count <= 0) {
        return 0;
    }
    
    int32_t added = 0;
    
    builder_lock(builder);
    
    for (int32_t i = 0; i < count; i++) {
        int32_t idx = indices[i];
        
        /* Validate index */
        if (idx < 0 || idx >= builder->chunk_count) {
            continue;
        }
        
        /* Skip if already set */
        if (builder->chunk_status[idx] != CHUNK_STATUS_EMPTY) {
            continue;
        }
        
        /* Skip empty data */
        if (!jpeg_data[i] || jpeg_sizes[i] == 0) {
            continue;
        }
        
        /* ZERO-COPY: Store pointer directly, no malloc/memcpy */
        builder->jpeg_buffers[idx] = (uint8_t*)jpeg_data[i];  /* Cast away const - we won't modify */
        builder->jpeg_sizes[idx] = jpeg_sizes[i];
        builder->jpeg_owned[idx] = 0;  /* Python owns this buffer - do NOT free */
        builder->chunk_status[idx] = CHUNK_STATUS_PENDING_DECODE;
        builder->status.chunks_received++;
        added++;
    }
    
    builder_unlock(builder);
    return added;
}

AODDS_API int32_t aodds_builder_add_fallback_image(
    aodds_builder_t* builder,
    int32_t chunk_index,
    const uint8_t* rgba_data,
    int32_t width,
    int32_t height
) {
    if (!builder || !rgba_data) {
        return 0;
    }
    
    if (chunk_index < 0 || chunk_index >= builder->chunk_count) {
        return 0;
    }
    
    /* Validate dimensions */
    if (width != CHUNK_WIDTH || height != CHUNK_HEIGHT) {
        return 0;
    }
    
    builder_lock(builder);
    
    /* Check if already set */
    if (builder->chunk_status[chunk_index] != CHUNK_STATUS_EMPTY) {
        builder_unlock(builder);
        return 0;
    }
    
    /* Allocate and copy image data */
    uint8_t* data = NULL;
    if (builder->pool) {
        data = aodecode_acquire_buffer(builder->pool);
    }
    if (!data) {
        data = (uint8_t*)malloc(CHUNK_BUFFER_SIZE);
    }
    
    if (!data) {
        builder_unlock(builder);
        return 0;
    }
    
    memcpy(data, rgba_data, CHUNK_BUFFER_SIZE);
    
    builder->chunks[chunk_index].data = data;
    builder->chunks[chunk_index].width = width;
    builder->chunks[chunk_index].height = height;
    builder->chunks[chunk_index].stride = width * 4;
    builder->chunks[chunk_index].channels = 4;
    builder->chunks[chunk_index].from_pool = (builder->pool != NULL);
    
    builder->chunk_status[chunk_index] = CHUNK_STATUS_FALLBACK;
    builder->status.chunks_received++;
    builder->status.chunks_fallback++;
    
    builder_unlock(builder);
    return 1;
}

AODDS_API void aodds_builder_mark_missing(
    aodds_builder_t* builder,
    int32_t chunk_index
) {
    if (!builder) {
        return;
    }
    
    if (chunk_index < 0 || chunk_index >= builder->chunk_count) {
        return;
    }
    
    builder_lock(builder);
    
    /* Only mark if not already set */
    if (builder->chunk_status[chunk_index] == CHUNK_STATUS_EMPTY) {
        builder->chunk_status[chunk_index] = CHUNK_STATUS_MISSING;
        builder->status.chunks_received++;
        builder->status.chunks_missing++;
    }
    
    builder_unlock(builder);
}

AODDS_API void aodds_builder_get_status(
    const aodds_builder_t* builder,
    aodds_builder_status_t* status
) {
    if (!builder || !status) {
        return;
    }
    
    /* Cast away const for locking (status read is thread-safe) */
    aodds_builder_t* b = (aodds_builder_t*)builder;
    builder_lock(b);
    *status = builder->status;
    builder_unlock(b);
}

AODDS_API int32_t aodds_builder_is_complete(const aodds_builder_t* builder) {
    if (!builder) {
        return 0;
    }
    
    aodds_builder_t* b = (aodds_builder_t*)builder;
    builder_lock(b);
    int32_t complete = (builder->status.chunks_received >= builder->status.chunks_total);
    builder_unlock(b);
    return complete;
}

AODDS_API int32_t aodds_builder_finalize(
    aodds_builder_t* builder,
    uint8_t* dds_output,
    uint32_t output_size,
    uint32_t* bytes_written,
    int32_t max_threads
) {
    if (!builder || !dds_output || !bytes_written) {
        return 0;
    }
    
    int32_t chunks_per_side = builder->config.chunks_per_side;
    int32_t tile_size = chunks_per_side * CHUNK_SIZE;
    int32_t mipmap_count = aodds_calc_mipmap_count(tile_size, tile_size);
    
    uint32_t required_size = aodds_calc_dds_size(tile_size, tile_size, mipmap_count, builder->config.format);
    if (output_size < required_size) {
        return 0;
    }
    
    /* Allocate tile image if not already */
    if (!builder->tile_allocated) {
        builder->tile_image.width = tile_size;
        builder->tile_image.height = tile_size;
        builder->tile_image.stride = tile_size * 4;
        builder->tile_image.channels = 4;
        builder->tile_image.data = (uint8_t*)malloc(tile_size * tile_size * 4);
        if (!builder->tile_image.data) {
            return 0;
        }
        builder->tile_allocated = 1;
    }
    
    /* ═══════════════════════════════════════════════════════════════════════
     * PARALLEL DECODE: Decode all pending JPEGs at once using OpenMP
     * This is the key performance optimization - 8 threads decode ~32 chunks each
     * instead of 256 sequential decodes during add_chunk() calls.
     * OpenMP efficiently handles loops with early-continues, so no need to
     * pre-count pending chunks.
     * ═══════════════════════════════════════════════════════════════════════ */
    int32_t decode_success_count = 0;
    int32_t decode_fail_count = 0;

#if AOPIPELINE_HAS_OPENMP
    if (max_threads > 0) {
        omp_set_num_threads(max_threads);
    }
#endif
    /* Parallel decode all pending JPEGs - OpenMP handles empty iterations efficiently */
    #pragma omp parallel for schedule(dynamic, 4) reduction(+:decode_success_count, decode_fail_count)
    for (int32_t i = 0; i < builder->chunk_count; i++) {
        if (builder->chunk_status[i] != CHUNK_STATUS_PENDING_DECODE) continue;
        
        /* Defensive null check (zero-copy mode safety) */
        if (!builder->jpeg_buffers[i] || builder->jpeg_sizes[i] == 0) {
            builder->chunk_status[i] = CHUNK_STATUS_MISSING;
            decode_fail_count++;
            continue;
        }
        
        aodecode_image_t decoded = {0};
        int32_t success = aodecode_single(
            builder->jpeg_buffers[i],
            builder->jpeg_sizes[i],
            &decoded,
            builder->pool  /* Pool is thread-safe */
        );
        
        if (success && decoded.data) {
            builder->chunks[i] = decoded;
            builder->chunk_status[i] = CHUNK_STATUS_JPEG;
            decode_success_count++;
        } else {
            builder->chunk_status[i] = CHUNK_STATUS_MISSING;
            decode_fail_count++;
        }
        
        /* Free JPEG buffer after decode - ONLY if C owns it (not zero-copy) */
        if (builder->jpeg_owned[i]) {
            free(builder->jpeg_buffers[i]);
        }
        builder->jpeg_buffers[i] = NULL;
        builder->jpeg_sizes[i] = 0;
        builder->jpeg_owned[i] = 0;
    }
    
    /* Update stats after parallel region */
    builder->status.chunks_decoded += decode_success_count;
    builder->status.chunks_failed += decode_fail_count;
    builder->status.chunks_missing += decode_fail_count;
    
    /* Fill and compose in a single pass */
    aodds_fill_and_compose(builder->chunks, chunks_per_side, &builder->tile_image,
                           builder->config.missing_r, builder->config.missing_g, builder->config.missing_b);
    
    /* Write DDS header */
    int32_t header_written = aodds_write_header(dds_output, tile_size, tile_size, 
                                                 mipmap_count, builder->config.format);
    if (header_written != DDS_HEADER_SIZE) {
        return 0;
    }
    
    uint32_t total_written = DDS_HEADER_SIZE;
    uint8_t* output_ptr = dds_output + DDS_HEADER_SIZE;
    
    /* Block size for compression size calculations */
    uint32_t block_size = (builder->config.format == DDS_FORMAT_BC1) ? 8 : 16;
    
    /* Ensure mipmap buffers are allocated and large enough (persistent in builder) */
    size_t mip1_size = ((size_t)tile_size / 2) * ((size_t)tile_size / 2) * 4;
    size_t mip2_size = ((size_t)tile_size / 4) * ((size_t)tile_size / 4) * 4;
    
    if (mipmap_count > 1) {
        if (builder->mip_buf_a_size < mip1_size) {
            free(builder->mip_buf_a);
            builder->mip_buf_a = (uint8_t*)malloc(mip1_size);
            builder->mip_buf_a_size = builder->mip_buf_a ? mip1_size : 0;
        }
    }
    if (mipmap_count > 2) {
        if (builder->mip_buf_b_size < mip2_size) {
            free(builder->mip_buf_b);
            builder->mip_buf_b = (uint8_t*)malloc(mip2_size);
            builder->mip_buf_b_size = builder->mip_buf_b ? mip2_size : 0;
        }
    }
    
    /* Local pointers for compatibility with existing code flow */
    uint8_t* mip_buf_a = builder->mip_buf_a;
    uint8_t* mip_buf_b = builder->mip_buf_b;
    
    aodecode_image_t current = builder->tile_image;
    aodecode_image_t next = {0};
    int success = 1;
    int use_buf_a = 1;
    
    for (int32_t mip = 0; mip < mipmap_count && success; mip++) {
        /* Calculate expected compressed size before compression */
        uint32_t blocks_x = (current.width + 3) / 4;
        uint32_t blocks_y = (current.height + 3) / 4;
        uint32_t expected_size = blocks_x * blocks_y * block_size;
        
        if (total_written + expected_size > output_size) {
            success = 0;
            break;
        }
        
        /* Compress directly to output buffer - no temp buffer needed */
        uint32_t compressed_size = aodds_compress(&current, builder->config.format, output_ptr);
        output_ptr += compressed_size;
        total_written += compressed_size;
        
        if (mip < mipmap_count - 1 && current.width > 4) {
            next.width = current.width / 2;
            next.height = current.height / 2;
            next.stride = next.width * 4;
            next.channels = 4;
            
            if (use_buf_a && mip_buf_a) {
                next.data = mip_buf_a;
            } else if (!use_buf_a && mip_buf_b) {
                next.data = mip_buf_b;
            } else {
                next.data = (uint8_t*)malloc(next.width * next.height * 4);
            }
            
            if (next.data) {
                aodds_reduce_half(&current, &next);
                current = next;
                memset(&next, 0, sizeof(next));
                use_buf_a = !use_buf_a;
            } else {
                success = 0;
            }
        }
    }
    
    /* No cleanup needed - buffers are persistent in builder struct */
    
    if (success) {
        *bytes_written = total_written;
    }
    
    return success;
}

AODDS_API int32_t aodds_builder_finalize_to_file(
    aodds_builder_t* builder,
    const char* output_path,
    uint32_t* bytes_written,
    int32_t max_threads
) {
    if (!builder || !output_path || !bytes_written) {
        return 0;
    }
    
    int32_t chunks_per_side = builder->config.chunks_per_side;
    int32_t tile_size = chunks_per_side * CHUNK_SIZE;
    int32_t mipmap_count = aodds_calc_mipmap_count(tile_size, tile_size);
    
    /* Allocate tile image if not already */
    if (!builder->tile_allocated) {
        builder->tile_image.width = tile_size;
        builder->tile_image.height = tile_size;
        builder->tile_image.stride = tile_size * 4;
        builder->tile_image.channels = 4;
        builder->tile_image.data = (uint8_t*)malloc(tile_size * tile_size * 4);
        if (!builder->tile_image.data) {
            return 0;
        }
        builder->tile_allocated = 1;
    }
    
    /* ═══════════════════════════════════════════════════════════════════════
     * PARALLEL DECODE: Decode all pending JPEGs at once using OpenMP
     * OpenMP efficiently handles loops with early-continues, so no need to
     * pre-count pending chunks.
     * ═══════════════════════════════════════════════════════════════════════ */
    int32_t decode_success_count = 0;
    int32_t decode_fail_count = 0;

#if AOPIPELINE_HAS_OPENMP
    if (max_threads > 0) {
        omp_set_num_threads(max_threads);
    }
#endif
    /* Parallel decode all pending JPEGs - OpenMP handles empty iterations efficiently */
    #pragma omp parallel for schedule(dynamic, 4) reduction(+:decode_success_count, decode_fail_count)
    for (int32_t i = 0; i < builder->chunk_count; i++) {
        if (builder->chunk_status[i] != CHUNK_STATUS_PENDING_DECODE) continue;
        
        /* Defensive null check (zero-copy mode safety) */
        if (!builder->jpeg_buffers[i] || builder->jpeg_sizes[i] == 0) {
            builder->chunk_status[i] = CHUNK_STATUS_MISSING;
            decode_fail_count++;
            continue;
        }
        
        aodecode_image_t decoded = {0};
        int32_t success = aodecode_single(
            builder->jpeg_buffers[i],
            builder->jpeg_sizes[i],
            &decoded,
            builder->pool
        );
        
        if (success && decoded.data) {
            builder->chunks[i] = decoded;
            builder->chunk_status[i] = CHUNK_STATUS_JPEG;
            decode_success_count++;
        } else {
            builder->chunk_status[i] = CHUNK_STATUS_MISSING;
            decode_fail_count++;
        }
        
        /* Free JPEG buffer after decode - ONLY if C owns it (not zero-copy) */
        if (builder->jpeg_owned[i]) {
            free(builder->jpeg_buffers[i]);
        }
        builder->jpeg_buffers[i] = NULL;
        builder->jpeg_sizes[i] = 0;
        builder->jpeg_owned[i] = 0;
    }
    
    /* Update stats after parallel region */
    builder->status.chunks_decoded += decode_success_count;
    builder->status.chunks_failed += decode_fail_count;
    builder->status.chunks_missing += decode_fail_count;
    
    /* Fill and compose in a single pass */
    aodds_fill_and_compose(builder->chunks, chunks_per_side, &builder->tile_image,
                           builder->config.missing_r, builder->config.missing_g, builder->config.missing_b);
    
    /* Create temp file path */
    char temp_path[4096];
    snprintf(temp_path, sizeof(temp_path), "%s.tmp", output_path);
    
    /* Open temp file */
    FILE* fp = fopen(temp_path, "wb");
    if (!fp) {
        return 0;
    }
    
    /* Optimized I/O: 256KB buffer + file pre-allocation */
    WRITE_BUFFER_LOCK();
    static TLS_VAR char dds_write_buffer3[AODDS_WRITE_BUFFER_SIZE];
    setvbuf(fp, dds_write_buffer3, _IOFBF, sizeof(dds_write_buffer3));
    preallocate_file_dds(fp, calc_dds_file_size(tile_size, mipmap_count, builder->config.format));
    
    /* Write DDS header */
    uint8_t header[DDS_HEADER_SIZE];
    aodds_write_header(header, tile_size, tile_size, mipmap_count, builder->config.format);
    
    if (fwrite(header, 1, DDS_HEADER_SIZE, fp) != DDS_HEADER_SIZE) {
        fclose(fp);
        WRITE_BUFFER_UNLOCK();
        remove(temp_path);
        return 0;
    }
    
    uint32_t total_written = DDS_HEADER_SIZE;
    
    /* Block size for compression size calculations */
    uint32_t block_size = (builder->config.format == DDS_FORMAT_BC1) ? 8 : 16;
    uint32_t max_blocks_x = (tile_size + 3) / 4;
    uint32_t max_blocks_y = (tile_size + 3) / 4;
    uint32_t max_compressed_size = max_blocks_x * max_blocks_y * block_size;
    
    /* Ensure compression buffer is allocated and large enough (still needed for file writes) */
    if (builder->compress_buffer_size < max_compressed_size) {
        free(builder->compress_buffer);
        builder->compress_buffer = (uint8_t*)malloc(max_compressed_size);
        builder->compress_buffer_size = builder->compress_buffer ? max_compressed_size : 0;
    }
    
    if (!builder->compress_buffer) {
        fclose(fp);
        WRITE_BUFFER_UNLOCK();
        remove(temp_path);
        return 0;
    }
    
    uint8_t* compress_buffer = builder->compress_buffer;
    
    /* Ensure mipmap buffers are allocated and large enough (persistent in builder) */
    size_t mip1_size = ((size_t)tile_size / 2) * ((size_t)tile_size / 2) * 4;
    size_t mip2_size = ((size_t)tile_size / 4) * ((size_t)tile_size / 4) * 4;
    
    if (mipmap_count > 1) {
        if (builder->mip_buf_a_size < (uint32_t)mip1_size) {
            free(builder->mip_buf_a);
            builder->mip_buf_a = (uint8_t*)malloc(mip1_size);
            builder->mip_buf_a_size = builder->mip_buf_a ? mip1_size : 0;
        }
    }
    if (mipmap_count > 2) {
        if (builder->mip_buf_b_size < (uint32_t)mip2_size) {
            free(builder->mip_buf_b);
            builder->mip_buf_b = (uint8_t*)malloc(mip2_size);
            builder->mip_buf_b_size = builder->mip_buf_b ? mip2_size : 0;
        }
    }
    
    /* Local pointers for compatibility with existing code flow */
    uint8_t* mip_buf_a = builder->mip_buf_a;
    uint8_t* mip_buf_b = builder->mip_buf_b;
    
    aodecode_image_t current = builder->tile_image;
    aodecode_image_t next = {0};
    int success = 1;
    int use_buf_a = 1;
    
    for (int32_t mip = 0; mip < mipmap_count && success; mip++) {
        uint32_t compressed_size = aodds_compress(&current, builder->config.format, compress_buffer);
        
        if (fwrite(compress_buffer, 1, compressed_size, fp) != compressed_size) {
            success = 0;
            break;
        }
        total_written += compressed_size;
        
        if (mip < mipmap_count - 1 && current.width > 4) {
            next.width = current.width / 2;
            next.height = current.height / 2;
            next.stride = next.width * 4;
            next.channels = 4;
            
            if (use_buf_a && mip_buf_a) {
                next.data = mip_buf_a;
            } else if (!use_buf_a && mip_buf_b) {
                next.data = mip_buf_b;
            } else {
                next.data = (uint8_t*)malloc(next.width * next.height * 4);
            }
            
            if (next.data) {
                aodds_reduce_half(&current, &next);
                current = next;
                memset(&next, 0, sizeof(next));
                use_buf_a = !use_buf_a;
            } else {
                success = 0;
            }
        }
    }
    
    /* No cleanup needed - buffers are persistent in builder struct */
    fclose(fp);
    WRITE_BUFFER_UNLOCK();
    
    if (!success) {
        remove(temp_path);
        return 0;
    }
    
    /* Atomic rename */
    if (!atomic_rename(temp_path, output_path)) {
        remove(temp_path);
        return 0;
    }
    
    *bytes_written = total_written;
    return 1;
}

AODDS_API const char* aodds_version(void) {
    aodds_init_ispc();
    
    static char version_buf[256];
    const char* compressor_status;
    if (!ispc_available) {
        compressor_status = "STB (ISPC unavailable)";
    } else if (force_fallback) {
        compressor_status = "STB (forced)";
    } else {
        compressor_status = "ISPC";
    }
    
    snprintf(version_buf, sizeof(version_buf),
             "aodds " AODDS_VERSION
#if AOPIPELINE_HAS_OPENMP
             " (OpenMP enabled)"
#else
             " (OpenMP disabled)"
#endif
             " [%s]"
#ifdef AOPIPELINE_WINDOWS
             " [Windows]"
#elif defined(AOPIPELINE_MACOS)
             " [macOS]"
#else
             " [Linux]"
#endif
             , compressor_status
    );
    
    return version_buf;
}

