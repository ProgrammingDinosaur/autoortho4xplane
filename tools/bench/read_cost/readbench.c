/* readbench: read textures the way X-Plane does and time every call.

   For each item of a list (path <TAB> start offset): open the file, read the
   header piece (64 KB at offset 0), then read from the start offset to the end
   of the file in 64 KB pieces, close. A start offset of -1 reads the header
   piece only. See autoortho/read_pattern.py for the pattern.

   Output, one line per call, to the CSV named on the command line:
     O,item,ns                 open
     R,item,offset,bytes,ns    read
     C,item,ns                 close
     T,item,ns,bytes,hash      the whole texture (FNV-1a of every byte read)
     P,wall_ns,user_ns,kernel_ns   this process

   Build: gcc -O2 -o readbench.exe readbench.c            (Windows, MinGW)
          gcc -O2 -pthread -o readbench readbench.c       (Linux, macOS)
   Windows reads with CreateFile and ReadFile, as X-Plane's reads arrive
   there; Linux and macOS with open, lseek and read. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
  #include <windows.h>
#else
  #include <errno.h>
  #include <fcntl.h>
  #include <pthread.h>
  #include <sys/resource.h>
  #include <sys/stat.h>
  #include <time.h>
  #include <unistd.h>
#endif

#define PIECE 65536
#define MAX_ITEMS 65536

typedef struct { char *path; int64_t start; } item_t;
typedef struct { char kind; int item; int64_t offset; uint32_t bytes; int64_t ns; uint64_t hash; } rec_t;
typedef struct { rec_t *recs; size_t n, cap; } log_t;

static item_t items[MAX_ITEMS];
static int n_items;
static int next_item;
static int failures;

/* ---- platform ---------------------------------------------------------- */

#ifdef _WIN32
static LARGE_INTEGER freq;
static int64_t now_ns(void) {
    LARGE_INTEGER c;
    QueryPerformanceCounter(&c);
    return (int64_t)((double)c.QuadPart * 1e9 / (double)freq.QuadPart);
}
typedef HANDLE file_t;
static int file_open(const char *path, file_t *out) {
    *out = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                       NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    return *out != INVALID_HANDLE_VALUE;
}
static int64_t file_size(file_t f) {
    LARGE_INTEGER size;
    size.QuadPart = 0;
    GetFileSizeEx(f, &size);
    return size.QuadPart;
}
static int file_seek(file_t f, int64_t pos) {
    LARGE_INTEGER li;
    li.QuadPart = pos;
    return SetFilePointerEx(f, li, NULL, FILE_BEGIN) != 0;
}
static int file_read(file_t f, unsigned char *buf, uint32_t want, uint32_t *got) {
    DWORD n = 0;
    BOOL ok = ReadFile(f, buf, want, &n, NULL);
    *got = n;
    return ok != 0;
}
static void file_close(file_t f) { CloseHandle(f); }
static unsigned long last_error(void) { return GetLastError(); }
static unsigned char *alloc_buffer(void) { return _aligned_malloc(PIECE, 4096); }
static void free_buffer(unsigned char *p) { _aligned_free(p); }
#else
static int64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}
typedef int file_t;
static int file_open(const char *path, file_t *out) {
    *out = open(path, O_RDONLY);
    return *out >= 0;
}
static int64_t file_size(file_t f) {
    struct stat st;
    return fstat(f, &st) == 0 ? (int64_t)st.st_size : 0;
}
static int file_seek(file_t f, int64_t pos) { return lseek(f, (off_t)pos, SEEK_SET) == (off_t)pos; }
static int file_read(file_t f, unsigned char *buf, uint32_t want, uint32_t *got) {
    ssize_t n;
    do {
        n = read(f, buf, want);
    } while (n < 0 && errno == EINTR);
    *got = n > 0 ? (uint32_t)n : 0;
    return n >= 0;
}
static void file_close(file_t f) { close(f); }
static unsigned long last_error(void) { return (unsigned long)errno; }
static unsigned char *alloc_buffer(void) {
    void *p = NULL;
    return posix_memalign(&p, 4096, PIECE) == 0 ? p : NULL;
}
static void free_buffer(unsigned char *p) { free(p); }
#endif

/* ---- the reads ---------------------------------------------------------- */

static void put(log_t *l, char kind, int item, int64_t offset, uint32_t bytes, int64_t ns, uint64_t hash) {
    if (l->n == l->cap) {
        l->cap = l->cap ? l->cap * 2 : 4096;
        l->recs = realloc(l->recs, l->cap * sizeof(rec_t));
    }
    rec_t *r = &l->recs[l->n++];
    r->kind = kind; r->item = item; r->offset = offset; r->bytes = bytes; r->ns = ns; r->hash = hash;
}

static uint64_t fnv(uint64_t h, const unsigned char *p, uint32_t n) {
    for (uint32_t i = 0; i < n; i++) { h ^= p[i]; h *= 1099511628211ULL; }
    return h;
}

static void run_worker(log_t *l) {
    unsigned char *buf = alloc_buffer();
    for (;;) {
        int i = __atomic_fetch_add(&next_item, 1, __ATOMIC_SEQ_CST);
        if (i >= n_items) break;
        uint64_t h = 1469598103934665603ULL;
        uint64_t total = 0;
        file_t f;
        int64_t t0 = now_ns();
        int opened = file_open(items[i].path, &f);
        int64_t t1 = now_ns();
        if (!opened) {
            fprintf(stderr, "open failed (%lu): %s\n", last_error(), items[i].path);
            __atomic_fetch_add(&failures, 1, __ATOMIC_SEQ_CST);
            continue;
        }
        put(l, 'O', i, 0, 0, t1 - t0, 0);
        /* X-Plane reads the sizes the header gives and never past the end. A
           mount may answer reads past the end with data (AutoOrtho's did while
           a texture was building), so stop at the size and not at a zero read. */
        int64_t fsize = file_size(f);
        uint32_t n = 0;
        int64_t a = now_ns();
        int ok = file_read(f, buf, PIECE, &n);
        int64_t b = now_ns();
        if (!ok) {
            fprintf(stderr, "header read failed (%lu): %s\n", last_error(), items[i].path);
            __atomic_fetch_add(&failures, 1, __ATOMIC_SEQ_CST);
        }
        put(l, 'R', i, 0, n, b - a, 0);
        h = fnv(h, buf, n);
        total += n;
        if (items[i].start >= 0) {
            int64_t pos = items[i].start < PIECE ? PIECE : items[i].start;
            file_seek(f, pos);
            while (pos < fsize) {
                a = now_ns();
                ok = file_read(f, buf, PIECE, &n);
                b = now_ns();
                if (!ok) {
                    fprintf(stderr, "read failed (%lu) at %lld: %s\n", last_error(), (long long)pos, items[i].path);
                    __atomic_fetch_add(&failures, 1, __ATOMIC_SEQ_CST);
                    break;
                }
                if (n == 0) break;
                put(l, 'R', i, pos, n, b - a, 0);
                h = fnv(h, buf, n);
                total += n;
                pos += n;
            }
        }
        a = now_ns();
        file_close(f);
        b = now_ns();
        put(l, 'C', i, 0, 0, b - a, 0);
        put(l, 'T', i, 0, (uint32_t)(total > 0xffffffffULL ? 0xffffffffULL : total), b - t0, h);
    }
    free_buffer(buf);
}

#ifdef _WIN32
static DWORD WINAPI worker(LPVOID arg) { run_worker((log_t *)arg); return 0; }
#else
static void *worker(void *arg) { run_worker((log_t *)arg); return NULL; }
#endif

int main(int argc, char **argv) {
    if (argc < 4) {
        fprintf(stderr, "usage: readbench <list> <threads> <out.csv>\n");
        return 2;
    }
    int threads = atoi(argv[2]);
    if (threads < 1) threads = 1;
    if (threads > 64) threads = 64;
    FILE *lf = fopen(argv[1], "r");
    if (!lf) { perror(argv[1]); return 2; }
    char line[4096];
    while (n_items < MAX_ITEMS && fgets(line, sizeof line, lf)) {
        char *tab = strchr(line, '\t');
        if (!tab) continue;
        *tab = 0;
        items[n_items].path = strdup(line);
        items[n_items].start = strtoll(tab + 1, NULL, 10);
        n_items++;
    }
    fclose(lf);
    static log_t logs[64];
    uint64_t user, kern;
#ifdef _WIN32
    QueryPerformanceFrequency(&freq);
    HANDLE th[64];
    int64_t w0 = now_ns();
    for (int t = 0; t < threads; t++) th[t] = CreateThread(NULL, 0, worker, &logs[t], 0, NULL);
    WaitForMultipleObjects(threads, th, TRUE, INFINITE);
    int64_t w1 = now_ns();
    FILETIME c, e, k, u;
    GetProcessTimes(GetCurrentProcess(), &c, &e, &k, &u);
    kern = (((uint64_t)k.dwHighDateTime << 32) | k.dwLowDateTime) * 100ULL;
    user = (((uint64_t)u.dwHighDateTime << 32) | u.dwLowDateTime) * 100ULL;
#else
    pthread_t th[64];
    int64_t w0 = now_ns();
    for (int t = 0; t < threads; t++) pthread_create(&th[t], NULL, worker, &logs[t]);
    for (int t = 0; t < threads; t++) pthread_join(th[t], NULL);
    int64_t w1 = now_ns();
    struct rusage ru;
    getrusage(RUSAGE_SELF, &ru);
    user = (uint64_t)ru.ru_utime.tv_sec * 1000000000ULL + (uint64_t)ru.ru_utime.tv_usec * 1000ULL;
    kern = (uint64_t)ru.ru_stime.tv_sec * 1000000000ULL + (uint64_t)ru.ru_stime.tv_usec * 1000ULL;
#endif
    FILE *out = fopen(argv[3], "w");
    if (!out) { perror(argv[3]); return 2; }
    for (int t = 0; t < threads; t++) {
        for (size_t i = 0; i < logs[t].n; i++) {
            rec_t *r = &logs[t].recs[i];
            switch (r->kind) {
            case 'O': fprintf(out, "O,%d,%lld\n", r->item, (long long)r->ns); break;
            case 'R': fprintf(out, "R,%d,%lld,%u,%lld\n", r->item, (long long)r->offset, r->bytes, (long long)r->ns); break;
            case 'C': fprintf(out, "C,%d,%lld\n", r->item, (long long)r->ns); break;
            case 'T': fprintf(out, "T,%d,%lld,%u,%016llx\n", r->item, (long long)r->ns, r->bytes, (unsigned long long)r->hash); break;
            }
        }
    }
    fprintf(out, "P,%lld,%llu,%llu\n", (long long)(w1 - w0), (unsigned long long)user, (unsigned long long)kern);
    fclose(out);
    return failures ? 1 : 0;
}
