/**
 * aodecode.c - Native Parallel JPEG Decoding Implementation
 * 
 * Provides high-performance batch JPEG decoding using libturbojpeg
 * with OpenMP parallelism and buffer pooling.
 */

#include "aodecode.h"
#include "aocache.h"
#include "internal.h"
#include <stdio.h>
#include <turbojpeg.h>

/* Version string */
#define AODECODE_VERSION "1.0.0"

/* Standard chunk dimensions */
#define CHUNK_SIZE 256
#define CHUNK_RGBA_BYTES (CHUNK_SIZE * CHUNK_SIZE * 4)

/*============================================================================
 * Buffer Pool Implementation
 *============================================================================*/

/**
 * Buffer pool structure.
 * 
 * Uses a simple stack-based free list for O(1) acquire/release.
 * Thread safety is provided by a mutex around the free list.
 * 
 * Memory Management:
 * - Fixed pool: Pre-allocated contiguous buffers (fast, no fragmentation)
 * - Overflow buffers: malloc'd when pool exhausted AND memory_limit allows
 * - Wait-queue: Condition variable for blocking when limit reached
 * - Auto-shrink: Overflow buffers freed on release (returns to fixed pool size)
 */
struct aodecode_pool {
    /* Fixed pool memory */
    uint8_t* memory;        /* Contiguous allocation for all buffers */
    int32_t buffer_size;    /* Size of each buffer (CHUNK_RGBA_BYTES) */
    int32_t count;          /* Total number of fixed buffers */
    int32_t* free_stack;    /* Stack of free buffer indices */
    int32_t free_top;       /* Top of free stack (number of free buffers) */
    
    /* Memory limit and overflow tracking */
    int64_t memory_limit;       /* Maximum total memory (0 = unlimited) */
    int64_t overflow_allocated; /* Bytes currently in overflow buffers */
    int32_t overflow_count;     /* Number of active overflow buffers */
    int32_t waiters_count;      /* Number of threads waiting for buffer */
    
    /* Synchronization */
    AOMUTEX lock;               /* Mutex for thread safety */
    AOCOND buffer_available;    /* Condition: buffer released or limit raised */
};

AODECODE_API aodecode_pool_t* aodecode_create_pool(int32_t count) {
    if (count <= 0) return NULL;
    
    aodecode_pool_t* pool = (aodecode_pool_t*)malloc(sizeof(aodecode_pool_t));
    if (!pool) return NULL;
    
    pool->buffer_size = CHUNK_RGBA_BYTES;
    pool->count = count;
    
    /* Initialize memory limit and overflow tracking */
    pool->memory_limit = 0;  /* 0 = unlimited (legacy behavior) */
    pool->overflow_allocated = 0;
    pool->overflow_count = 0;
    pool->waiters_count = 0;
    
    /* Allocate contiguous memory for all buffers */
    pool->memory = (uint8_t*)malloc((size_t)count * CHUNK_RGBA_BYTES);
    if (!pool->memory) {
        free(pool);
        return NULL;
    }
    
    /* Allocate free stack */
    pool->free_stack = (int32_t*)malloc((size_t)count * sizeof(int32_t));
    if (!pool->free_stack) {
        free(pool->memory);
        free(pool);
        return NULL;
    }
    
    /* Initialize free stack with all buffer indices */
    for (int32_t i = 0; i < count; i++) {
        pool->free_stack[i] = i;
    }
    pool->free_top = count;
    
    /* Initialize synchronization primitives */
    AOMUTEX_INIT(pool->lock);
    AOCOND_INIT(pool->buffer_available);
    
    return pool;
}

/**
 * Create a buffer pool with memory limit.
 * 
 * Extended version that sets a memory limit for overflow buffers.
 * When the limit is reached, acquire will block until a buffer is released.
 */
AODECODE_API aodecode_pool_t* aodecode_create_pool_ex(int32_t count, int64_t memory_limit) {
    aodecode_pool_t* pool = aodecode_create_pool(count);
    if (pool) {
        pool->memory_limit = memory_limit;
    }
    return pool;
}

AODECODE_API void aodecode_destroy_pool(aodecode_pool_t* pool) {
    if (!pool) return;
    
    /* Destroy synchronization primitives */
    AOCOND_DESTROY(pool->buffer_available);
    AOMUTEX_DESTROY(pool->lock);
    
    free(pool->free_stack);
    free(pool->memory);
    free(pool);
}

/**
 * Acquire a buffer from the pool.
 * 
 * Strategy:
 * 1. Try fixed pool first (fastest, no fragmentation)
 * 2. If pool exhausted, try malloc overflow if within memory_limit
 * 3. If limit reached, wait for buffer to be released
 * 4. Safety fallback: malloc if wait fails (shouldn't happen)
 */
AODECODE_API uint8_t* aodecode_acquire_buffer(aodecode_pool_t* pool) {
    if (!pool) {
        /* No pool - fallback to malloc */
        return (uint8_t*)malloc(CHUNK_RGBA_BYTES);
    }
    
    uint8_t* buffer = NULL;
    int64_t fixed_pool_size = (int64_t)pool->count * CHUNK_RGBA_BYTES;
    
    AOMUTEX_LOCK(pool->lock);
    
    while (1) {
        /* Step 1: Try fixed pool first (O(1), no fragmentation) */
        if (pool->free_top > 0) {
            pool->free_top--;
            int32_t idx = pool->free_stack[pool->free_top];
            buffer = pool->memory + (idx * CHUNK_RGBA_BYTES);
            break;
        }
        
        /* Step 2: Fixed pool exhausted - try overflow malloc if within limit */
        if (pool->memory_limit == 0) {
            /* No limit - allow unlimited overflow (legacy behavior) */
            AOMUTEX_UNLOCK(pool->lock);
            buffer = (uint8_t*)malloc(CHUNK_RGBA_BYTES);
            if (buffer) {
                /* Track overflow outside lock (approximate count is fine) */
                AOMUTEX_LOCK(pool->lock);
                pool->overflow_allocated += CHUNK_RGBA_BYTES;
                pool->overflow_count++;
                AOMUTEX_UNLOCK(pool->lock);
            }
            return buffer;
        }
        
        /* Check if we have room for another overflow buffer */
        int64_t current_usage = fixed_pool_size + pool->overflow_allocated;
        if (current_usage + CHUNK_RGBA_BYTES <= pool->memory_limit) {
            /* Within limit - allocate overflow buffer */
            pool->overflow_allocated += CHUNK_RGBA_BYTES;
            pool->overflow_count++;
            AOMUTEX_UNLOCK(pool->lock);
            
            buffer = (uint8_t*)malloc(CHUNK_RGBA_BYTES);
            if (!buffer) {
                /* malloc failed - revert tracking */
                AOMUTEX_LOCK(pool->lock);
                pool->overflow_allocated -= CHUNK_RGBA_BYTES;
                pool->overflow_count--;
                AOMUTEX_UNLOCK(pool->lock);
                return NULL;
            }
            return buffer;
        }
        
        /* Step 3: Limit reached - wait for buffer to be released */
        pool->waiters_count++;
        AOCOND_WAIT(pool->buffer_available, pool->lock);
        pool->waiters_count--;
        /* Loop back to try again after wakeup */
    }
    
    AOMUTEX_UNLOCK(pool->lock);
    return buffer;
}

/**
 * Release a buffer back to the pool.
 * 
 * Auto-shrink behavior:
 * - Fixed pool buffers: returned to free stack
 * - Overflow buffers: freed immediately (auto-shrink to fixed pool size)
 * 
 * Signals waiters when buffer becomes available.
 */
AODECODE_API void aodecode_release_buffer(aodecode_pool_t* pool, uint8_t* buffer) {
    if (!buffer) return;
    
    if (!pool) {
        /* No pool - must have been malloc'd */
        free(buffer);
        return;
    }
    
    /* Check if buffer is from the fixed pool */
    if (buffer >= pool->memory && 
        buffer < pool->memory + ((size_t)pool->count * CHUNK_RGBA_BYTES)) {
        /* Fixed pool buffer - return to free stack */
        int32_t idx = (int32_t)((buffer - pool->memory) / CHUNK_RGBA_BYTES);
        
        AOMUTEX_LOCK(pool->lock);
        
        /* Push back to free stack */
        if (pool->free_top < pool->count) {
            pool->free_stack[pool->free_top] = idx;
            pool->free_top++;
        }
        
        /* Signal waiters if any */
        if (pool->waiters_count > 0) {
            AOCOND_SIGNAL(pool->buffer_available);
        }
        
        AOMUTEX_UNLOCK(pool->lock);
    } else {
        /* Overflow buffer - free it (auto-shrink) and update tracking */
        free(buffer);
        
        AOMUTEX_LOCK(pool->lock);
        pool->overflow_allocated -= CHUNK_RGBA_BYTES;
        pool->overflow_count--;
        
        /* Signal waiters if any - memory is now available */
        if (pool->waiters_count > 0) {
            AOCOND_SIGNAL(pool->buffer_available);
        }
        
        AOMUTEX_UNLOCK(pool->lock);
    }
}

AODECODE_API void aodecode_pool_stats(
    aodecode_pool_t* pool,
    int32_t* out_total,
    int32_t* out_available,
    int32_t* out_acquired
) {
    if (!pool) {
        if (out_total) *out_total = 0;
        if (out_available) *out_available = 0;
        if (out_acquired) *out_acquired = 0;
        return;
    }
    
    AOMUTEX_LOCK(pool->lock);
    
    if (out_total) *out_total = pool->count;
    if (out_available) *out_available = pool->free_top;
    if (out_acquired) *out_acquired = pool->count - pool->free_top;
    
    AOMUTEX_UNLOCK(pool->lock);
}

/**
 * Set the memory limit for a pool.
 * 
 * @param pool Pool to configure
 * @param memory_limit Maximum total memory (0 = unlimited)
 */
AODECODE_API void aodecode_pool_set_limit(aodecode_pool_t* pool, int64_t memory_limit) {
    if (!pool) return;
    
    AOMUTEX_LOCK(pool->lock);
    pool->memory_limit = memory_limit;
    /* Wake all waiters so they can re-check the new limit */
    if (pool->waiters_count > 0) {
        AOCOND_BROADCAST(pool->buffer_available);
    }
    AOMUTEX_UNLOCK(pool->lock);
}

/**
 * Get the current memory limit.
 */
AODECODE_API int64_t aodecode_pool_get_limit(aodecode_pool_t* pool) {
    if (!pool) return 0;
    
    AOMUTEX_LOCK(pool->lock);
    int64_t limit = pool->memory_limit;
    AOMUTEX_UNLOCK(pool->lock);
    
    return limit;
}

/**
 * Get current memory usage.
 * 
 * Returns the total memory used by fixed pool + overflow buffers.
 */
AODECODE_API int64_t aodecode_pool_get_usage(aodecode_pool_t* pool) {
    if (!pool) return 0;
    
    AOMUTEX_LOCK(pool->lock);
    int64_t fixed = (int64_t)pool->count * CHUNK_RGBA_BYTES;
    int64_t overflow = pool->overflow_allocated;
    AOMUTEX_UNLOCK(pool->lock);
    
    return fixed + overflow;
}

/**
 * Extended pool statistics.
 * 
 * Returns detailed stats including overflow tracking.
 */
AODECODE_API void aodecode_pool_stats_ex(
    aodecode_pool_t* pool,
    int32_t* out_total,
    int32_t* out_available,
    int32_t* out_acquired,
    int32_t* out_overflow_count,
    int64_t* out_overflow_bytes,
    int64_t* out_memory_limit
) {
    if (!pool) {
        if (out_total) *out_total = 0;
        if (out_available) *out_available = 0;
        if (out_acquired) *out_acquired = 0;
        if (out_overflow_count) *out_overflow_count = 0;
        if (out_overflow_bytes) *out_overflow_bytes = 0;
        if (out_memory_limit) *out_memory_limit = 0;
        return;
    }
    
    AOMUTEX_LOCK(pool->lock);
    
    if (out_total) *out_total = pool->count;
    if (out_available) *out_available = pool->free_top;
    if (out_acquired) *out_acquired = pool->count - pool->free_top;
    if (out_overflow_count) *out_overflow_count = pool->overflow_count;
    if (out_overflow_bytes) *out_overflow_bytes = pool->overflow_allocated;
    if (out_memory_limit) *out_memory_limit = pool->memory_limit;
    
    AOMUTEX_UNLOCK(pool->lock);
}

/*============================================================================
 * Persistent TurboJPEG Decoder Pool
 *============================================================================
 * Maintains a pool of persistent TurboJPEG decoder handles, one per OpenMP
 * thread. This eliminates the overhead of creating/destroying handles in
 * each parallel decode loop (~0.15ms per thread per call).
 *
 * Thread Safety:
 * - Initialization uses mutex for thread-safe lazy init
 * - Each thread accesses only its own slot (indexed by omp_get_thread_num())
 * - Cleanup should only be called during shutdown
 */

/* Persistent TurboJPEG decoder handles - one per OpenMP thread */
static tjhandle g_persistent_decoders[MAX_OMP_THREADS] = {NULL};
static int g_decoders_initialized = 0;

/* Mutex for thread-safe initialization */
#ifdef AOPIPELINE_WINDOWS
static CRITICAL_SECTION g_decoder_cs;
static int g_decoder_cs_initialized = 0;

static void ensure_decoder_cs_init(void) {
    if (!g_decoder_cs_initialized) {
        InitializeCriticalSection(&g_decoder_cs);
        g_decoder_cs_initialized = 1;
    }
}
#define DECODER_LOCK() do { ensure_decoder_cs_init(); EnterCriticalSection(&g_decoder_cs); } while(0)
#define DECODER_UNLOCK() LeaveCriticalSection(&g_decoder_cs)
#else
static pthread_mutex_t g_decoder_mutex = PTHREAD_MUTEX_INITIALIZER;
#define DECODER_LOCK() pthread_mutex_lock(&g_decoder_mutex)
#define DECODER_UNLOCK() pthread_mutex_unlock(&g_decoder_mutex)
#endif

/**
 * Get or create a persistent decoder for the current thread.
 * 
 * Returns the persistent decoder if available, or creates a new one
 * if this is the first call from this thread.
 * 
 * Falls back to creating a non-persistent handle if thread ID is out
 * of range - caller must check and destroy these with is_persistent_decoder().
 */
static tjhandle get_thread_decoder(void) {
#if AOPIPELINE_HAS_OPENMP
    int tid = omp_get_thread_num();
    if (tid >= 0 && tid < MAX_OMP_THREADS) {
        if (!g_persistent_decoders[tid]) {
            /* Double-checked locking for thread-safe lazy init */
            DECODER_LOCK();
            if (!g_persistent_decoders[tid]) {
                g_persistent_decoders[tid] = tjInitDecompress();
            }
            DECODER_UNLOCK();
        }
        return g_persistent_decoders[tid];
    }
#endif
    /* Fallback for non-OpenMP or thread ID overflow - caller must destroy */
    return tjInitDecompress();
}

/**
 * Check if a decoder handle is from the persistent pool.
 * 
 * Returns 1 if the handle is persistent (don't destroy it),
 * 0 if it was created as a fallback (caller should destroy).
 */
static int is_persistent_decoder(tjhandle tjh) {
    if (!tjh) return 0;
#if AOPIPELINE_HAS_OPENMP
    for (int i = 0; i < MAX_OMP_THREADS; i++) {
        if (g_persistent_decoders[i] == tjh) return 1;
    }
#endif
    return 0;
}

AODECODE_API void aodecode_init_persistent_decoders(void) {
    if (g_decoders_initialized) return;
    
    DECODER_LOCK();
    if (!g_decoders_initialized) {
#if AOPIPELINE_HAS_OPENMP
        /* Initialize in parallel to create one handle per thread */
        #pragma omp parallel
        {
            int tid = omp_get_thread_num();
            if (tid >= 0 && tid < MAX_OMP_THREADS) {
                if (!g_persistent_decoders[tid]) {
                    g_persistent_decoders[tid] = tjInitDecompress();
                }
            }
        }
#else
        /* Single-threaded: just init one decoder */
        if (!g_persistent_decoders[0]) {
            g_persistent_decoders[0] = tjInitDecompress();
        }
#endif
        g_decoders_initialized = 1;
    }
    DECODER_UNLOCK();
}

AODECODE_API void aodecode_cleanup_persistent_decoders(void) {
    DECODER_LOCK();
    for (int i = 0; i < MAX_OMP_THREADS; i++) {
        if (g_persistent_decoders[i]) {
            tjDestroy(g_persistent_decoders[i]);
            g_persistent_decoders[i] = NULL;
        }
    }
    g_decoders_initialized = 0;
    DECODER_UNLOCK();
}

AODECODE_API void* aodecode_get_thread_decoder(void) {
    return (void*)get_thread_decoder();
}

AODECODE_API int aodecode_is_persistent_decoder(void* tjh) {
    return is_persistent_decoder((tjhandle)tjh);
}

AODECODE_API void aodecode_warmup_full(aodecode_pool_t* pool) {
    /* Initialize persistent decoders first */
    aodecode_init_persistent_decoders();
    
    /* Pre-fault pool memory if provided */
    if (pool && pool->memory && pool->count > 0) {
        volatile uint8_t dummy = 0;
        size_t stride = CHUNK_RGBA_BYTES;
        for (int32_t i = 0; i < pool->count; i++) {
            /* Touch first byte of each buffer to fault pages into RAM */
            dummy += pool->memory[i * stride];
        }
        (void)dummy;  /* Suppress unused warning */
    }
    
#if AOPIPELINE_HAS_OPENMP
    /* Force OpenMP runtime initialization by running a trivial parallel region */
    #pragma omp parallel
    {
        volatile int tid = omp_get_thread_num();
        (void)tid;  /* Suppress unused warning */
    }
#endif
}

/*============================================================================
 * JPEG Decoding Implementation
 *============================================================================*/

/**
 * Internal: Decode a single JPEG using provided turbojpeg handle.
 * 
 * This function is designed to be called from OpenMP parallel loops
 * where each thread has its own tjhandle.
 */
static int decode_jpeg_internal(
    tjhandle tjh,
    const uint8_t* jpeg_data,
    uint32_t jpeg_length,
    aodecode_image_t* image,
    aodecode_pool_t* pool,
    char* error_buf
) {
    if (!tjh || !jpeg_data || jpeg_length == 0 || !image) {
        if (error_buf) safe_strcpy(error_buf, "Invalid parameters", 64);
        return 0;
    }
    
    /* Get JPEG dimensions */
    int width, height, subsamp, colorspace;
    if (tjDecompressHeader3(tjh, jpeg_data, jpeg_length,
                            &width, &height, &subsamp, &colorspace) < 0) {
        if (error_buf) {
            const char* tj_err = tjGetErrorStr2(tjh);
            safe_strcpy(error_buf, tj_err ? tj_err : "Header parse failed", 64);
        }
        return 0;
    }
    
    /* Validate dimensions */
    if (width <= 0 || height <= 0 || width > 4096 || height > 4096) {
        if (error_buf) safe_strcpy(error_buf, "Invalid image dimensions", 64);
        return 0;
    }
    
    /* Acquire buffer for decoded image */
    uint8_t* buffer;
    int from_pool = 0;
    
    if (pool && width == CHUNK_SIZE && height == CHUNK_SIZE) {
        /* Standard chunk size - try to use pool */
        buffer = aodecode_acquire_buffer(pool);
        /* Check if it's actually from the pool */
        if (pool->memory && buffer >= pool->memory && 
            buffer < pool->memory + ((size_t)pool->count * CHUNK_RGBA_BYTES)) {
            from_pool = 1;
        }
    } else {
        /* Non-standard size - must malloc */
        buffer = (uint8_t*)malloc((size_t)width * height * 4);
    }
    
    if (!buffer) {
        if (error_buf) safe_strcpy(error_buf, "Buffer allocation failed", 64);
        return 0;
    }
    
    /* Decode JPEG to RGBA */
    if (tjDecompress2(tjh, jpeg_data, jpeg_length,
                      buffer, width, 0, height,
                      TJPF_RGBA, TJFLAG_FASTDCT) < 0) {
        if (error_buf) {
            const char* tj_err = tjGetErrorStr2(tjh);
            safe_strcpy(error_buf, tj_err ? tj_err : "Decompress failed", 64);
        }
        if (from_pool) {
            aodecode_release_buffer(pool, buffer);
        } else {
            free(buffer);
        }
        return 0;
    }
    
    /* Fill output structure */
    image->data = buffer;
    image->width = width;
    image->height = height;
    image->stride = width * 4;
    image->channels = 4;
    image->from_pool = from_pool;
    
    return 1;
}

AODECODE_API int32_t aodecode_batch(
    aodecode_request_t* requests,
    int32_t count,
    aodecode_pool_t* pool,
    int32_t max_threads
) {
    if (!requests || count <= 0) return 0;
    
    int32_t success_count = 0;
    
#if AOPIPELINE_HAS_OPENMP
    if (max_threads > 0) {
        omp_set_num_threads(max_threads);
    }
#endif
    
#pragma omp parallel reduction(+:success_count)
    {
        /* Get persistent decoder for this thread (or create fallback) */
        tjhandle tjh = get_thread_decoder();
        int is_persistent = is_persistent_decoder(tjh);
        
        if (!tjh) {
            /* Thread can't decode - skip all its work */
        } else {
#pragma omp for schedule(dynamic, 4)
            for (int32_t i = 0; i < count; i++) {
                /* Initialize output */
                requests[i].image.data = NULL;
                requests[i].image.width = 0;
                requests[i].image.height = 0;
                requests[i].image.stride = 0;
                requests[i].image.channels = 0;
                requests[i].image.from_pool = 0;
                requests[i].success = 0;
                requests[i].error[0] = '\0';
                
                if (!requests[i].jpeg_data || requests[i].jpeg_length == 0) {
                    safe_strcpy(requests[i].error, "No JPEG data", 64);
                    continue;
                }
                
                if (decode_jpeg_internal(tjh, 
                                         requests[i].jpeg_data,
                                         requests[i].jpeg_length,
                                         &requests[i].image,
                                         pool,
                                         requests[i].error)) {
                    requests[i].success = 1;
                    success_count++;
                }
            }
            
            /* Only destroy non-persistent handles */
            if (!is_persistent) {
                tjDestroy(tjh);
            }
        }
    }
    
    return success_count;
}

AODECODE_API int32_t aodecode_from_cache(
    const char** cache_paths,
    int32_t count,
    aodecode_image_t* images,
    aodecode_pool_t* pool,
    int32_t max_threads
) {
    if (!cache_paths || !images || count <= 0) return 0;
    
    /* First, batch read all cache files */
    aocache_result_t* cache_results = (aocache_result_t*)malloc(
        (size_t)count * sizeof(aocache_result_t)
    );
    if (!cache_results) return 0;
    
    aocache_batch_read(cache_paths, count, cache_results, max_threads);
    
    int32_t success_count = 0;
    
#if AOPIPELINE_HAS_OPENMP
    if (max_threads > 0) {
        omp_set_num_threads(max_threads);
    }
#endif
    
#pragma omp parallel reduction(+:success_count)
    {
        /* Get persistent decoder for this thread (or create fallback) */
        tjhandle tjh = get_thread_decoder();
        int is_persistent = is_persistent_decoder(tjh);
        
        if (tjh) {
#pragma omp for schedule(dynamic, 4)
            for (int32_t i = 0; i < count; i++) {
                /* Initialize output */
                images[i].data = NULL;
                images[i].width = 0;
                images[i].height = 0;
                images[i].stride = 0;
                images[i].channels = 0;
                images[i].from_pool = 0;
                
                if (!cache_results[i].success) {
                    continue;
                }
                
                char error[64];
                if (decode_jpeg_internal(tjh,
                                         cache_results[i].data,
                                         cache_results[i].length,
                                         &images[i],
                                         pool,
                                         error)) {
                    success_count++;
                }
            }
            
            /* Only destroy non-persistent handles */
            if (!is_persistent) {
                tjDestroy(tjh);
            }
        }
    }
    
    /* Free cache file data */
    aocache_batch_free(cache_results, count);
    free(cache_results);
    
    return success_count;
}

AODECODE_API int32_t aodecode_single(
    const uint8_t* jpeg_data,
    uint32_t jpeg_length,
    aodecode_image_t* image,
    aodecode_pool_t* pool
) {
    if (!jpeg_data || jpeg_length == 0 || !image) return 0;
    
    /* Initialize output */
    image->data = NULL;
    image->width = 0;
    image->height = 0;
    image->stride = 0;
    image->channels = 0;
    image->from_pool = 0;
    
    /* Get persistent decoder (or create fallback) */
    tjhandle tjh = get_thread_decoder();
    int is_persistent = is_persistent_decoder(tjh);
    
    if (!tjh) return 0;
    
    char error[64];
    int result = decode_jpeg_internal(tjh, jpeg_data, jpeg_length, 
                                       image, pool, error);
    
    /* Only destroy non-persistent handles */
    if (!is_persistent) {
        tjDestroy(tjh);
    }
    return result;
}

AODECODE_API void aodecode_free_image(
    aodecode_image_t* image,
    aodecode_pool_t* pool
) {
    if (!image || !image->data) return;
    
    if (image->from_pool && pool) {
        aodecode_release_buffer(pool, image->data);
    } else {
        free(image->data);
    }
    
    image->data = NULL;
    image->width = 0;
    image->height = 0;
    image->stride = 0;
    image->channels = 0;
    image->from_pool = 0;
}

AODECODE_API const char* aodecode_version(void) {
    return "aodecode " AODECODE_VERSION
#if AOPIPELINE_HAS_OPENMP
           " (OpenMP enabled)"
#else
           " (OpenMP disabled)"
#endif
#ifdef AOPIPELINE_WINDOWS
           " [Windows]"
#elif defined(AOPIPELINE_MACOS)
           " [macOS]"
#else
           " [Linux]"
#endif
    ;
}

