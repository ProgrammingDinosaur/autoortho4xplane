/**
 * aodds.h - Native DDS Texture Building for AutoOrtho
 * 
 * Provides complete native DDS generation pipeline:
 * - Cache reading + JPEG decoding + image composition
 * - Mipmap generation with efficient image reduction
 * - DXT1/DXT5 (BC1/BC3) compression via ISPC texcomp
 * - DDS header generation compliant with X-Plane requirements
 * 
 * This module provides the highest performance impact by moving
 * the entire DDS building pipeline to native code.
 */

#ifndef AODDS_H
#define AODDS_H

#include <stdint.h>
#include <stddef.h>  /* For size_t */
#include "aodecode.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Export/Import macros */
#ifdef _WIN32
  #ifdef AOPIPELINE_EXPORTS
    #define AODDS_API __declspec(dllexport)
  #else
    #define AODDS_API __declspec(dllimport)
  #endif
#else
  #define AODDS_API __attribute__((__visibility__("default")))
#endif

/**
 * DDS compression format.
 */
typedef enum {
    DDS_FORMAT_BC1 = 0,  /**< DXT1, 8 bytes per 4x4 block, no alpha */
    DDS_FORMAT_BC3 = 1,  /**< DXT5, 16 bytes per 4x4 block, with alpha */
} dds_format_t;

/**
 * Statistics from DDS building.
 */
typedef struct {
    int32_t chunks_found;       /**< Number of cache hits */
    int32_t chunks_decoded;     /**< Number successfully decoded */
    int32_t chunks_failed;      /**< Number that used fallback color */
    int32_t mipmaps_generated;  /**< Number of mipmap levels created */
    double  elapsed_ms;         /**< Total time in milliseconds */
} dds_stats_t;

/**
 * Tile build request structure.
 * 
 * Contains all parameters needed to build a complete DDS tile
 * from cached JPEG chunks.
 */
typedef struct {
    /* Input parameters */
    const char* cache_dir;      /**< Directory containing cached JPEGs */
    int32_t tile_row;           /**< Tile row coordinate (in tile units) */
    int32_t tile_col;           /**< Tile column coordinate (in tile units) */
    const char* maptype;        /**< Map source identifier (e.g., "BI", "EOX") */
    int32_t zoom;               /**< Zoom level for chunk fetching */
    int32_t chunks_per_side;    /**< Chunks per side (typically 16 for ZL16) */
    dds_format_t format;        /**< Output compression format */
    
    /* Fallback color for missing chunks (RGB) */
    uint8_t missing_r;
    uint8_t missing_g;
    uint8_t missing_b;
    
    /* Output buffer (caller provides) */
    uint8_t* dds_buffer;        /**< Pre-allocated output buffer */
    uint32_t dds_buffer_size;   /**< Size of output buffer in bytes */
    uint32_t dds_written;       /**< Actual bytes written (output) */
    
    /* Statistics (output) */
    dds_stats_t stats;
    
    /* Error handling */
    int32_t success;            /**< 1 = success, 0 = failure */
    char error[256];            /**< Error message if failed */
} dds_tile_request_t;

/**
 * Build a complete DDS tile from cached JPEGs.
 * 
 * This is the main entry point for tile building. It performs the entire
 * pipeline in native code:
 * 1. Batch read all chunk cache files
 * 2. Parallel decode all JPEGs
 * 3. Compose chunks into full tile image
 * 4. Generate all mipmap levels
 * 5. Compress each mipmap with ISPC texcomp
 * 6. Write complete DDS file to output buffer
 * 
 * @param request   Tile build request (input/output)
 * @param pool      Buffer pool for decoding (may be NULL)
 * 
 * @return 1 on success, 0 on failure (check request->error)
 * 
 * Memory: The output buffer must be pre-allocated by caller.
 * Use aodds_calc_dds_size() to determine required size.
 * 
 * Thread Safety: This function is thread-safe and can be called
 * from multiple Python threads simultaneously.
 */
AODDS_API int32_t aodds_build_tile(
    dds_tile_request_t* request,
    aodecode_pool_t* pool
);

/**
 * Calculate required DDS buffer size.
 * 
 * @param width         Image width in pixels
 * @param height        Image height in pixels
 * @param mipmap_count  Number of mipmap levels (0 = auto-calculate)
 * @param format        Compression format
 * 
 * @return Required buffer size in bytes including DDS header
 */
AODDS_API size_t aodds_calc_dds_size(
    int32_t width,
    int32_t height,
    int32_t mipmap_count,
    dds_format_t format
);

/**
 * Calculate number of mipmap levels for given dimensions.
 * 
 * @param width     Image width
 * @param height    Image height
 * 
 * @return Number of mipmap levels (minimum 1)
 */
AODDS_API int32_t aodds_calc_mipmap_count(int32_t width, int32_t height);

/**
 * Write DDS header to buffer.
 * 
 * @param buffer        Output buffer
 * @param width         Image width
 * @param height        Image height
 * @param mipmap_count  Number of mipmap levels
 * @param format        Compression format
 * 
 * @return Number of bytes written (128 for standard DDS header)
 */
AODDS_API int32_t aodds_write_header(
    uint8_t* buffer,
    int32_t width,
    int32_t height,
    int32_t mipmap_count,
    dds_format_t format
);

/**
 * Compose multiple chunk images into a single tile image.
 * 
 * @param chunks            Array of decoded chunk images
 * @param chunks_per_side   Number of chunks per side
 * @param output            Output image (caller must allocate data)
 * 
 * @return 1 on success, 0 on failure
 * 
 * Note: Output image data must be pre-allocated to 
 * (chunks_per_side * 256) ^ 2 * 4 bytes
 */
AODDS_API int32_t aodds_compose_chunks(
    aodecode_image_t* chunks,
    int32_t chunks_per_side,
    aodecode_image_t* output
);

/**
 * Fill missing chunk positions with solid color.
 * 
 * @param chunks            Array of chunk images (NULL = missing)
 * @param chunks_per_side   Number of chunks per side
 * @param output            Output image to fill
 * @param r, g, b           Fill color
 */
AODDS_API void aodds_fill_missing(
    aodecode_image_t* chunks,
    int32_t chunks_per_side,
    aodecode_image_t* output,
    uint8_t r, uint8_t g, uint8_t b
);

/**
 * Reduce image by half (generate next mipmap level).
 * 
 * Uses 2x2 box filter averaging for smooth mipmap transitions.
 * 
 * @param input     Source image
 * @param output    Output image (dimensions = input/2)
 * 
 * @return 1 on success, 0 on failure
 * 
 * Note: Output data must be pre-allocated to (width/2) * (height/2) * 4 bytes
 */
AODDS_API int32_t aodds_reduce_half(
    const aodecode_image_t* input,
    aodecode_image_t* output
);

/**
 * Compress RGBA image to BC1/BC3 format.
 * 
 * Uses ISPC texcomp library for high-performance SIMD compression.
 * 
 * @param image     Source RGBA image
 * @param format    Target compression format
 * @param output    Output buffer (must be correctly sized)
 * 
 * @return Number of bytes written, or 0 on failure
 * 
 * Required output size:
 * - BC1: (width/4) * (height/4) * 8 bytes
 * - BC3: (width/4) * (height/4) * 16 bytes
 */
AODDS_API uint32_t aodds_compress(
    const aodecode_image_t* image,
    dds_format_t format,
    uint8_t* output
);

/**
 * Build DDS from pre-decoded images.
 * 
 * Lower-level function for custom pipelines. Assumes chunks are
 * already decoded and in the correct order.
 * 
 * @param chunks            Array of decoded chunk images
 * @param chunks_per_side   Number of chunks per side
 * @param format            Compression format
 * @param missing_color     RGB color for missing chunks
 * @param dds_output        Pre-allocated output buffer
 * @param output_size       Output buffer size
 * @param bytes_written     Actual bytes written (output)
 * 
 * @return 1 on success, 0 on failure
 */
AODDS_API int32_t aodds_build_from_chunks(
    aodecode_image_t* chunks,
    int32_t chunks_per_side,
    dds_format_t format,
    uint8_t missing_color[3],
    uint8_t* dds_output,
    uint32_t output_size,
    uint32_t* bytes_written
);

/**
 * Build DDS from pre-read JPEG data (HYBRID APPROACH).
 * 
 * This is the optimal entry point for the hybrid pipeline:
 * - Python reads cache files (fast for OS-cached files)
 * - Native decodes + composes + compresses (parallelism helps)
 * 
 * This avoids file I/O overhead in native code and ctypes path overhead.
 * 
 * @param jpeg_data         Array of JPEG data pointers (NULL = missing chunk)
 * @param jpeg_sizes        Array of JPEG data sizes (0 = missing chunk)
 * @param chunk_count       Number of chunks (must be perfect square)
 * @param format            Output compression format
 * @param missing_r/g/b     Fill color for missing chunks
 * @param dds_output        Pre-allocated output buffer
 * @param output_size       Output buffer size
 * @param bytes_written     Actual bytes written (output)
 * @param pool              Optional buffer pool (may be NULL)
 * 
 * @return 1 on success, 0 on failure
 * 
 * Performance: This is ~2-3x faster than aodds_build_tile because:
 * - No file I/O in native code (done in Python)
 * - No path string processing
 * - Single ctypes call instead of many
 */
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
    int32_t max_threads          /**< 0 = auto (default), >0 = limit OpenMP threads */
);

/**
 * Calculate compressed size for a single mipmap level.
 * 
 * @param width     Image width in pixels
 * @param height    Image height in pixels
 * @param format    Compression format
 * 
 * @return Compressed size in bytes (no header included)
 */
AODDS_API uint32_t aodds_calc_mipmap_size(
    int32_t width,
    int32_t height,
    dds_format_t format
);

/**
 * Build a single mipmap level from pre-read JPEG data.
 * 
 * This function builds ONLY the specified mipmap level, not the entire
 * mipmap chain. Returns raw DXT-compressed bytes without a DDS header.
 * 
 * Use this for on-demand mipmap building where X-Plane requests a specific
 * mipmap level. The output can be written directly to pydds.DDS.mipmap_list[n].databuffer.
 * 
 * Performance: ~3-4x faster than Python path for 64-256 chunk grids due to:
 * - Parallel JPEG decoding (OpenMP)
 * - ISPC SIMD compression
 * - No Python GIL contention
 * 
 * @param jpeg_data         Array of JPEG data pointers (NULL = missing chunk)
 * @param jpeg_sizes        Array of JPEG data sizes (0 = missing chunk)
 * @param chunk_count       Number of chunks (must be perfect square: 4, 16, 64, 256)
 * @param format            Output compression format (BC1 or BC3)
 * @param missing_r/g/b     Fill color for missing chunks
 * @param output            Pre-allocated output buffer for raw DXT bytes
 * @param output_size       Output buffer size in bytes
 * @param bytes_written     Actual bytes written (output)
 * @param pool              Optional buffer pool for decode (may be NULL)
 * 
 * @return 1 on success, 0 on failure
 * 
 * Memory: Output buffer must be at least aodds_calc_mipmap_size() bytes.
 * 
 * Thread Safety: Thread-safe, can be called from multiple threads.
 */
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
);

/**
 * Build a rectangular partial mipmap from JPEG data.
 * 
 * Unlike aodds_build_single_mipmap() which requires square chunk grids,
 * this function supports arbitrary width × height chunk layouts.
 * Outputs ONLY the compressed DXT data (no DDS header, no mipmap chain).
 * 
 * Use case: Building specific rows of mipmap 0 for partial reads,
 * providing 10-20x speedup over Python PIL + DXT compression path.
 * 
 * @param jpeg_data         Array of JPEG data pointers (NULL = missing chunk)
 * @param jpeg_sizes        Array of JPEG data sizes (0 = missing chunk)
 * @param chunks_width      Number of chunks horizontally (e.g., 8)
 * @param chunks_height     Number of chunks vertically (e.g., 1 for single row)
 * @param format            Output compression format (BC1 or BC3)
 * @param missing_r/g/b     Fill color for missing chunks
 * @param output            Pre-allocated output buffer for DXT bytes
 * @param output_size       Output buffer size in bytes
 * @param bytes_written     Actual bytes written (output)
 * @param pool              Optional buffer pool for decode (may be NULL)
 * 
 * @return 1 on success, 0 on failure
 * 
 * Memory: Output buffer must be at least:
 *         (chunks_width * 256 / 4) * (chunks_height * 256 / 4) * blocksize
 *         where blocksize is 8 for BC1, 16 for BC3.
 * 
 * Thread Safety: Thread-safe, can be called from multiple threads.
 */
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
);

/**
 * Build a mipmap chain from JPEG data: starting level + all smaller mipmaps.
 * 
 * This function builds the requested mipmap level AND all smaller mipmaps
 * down to 4×4, matching the behavior of Python's gen_mipmaps().
 * 
 * Use this for on-demand mipmap building to ensure smaller mipmaps are
 * also populated, preventing NULL buffer warnings when X-Plane reads
 * into smaller mipmap positions.
 * 
 * @param jpeg_data         Array of JPEG data pointers (NULL = missing chunk)
 * @param jpeg_sizes        Array of JPEG data sizes (0 = missing chunk)
 * @param chunk_count       Number of chunks (must be perfect square: 4, 16, 64, 256)
 * @param format            Output compression format (BC1 or BC3)
 * @param missing_r/g/b     Fill color for missing chunks
 * @param output            Pre-allocated output buffer for ALL mipmap DXT bytes
 * @param output_size       Output buffer size in bytes
 * @param bytes_written     Total bytes written (output)
 * @param mipmap_count_out  Number of mipmaps generated (output)
 * @param mipmap_offsets    Array of offsets for each mipmap in output (output)
 * @param mipmap_sizes      Array of sizes for each mipmap (output)
 * @param max_mipmaps       Maximum mipmaps to generate (0 = all down to 4×4)
 * @param pool              Optional buffer pool for decode (may be NULL)
 * 
 * @return 1 on success, 0 on failure
 * 
 * Memory: Output buffer must be large enough for all mipmaps.
 *         mipmap_offsets and mipmap_sizes must have at least max_mipmaps entries.
 * 
 * Thread Safety: Thread-safe, can be called from multiple threads.
 */
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
);

/**
 * Build ALL mipmaps from native zoom level chunks.
 * 
 * QUALITY OPTIMIZATION:
 * Builds each mipmap from its native zoom level's JPEG chunks:
 *   - Mipmap 0: ZL16 chunks (256 chunks for 4096x4096)
 *   - Mipmap 1: ZL15 chunks (64 chunks for 2048x2048)
 *   - Mipmap 2: ZL14 chunks (16 chunks for 1024x1024)
 *   - Mipmap 3: ZL13 chunks (4 chunks for 512x512)
 *   - Mipmap 4: ZL12 chunks (1 chunk for 256x256)
 * 
 * FALLBACK:
 * If a zoom level has no chunks, falls back to reduce_half from the
 * previous mipmap level. This maintains quality while handling failures.
 * 
 * @param jpeg_data_per_zoom    Array of JPEG data arrays [zoom_count][chunk_count]
 * @param jpeg_sizes_per_zoom   Array of size arrays [zoom_count][chunk_count]
 * @param chunk_counts_per_zoom Array of chunk counts per zoom [256, 64, 16, 4, 1]
 * @param zoom_count            Number of zoom levels provided
 * @param format                Output compression format (BC1 or BC3)
 * @param missing_r/g/b         Fill color for missing chunks
 * @param output                Pre-allocated output buffer
 * @param output_size           Output buffer size
 * @param bytes_written         Actual bytes written (output)
 * @param pool                  Optional buffer pool (may be NULL)
 * 
 * @return 1 on success, 0 on failure
 * 
 * Thread Safety: Thread-safe, can be called from multiple threads.
 */
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
);

/**
 * Build DDS from pre-read JPEG data and write directly to file.
 * 
 * PERFORMANCE OPTIMIZATION for predictive DDS:
 * This eliminates ~65ms Python copy overhead by writing DDS data
 * directly to the disk cache file. Perfect for EphemeralDDSCache.
 * 
 * Uses temp file + rename for atomicity (no corrupt files on crash).
 * 
 * @param jpeg_data         Array of JPEG data pointers (NULL = missing chunk)
 * @param jpeg_sizes        Array of JPEG data sizes (0 = missing chunk)
 * @param chunk_count       Number of chunks (must be perfect square)
 * @param format            Output compression format
 * @param missing_r/g/b     Fill color for missing chunks
 * @param output_path       Path to output DDS file (will be created/overwritten)
 * @param bytes_written     Actual bytes written (output)
 * @param pool              Optional buffer pool (may be NULL)
 * 
 * @return 1 on success, 0 on failure
 * 
 * Cross-platform: Uses standard C file I/O (fopen, fwrite, rename).
 */
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
    int32_t max_threads          /**< 0 = auto (default), >0 = limit OpenMP threads */
);

/**
 * Build DDS from cache files and write directly to disk.
 * 
 * NATIVE MODE OPTIMIZATION for predictive DDS:
 * Same as aodds_build_tile() but writes directly to disk instead of buffer.
 * Eliminates ~65ms Python copy overhead.
 * 
 * Uses temp file + rename for atomicity.
 * 
 * @param cache_dir         Directory containing cached JPEGs
 * @param tile_row          Tile row coordinate
 * @param tile_col          Tile column coordinate
 * @param maptype           Map source identifier (e.g., "BI")
 * @param zoom              Zoom level
 * @param chunks_per_side   Chunks per side (typically 16)
 * @param format            Output compression format
 * @param missing_r/g/b     Fill color for missing chunks
 * @param output_path       Path to output DDS file
 * @param bytes_written     Actual bytes written (output)
 * @param pool              Optional buffer pool (may be NULL)
 * 
 * @return 1 on success, 0 on failure
 */
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
);

/**
 * Initialize the ISPC compression library.
 * 
 * This loads the ISPC texcomp dynamic library. Called automatically
 * on first use, but can be called explicitly for early initialization.
 * 
 * @return 1 if ISPC loaded successfully, 0 if failed (fallback to stb_dxt)
 */
AODDS_API int32_t aodds_init_ispc(void);

/**
 * Set whether to use ISPC compression or fallback (STB).
 * 
 * This allows respecting user configuration for compressor preference.
 * When use_ispc=0, the fallback compressor is used even if ISPC is available.
 * 
 * @param use_ispc  1 to use ISPC (if available), 0 to force fallback
 */
AODDS_API void aodds_set_use_ispc(int32_t use_ispc);

/**
 * Get whether ISPC compression is currently active.
 * 
 * @return 1 if ISPC will be used, 0 if fallback will be used
 */
AODDS_API int32_t aodds_get_use_ispc(void);

/**
 * Check if fallback compressor is being used.
 * 
 * Returns 1 if ISPC is unavailable or force_fallback is set.
 * Useful for displaying a warning to users that quality may be reduced.
 * 
 * @return 1 if fallback compressor will be used, 0 if ISPC is active
 */
AODDS_API int32_t aodds_using_fallback_compressor(void);

/**
 * Get version information for the aodds module.
 * 
 * @return Static string with version info (do not free)
 */
AODDS_API const char* aodds_version(void);

/* ============================================================================
 * STREAMING TILE BUILDER API
 * ============================================================================
 * The streaming builder allows incremental chunk feeding with fallback support.
 * Chunks can be added as they download, with the final DDS generated when
 * all chunks are processed (or marked as missing).
 * 
 * Usage:
 *   1. Create builder with aodds_builder_create()
 *   2. Add chunks as they become available:
 *      - aodds_builder_add_chunk() for JPEG data
 *      - aodds_builder_add_fallback_image() for pre-decoded fallbacks
 *      - aodds_builder_mark_missing() when all fallbacks exhausted
 *   3. Finalize with aodds_builder_finalize() or aodds_builder_finalize_to_file()
 *   4. Reset with aodds_builder_reset() for reuse, or destroy
 * ============================================================================*/

/**
 * Chunk status values for streaming builder.
 */
typedef enum {
    CHUNK_STATUS_EMPTY = 0,          /**< Not yet received */
    CHUNK_STATUS_PENDING_DECODE = 1, /**< JPEG stored, awaiting parallel decode at finalize */
    CHUNK_STATUS_JPEG = 2,           /**< Received as JPEG, decoded */
    CHUNK_STATUS_FALLBACK = 3,       /**< Received as pre-decoded fallback */
    CHUNK_STATUS_MISSING = 4         /**< Marked as missing, will use missing_color */
} aodds_chunk_status_t;

/**
 * Streaming builder configuration.
 */
typedef struct {
    int32_t chunks_per_side;    /**< Chunks per side (typically 16) */
    dds_format_t format;        /**< Output compression format (BC1/BC3) */
    uint8_t missing_r;          /**< Fallback color R component */
    uint8_t missing_g;          /**< Fallback color G component */
    uint8_t missing_b;          /**< Fallback color B component */
    uint8_t nocopy_mode;        /**< Zero-copy mode: 0=C copies JPEG data (default), 
                                     1=C stores pointers only (caller owns memory) */
} aodds_builder_config_t;

/**
 * Streaming builder status for monitoring progress.
 */
typedef struct {
    int32_t chunks_total;       /**< Total chunks expected (chunks_per_side^2) */
    int32_t chunks_received;    /**< Chunks added (JPEG, fallback, or marked missing) */
    int32_t chunks_decoded;     /**< Successfully decoded JPEGs */
    int32_t chunks_failed;      /**< JPEG decode failures (will use missing_color) */
    int32_t chunks_fallback;    /**< Chunks using fallback images */
    int32_t chunks_missing;     /**< Chunks marked as missing */
} aodds_builder_status_t;

/**
 * Opaque handle to a streaming tile builder.
 * Created with aodds_builder_create(), destroyed with aodds_builder_destroy().
 */
typedef struct aodds_builder_s aodds_builder_t;

/* ========== BUILDER LIFECYCLE ========== */

/**
 * Create a new streaming tile builder.
 * 
 * The builder maintains internal state as chunks are added incrementally.
 * All memory is pre-allocated based on config->chunks_per_side.
 * 
 * @param config        Builder configuration (copied, caller can free)
 * @param decode_pool   Optional decode buffer pool (may be NULL)
 * 
 * @return New builder handle, or NULL on failure
 * 
 * Thread Safety: Can be called from any thread.
 */
AODDS_API aodds_builder_t* aodds_builder_create(
    const aodds_builder_config_t* config,
    aodecode_pool_t* decode_pool
);

/**
 * Reset builder for reuse (pooling support).
 * 
 * Clears all chunk data and status, but keeps memory allocations.
 * More efficient than destroy + create for pooled builders.
 * 
 * @param builder   Builder to reset
 * @param config    New configuration (may differ from original)
 * 
 * Thread Safety: NOT thread-safe. Ensure no other operations in progress.
 */
AODDS_API void aodds_builder_reset(
    aodds_builder_t* builder,
    const aodds_builder_config_t* config
);

/**
 * Destroy builder and free all resources.
 * 
 * @param builder   Builder to destroy (NULL is safe)
 * 
 * Thread Safety: NOT thread-safe. Ensure no other operations in progress.
 */
AODDS_API void aodds_builder_destroy(aodds_builder_t* builder);

/* ========== CHUNK FEEDING (Thread-Safe) ========== */

/**
 * Add a JPEG chunk for decoding.
 * 
 * The JPEG data is copied internally and decoded immediately.
 * If decode fails, the chunk is marked as failed (will use missing_color).
 * 
 * @param builder       Target builder
 * @param chunk_index   Chunk index in row-major order (0 to chunks_per_side^2 - 1)
 * @param jpeg_data     JPEG bytes (copied internally)
 * @param jpeg_size     Size of JPEG data in bytes
 * 
 * @return 1 on success (decode OK), 0 on failure (invalid index, already set, or decode failed)
 * 
 * Thread Safety: Thread-safe. Multiple threads can add chunks simultaneously.
 */
AODDS_API int32_t aodds_builder_add_chunk(
    aodds_builder_t* builder,
    int32_t chunk_index,
    const uint8_t* jpeg_data,
    uint32_t jpeg_size
);

/**
 * Add multiple JPEG chunks in a single call (batch API).
 * 
 * More efficient than calling add_chunk() repeatedly due to reduced
 * Python/C crossing overhead. All chunks are stored for deferred
 * parallel decode at finalize time.
 * 
 * @param builder       Target builder
 * @param count         Number of chunks to add
 * @param indices       Array of chunk indices (count elements)
 * @param jpeg_data     Array of JPEG data pointers (count elements)
 * @param jpeg_sizes    Array of JPEG sizes (count elements)
 * 
 * @return Number of chunks successfully added
 * 
 * Thread Safety: Thread-safe.
 */
AODDS_API int32_t aodds_builder_add_chunks_batch(
    aodds_builder_t* builder,
    int32_t count,
    const int32_t* indices,
    const uint8_t** jpeg_data,
    const uint32_t* jpeg_sizes
);

/**
 * Add multiple JPEG chunks WITHOUT copying data (zero-copy mode).
 * 
 * ZERO-COPY: C stores pointers directly, does NOT allocate or copy.
 * CALLER MUST guarantee JPEG data remains valid until finalize() completes.
 * 
 * Use this for optimal performance when Python holds references that
 * outlive the builder operation (e.g., chunk.data held by Tile objects).
 * 
 * Memory safety: C will NOT free these pointers at finalize() or reset().
 * 
 * @param builder       Target builder
 * @param count         Number of chunks to add
 * @param indices       Array of chunk indices (count elements)
 * @param jpeg_data     Array of JPEG data pointers (count elements) - NOT COPIED
 * @param jpeg_sizes    Array of JPEG sizes (count elements)
 * 
 * @return Number of chunks successfully added
 * 
 * Thread Safety: Thread-safe.
 */
AODDS_API int32_t aodds_builder_add_chunks_batch_nocopy(
    aodds_builder_t* builder,
    int32_t count,
    const int32_t* indices,
    const uint8_t** jpeg_data,
    const uint32_t* jpeg_sizes
);

/**
 * Add a pre-decoded fallback image for a chunk.
 * 
 * Used when Python has resolved a fallback (disk cache, mipmap scale, network).
 * The RGBA data is copied internally.
 * 
 * @param builder       Target builder
 * @param chunk_index   Chunk index in row-major order
 * @param rgba_data     RGBA pixel data (width * height * 4 bytes, copied)
 * @param width         Image width (must be 256)
 * @param height        Image height (must be 256)
 * 
 * @return 1 on success, 0 on failure (invalid index, already set, invalid dimensions)
 * 
 * Thread Safety: Thread-safe. Multiple threads can add chunks simultaneously.
 */
AODDS_API int32_t aodds_builder_add_fallback_image(
    aodds_builder_t* builder,
    int32_t chunk_index,
    const uint8_t* rgba_data,
    int32_t width,
    int32_t height
);

/**
 * Mark a chunk as permanently missing.
 * 
 * Call this when all fallbacks have been exhausted for a chunk.
 * The chunk position will be filled with missing_color during finalization.
 * 
 * @param builder       Target builder
 * @param chunk_index   Chunk index in row-major order
 * 
 * Thread Safety: Thread-safe.
 */
AODDS_API void aodds_builder_mark_missing(
    aodds_builder_t* builder,
    int32_t chunk_index
);

/* ========== STATUS QUERY ========== */

/**
 * Get current builder status.
 * 
 * @param builder   Source builder
 * @param status    Output status structure
 * 
 * Thread Safety: Thread-safe. Can be called while chunks are being added.
 */
AODDS_API void aodds_builder_get_status(
    const aodds_builder_t* builder,
    aodds_builder_status_t* status
);

/**
 * Check if all chunks have been processed.
 * 
 * A chunk is "processed" if it has been added (JPEG or fallback) or marked missing.
 * 
 * @param builder   Source builder
 * 
 * @return 1 if all chunks processed, 0 if some chunks still pending
 * 
 * Thread Safety: Thread-safe.
 */
AODDS_API int32_t aodds_builder_is_complete(const aodds_builder_t* builder);

/* ========== FINALIZATION ========== */

/**
 * Finalize the tile and write DDS to buffer.
 * 
 * This performs the final steps:
 * 1. Fill missing chunk positions with missing_color
 * 2. Compose all chunks into full tile image
 * 3. Generate all mipmap levels
 * 4. Compress with BC1/BC3
 * 5. Write complete DDS to output buffer
 * 
 * @param builder       Source builder
 * @param dds_output    Pre-allocated output buffer
 * @param output_size   Size of output buffer
 * @param bytes_written Actual bytes written (output)
 * 
 * @return 1 on success, 0 on failure
 * 
 * Thread Safety: NOT thread-safe with add_chunk/add_fallback_image.
 *                Ensure all chunk operations complete before calling.
 */
AODDS_API int32_t aodds_builder_finalize(
    aodds_builder_t* builder,
    uint8_t* dds_output,
    uint32_t output_size,
    uint32_t* bytes_written,
    int32_t max_threads          /**< 0 = auto (default), >0 = limit OpenMP threads */
);

/**
 * Finalize the tile and write DDS directly to file.
 * 
 * Zero-copy optimization for prefetch cache. Uses temp file + rename
 * for atomicity.
 * 
 * @param builder       Source builder
 * @param output_path   Path to output DDS file (will be created/overwritten)
 * @param bytes_written Actual bytes written (output)
 * 
 * @return 1 on success, 0 on failure
 * 
 * Thread Safety: NOT thread-safe with add_chunk/add_fallback_image.
 */
AODDS_API int32_t aodds_builder_finalize_to_file(
    aodds_builder_t* builder,
    const char* output_path,
    uint32_t* bytes_written,
    int32_t max_threads          /**< 0 = auto (default), >0 = limit OpenMP threads */
);

/* ========== DECODER POOL CONFIGURATION ========== */

/**
 * Initialize the JPEG decoder pool with a specific size.
 * 
 * The decoder pool provides thread-safe JPEG decoding for parallel tile
 * building. Pool size should be calculated as:
 *   pool_size = max_builders * cpu_threads
 * 
 * For example: 4 background builders on 32-thread CPU = 128 decoders
 * 
 * Must be called before any decoding operations (typically at startup).
 * If not called, a default pool size of 64 is used.
 * 
 * Memory usage: ~2KB per pooled decoder (idle), ~350KB per active decode.
 * 
 * @param pool_size  Number of decoders to pool (minimum 1, no upper limit)
 * @return 1 on success, 0 if pool already in use (too late to resize)
 * 
 * Thread Safety: Thread-safe. Should be called once at startup.
 */
AODDS_API int32_t aodds_init_decoder_pool(int32_t pool_size);

/**
 * Get the current decoder pool size.
 * @return Current pool size, or default (64) if not explicitly initialized
 */
AODDS_API int32_t aodds_get_decoder_pool_size(void);

/**
 * Get decoder pool statistics for monitoring.
 * 
 * @param out_pool_size     Output: total pool capacity
 * @param out_in_use        Output: decoders currently in use
 * @param out_allocated     Output: decoders actually created (lazy allocation)
 * 
 * Thread Safety: Thread-safe.
 */
AODDS_API void aodds_get_decoder_pool_stats(
    int32_t* out_pool_size,
    int32_t* out_in_use,
    int32_t* out_allocated
);

#ifdef __cplusplus
}
#endif

#endif /* AODDS_H */

