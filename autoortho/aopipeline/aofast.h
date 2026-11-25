/**
 * aofast.h - Native reads of textures served from their cache file.
 *
 * Hybrid read path plan (docs/developer/hybrid-read-path-plan.md), stage S2.
 *
 * The mount's FUSE `read` callback is replaced by aofast_read. A read of a
 * texture Python registered (a complete cache file, no season colours, every
 * mipmap-0 row it touches already checked) is answered here from the file,
 * on the file system's own thread, without Python or its lock. Every other
 * read, and any read this code has a doubt about, is handed to the Python
 * callback it replaced, which answers it as before.
 *
 * Nothing here decides what a texture is: Python declares, at open, what a
 * handle may serve. The code includes no FUSE header; the offset of `fh` in
 * `struct fuse_file_info` is given at init.
 */
#ifndef AOFAST_H
#define AOFAST_H

#include <stddef.h>
#include <stdint.h>

#ifdef _WIN32
  #if defined(AOPIPELINE_EXPORTS) || defined(AOFAST_EXPORTS)
    #define AOFAST_API __declspec(dllexport)
  #else
    #define AOFAST_API
  #endif
#else
  #define AOFAST_API __attribute__((__visibility__("default")))
#endif

#define AOFAST_VERSION "1.1.0"

/* Handles of registered textures carry this bit, the slot's generation and
 * its index: TAG | (generation << 16) | slot. */
#define AOFAST_TAG ((uint64_t)1 << 62)
#define AOFAST_SLOTS 4096
#define AOFAST_MAX_LEVELS 24
#define AOFAST_MAX_ROWS 64
/* Mounts one process may serve with native reads (shared workers serve
 * several, each with its own Python callback). */
#define AOFAST_MOUNTS 32
/* Season colour tables (65,536 RGB565 -> RGB565 entries) held at once. */
#define AOFAST_TABLES 32

/* The FUSE read callback signature (FUSE 2 and 3 alike). */
typedef int (*aofast_read_fn)(const char *path, char *buf, size_t size,
                              int64_t offset, void *fi);

/* One wanted-level sample for the paging model, drained by Python. */
typedef struct {
    int32_t slot;
    uint32_t generation;
    int32_t level;
    int32_t count;
} aofast_sample;

/* Counters since the start (Python works out differences). */
typedef struct {
    int64_t reads;           /* answered here */
    int64_t bytes;
    int64_t deferred;        /* tagged handle handed to Python, any reason */
    int64_t deferred_rows;   /* ... because a mipmap-0 row was not checked */
    int64_t deferred_io;     /* ... because the file read failed or was short */
    int64_t deferred_closed; /* ... because the slot was disabled or reused */
    int64_t past_end;        /* reads at or past the end: answered with 0 bytes */
    int64_t active_slots;
    int64_t deferred_level;  /* ... because a partial entry lacks the level wanted */
} aofast_stats;

AOFAST_API const char *aofast_version(void);

/* The byte offset of `fh` inside `struct fuse_file_info`. */
AOFAST_API void aofast_set_fh_offset(int32_t fh_offset);
/* One mount: `python_read` answers what this code does not. Returns the
 * mount's index, or -1 when every entry is taken. */
AOFAST_API int32_t aofast_attach(aofast_read_fn python_read);
/* The read callback to put in that mount's fuse_operations. */
AOFAST_API aofast_read_fn aofast_entry(int32_t mount);
/* After the mount's loop ended. */
AOFAST_API void aofast_detach(int32_t mount);
/* 0 hands every read to Python. */
AOFAST_API void aofast_set_enabled(int32_t enabled);

/* Put a season colour table in entry `id` (copied). Refused (0) while a
 * registered open uses that entry. */
AOFAST_API int32_t aofast_table_set(int32_t id, const uint16_t *table);

/* Register one open of a texture. `path` is UTF-8 (wide on Windows through
 * aofast_register_w). The file must have `size` bytes and the modification
 * time `mtime_ns` (nanoseconds since 1970). `level_starts` holds
 * `level_count + 1` offsets: the start of each mipmap and the end of the last.
 * Mipmap 0 is `rows` rows of `row_bytes` from `row_start`; `verified` has a
 * bit set for each row already checked against its manifest.
 * `finest_level` > 0 is a partial entry: the file holds that level and the
 * coarser ones; bytes of finer levels a read only touches are stand-in
 * blocks (the coarsest stored block repeated), a read that needs one goes to
 * Python. `block_size` is 8 (BC1) or 16 (BC3). `table` is a season table
 * entry (aofast_table_set) applied to every answer, or -1.
 * Returns the tagged handle, or 0 (the open stays with Python). */
AOFAST_API uint64_t aofast_register(const char *path, int64_t size,
                                    int64_t mtime_ns,
                                    const int64_t *level_starts,
                                    int32_t level_count, int64_t row_start,
                                    int64_t row_bytes, int32_t rows,
                                    uint64_t verified, int32_t finest_level,
                                    int32_t block_size, int32_t table);
#ifdef _WIN32
AOFAST_API uint64_t aofast_register_w(const wchar_t *path, int64_t size,
                                      int64_t mtime_ns,
                                      const int64_t *level_starts,
                                      int32_t level_count, int64_t row_start,
                                      int64_t row_bytes, int32_t rows,
                                      uint64_t verified, int32_t finest_level,
                                      int32_t block_size, int32_t table);
#endif

/* Rows Python checked since: answered here from now on. */
AOFAST_API int32_t aofast_mark_verified(uint64_t handle, uint64_t rows);
/* Every later read of this handle goes to Python (the open switched to a
 * live tile). Never waits. */
AOFAST_API int32_t aofast_disable(uint64_t handle);
/* At release: no new reads, waits for reads in flight, closes the file.
 * The handle's pending samples are written to `out` (up to `cap`); returns
 * how many, or -1 if the handle was not registered. */
AOFAST_API int32_t aofast_unregister(uint64_t handle, aofast_sample *out,
                                     int32_t cap);
/* Pending wanted-level samples of every slot, reset as they are read. */
AOFAST_API int32_t aofast_drain(aofast_sample *out, int32_t cap);
AOFAST_API void aofast_get_stats(aofast_stats *out);

/* The FUSE read callback of mount `mount` (what aofast_entry returns). */
AOFAST_API int aofast_read_mount(int32_t mount, const char *path, char *buf,
                                 size_t size, int64_t offset, void *fi);

/* read_pattern.classify, for the tests that keep the two equal.
 * cursor[0] = last_end, cursor[1] = header_chunk_end (-1: none).
 * out[0] = header chunk (0/1), out[1] = wanted (-1: none), out[2] = first
 * needed. */
AOFAST_API void aofast_classify(const int64_t *level_starts,
                                int32_t level_count, int64_t *cursor,
                                int64_t offset, int64_t length, int32_t *out);

/* colour_table.apply, for the tests that keep the two equal: recolours the
 * whole blocks of `data` (bytes of a DDS from `file_offset`) in place. */
AOFAST_API void aofast_recolour(const uint16_t *table, char *data, int64_t file_offset,
                                int64_t length, int32_t block_size);

#endif /* AOFAST_H */
