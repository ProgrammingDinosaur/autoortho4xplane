/**
 * aopipeline.h - Unified Native Pipeline API for AutoOrtho
 * 
 * This is the main public header that combines all pipeline components
 * into a single unified API. For most use cases, include only this header.
 * 
 * Components:
 * - aocache:  Parallel cache file I/O
 * - aodecode: Parallel JPEG decoding with turbojpeg
 * - aodds:    DDS texture building with ISPC compression
 * 
 * Quick Start:
 *   // Build a DDS tile from cached JPEGs
 *   dds_tile_request_t req = {
 *       .cache_dir = "/path/to/cache",
 *       .tile_row = 1234,
 *       .tile_col = 5678,
 *       .maptype = "BI",
 *       .zoom = 16,
 *       .chunks_per_side = 16,
 *       .format = DDS_FORMAT_BC1,
 *       .missing_r = 66, .missing_g = 77, .missing_b = 55,
 *       .dds_buffer = buffer,
 *       .dds_buffer_size = sizeof(buffer),
 *   };
 *   aodds_build_tile(&req, NULL);
 */

#ifndef AOPIPELINE_H
#define AOPIPELINE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Export/Import macros */
#ifdef _WIN32
  #ifdef AOPIPELINE_EXPORTS
    #define AOPIPELINE_API __declspec(dllexport)
  #else
    #define AOPIPELINE_API __declspec(dllimport)
  #endif
#else
  #define AOPIPELINE_API __attribute__((__visibility__("default")))
#endif

/*============================================================================
 * Component Headers
 *============================================================================*/

#include "aocache.h"
#include "aodecode.h"
#include "aodds.h"

/*============================================================================
 * Pipeline Statistics
 *============================================================================*/

/**
 * Complete pipeline statistics.
 */
typedef struct {
    /* Cache I/O */
    int32_t files_read;
    int32_t files_failed;
    double cache_read_ms;
    
    /* JPEG decoding */
    int32_t jpegs_decoded;
    int32_t jpegs_failed;
    double decode_ms;
    
    /* Image composition */
    int32_t chunks_composed;
    int32_t chunks_missing;
    double compose_ms;
    
    /* DDS building */
    int32_t mipmaps_generated;
    double compress_ms;
    
    /* Total */
    double total_ms;
} aopipeline_stats_t;

/*============================================================================
 * Unified Pipeline API
 *============================================================================*/

/**
 * Initialize all pipeline components.
 * 
 * This is optional - components are initialized lazily on first use.
 * Call this for early initialization during startup.
 * 
 * @return Number of components successfully initialized
 */
AOPIPELINE_API int32_t aopipeline_init(void);

/**
 * Shutdown all pipeline components.
 * 
 * Frees all resources. Call during application shutdown.
 */
AOPIPELINE_API void aopipeline_shutdown(void);

/**
 * Get combined version information.
 * 
 * @return Static string with all component versions
 */
AOPIPELINE_API const char* aopipeline_version(void);

/**
 * Check which components are available.
 * 
 * @param out_cache     Output: 1 if cache I/O available
 * @param out_decode    Output: 1 if JPEG decoding available  
 * @param out_dds       Output: 1 if DDS building available
 */
AOPIPELINE_API void aopipeline_check_components(
    int32_t* out_cache,
    int32_t* out_decode,
    int32_t* out_dds
);

/**
 * Build a complete DDS tile from cache (high-level API).
 * 
 * This is the recommended entry point for building DDS tiles.
 * It handles all steps: cache reading, JPEG decoding, composition,
 * mipmap generation, and compression.
 * 
 * @param cache_dir         Directory containing cached JPEGs
 * @param tile_row          Tile row coordinate
 * @param tile_col          Tile column coordinate  
 * @param maptype           Map source (e.g., "BI", "EOX")
 * @param zoom              Zoom level
 * @param chunks_per_side   Chunks per side (typically 16)
 * @param format            DDS_FORMAT_BC1 or DDS_FORMAT_BC3
 * @param missing_color     RGB color for missing chunks (3 bytes)
 * @param dds_output        Pre-allocated output buffer
 * @param output_size       Size of output buffer
 * @param bytes_written     Output: actual bytes written
 * @param stats             Output: optional pipeline statistics
 * 
 * @return 1 on success, 0 on failure
 */
AOPIPELINE_API int32_t aopipeline_build_cached_tile(
    const char* cache_dir,
    int32_t tile_row,
    int32_t tile_col,
    const char* maptype,
    int32_t zoom,
    int32_t chunks_per_side,
    dds_format_t format,
    const uint8_t* missing_color,
    uint8_t* dds_output,
    uint32_t output_size,
    uint32_t* bytes_written,
    aopipeline_stats_t* stats
);

#ifdef __cplusplus
}
#endif

#endif /* AOPIPELINE_H */

