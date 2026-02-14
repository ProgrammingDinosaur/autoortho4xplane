/**
 * aodecode.h - Native Parallel JPEG Decoding for AutoOrtho
 * 
 * Provides high-performance batch JPEG decoding with:
 * - OpenMP-based parallelism for concurrent decoding
 * - libturbojpeg for optimized JPEG decompression
 * - Buffer pooling for zero-allocation decode paths
 * - Direct integration with cache reading
 * 
 * This module bypasses Python's GIL by handling all JPEG decoding
 * in native C code with true parallelism.
 */

#ifndef AODECODE_H
#define AODECODE_H

#include <stdint.h>
#include "aocache.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Export/Import macros */
#ifdef _WIN32
  #ifdef AOPIPELINE_EXPORTS
    #define AODECODE_API __declspec(dllexport)
  #else
    #define AODECODE_API __declspec(dllimport)
  #endif
#else
  #define AODECODE_API __attribute__((__visibility__("default")))
#endif

/**
 * Decoded image structure.
 * 
 * Holds the result of JPEG decoding - raw RGBA pixel data.
 * The data pointer is either from a buffer pool or malloc'd.
 */
typedef struct {
    uint8_t* data;      /**< RGBA pixel data (4 bytes per pixel) */
    int32_t width;      /**< Image width in pixels */
    int32_t height;     /**< Image height in pixels */
    int32_t stride;     /**< Bytes per row (usually width * 4) */
    int32_t channels;   /**< Number of channels (always 4 for RGBA) */
    int32_t from_pool;  /**< 1 if data is from buffer pool, 0 if malloc'd */
} aodecode_image_t;

/**
 * Decode request structure for batch operations.
 * 
 * Contains input JPEG data and receives output decoded image.
 */
typedef struct {
    /* Input */
    const uint8_t* jpeg_data;   /**< Pointer to JPEG data in memory */
    uint32_t jpeg_length;       /**< Length of JPEG data in bytes */
    
    /* Output */
    aodecode_image_t image;     /**< Decoded image (populated on success) */
    int32_t success;            /**< 1 = success, 0 = failure */
    char error[64];             /**< Error message if success=0 */
} aodecode_request_t;

/**
 * Buffer pool for zero-allocation decoding.
 * 
 * Pre-allocates a fixed number of RGBA buffers for chunk images.
 * This avoids malloc/free overhead during hot paths.
 */
typedef struct aodecode_pool aodecode_pool_t;

/**
 * Create a buffer pool for chunk decoding.
 * 
 * Each buffer is sized for a 256x256 RGBA image (256KB).
 * 
 * @param count     Number of buffers to pre-allocate
 * @return Pool handle, or NULL on allocation failure
 */
AODECODE_API aodecode_pool_t* aodecode_create_pool(int32_t count);

/**
 * Destroy a buffer pool and free all memory.
 * 
 * @param pool  Pool handle (may be NULL)
 */
AODECODE_API void aodecode_destroy_pool(aodecode_pool_t* pool);

/**
 * Acquire a buffer from the pool.
 * 
 * Returns a pre-allocated 256x256 RGBA buffer. If the pool is empty,
 * falls back to malloc.
 * 
 * @param pool  Pool handle
 * @return Pointer to RGBA buffer (caller must release or free)
 */
AODECODE_API uint8_t* aodecode_acquire_buffer(aodecode_pool_t* pool);

/**
 * Release a buffer back to the pool.
 * 
 * If the buffer was malloc'd (pool exhausted), this frees it.
 * 
 * @param pool      Pool handle
 * @param buffer    Buffer to release
 */
AODECODE_API void aodecode_release_buffer(aodecode_pool_t* pool, uint8_t* buffer);

/**
 * Get pool statistics.
 * 
 * @param pool          Pool handle
 * @param out_total     Output: total buffers in pool
 * @param out_available Output: currently available buffers
 * @param out_acquired  Output: currently acquired buffers
 */
AODECODE_API void aodecode_pool_stats(
    aodecode_pool_t* pool,
    int32_t* out_total,
    int32_t* out_available,
    int32_t* out_acquired
);

/**
 * Create a buffer pool with memory limit.
 * 
 * Extended version that sets a memory limit for overflow buffers.
 * When the limit is reached, acquire will block until a buffer is released.
 * 
 * @param count         Number of fixed buffers to pre-allocate
 * @param memory_limit  Maximum total memory (0 = unlimited)
 * @return Pool handle, or NULL on allocation failure
 */
AODECODE_API aodecode_pool_t* aodecode_create_pool_ex(int32_t count, int64_t memory_limit);

/**
 * Set the memory limit for a pool.
 * 
 * Can be called at any time to adjust the limit.
 * 
 * @param pool          Pool handle
 * @param memory_limit  Maximum total memory (0 = unlimited)
 */
AODECODE_API void aodecode_pool_set_limit(aodecode_pool_t* pool, int64_t memory_limit);

/**
 * Get the current memory limit.
 * 
 * @param pool  Pool handle
 * @return Current memory limit (0 = unlimited)
 */
AODECODE_API int64_t aodecode_pool_get_limit(aodecode_pool_t* pool);

/**
 * Get current memory usage.
 * 
 * @param pool  Pool handle
 * @return Total memory used (fixed pool + overflow buffers)
 */
AODECODE_API int64_t aodecode_pool_get_usage(aodecode_pool_t* pool);

/**
 * Extended pool statistics.
 * 
 * @param pool              Pool handle
 * @param out_total         Output: total fixed buffers in pool
 * @param out_available     Output: currently available fixed buffers
 * @param out_acquired      Output: currently acquired fixed buffers
 * @param out_overflow_count Output: number of overflow buffers in use
 * @param out_overflow_bytes Output: bytes in overflow buffers
 * @param out_memory_limit  Output: current memory limit
 */
AODECODE_API void aodecode_pool_stats_ex(
    aodecode_pool_t* pool,
    int32_t* out_total,
    int32_t* out_available,
    int32_t* out_acquired,
    int32_t* out_overflow_count,
    int64_t* out_overflow_bytes,
    int64_t* out_memory_limit
);

/**
 * Decode multiple JPEGs in parallel.
 * 
 * This is the main batch decode function. It decodes all input JPEGs
 * concurrently using OpenMP, with each thread using its own turbojpeg
 * decompressor instance for thread safety.
 * 
 * @param requests      Array of decode requests (input/output)
 * @param count         Number of requests
 * @param pool          Buffer pool for output images (may be NULL for malloc)
 * @param max_threads   Maximum parallel threads (0 = auto)
 * 
 * @return Number of successfully decoded images
 * 
 * Note: Caller is responsible for freeing decoded images either by
 * returning buffers to the pool or calling aodecode_free_image().
 */
AODECODE_API int32_t aodecode_batch(
    aodecode_request_t* requests,
    int32_t count,
    aodecode_pool_t* pool,
    int32_t max_threads
);

/**
 * Combined: read cache files and decode in one native call.
 * 
 * This is the optimal path for loading cached chunks - it reads
 * multiple cache files and decodes them in parallel, all in native
 * code without Python involvement.
 * 
 * @param cache_paths   Array of cache file paths
 * @param count         Number of paths
 * @param images        Output array of decoded images (caller provides)
 * @param pool          Buffer pool (may be NULL)
 * @param max_threads   Maximum parallel threads (0 = auto)
 * 
 * @return Number of successfully decoded images
 * 
 * Note: images[i].data will be NULL for files that failed to read/decode.
 * Check images[i].width > 0 to determine success.
 */
AODECODE_API int32_t aodecode_from_cache(
    const char** cache_paths,
    int32_t count,
    aodecode_image_t* images,
    aodecode_pool_t* pool,
    int32_t max_threads
);

/**
 * Decode a single JPEG from memory.
 * 
 * Convenience function for decoding one JPEG without batch overhead.
 * 
 * @param jpeg_data     Pointer to JPEG data
 * @param jpeg_length   Length of JPEG data
 * @param image         Output image structure
 * @param pool          Buffer pool (may be NULL)
 * 
 * @return 1 on success, 0 on failure
 */
AODECODE_API int32_t aodecode_single(
    const uint8_t* jpeg_data,
    uint32_t jpeg_length,
    aodecode_image_t* image,
    aodecode_pool_t* pool
);

/**
 * Free a decoded image.
 * 
 * If the image buffer was from a pool, returns it to the pool.
 * Otherwise, frees the malloc'd memory.
 * 
 * @param image     Image to free
 * @param pool      Pool the image may have come from (may be NULL)
 */
AODECODE_API void aodecode_free_image(
    aodecode_image_t* image,
    aodecode_pool_t* pool
);

/**
 * Get version information for the aodecode module.
 * 
 * @return Static string with version info (do not free)
 */
AODECODE_API const char* aodecode_version(void);

/* ============================================================================
 * Persistent Decoder Pool
 * ============================================================================
 * Functions for managing persistent TurboJPEG decoder handles. These handles
 * are reused across decode calls, eliminating the ~0.15ms overhead of
 * creating/destroying handles in each parallel loop.
 */

/**
 * Initialize persistent decoder handles.
 * 
 * Creates one TurboJPEG decompressor per OpenMP thread for reuse.
 * Call this once during application startup for optimal first-tile latency.
 * 
 * Thread Safety: Safe to call from multiple threads (uses internal locking).
 */
AODECODE_API void aodecode_init_persistent_decoders(void);

/**
 * Cleanup persistent decoder handles.
 * 
 * Destroys all cached TurboJPEG decompressors. Call during application
 * shutdown to release resources.
 * 
 * Thread Safety: Should only be called when no decode operations are active.
 */
AODECODE_API void aodecode_cleanup_persistent_decoders(void);

/**
 * Get a persistent decoder handle for the current thread.
 * 
 * Returns the cached TurboJPEG decompressor for this thread, creating it
 * if necessary. If OpenMP is not available or thread ID is out of range,
 * creates a new handle that must be destroyed by the caller.
 * 
 * Use aodecode_is_persistent_decoder() to check if destruction is needed.
 * 
 * @return TurboJPEG handle (never NULL unless TurboJPEG init fails)
 */
AODECODE_API void* aodecode_get_thread_decoder(void);

/**
 * Check if a decoder handle is from the persistent pool.
 * 
 * @param tjh  TurboJPEG handle to check
 * @return 1 if persistent (don't destroy), 0 if temporary (caller must destroy)
 */
AODECODE_API int aodecode_is_persistent_decoder(void* tjh);

/**
 * Full warmup of the decode pipeline.
 * 
 * Prepares the native decode pipeline for optimal first-tile performance:
 * - Initializes persistent TurboJPEG decoder handles
 * - Forces OpenMP thread pool creation
 * - Pre-faults buffer pool memory pages (if pool provided)
 * 
 * Call this once during application startup before any decode operations.
 * 
 * @param pool  Buffer pool to pre-warm (may be NULL)
 */
AODECODE_API void aodecode_warmup_full(aodecode_pool_t* pool);

#ifdef __cplusplus
}
#endif

#endif /* AODECODE_H */

