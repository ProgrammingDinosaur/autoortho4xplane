/*
 * AutoOrtho X-Plane plugin (files mode).
 *
 * 1. At a flight start it holds X-Plane's loading screen until AutoOrtho has
 *    built the textures around the start (the "gate", UDP on 127.0.0.1).
 * 2. When X-Plane starts without AutoOrtho running, it removes AutoOrtho's
 *    stale files-mode links from Custom Scenery before X-Plane scans it
 *    (only links that resolve under AutoOrtho's files folder; never folders).
 *
 * Protocol: docs in autoortho/files_mode/gate.py. Facts used (measured by
 * OrthoStudio XP Live's hold test, reimplemented here): X-Plane sends
 * PLANE_LOADED and LIVERY_LOADED before it reads any scenery at a first
 * flight; it sends the undocumented message 115 (param 1) at every new
 * flight, also one started from within a flight, again before any scenery.
 * A plugin that does not return from that message holds the whole load.
 *
 * XPLM functions are looked up at run time in the XPLM library X-Plane has
 * already loaded, so the plugin builds without the SDK. Licensed Apache-2.0
 * with AutoOrtho.
 */

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#ifdef _WIN32
#  define WIN32_LEAN_AND_MEAN
#  include <winsock2.h>
#  include <ws2tcpip.h>
#  include <windows.h>
#  include <winioctl.h>
#  define PLUGIN_API __declspec(dllexport)
typedef SOCKET ao_socket;
#  define AO_BAD_SOCKET INVALID_SOCKET
#  define ao_close closesocket
#  ifndef SIO_UDP_CONNRESET
#    define SIO_UDP_CONNRESET _WSAIOW(IOC_VENDOR, 12)
#  endif
#else
#  include <dlfcn.h>
#  include <unistd.h>
#  include <dirent.h>
#  include <fcntl.h>
#  include <sys/types.h>
#  include <sys/stat.h>
#  include <sys/socket.h>
#  include <sys/time.h>
#  include <netinet/in.h>
#  include <arpa/inet.h>
#  include <limits.h>
#  define PLUGIN_API __attribute__((visibility("default")))
typedef int ao_socket;
#  define AO_BAD_SOCKET (-1)
#  define ao_close close
#endif

#define AO_VERSION "1.0.0"
#define MSG_PLANE_LOADED 102
#define MSG_AIRPORT_LOADED 103
#define MSG_LIVERY_LOADED 108
#define MSG_NEW_FLIGHT 115
#define HOLD_STEP_MS 250
#define FIRST_REPLY_MS 1000
#define SILENT_MS 5000

typedef void (*fn_debug)(const char *);
typedef void *(*fn_find_dataref)(const char *);
typedef double (*fn_get_datad)(void *);
typedef float (*fn_get_dataf)(void *);
typedef int (*fn_get_my_id)(void);
typedef void (*fn_get_plugin_info)(int, char *, char *, char *, char *);
typedef void (*fn_get_version)(int *, int *, int *);
typedef float (*fn_flight_loop)(float, float, int, void *);
typedef void (*fn_register_loop)(fn_flight_loop, float, void *);
typedef void (*fn_unregister_loop)(fn_flight_loop, void *);

static fn_debug x_debug;
static fn_find_dataref x_find;
static fn_get_datad x_getd;
static fn_get_dataf x_getf;
static fn_get_my_id x_my_id;
static fn_get_plugin_info x_info;
static fn_get_version x_version;
static fn_register_loop x_register_loop;
static fn_unregister_loop x_unregister_loop;

static void *dr_lat, *dr_lon, *dr_frame;
static char g_plugin_root[1024];
static int g_flight_loaded = 0;
static int g_seq = 0;
static int g_xp_version = 0;

/* ------------------------------------------------------------------ */

static void *xplm_symbol(const char *name)
{
#ifdef _WIN32
    HMODULE mod = GetModuleHandleA("XPLM_64.dll");
    if (!mod) return NULL;
    return (void *)GetProcAddress(mod, name);
#else
    return dlsym(RTLD_DEFAULT, name);
#endif
}

static void say(const char *fmt, ...)
{
    char buf[600];
    va_list ap;
    int n = snprintf(buf, sizeof(buf), "AutoOrtho: ");
    va_start(ap, fmt);
    vsnprintf(buf + n, sizeof(buf) - n - 2, fmt, ap);
    va_end(ap);
    strncat(buf, "\n", sizeof(buf) - strlen(buf) - 1);
    if (x_debug) x_debug(buf);
}

static uint64_t now_ms(void)
{
#ifdef _WIN32
    return (uint64_t)GetTickCount64();
#else
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (uint64_t)tv.tv_sec * 1000u + (uint64_t)(tv.tv_usec / 1000);
#endif
}

/* ------------------------------------------------------------------ */
/* Config: <plugin root>/autoortho_gate.txt, key=value lines.          */

typedef struct {
    int port;
    char files_root[1024];
    char custom_scenery[1024];
} ao_config;

static void trim(char *s)
{
    size_t n = strlen(s);
    while (n && (s[n - 1] == '\n' || s[n - 1] == '\r' || s[n - 1] == ' ')) s[--n] = 0;
}

static int read_config(ao_config *cfg)
{
    char path[1100];
    char line[1100];
    FILE *f;
    memset(cfg, 0, sizeof(*cfg));
    cfg->port = 49317;
    snprintf(path, sizeof(path), "%s%cautoortho_gate.txt", g_plugin_root,
#ifdef _WIN32
             '\\'
#else
             '/'
#endif
    );
    f = fopen(path, "r");
    if (!f) return 0;
    while (fgets(line, sizeof(line), f)) {
        trim(line);
        if (strncmp(line, "port=", 5) == 0) {
            int p = atoi(line + 5);
            if (p > 0 && p < 65536) cfg->port = p;
        } else if (strncmp(line, "files_root=", 11) == 0) {
            strncpy(cfg->files_root, line + 11, sizeof(cfg->files_root) - 1);
        } else if (strncmp(line, "custom_scenery=", 15) == 0) {
            strncpy(cfg->custom_scenery, line + 15, sizeof(cfg->custom_scenery) - 1);
        }
    }
    fclose(f);
    return 1;
}

/* ------------------------------------------------------------------ */
/* UDP                                                                  */

static int g_wsa = 0;

static ao_socket open_socket(int port, struct sockaddr_in *to)
{
    ao_socket s;
#ifdef _WIN32
    if (!g_wsa) {
        WSADATA wsa;
        if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) return AO_BAD_SOCKET;
        g_wsa = 1;
    }
#endif
    s = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (s == AO_BAD_SOCKET) return s;
#ifdef _WIN32
    {
        /* Without this, an ICMP "port unreachable" makes the next recv fail. */
        BOOL off = FALSE;
        DWORD ret = 0;
        WSAIoctl(s, SIO_UDP_CONNRESET, &off, sizeof(off), NULL, 0, &ret, NULL, NULL);
    }
#endif
    memset(to, 0, sizeof(*to));
    to->sin_family = AF_INET;
    to->sin_port = htons((unsigned short)port);
    to->sin_addr.s_addr = htonl(0x7F000001); /* 127.0.0.1 */
    return s;
}

/* Wait up to ms for a datagram. Returns its length, 0 on timeout. */
static int recv_wait(ao_socket s, char *buf, int size, int ms)
{
    fd_set set;
    struct timeval tv;
    int r;
    FD_ZERO(&set);
    FD_SET(s, &set);
    tv.tv_sec = ms / 1000;
    tv.tv_usec = (ms % 1000) * 1000;
    r = select((int)s + 1, &set, NULL, NULL, &tv);
    if (r <= 0) return 0;
    r = (int)recv(s, buf, size - 1, 0);
    if (r <= 0) return 0;
    buf[r] = 0;
    return r;
}

static int send_text(ao_socket s, struct sockaddr_in *to, const char *text)
{
    return (int)sendto(s, text, (int)strlen(text), 0, (struct sockaddr *)to, sizeof(*to));
}

/* One request, one reply (ms timeout). Returns 1 when a reply arrived. */
static int ask(const char *text, char *reply, int size, int ms)
{
    ao_config cfg;
    struct sockaddr_in to;
    ao_socket s;
    int got;
    read_config(&cfg);
    s = open_socket(cfg.port, &to);
    if (s == AO_BAD_SOCKET) return 0;
    send_text(s, &to, text);
    got = recv_wait(s, reply, size, ms);
    ao_close(s);
    return got > 0;
}

/* ------------------------------------------------------------------ */
/* Position                                                             */

static void position(double *lat, double *lon)
{
    *lat = (x_getd && dr_lat) ? x_getd(dr_lat) : 0.0;
    *lon = (x_getd && dr_lon) ? x_getd(dr_lon) : 0.0;
}

static int position_known(double lat, double lon)
{
    return !(lat > -1e-6 && lat < 1e-6 && lon > -1e-6 && lon < 1e-6);
}

/* ------------------------------------------------------------------ */
/* The hold                                                             */

static void hold(const char *why)
{
    ao_config cfg;
    struct sockaddr_in to;
    ao_socket s;
    char msg[160], reply[256];
    uint64_t started = now_ms(), last_reply = 0, last_log = 0;
    int answered = 0;
    double lat, lon;

    read_config(&cfg);
    s = open_socket(cfg.port, &to);
    if (s == AO_BAD_SOCKET) return;
#ifdef _WIN32
    {
        static int ghosting_off = 0;
        if (!ghosting_off) {
            DisableProcessWindowsGhosting();
            ghosting_off = 1;
        }
    }
#endif
    for (;;) {
        position(&lat, &lon);
        snprintf(msg, sizeof(msg), "AO1 HOLD %.6f %.6f %d", lat, lon, ++g_seq);
        send_text(s, &to, msg);
        if (recv_wait(s, reply, sizeof(reply), HOLD_STEP_MS) > 0) {
            if (strncmp(reply, "AO1 GO", 6) == 0) {
                say("the area is built after %.0f s: X-Plane goes on (%s)",
                    (now_ms() - started) / 1000.0, reply + 6);
                break;
            }
            if (strncmp(reply, "AO1 WAIT", 8) == 0) {
                int built = 0, total = 0;
                if (!answered) say("X-Plane waits (%s) until the ortho around %.4f %.4f is built",
                                   why, lat, lon);
                answered = 1;
                last_reply = now_ms();
                sscanf(reply + 8, "%d %d", &built, &total);
                if (now_ms() - last_log > 10000) {
                    say("building the start area: %d of %d textures", built, total);
                    last_log = now_ms();
                }
                continue;
            }
        }
        if (!answered && now_ms() - started > FIRST_REPLY_MS) {
            break; /* AutoOrtho not running */
        }
        if (answered && now_ms() - last_reply > SILENT_MS) {
            say("AutoOrtho stopped answering: X-Plane goes on");
            break;
        }
    }
    ao_close(s);
}

static void announce_load(void)
{
    double lat, lon;
    char msg[128], reply[64];
    position(&lat, &lon);
    if (!position_known(lat, lon)) return;
    snprintf(msg, sizeof(msg), "AO1 LOAD %.6f %.6f", lat, lon);
    ask(msg, reply, sizeof(reply), 300);
}

/* ------------------------------------------------------------------ */
/* Stale links in Custom Scenery                                        */

static int starts_with_path(const char *path, const char *root)
{
    size_t n = strlen(root);
    if (n == 0) return 0;
#ifdef _WIN32
    if (_strnicmp(path, root, n) != 0) return 0;
#else
    if (strncmp(path, root, n) != 0) return 0;
#endif
    return path[n] == 0 || path[n] == '\\' || path[n] == '/';
}

#ifdef _WIN32
typedef struct {
    ULONG ReparseTag;
    USHORT ReparseDataLength;
    USHORT Reserved;
    USHORT SubstituteNameOffset;
    USHORT SubstituteNameLength;
    USHORT PrintNameOffset;
    USHORT PrintNameLength;
    WCHAR PathBuffer[1];
} ao_mount_point;

/* Target of a junction, as UTF-8 without the \??\ prefix. */
static int junction_target(const wchar_t *path, char *out, int size)
{
    BYTE buf[16384];
    DWORD got = 0;
    HANDLE h = CreateFileW(path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL,
                           OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS,
                           NULL);
    ao_mount_point *rp = (ao_mount_point *)buf;
    WCHAR target[4096];
    int len;
    if (h == INVALID_HANDLE_VALUE) return 0;
    if (!DeviceIoControl(h, FSCTL_GET_REPARSE_POINT, NULL, 0, buf, sizeof(buf), &got, NULL)) {
        CloseHandle(h);
        return 0;
    }
    CloseHandle(h);
    if (rp->ReparseTag != IO_REPARSE_TAG_MOUNT_POINT) return 0;
    len = rp->SubstituteNameLength / 2;
    if (len <= 0 || len >= 4096) return 0;
    memcpy(target, (BYTE *)rp->PathBuffer + rp->SubstituteNameOffset, len * 2);
    target[len] = 0;
    {
        const WCHAR *t = target;
        if (wcsncmp(t, L"\\??\\", 4) == 0) t += 4;
        return WideCharToMultiByte(CP_UTF8, 0, t, -1, out, size, NULL, NULL) > 0;
    }
}

static void remove_stale_links(const ao_config *cfg)
{
    wchar_t wroot[1100], pattern[1200], full[1400];
    WIN32_FIND_DATAW fd;
    HANDLE find;
    char target[4200];
    if (!cfg->custom_scenery[0] || !cfg->files_root[0]) return;
    MultiByteToWideChar(CP_UTF8, 0, cfg->custom_scenery, -1, wroot, 1100);
    _snwprintf(pattern, 1200, L"%s\\z_ao_*", wroot);
    find = FindFirstFileW(pattern, &fd);
    if (find == INVALID_HANDLE_VALUE) return;
    do {
        if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT)) continue;
        if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) continue;
        _snwprintf(full, 1400, L"%s\\%s", wroot, fd.cFileName);
        if (!junction_target(full, target, sizeof(target))) continue;
        if (!starts_with_path(target, cfg->files_root)) continue;
        if (RemoveDirectoryW(full)) say("removed a stale AutoOrtho link: %ls", fd.cFileName);
    } while (FindNextFileW(find, &fd));
    FindClose(find);
}
#else
static void remove_stale_links(const ao_config *cfg)
{
    DIR *d;
    struct dirent *e;
    char full[2200], target[PATH_MAX];
    struct stat st;
    if (!cfg->custom_scenery[0] || !cfg->files_root[0]) return;
    d = opendir(cfg->custom_scenery);
    if (!d) return;
    while ((e = readdir(d)) != NULL) {
        ssize_t n;
        if (strncmp(e->d_name, "z_ao_", 5) != 0) continue;
        snprintf(full, sizeof(full), "%s/%s", cfg->custom_scenery, e->d_name);
        if (lstat(full, &st) != 0 || !S_ISLNK(st.st_mode)) continue;
        n = readlink(full, target, sizeof(target) - 1);
        if (n <= 0) continue;
        target[n] = 0;
        if (!starts_with_path(target, cfg->files_root)) continue;
        if (unlink(full) == 0) say("removed a stale AutoOrtho link: %s", e->d_name);
    }
    closedir(d);
}
#endif

/* ------------------------------------------------------------------ */
/* In flight: frame times (stutter log) and polls (relocation pause)    */
/*                                                                      */
/* One non-blocking socket; the flight loop never waits on AutoOrtho.   */

#define POLL_EVERY_MS 1000
#define CONFIG_EVERY_MS 5000
#define PAUSE_SILENT_MS 10000

typedef int (*fn_get_datai)(void *);
typedef void *(*fn_find_command)(const char *);
typedef void (*fn_command_once)(void *);
static fn_get_datai x_geti;
static fn_find_command x_find_cmd;
static fn_command_once x_cmd_once;
static void *dr_paused, *cmd_pause_on, *cmd_pause_off;

static ao_socket g_loop_sock = AO_BAD_SOCKET;
static struct sockaddr_in g_loop_to;
static uint64_t g_last_frame_sent = 0, g_last_poll = 0, g_last_config = 0, g_last_answer = 0;
static int g_we_paused = 0;

static void loop_socket(void)
{
    ao_config cfg;
    if (g_loop_sock != AO_BAD_SOCKET && now_ms() - g_last_config < CONFIG_EVERY_MS) return;
    g_last_config = now_ms();
    read_config(&cfg);
    if (g_loop_sock != AO_BAD_SOCKET && ntohs(g_loop_to.sin_port) == cfg.port) return;
    if (g_loop_sock != AO_BAD_SOCKET) ao_close(g_loop_sock);
    g_loop_sock = open_socket(cfg.port, &g_loop_to);
    if (g_loop_sock == AO_BAD_SOCKET) return;
#ifdef _WIN32
    {
        u_long on = 1;
        ioctlsocket(g_loop_sock, FIONBIO, &on);
    }
#else
    fcntl(g_loop_sock, F_SETFL, fcntl(g_loop_sock, F_GETFL, 0) | O_NONBLOCK);
#endif
}

static int sim_paused(void)
{
    return (x_geti && dr_paused) ? x_geti(dr_paused) != 0 : 0;
}

static void set_pause(int on)
{
    void *cmd = on ? cmd_pause_on : cmd_pause_off;
    if (x_cmd_once && cmd) x_cmd_once(cmd);
}

static void handle_answer(const char *reply)
{
    g_last_answer = now_ms();
    if (strncmp(reply, "AO1 PAUSE", 9) == 0) {
        if (!g_we_paused && !sim_paused()) {
            set_pause(1);
            g_we_paused = 1;
            say("the aircraft moved: X-Plane pauses until AutoOrtho has built the new area");
        }
    } else if (strncmp(reply, "AO1 RESUME", 10) == 0) {
        if (g_we_paused) {
            if (sim_paused()) set_pause(0);
            g_we_paused = 0;
            say("the new area is built: X-Plane goes on");
        }
    }
}

static float flight_loop(float since_call, float since_loop, int counter, void *ref)
{
    char buf[128];
    int n;
    (void)since_call; (void)since_loop; (void)counter; (void)ref;
    loop_socket();
    if (g_loop_sock == AO_BAD_SOCKET) return -1.0f;
    for (;;) { /* drain answers, never block */
        n = (int)recv(g_loop_sock, buf, sizeof(buf) - 1, 0);
        if (n <= 0) break;
        buf[n] = 0;
        handle_answer(buf);
    }
    if (x_getf && dr_frame) {
        float period = x_getf(dr_frame);
        if (period >= 0.1f && now_ms() - g_last_frame_sent > 1000) {
            snprintf(buf, sizeof(buf), "AO1 FRAME %.0f", period * 1000.0f);
            send_text(g_loop_sock, &g_loop_to, buf);
            g_last_frame_sent = now_ms();
        }
    }
    if (g_flight_loaded && now_ms() - g_last_poll >= POLL_EVERY_MS) {
        double lat, lon;
        position(&lat, &lon);
        if (position_known(lat, lon)) {
            snprintf(buf, sizeof(buf), "AO1 POLL %.6f %.6f", lat, lon);
            send_text(g_loop_sock, &g_loop_to, buf);
        }
        g_last_poll = now_ms();
    }
    if (g_we_paused && now_ms() - g_last_answer > PAUSE_SILENT_MS) {
        /* AutoOrtho gone while we held the pause: never leave X-Plane paused. */
        if (sim_paused()) set_pause(0);
        g_we_paused = 0;
        say("AutoOrtho stopped answering: X-Plane goes on");
    }
    return -1.0f; /* every frame */
}

/* ------------------------------------------------------------------ */
/* Plugin entry points                                                  */

static void find_plugin_root(void)
{
    char path[1024] = {0};
    char *cut;
    int i;
    g_plugin_root[0] = 0;
    if (!x_my_id || !x_info) return;
    x_info(x_my_id(), NULL, path, NULL, NULL);
    /* .../plugins/AutoOrtho/win_x64/AutoOrtho.xpl -> .../plugins/AutoOrtho */
    for (i = 0; i < 2; ++i) {
        cut = strrchr(path, '/');
        {
            char *b = strrchr(path, '\\');
            if (!cut || (b && b > cut)) cut = b;
        }
        if (!cut) break;
        *cut = 0;
    }
    strncpy(g_plugin_root, path, sizeof(g_plugin_root) - 1);
}

PLUGIN_API int XPluginStart(char *outName, char *outSig, char *outDesc)
{
    ao_config cfg;
    char msg[128], reply[64];
    strcpy(outName, "AutoOrtho");
    strcpy(outSig, "org.autoortho.files");
    strcpy(outDesc, "Holds a flight's loading until AutoOrtho has built the ortho around it.");

    x_debug = (fn_debug)xplm_symbol("XPLMDebugString");
    x_find = (fn_find_dataref)xplm_symbol("XPLMFindDataRef");
    x_getd = (fn_get_datad)xplm_symbol("XPLMGetDatad");
    x_getf = (fn_get_dataf)xplm_symbol("XPLMGetDataf");
    x_my_id = (fn_get_my_id)xplm_symbol("XPLMGetMyID");
    x_info = (fn_get_plugin_info)xplm_symbol("XPLMGetPluginInfo");
    x_version = (fn_get_version)xplm_symbol("XPLMGetVersions");
    x_register_loop = (fn_register_loop)xplm_symbol("XPLMRegisterFlightLoopCallback");
    x_unregister_loop = (fn_unregister_loop)xplm_symbol("XPLMUnregisterFlightLoopCallback");
    if (!x_debug || !x_find || !x_getd) return 0;

    dr_lat = x_find("sim/flightmodel/position/latitude");
    dr_lon = x_find("sim/flightmodel/position/longitude");
    dr_frame = x_find("sim/operation/misc/frame_rate_period");
    dr_paused = x_find("sim/time/paused");
    x_geti = (fn_get_datai)xplm_symbol("XPLMGetDatai");
    x_find_cmd = (fn_find_command)xplm_symbol("XPLMFindCommand");
    x_cmd_once = (fn_command_once)xplm_symbol("XPLMCommandOnce");
    if (x_find_cmd) {
        cmd_pause_on = x_find_cmd("sim/operation/pause_on");
        cmd_pause_off = x_find_cmd("sim/operation/pause_off");
    }
    if (x_version) {
        int xplm = 0, host = 0;
        x_version(&g_xp_version, &xplm, &host);
    }
    find_plugin_root();

    snprintf(msg, sizeof(msg), "AO1 HELLO %d %d", g_xp_version,
#ifdef _WIN32
             (int)GetCurrentProcessId()
#else
             (int)getpid()
#endif
    );
    if (ask(msg, reply, sizeof(reply), 300)) {
        say("version %s, AutoOrtho files mode is running", AO_VERSION);
    } else {
        say("version %s, AutoOrtho is not running", AO_VERSION);
        if (read_config(&cfg)) remove_stale_links(&cfg);
    }
    return 1;
}

PLUGIN_API void XPluginStop(void)
{
    char reply[16];
    ask("AO1 BYE", reply, sizeof(reply), 100);
#ifdef _WIN32
    if (g_wsa) WSACleanup();
#endif
}

PLUGIN_API int XPluginEnable(void)
{
    if (x_register_loop) x_register_loop(flight_loop, 1.0f, NULL);
    return 1;
}

PLUGIN_API void XPluginDisable(void)
{
    if (x_unregister_loop) x_unregister_loop(flight_loop, NULL);
    if (g_we_paused && sim_paused()) set_pause(0);
    g_we_paused = 0;
    if (g_loop_sock != AO_BAD_SOCKET) {
        ao_close(g_loop_sock);
        g_loop_sock = AO_BAD_SOCKET;
    }
}

PLUGIN_API void XPluginReceiveMessage(int inFrom, int inMsg, void *inParam)
{
    intptr_t param = (intptr_t)inParam;
    (void)inFrom;
    if (inMsg == MSG_PLANE_LOADED && param == 0) {
        announce_load();
    } else if (inMsg == MSG_LIVERY_LOADED && param == 0 && !g_flight_loaded) {
        /* First flight of this X-Plane session: hold before its scenery. */
        announce_load();
        hold("first flight");
        g_flight_loaded = 1;
    } else if (inMsg == MSG_NEW_FLIGHT && param == 1 && g_flight_loaded) {
        /* A new flight from within a flight: no aircraft load, so hold here. */
        announce_load();
        hold("new flight");
    } else if (inMsg == MSG_AIRPORT_LOADED) {
        g_flight_loaded = 1;
    }
}
