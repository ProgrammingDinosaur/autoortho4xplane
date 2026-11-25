/**
 * aofast.c - Native reads of textures served from their cache file.
 *
 * See aofast.h and docs/developer/hybrid-read-path-plan.md (stage S2).
 *
 * Safety rules this file keeps:
 *  - Fixed arrays only; every offset is clipped to the slot's file size.
 *  - No pointer into Python memory is kept after a call returns.
 *  - A slot is used only while a reader holds a reference and the slot is
 *    ACTIVE with the handle's generation; a stale or released handle is
 *    handed to Python, never to another texture's file.
 *  - The reference is dropped before Python is called, so Python may
 *    disable or unregister the slot from inside that call.
 *  - Any doubt (row not checked, short read, error) hands the read to
 *    Python, which answers it as before. No error is returned to X-Plane.
 */
#include "aofast.h"

#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
  #ifndef WIN32_LEAN_AND_MEAN
    #define WIN32_LEAN_AND_MEAN
  #endif
  #include <windows.h>
#else
  #include <errno.h>
  #include <fcntl.h>
  #include <sched.h>
  #include <sys/stat.h>
  #include <sys/types.h>
  #include <unistd.h>
  #ifndef O_CLOEXEC
    #define O_CLOEXEC 0
  #endif
#endif

#define XPLANE_CHUNK 65536

enum { SLOT_FREE = 0, SLOT_ACTIVE = 1, SLOT_DISABLED = 2, SLOT_CLOSING = 3, SLOT_INIT = 4 };

typedef struct {
    int32_t state;        /* atomic: SLOT_* */
    int32_t refs;         /* atomic: reads using the file now */
    uint32_t generation;  /* atomic: changes when the slot is released */
    int32_t cursor_lock;  /* atomic spin lock for the cursor */
#ifdef _WIN32
    HANDLE file;
#else
    int fd;
#endif
    int64_t size;
    int32_t level_count;
    int64_t starts[AOFAST_MAX_LEVELS + 1];
    int64_t row_start;
    int64_t row_bytes;
    int32_t rows;
    uint64_t verified;    /* atomic: bit per checked mipmap-0 row */
    int32_t finest_level; /* > 0: a partial entry (see aofast_register) */
    int32_t block_size;   /* 8 (BC1) or 16 (BC3) */
    int32_t table;        /* season table entry, or -1 */
    unsigned char standin[16];  /* the coarsest stored block */
    int64_t last_end;     /* the read cursor (read_pattern.ReadCursor) */
    int64_t header_chunk_end;
    int32_t wanted[AOFAST_MAX_LEVELS];  /* atomic: samples not yet drained */
} slot_t;

static slot_t g_slots[AOFAST_SLOTS];
static aofast_read_fn g_python_read[AOFAST_MOUNTS];
static int32_t g_fh_offset = -1;
static int32_t g_enabled = 1;
static uint32_t g_next_slot = 0;

static int64_t g_reads, g_bytes, g_deferred, g_deferred_rows, g_deferred_io,
    g_deferred_closed, g_past_end, g_active, g_deferred_level;

/* Season colour tables: written only while no registered open uses them. */
static uint16_t *g_tables[AOFAST_TABLES];
static int32_t g_table_refs[AOFAST_TABLES];

#define LOAD(p) __atomic_load_n((p), __ATOMIC_SEQ_CST)
#define STORE(p, v) __atomic_store_n((p), (v), __ATOMIC_SEQ_CST)
#define ADD(p, v) __atomic_add_fetch((p), (v), __ATOMIC_SEQ_CST)
#define SUB(p, v) __atomic_sub_fetch((p), (v), __ATOMIC_SEQ_CST)
#define XCHG(p, v) __atomic_exchange_n((p), (v), __ATOMIC_SEQ_CST)

static int cas32(int32_t *p, int32_t expected, int32_t desired) {
    return __atomic_compare_exchange_n(p, &expected, desired, 0,
                                       __ATOMIC_SEQ_CST, __ATOMIC_SEQ_CST);
}

static void yield_cpu(void) {
#ifdef _WIN32
    SwitchToThread();
#else
    sched_yield();
#endif
}

static void cursor_lock(slot_t *s) {
    while (XCHG(&s->cursor_lock, 1)) {
        yield_cpu();
    }
}

static void cursor_unlock(slot_t *s) { STORE(&s->cursor_lock, 0); }

AOFAST_API const char *aofast_version(void) { return "aofast " AOFAST_VERSION; }

AOFAST_API void aofast_set_fh_offset(int32_t fh_offset) { STORE(&g_fh_offset, fh_offset); }

AOFAST_API void aofast_set_enabled(int32_t enabled) { STORE(&g_enabled, enabled ? 1 : 0); }

AOFAST_API int32_t aofast_attach(aofast_read_fn python_read) {
    if (python_read == NULL) return -1;
    for (int32_t k = 0; k < AOFAST_MOUNTS; k++) {
        aofast_read_fn expected = NULL;
        if (__atomic_compare_exchange_n(&g_python_read[k], &expected, python_read, 0,
                                        __ATOMIC_SEQ_CST, __ATOMIC_SEQ_CST)) {
            return k;
        }
    }
    return -1;
}

AOFAST_API void aofast_detach(int32_t mount) {
    if (mount >= 0 && mount < AOFAST_MOUNTS) STORE(&g_python_read[mount], NULL);
}

/* ------------------------------------------------------------------------
 * read_pattern.classify, line for line
 * ---------------------------------------------------------------------- */

static int32_t mip_at(const int64_t *starts, int32_t n, int64_t position) {
    for (int32_t i = 0; i < n; i++) {
        if (position < starts[i + 1]) return i;
    }
    return n - 1;
}

static int is_header_chunk(const int64_t *starts, int32_t n, int64_t offset, int64_t end) {
    if (n <= 0) return 0;
    int64_t mm0_start = starts[0];
    int64_t mm0_length = starts[1] - starts[0];
    return offset < mm0_start && end > mm0_start && mip_at(starts, n, end - 1) == 0 &&
           (end - mm0_start) * 2 < mm0_length;
}

/* cursor[0] = last_end, cursor[1] = header_chunk_end (-1: none). */
static void classify(const int64_t *starts, int32_t n, int64_t *last_end,
                     int64_t *header_chunk_end, int64_t offset, int64_t length,
                     int32_t *header_out, int32_t *wanted_out, int32_t *first_needed_out) {
    int64_t end = offset + length;
    *header_out = 0;
    *wanted_out = -1;
    *first_needed_out = 0;
    if (n <= 0 || length <= 0) return;
    int64_t mm0_start = starts[0];
    int64_t mm0_length = starts[1] - starts[0];
    if (end <= mm0_start) {
        *last_end = end;
        *first_needed_out = n;
        return;
    }
    int header = is_header_chunk(starts, n, offset, end) ||
                 (*header_chunk_end >= 0 && offset == *header_chunk_end &&
                  end <= XPLANE_CHUNK && mip_at(starts, n, end - 1) == 0 &&
                  (end - mm0_start) * 2 < mm0_length);
    int64_t data_start = offset > mm0_start ? offset : mm0_start;
    int32_t first = mip_at(starts, n, data_start);
    int32_t last = mip_at(starts, n, end - 1);
    /* Python's "(cursor.header_chunk_end or -1)": 0 and None read as -1. */
    int64_t hce = *header_chunk_end > 0 ? *header_chunk_end : -1;
    if (header) {
        *wanted_out = -1;
        *first_needed_out = last + 1;
        *header_chunk_end = end;
    } else if (data_start == starts[first]) {
        *wanted_out = first;
        *first_needed_out = first;
    } else if (first == 0 && data_start == hce) {
        *wanted_out = 0;
        *first_needed_out = 0;
    } else if (data_start == *last_end) {
        *wanted_out = -1;
        *first_needed_out = first;
    } else if (last > first) {
        *wanted_out = first + 1;
        *first_needed_out = first + 1;
    } else {
        *wanted_out = -1;
        *first_needed_out = first;
    }
    *header_out = header ? 1 : 0;
    *last_end = end;
}

AOFAST_API void aofast_classify(const int64_t *level_starts, int32_t level_count,
                                int64_t *cursor, int64_t offset, int64_t length,
                                int32_t *out) {
    if (level_count > AOFAST_MAX_LEVELS) level_count = AOFAST_MAX_LEVELS;
    classify(level_starts, level_count, &cursor[0], &cursor[1], offset, length,
             &out[0], &out[1], &out[2]);
}

/* ------------------------------------------------------------------------
 * Files
 * ---------------------------------------------------------------------- */

#ifdef _WIN32
static int open_checked_w(const wchar_t *path, int64_t size, int64_t mtime_ns, HANDLE *out) {
    HANDLE h = CreateFileW(path, GENERIC_READ,
                           FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) return 0;
    LARGE_INTEGER actual;
    FILETIME written;
    if (!GetFileSizeEx(h, &actual) || actual.QuadPart != size ||
        !GetFileTime(h, NULL, NULL, &written)) {
        CloseHandle(h);
        return 0;
    }
    ULARGE_INTEGER ft;
    ft.LowPart = written.dwLowDateTime;
    ft.HighPart = written.dwHighDateTime;
    int64_t ns = ((int64_t)ft.QuadPart - 116444736000000000LL) * 100;
    if (ns != mtime_ns) {
        CloseHandle(h);
        return 0;
    }
    *out = h;
    return 1;
}

static int read_exact(slot_t *s, char *buf, int64_t length, int64_t offset) {
    int64_t total = 0;
    while (total < length) {
        OVERLAPPED ov;
        ZeroMemory(&ov, sizeof ov);
        int64_t at = offset + total;
        ov.Offset = (DWORD)(at & 0xffffffff);
        ov.OffsetHigh = (DWORD)((uint64_t)at >> 32);
        DWORD want = (DWORD)((length - total) > 0x40000000 ? 0x40000000 : (length - total));
        DWORD got = 0;
        if (!ReadFile(s->file, buf + total, want, &got, &ov)) return 0;
        if (got == 0) return 0;
        total += got;
    }
    return 1;
}

static void close_file(slot_t *s) {
    if (s->file != NULL && s->file != INVALID_HANDLE_VALUE) CloseHandle(s->file);
    s->file = NULL;
}
#else
static int open_checked(const char *path, int64_t size, int64_t mtime_ns, int *out) {
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return 0;
    struct stat st;
    if (fstat(fd, &st) != 0 || (int64_t)st.st_size != size) {
        close(fd);
        return 0;
    }
  #ifdef __APPLE__
    int64_t ns = (int64_t)st.st_mtimespec.tv_sec * 1000000000LL + st.st_mtimespec.tv_nsec;
  #else
    int64_t ns = (int64_t)st.st_mtim.tv_sec * 1000000000LL + st.st_mtim.tv_nsec;
  #endif
    if (ns != mtime_ns) {
        close(fd);
        return 0;
    }
    *out = fd;
    return 1;
}

static int read_exact(slot_t *s, char *buf, int64_t length, int64_t offset) {
    int64_t total = 0;
    while (total < length) {
        ssize_t got = pread(s->fd, buf + total, (size_t)(length - total), (off_t)(offset + total));
        if (got < 0) {
            if (errno == EINTR) continue;
            return 0;
        }
        if (got == 0) return 0;
        total += got;
    }
    return 1;
}

static void close_file(slot_t *s) {
    if (s->fd >= 0) close(s->fd);
    s->fd = -1;
}
#endif

/* ------------------------------------------------------------------------
 * Slots
 * ---------------------------------------------------------------------- */

static int layout_ok(int64_t size, const int64_t *starts, int32_t n, int64_t row_start,
                     int64_t row_bytes, int32_t rows) {
    if (n <= 0 || n > AOFAST_MAX_LEVELS || size <= 0) return 0;
    if (rows <= 0 || rows > AOFAST_MAX_ROWS || row_bytes <= 0) return 0;
    for (int32_t i = 0; i < n; i++) {
        if (starts[i] < 0 || starts[i + 1] <= starts[i]) return 0;
    }
    if (starts[n] != size) return 0;
    if (row_start != starts[0] || row_start + row_bytes * rows != starts[1]) return 0;
    return 1;
}

static int32_t claim_slot(void) {
    uint32_t hint = ADD(&g_next_slot, 1);
    for (uint32_t i = 0; i < AOFAST_SLOTS; i++) {
        int32_t idx = (int32_t)((hint + i) % AOFAST_SLOTS);
        if (cas32(&g_slots[idx].state, SLOT_FREE, SLOT_INIT)) return idx;
    }
    return -1;
}

static int read_exact(slot_t *s, char *buf, int64_t length, int64_t offset);

static int partial_ok(int32_t n, int32_t finest_level, int32_t block_size, int32_t table) {
    if (finest_level < 0 || finest_level >= n) return 0;
    if (block_size != 8 && block_size != 16) return 0;
    if (table < -1 || table >= AOFAST_TABLES) return 0;
    if (table >= 0 && LOAD(&g_tables[table]) == NULL) return 0;
    return 1;
}

static uint64_t publish(int32_t idx, int64_t size, const int64_t *starts, int32_t n,
                        int64_t row_start, int64_t row_bytes, int32_t rows, uint64_t verified,
                        int32_t finest_level, int32_t block_size, int32_t table) {
    slot_t *s = &g_slots[idx];
    s->finest_level = finest_level;
    s->block_size = block_size;
    memset(s->standin, 0, sizeof s->standin);
    if (finest_level > 0) {
        /* DiskTexture._standin: the coarsest level's first block. */
        int64_t coarsest = starts[n - 1];
        int64_t length = starts[n] - coarsest;
        if (length > block_size) length = block_size;
        if (!read_exact(s, (char *)s->standin, length, coarsest)) return 0;
    }
    s->table = table;
    if (table >= 0) ADD(&g_table_refs[table], 1);
    s->size = size;
    s->level_count = n;
    memcpy(s->starts, starts, sizeof(int64_t) * (size_t)(n + 1));
    s->row_start = row_start;
    s->row_bytes = row_bytes;
    s->rows = rows;
    STORE(&s->verified, verified);
    s->last_end = -1;
    s->header_chunk_end = -1;
    STORE(&s->cursor_lock, 0);
    for (int32_t i = 0; i < AOFAST_MAX_LEVELS; i++) STORE(&s->wanted[i], 0);
    uint32_t gen = LOAD(&s->generation);
    ADD(&g_active, 1);
    STORE(&s->state, SLOT_ACTIVE);
    return AOFAST_TAG | ((uint64_t)gen << 16) | (uint64_t)idx;
}

#ifdef _WIN32
AOFAST_API uint64_t aofast_register_w(const wchar_t *path, int64_t size, int64_t mtime_ns,
                                      const int64_t *level_starts, int32_t level_count,
                                      int64_t row_start, int64_t row_bytes, int32_t rows,
                                      uint64_t verified, int32_t finest_level,
                                      int32_t block_size, int32_t table) {
    if (path == NULL || level_starts == NULL) return 0;
    if (!layout_ok(size, level_starts, level_count, row_start, row_bytes, rows)) return 0;
    if (!partial_ok(level_count, finest_level, block_size, table)) return 0;
    HANDLE h;
    if (!open_checked_w(path, size, mtime_ns, &h)) return 0;
    int32_t idx = claim_slot();
    if (idx < 0) {
        CloseHandle(h);
        return 0;
    }
    g_slots[idx].file = h;
    uint64_t handle = publish(idx, size, level_starts, level_count, row_start, row_bytes, rows,
                              verified, finest_level, block_size, table);
    if (handle == 0) {
        close_file(&g_slots[idx]);
        STORE(&g_slots[idx].state, SLOT_FREE);
    }
    return handle;
}

AOFAST_API uint64_t aofast_register(const char *path, int64_t size, int64_t mtime_ns,
                                    const int64_t *level_starts, int32_t level_count,
                                    int64_t row_start, int64_t row_bytes, int32_t rows,
                                    uint64_t verified, int32_t finest_level,
                                    int32_t block_size, int32_t table) {
    if (path == NULL) return 0;
    int chars = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, -1, NULL, 0);
    if (chars <= 0 || chars > 32768) return 0;
    wchar_t *wide = (wchar_t *)HeapAlloc(GetProcessHeap(), 0, sizeof(wchar_t) * (size_t)chars);
    if (wide == NULL) return 0;
    uint64_t handle = 0;
    if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, -1, wide, chars) > 0) {
        handle = aofast_register_w(wide, size, mtime_ns, level_starts, level_count, row_start,
                                   row_bytes, rows, verified, finest_level, block_size, table);
    }
    HeapFree(GetProcessHeap(), 0, wide);
    return handle;
}
#else
AOFAST_API uint64_t aofast_register(const char *path, int64_t size, int64_t mtime_ns,
                                    const int64_t *level_starts, int32_t level_count,
                                    int64_t row_start, int64_t row_bytes, int32_t rows,
                                    uint64_t verified, int32_t finest_level,
                                    int32_t block_size, int32_t table) {
    if (path == NULL || level_starts == NULL) return 0;
    if (!layout_ok(size, level_starts, level_count, row_start, row_bytes, rows)) return 0;
    if (!partial_ok(level_count, finest_level, block_size, table)) return 0;
    int fd;
    if (!open_checked(path, size, mtime_ns, &fd)) return 0;
    int32_t idx = claim_slot();
    if (idx < 0) {
        close(fd);
        return 0;
    }
    g_slots[idx].fd = fd;
    uint64_t handle = publish(idx, size, level_starts, level_count, row_start, row_bytes, rows,
                              verified, finest_level, block_size, table);
    if (handle == 0) {
        close_file(&g_slots[idx]);
        STORE(&g_slots[idx].state, SLOT_FREE);
    }
    return handle;
}
#endif

/* The slot of a handle, or NULL. Does not take a reference. */
static slot_t *slot_of(uint64_t handle, uint32_t *gen_out) {
    if (!(handle & AOFAST_TAG)) return NULL;
    uint64_t idx = handle & 0xffff;
    if (idx >= AOFAST_SLOTS) return NULL;
    *gen_out = (uint32_t)((handle >> 16) & 0xffffffffu);
    return &g_slots[idx];
}

AOFAST_API int32_t aofast_mark_verified(uint64_t handle, uint64_t rows) {
    uint32_t gen;
    slot_t *s = slot_of(handle, &gen);
    if (s == NULL) return 0;
    ADD(&s->refs, 1);
    int32_t ok = 0;
    int32_t state = LOAD(&s->state);
    if ((state == SLOT_ACTIVE || state == SLOT_DISABLED) && LOAD(&s->generation) == gen) {
        __atomic_fetch_or(&s->verified, rows, __ATOMIC_SEQ_CST);
        ok = 1;
    }
    SUB(&s->refs, 1);
    return ok;
}

AOFAST_API int32_t aofast_disable(uint64_t handle) {
    uint32_t gen;
    slot_t *s = slot_of(handle, &gen);
    if (s == NULL) return 0;
    ADD(&s->refs, 1);
    int32_t ok = 0;
    if (LOAD(&s->generation) == gen) ok = cas32(&s->state, SLOT_ACTIVE, SLOT_DISABLED);
    SUB(&s->refs, 1);
    return ok;
}

static int32_t drain_slot(slot_t *s, int32_t idx, uint32_t gen, aofast_sample *out, int32_t cap,
                          int32_t count) {
    for (int32_t level = 0; level < AOFAST_MAX_LEVELS && count < cap; level++) {
        int32_t n = XCHG(&s->wanted[level], 0);
        if (n > 0) {
            out[count].slot = idx;
            out[count].generation = gen;
            out[count].level = level;
            out[count].count = n;
            count++;
        }
    }
    return count;
}

AOFAST_API int32_t aofast_unregister(uint64_t handle, aofast_sample *out, int32_t cap) {
    uint32_t gen;
    slot_t *s = slot_of(handle, &gen);
    if (s == NULL || LOAD(&s->generation) != gen) return -1;
    if (!cas32(&s->state, SLOT_ACTIVE, SLOT_CLOSING) &&
        !cas32(&s->state, SLOT_DISABLED, SLOT_CLOSING)) {
        return -1;
    }
    /* Reads that took a reference before CLOSING finish with the file. */
    while (LOAD(&s->refs) != 0) yield_cpu();
    int32_t count = 0;
    if (out != NULL && cap > 0) {
        count = drain_slot(s, (int32_t)(handle & 0xffff), gen, out, cap, 0);
    }
    close_file(s);
    if (s->table >= 0) SUB(&g_table_refs[s->table], 1);
    s->table = -1;
    ADD(&s->generation, 1);
    SUB(&g_active, 1);
    STORE(&s->state, SLOT_FREE);
    return count;
}

AOFAST_API int32_t aofast_drain(aofast_sample *out, int32_t cap) {
    int32_t count = 0;
    if (out == NULL) return 0;
    for (int32_t idx = 0; idx < AOFAST_SLOTS && count < cap; idx++) {
        slot_t *s = &g_slots[idx];
        int32_t state = LOAD(&s->state);
        if (state != SLOT_ACTIVE && state != SLOT_DISABLED) continue;
        ADD(&s->refs, 1);
        state = LOAD(&s->state);
        if (state == SLOT_ACTIVE || state == SLOT_DISABLED) {
            count = drain_slot(s, idx, LOAD(&s->generation), out, cap, count);
        }
        SUB(&s->refs, 1);
    }
    return count;
}

AOFAST_API void aofast_get_stats(aofast_stats *out) {
    if (out == NULL) return;
    out->reads = LOAD(&g_reads);
    out->bytes = LOAD(&g_bytes);
    out->deferred = LOAD(&g_deferred);
    out->deferred_rows = LOAD(&g_deferred_rows);
    out->deferred_io = LOAD(&g_deferred_io);
    out->deferred_closed = LOAD(&g_deferred_closed);
    out->past_end = LOAD(&g_past_end);
    out->active_slots = LOAD(&g_active);
    out->deferred_level = LOAD(&g_deferred_level);
}

AOFAST_API int32_t aofast_table_set(int32_t id, const uint16_t *table) {
    if (id < 0 || id >= AOFAST_TABLES || table == NULL) return 0;
    if (LOAD(&g_table_refs[id]) != 0) return 0;
    uint16_t *data = LOAD(&g_tables[id]);
    if (data == NULL) {
        data = (uint16_t *)malloc(sizeof(uint16_t) * 65536);
        if (data == NULL) return 0;
    }
    memcpy(data, table, sizeof(uint16_t) * 65536);
    STORE(&g_tables[id], data);
    return 1;
}

/* colour_table.apply, line for line: whole blocks only; BC1 blocks keep
 * their opaque four-colour mode (swap colours and indices when the table
 * turns the order around), three-colour BC1 blocks are left as made. */
static void recolour(const uint16_t *t, unsigned char *buf, int64_t file_offset, int64_t length,
                     int32_t bs) {
    int64_t start = (file_offset > 128 ? file_offset : 128) - 128;
    int64_t first = 128 + ((start + bs - 1) / bs) * bs;
    int64_t end = file_offset + length;
    int64_t count = (end - first) / bs;
    if (count <= 0) return;
    unsigned char *p = buf + (first - file_offset);
    for (int64_t i = 0; i < count; i++, p += bs) {
        unsigned char *c = bs == 16 ? p + 8 : p;
        uint16_t c0 = (uint16_t)(c[0] | (c[1] << 8));
        uint16_t c1 = (uint16_t)(c[2] | (c[3] << 8));
        uint16_t n0 = t[c0];
        uint16_t n1 = t[c1];
        if (bs == 8) {
            if (c0 > c1) {
                if (n0 < n1) {
                    uint16_t tmp = n0;
                    n0 = n1;
                    n1 = tmp;
                    c[4] ^= 0x55; c[5] ^= 0x55; c[6] ^= 0x55; c[7] ^= 0x55;
                } else if (n0 == n1) {
                    c[4] = c[5] = c[6] = c[7] = 0;
                    if (n0 > 0) n1 = (uint16_t)(n0 - 1);
                }
            } else {
                n0 = c0;
                n1 = c1;
            }
        }
        c[0] = (unsigned char)(n0 & 0xff);
        c[1] = (unsigned char)(n0 >> 8);
        c[2] = (unsigned char)(n1 & 0xff);
        c[3] = (unsigned char)(n1 >> 8);
    }
}

AOFAST_API void aofast_recolour(const uint16_t *table, char *data, int64_t file_offset,
                                int64_t length, int32_t block_size) {
    if (table == NULL || data == NULL || length <= 0) return;
    if (block_size != 8 && block_size != 16) return;
    recolour(table, (unsigned char *)data, file_offset, length, block_size);
}

/* ------------------------------------------------------------------------
 * The read
 * ---------------------------------------------------------------------- */

static int to_python(aofast_read_fn python_read, const char *path, char *buf, size_t size,
                     int64_t offset, void *fi, int64_t *reason) {
    if (reason != NULL) {
        ADD(&g_deferred, 1);
        ADD(reason, 1);
    }
    return python_read(path, buf, size, offset, fi);
}

static int rows_checked(slot_t *s, int64_t offset, int64_t end) {
    int64_t mm0_end = s->row_start + s->row_bytes * s->rows;
    if (end <= s->row_start || offset >= mm0_end) return 1;
    int64_t first = offset > s->row_start ? (offset - s->row_start) / s->row_bytes : 0;
    int64_t last_pos = (end < mm0_end ? end : mm0_end) - 1;
    int64_t last = (last_pos - s->row_start) / s->row_bytes;
    if (last >= s->rows) last = s->rows - 1;
    uint64_t mask = 0;
    for (int64_t row = first; row <= last; row++) mask |= (uint64_t)1 << row;
    return (LOAD(&s->verified) & mask) == mask;
}

AOFAST_API int aofast_read_mount(int32_t mount, const char *path, char *buf, size_t size,
                                 int64_t offset, void *fi) {
    if (mount < 0 || mount >= AOFAST_MOUNTS) return 0;
    aofast_read_fn python_read = LOAD(&g_python_read[mount]);
    if (python_read == NULL) return 0;  /* never installed without it */
    int32_t fh_offset = LOAD(&g_fh_offset);
    if (fi == NULL || fh_offset < 0 || !LOAD(&g_enabled)) {
        return to_python(python_read, path, buf, size, offset, fi, NULL);
    }
    uint64_t fh;
    memcpy(&fh, (const char *)fi + fh_offset, sizeof fh);
    uint32_t gen;
    slot_t *s = slot_of(fh, &gen);
    if (s == NULL) return to_python(python_read, path, buf, size, offset, fi, NULL);

    ADD(&s->refs, 1);
    if (LOAD(&s->state) != SLOT_ACTIVE || LOAD(&s->generation) != gen) {
        SUB(&s->refs, 1);
        return to_python(python_read, path, buf, size, offset, fi, &g_deferred_closed);
    }
    if (offset < 0 || buf == NULL) {
        SUB(&s->refs, 1);
        return to_python(python_read, path, buf, size, offset, fi, &g_deferred_closed);
    }
    if (offset >= s->size || size == 0) {
        SUB(&s->refs, 1);
        ADD(&g_past_end, 1);
        return 0;
    }
    int64_t length = (int64_t)size;
    if (length > s->size - offset) length = s->size - offset;
    if (length > 0x7fffffff) length = 0x7fffffff;
    int64_t end = offset + length;

    /* Every read of this open is classified here, also those handed to
     * Python, so the cursor sees them all (Python skips its own). */
    int32_t header, wanted, first_needed;
    cursor_lock(s);
    classify(s->starts, s->level_count, &s->last_end, &s->header_chunk_end, offset, length,
             &header, &wanted, &first_needed);
    cursor_unlock(s);
    if (wanted >= 0 && wanted < AOFAST_MAX_LEVELS) ADD(&s->wanted[wanted], 1);

    int ok;
    if (s->finest_level > 0) {
        /* DiskTexture._read_partial: the header from the file, the bytes of
         * levels the file does not hold as stand-in blocks, the rest from
         * the file. A read that needs such a level goes to Python, which
         * serves the open from a live tile. */
        int64_t boundary = s->starts[s->finest_level];
        int64_t needed_start = first_needed < s->level_count ? s->starts[first_needed] : end;
        if (needed_start < offset) needed_start = offset;
        if (needed_start < (end < boundary ? end : boundary)) {
            SUB(&s->refs, 1);
            return to_python(python_read, path, buf, size, offset, fi, &g_deferred_level);
        }
        ok = 1;
        if (offset < 128) {
            int64_t header_end = end < 128 ? end : 128;
            ok = read_exact(s, buf, header_end - offset, offset);
        }
        int64_t standin_start = offset > 128 ? offset : 128;
        int64_t standin_end = end < boundary ? end : boundary;
        if (ok && standin_start < standin_end) {
            int64_t bs = s->block_size;
            int64_t phase = (standin_start - 128) % bs;
            char *dst = buf + (standin_start - offset);
            int64_t count = standin_end - standin_start;
            int64_t filled = count < bs ? count : bs;
            for (int64_t i = 0; i < filled; i++) dst[i] = (char)s->standin[(phase + i) % bs];
            /* The pattern repeats every block: copy what is filled onto the
             * rest, doubling (filled stays a whole number of blocks). */
            while (filled < count) {
                int64_t chunk = filled < count - filled ? filled : count - filled;
                memcpy(dst + filled, dst, (size_t)chunk);
                filled += chunk;
            }
        }
        int64_t file_start = offset > boundary ? offset : boundary;
        if (ok && file_start < end) {
            ok = read_exact(s, buf + (file_start - offset), end - file_start, file_start);
        }
    } else {
        if (!rows_checked(s, offset, end)) {
            SUB(&s->refs, 1);
            return to_python(python_read, path, buf, size, offset, fi, &g_deferred_rows);
        }
        ok = read_exact(s, buf, length, offset);
    }
    if (ok && s->table >= 0) {
        recolour(g_tables[s->table], (unsigned char *)buf, offset, length, s->block_size);
    }
    SUB(&s->refs, 1);
    if (!ok) return to_python(python_read, path, buf, size, offset, fi, &g_deferred_io);
    ADD(&g_reads, 1);
    ADD(&g_bytes, length);
    return (int)length;
}

/* One entry point per mount: FUSE does not say which mount a read is for. */
#define AOFAST_ENTRY(k)                                                                   \
    static int aofast_entry_##k(const char *path, char *buf, size_t size, int64_t offset, \
                                void *fi) {                                               \
        return aofast_read_mount(k, path, buf, size, offset, fi);                         \
    }
AOFAST_ENTRY(0) AOFAST_ENTRY(1) AOFAST_ENTRY(2) AOFAST_ENTRY(3)
AOFAST_ENTRY(4) AOFAST_ENTRY(5) AOFAST_ENTRY(6) AOFAST_ENTRY(7)
AOFAST_ENTRY(8) AOFAST_ENTRY(9) AOFAST_ENTRY(10) AOFAST_ENTRY(11)
AOFAST_ENTRY(12) AOFAST_ENTRY(13) AOFAST_ENTRY(14) AOFAST_ENTRY(15)
AOFAST_ENTRY(16) AOFAST_ENTRY(17) AOFAST_ENTRY(18) AOFAST_ENTRY(19)
AOFAST_ENTRY(20) AOFAST_ENTRY(21) AOFAST_ENTRY(22) AOFAST_ENTRY(23)
AOFAST_ENTRY(24) AOFAST_ENTRY(25) AOFAST_ENTRY(26) AOFAST_ENTRY(27)
AOFAST_ENTRY(28) AOFAST_ENTRY(29) AOFAST_ENTRY(30) AOFAST_ENTRY(31)

static const aofast_read_fn g_entries[AOFAST_MOUNTS] = {
    aofast_entry_0,  aofast_entry_1,  aofast_entry_2,  aofast_entry_3,  aofast_entry_4,
    aofast_entry_5,  aofast_entry_6,  aofast_entry_7,  aofast_entry_8,  aofast_entry_9,
    aofast_entry_10, aofast_entry_11, aofast_entry_12, aofast_entry_13, aofast_entry_14,
    aofast_entry_15, aofast_entry_16, aofast_entry_17, aofast_entry_18, aofast_entry_19,
    aofast_entry_20, aofast_entry_21, aofast_entry_22, aofast_entry_23, aofast_entry_24,
    aofast_entry_25, aofast_entry_26, aofast_entry_27, aofast_entry_28, aofast_entry_29,
    aofast_entry_30, aofast_entry_31,
};

AOFAST_API aofast_read_fn aofast_entry(int32_t mount) {
    if (mount < 0 || mount >= AOFAST_MOUNTS) return NULL;
    return g_entries[mount];
}
