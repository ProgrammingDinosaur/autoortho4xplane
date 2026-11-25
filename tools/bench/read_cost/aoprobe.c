/* aoprobe: a FUSE read callback in C, for the read-cost benchmark.

   It answers a read from the file handle the Python open stored in
   fuse_file_info.fh (a Win32 handle on Windows, a file descriptor on Linux
   and macOS), without entering Python. The offset of fh inside
   fuse_file_info is given at start (read from mfusepy's ctypes struct), so
   no FUSE header is needed. This is the mechanism the hybrid read path plan
   proposes for ready textures, reduced to its core (the real one is
   autoortho/aopipeline/aofast.c).

   Build: gcc -O2 -shared -static-libgcc -o aoprobe.dll aoprobe.c   (Windows)
          gcc -O2 -shared -fPIC -o aoprobe.so aoprobe.c             (Linux)
          clang -O2 -dynamiclib -o aoprobe.dylib aoprobe.c          (macOS) */
#include <stdint.h>
#include <stddef.h>

#ifdef _WIN32
  #include <windows.h>
  #define EXPORT __declspec(dllexport)
#else
  #include <errno.h>
  #include <unistd.h>
  #define EXPORT __attribute__((visibility("default")))
#endif

static int g_fh_offset;
static int64_t g_reads;
static int64_t g_bytes;

EXPORT void aoprobe_init(int fh_offset) { g_fh_offset = fh_offset; }
EXPORT int64_t aoprobe_reads(void) { return __atomic_load_n(&g_reads, __ATOMIC_SEQ_CST); }
EXPORT int64_t aoprobe_bytes(void) { return __atomic_load_n(&g_bytes, __ATOMIC_SEQ_CST); }

EXPORT int aoprobe_read(const char *path, char *buf, size_t size, int64_t offset, void *fi) {
    (void)path;
    uint64_t fh = *(uint64_t *)((char *)fi + g_fh_offset);
    size_t total = 0;
    while (total < size) {
#ifdef _WIN32
        HANDLE h = (HANDLE)(uintptr_t)fh;
        OVERLAPPED ov;
        ZeroMemory(&ov, sizeof ov);
        ov.Offset = (DWORD)(offset & 0xffffffff);
        ov.OffsetHigh = (DWORD)(offset >> 32);
        DWORD want = (DWORD)(size - total > 0x40000000 ? 0x40000000 : size - total);
        DWORD n = 0;
        if (!ReadFile(h, buf + total, want, &n, &ov)) {
            if (GetLastError() == ERROR_HANDLE_EOF) break;
            return -5; /* EIO */
        }
#else
        ssize_t n = pread((int)fh, buf + total, size - total, (off_t)offset);
        if (n < 0) {
            if (errno == EINTR) continue;
            return -EIO;
        }
#endif
        if (n == 0) break;
        total += (size_t)n;
        offset += (int64_t)n;
    }
    __atomic_fetch_add(&g_reads, 1, __ATOMIC_SEQ_CST);
    __atomic_fetch_add(&g_bytes, (int64_t)total, __ATOMIC_SEQ_CST);
    return (int)total;
}
