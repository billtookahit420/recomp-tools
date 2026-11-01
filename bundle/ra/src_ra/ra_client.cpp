/* ra_client.cpp — RetroAchievements integration for the Tekken 3 recomp.
 *
 * Drop-in module: it registers itself with the runtime's mod frame-hook
 * system via a constructor, so NO framework file needs editing. The same
 * sources can be reused by other PSXRecomp game projects (Survivor,
 * Rayman, Gran Turismo) by adding them to that game's CMakeLists.
 *
 * How it works:
 *  - First game frame: lazily initializes (disc path is final by then).
 *    Reads retroachievements.toml next to the exe for username + API token
 *    (a token, never a password). If the file is absent, RA stays off and
 *    the game runs normally — RA is opt-in, never a boot blocker.
 *  - Disc hash: rcheevos iterator over the --disc image (.cue/.bin), which
 *    yields the hash RetroAchievements uses to identify the game.
 *  - Memory: PSX main RAM via g_psx_ram (2 MiB, mirrors folded with the
 *    same mask the runtime's canonical_offset uses).
 *  - Network: server calls run on detached threads (never block the game
 *    thread); responses are delivered back on the game thread from the
 *    frame hook, which is the thread rc_client_do_frame runs on.
 *  - Unlocks: logged to stdout for now. In-game toast popups need a
 *    launcher-UI hook and are a follow-up, not part of this first pass.
 *  - Hardcore: off by default (config: hardcore = true). Hardcore disables
 *    cheats/savestates per RA rules; default off keeps memory-card saves
 *    and normal play unaffected.
 */
#include <chrono>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "rc_client.h"
#include "rc_consoles.h"
#include "rc_hash.h"
#include "ra_http.h"

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#else
#include <unistd.h>
#include <limits.h>
#endif

#include "mod_runtime.h" /* PSXRecompV4::mod_runtime_effective_disc_path() */

/* Runtime symbols (C linkage, defined in the runtime's memory.c / main.cpp). */
extern "C" {
extern uint8_t* g_psx_ram;
void mod_register_frame_hook(void (*hook)(void));
}

/* Host overlay draw callback (C linkage, runtime). Declared here so this
 * module does not depend on host_overlay.h's include path. */
extern "C" {
void psx_host_overlay_set_draw_cb(void (*fn)(int width, int height, void* ctx),
                                  void* ctx);
}

/* recomp-ui model (C++-safe header): the toast UI is this module's own
 * instance (see ra_toast_ui), NOT the in-game menu's model, because the
 * menu only exists for multi-disc games. */
#include "recomp_runtime_ui.h"

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <GL/gl.h>
#include <wingdi.h>
#else
#include <GL/gl.h>
#include <GL/glx.h>
#endif
/* MinGW's GL header is strict GL 1.1: post-1.1 constants used below are
 * defined here when the header lacks them (values are the standard ones;
 * on Linux/Mesa they are already defined and these are skipped). */
#ifndef GL_CLAMP_TO_EDGE
#define GL_CLAMP_TO_EDGE 0x812F
#endif
#ifndef GL_TEXTURE0
#define GL_TEXTURE0 0x84C0
#endif
#ifndef GL_ARRAY_BUFFER
#define GL_ARRAY_BUFFER 0x8892
#endif
#ifndef GL_ARRAY_BUFFER_BINDING
#define GL_ARRAY_BUFFER_BINDING 0x8894
#endif
#ifndef GL_STREAM_DRAW
#define GL_STREAM_DRAW 0x88E0
#endif
#ifndef GL_VERTEX_ARRAY_BINDING
#define GL_VERTEX_ARRAY_BINDING 0x85B5
#endif
#ifndef GL_VERTEX_SHADER
#define GL_VERTEX_SHADER 0x8B31
#endif
#ifndef GL_FRAGMENT_SHADER
#define GL_FRAGMENT_SHADER 0x8B30
#endif
#ifndef GL_COMPILE_STATUS
#define GL_COMPILE_STATUS 0x8B81
#endif
#ifndef GL_CURRENT_PROGRAM
#define GL_CURRENT_PROGRAM 0x8B8D
#endif

namespace {

/* ------------------------------------------------------------------ */
/* Config: retroachievements.toml next to the exe                      */
/* ------------------------------------------------------------------ */

struct RaConfig {
    std::string username;
    std::string api_token;
    bool hardcore = false;
    bool present = false;
};

std::string ra_exe_dir() {
#ifdef _WIN32
    char path[MAX_PATH];
    DWORD n = GetModuleFileNameA(NULL, path, (DWORD)sizeof(path));
    if (n == 0 || n >= sizeof(path)) return std::string();
    std::string s(path);
    size_t slash = s.find_last_of("\\/");
    return (slash == std::string::npos) ? std::string() : s.substr(0, slash);
#else
    char path[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", path, sizeof(path) - 1);
    if (n <= 0) return std::string();
    path[n] = '\0';
    std::string s(path);
    size_t slash = s.find_last_of('/');
    return (slash == std::string::npos) ? std::string() : s.substr(0, slash);
#endif
}

std::string ra_trim(const std::string& s) {
    size_t a = 0, b = s.size();
    while (a < b && (s[a] == ' ' || s[a] == '\t' || s[a] == '\r' || s[a] == '\n')) a++;
    while (b > a && (s[b - 1] == ' ' || s[b - 1] == '\t' || s[b - 1] == '\r' || s[b - 1] == '\n')) b--;
    return s.substr(a, b - a);
}

std::string ra_unquote(const std::string& s) {
    if (s.size() >= 2 && s.front() == '"' && s.back() == '"')
        return s.substr(1, s.size() - 2);
    if (s.size() >= 2 && s.front() == '\'' && s.back() == '\'')
        return s.substr(1, s.size() - 2);
    return s;
}

RaConfig ra_read_config(const std::string& exe_dir) {
    RaConfig cfg;
    std::string path = exe_dir + "/retroachievements.toml";
    FILE* f = fopen(path.c_str(), "r");
    char line[1024];
    if (!f) return cfg;
    cfg.present = true;
    while (fgets(line, sizeof(line), f)) {
        std::string s = ra_trim(line);
        size_t eq;
        std::string key, val;
        if (s.empty() || s[0] == '#') continue;
        eq = s.find('=');
        if (eq == std::string::npos) continue;
        key = ra_trim(s.substr(0, eq));
        val = ra_unquote(ra_trim(s.substr(eq + 1)));
        if (key == "username") cfg.username = val;
        else if (key == "api_token") cfg.api_token = val;
        else if (key == "hardcore") cfg.hardcore = (val == "true" || val == "1");
    }
    fclose(f);
    return cfg;
}

/* ------------------------------------------------------------------ */
/* Memory peek: PSX main RAM                                           */
/* ------------------------------------------------------------------ */

/* PSX main RAM is 2 MiB, mirrored across the 8 MiB window; folding with
 * & 0x1FFFFF matches the runtime's canonical_offset for this window. */
uint32_t ra_read_memory(uint32_t address, uint8_t* buffer, uint32_t num_bytes,
                        rc_client_t* /*client*/) {
    uint32_t offset;
    if (!g_psx_ram || !buffer || num_bytes == 0) return 0;
    offset = address & 0x1FFFFFu;
    if (offset >= 0x200000u) return 0;
    if (offset + num_bytes > 0x200000u) num_bytes = 0x200000u - offset;
    memcpy(buffer, g_psx_ram + offset, num_bytes);
    return num_bytes;
}

/* ------------------------------------------------------------------ */
/* Server calls: detached thread + game-thread delivery                */
/* ------------------------------------------------------------------ */

struct RaPending {
    rc_client_server_callback_t callback = nullptr;
    void* callback_data = nullptr;
    char* body = nullptr;
    size_t body_len = 0;
    int http_status = 0;
};

std::mutex g_pending_mutex;
std::vector<RaPending> g_pending;

void ra_server_call(const rc_api_request_t* request,
                    rc_client_server_callback_t callback, void* callback_data,
                    rc_client_t* /*client*/) {
    /* Copy the request: rcheevos only guarantees it during this call. */
    std::string url = request->url ? request->url : "";
    std::string post = request->post_data ? request->post_data : "";
    std::string ctype = request->content_type ? request->content_type : "";
    std::thread([url, post, ctype, callback, callback_data]() {
        char* body = nullptr;
        size_t body_len = 0;
        int status = 0;
        RaPending p;
        if (ra_http_post(url.c_str(), post.c_str(), ctype.c_str(),
                         &body, &body_len, &status) != 0) {
            /* Network failure: report an empty 0-status response so the
             * client can retry/queue rather than hang. */
            body = nullptr;
            body_len = 0;
            status = 0;
        }
        p.callback = callback;
        p.callback_data = callback_data;
        p.body = body;
        p.body_len = body_len;
        p.http_status = status;
        {
            std::lock_guard<std::mutex> lock(g_pending_mutex);
            g_pending.push_back(p);
        }
    }).detach();
}

void ra_deliver_pending() {
    std::vector<RaPending> due;
    {
        std::lock_guard<std::mutex> lock(g_pending_mutex);
        due.swap(g_pending);
    }
    for (auto& p : due) {
        rc_api_server_response_t resp;
        memset(&resp, 0, sizeof(resp));
        resp.body = p.body ? p.body : "";
        resp.body_length = p.body_len;
        resp.http_status_code = p.http_status;
        if (p.callback) p.callback(&resp, p.callback_data);
        free(p.body);
    }
}

/* Forward: toast overlay (defined below; used by the event handler). */
void ra_toast_show(const std::string& title, const std::string& body,
                   int millis);
extern const int kToastMillis;

/* ------------------------------------------------------------------ */
/* Events + async callbacks                                            */
/* ------------------------------------------------------------------ */

void ra_event_handler(const rc_client_event_t* event, rc_client_t* /*client*/) {
    if (!event) return;
    switch (event->type) {
        case RC_CLIENT_EVENT_ACHIEVEMENT_TRIGGERED:
            if (event->achievement) {
                fprintf(stdout,
                        "[RA] Achievement unlocked: %s (%u points) - %s\n",
                        event->achievement->title ? event->achievement->title : "?",
                        event->achievement->points,
                        event->achievement->description
                            ? event->achievement->description
                            : "");
                fflush(stdout);
                /* In-game pop-up (OpenGL overlay; see toast section). */
                ra_toast_show(
                    event->achievement->title ? event->achievement->title : "?",
                    event->achievement->description
                        ? event->achievement->description
                        : "",
                    kToastMillis);
            }
            break;
        case RC_CLIENT_EVENT_GAME_COMPLETED:
            fprintf(stdout, "[RA] All achievements for this game earned!\n");
            fflush(stdout);
            break;
        case RC_CLIENT_EVENT_SERVER_ERROR:
            if (event->server_error) {
                fprintf(stderr, "[RA] Server error (%s): %s\n",
                        event->server_error->api ? event->server_error->api : "?",
                        event->server_error->error_message
                            ? event->server_error->error_message
                            : "?");
            }
            break;
        case RC_CLIENT_EVENT_DISCONNECTED:
            fprintf(stderr, "[RA] Disconnected: unlocks will queue until reconnect.\n");
            break;
        case RC_CLIENT_EVENT_RECONNECTED:
            fprintf(stdout, "[RA] Reconnected: queued unlocks submitted.\n");
            fflush(stdout);
            break;
        default:
            break;
    }
}

void ra_login_callback(int result, const char* error_message,
                       rc_client_t* /*client*/, void* /*userdata*/) {
    /* NOTE: rcheevos reports RC_OK (0) on success, nonzero on failure --
       the original branches here had this backwards, so a failed login
       printed "Logged in." and a good login printed "Login failed." */
    if (result == 0) {
        fprintf(stdout, "[RA] Logged in.\n");
        /* Visible login-confirmation toast (OpenGL overlay, same as unlock toasts). */
        ra_toast_show("RetroAchievements", "Logged in", kToastMillis);
    } else {
        fprintf(stdout, "[RA] Login failed: %s\n",
                error_message ? error_message : "unknown error");
        /* Visible login-failure toast (OpenGL overlay, same as unlock toasts). */
        ra_toast_show("RetroAchievements", "Login failed - check retroachievements.toml",
                      kToastMillis);
    }
    fflush(stdout);
}

void ra_load_callback(int result, const char* error_message,
                      rc_client_t* /*client*/, void* /*userdata*/) {
    /* Same polarity note as ra_login_callback: RC_OK (0) is success. */
    if (result == 0) {
        fprintf(stdout, "[RA] Game achievements loaded.\n");
    } else {
        fprintf(stderr, "[RA] Could not load game achievements: %s\n",
                error_message ? error_message : "unknown error");
    }
    fflush(stdout);
}

/* ------------------------------------------------------------------ */
/* In-game toast overlay (OpenGL)                                      */
/*                                                                     */
/* Achievements pop up ON TOP of gameplay via the host overlay draw    */
/* callback (OpenGL backend, the default renderer; Vulkan/SDL renderers */
/* never call it, so pop-ups need OpenGL). The toast itself is rendered */
/* by recomp-ui's software renderer into a pixel buffer (so it matches */
/* the launcher's styling and fonts), uploaded as a GL texture, and    */
/* drawn as a top-center quad. The UI struct is only touched from the  */
/* GL thread (the draw callback); the game thread only sets this       */
/* module's own toast state under a mutex.                             */
/* ------------------------------------------------------------------ */

struct RaToastState {
    std::string title;
    std::string body;
    bool pending = false; /* new toast waiting to be pushed to the UI */
    bool shown = false;   /* toast currently on screen */
    std::chrono::steady_clock::time_point until{};
    unsigned long generation = 0; /* increments per new toast */
};

std::mutex g_toast_mutex;
RaToastState g_toast;
const int kToastMillis = 5000; /* on-screen duration per unlock */

void ra_toast_show(const std::string& title, const std::string& body,
                   int millis) {
#ifdef RA_DISABLE_POPUPS
    /* Recomp Tools GUI option: pop-ups hidden at build time. This also
       hides the login toast, which uses the same draw path. */
    (void)title; (void)body; (void)millis;
    return;
#else
    std::lock_guard<std::mutex> lock(g_toast_mutex);
    g_toast.title = title;
    g_toast.body = body;
    g_toast.pending = true;
    g_toast.until = std::chrono::steady_clock::now() +
                    std::chrono::milliseconds(millis);
    g_toast.generation++;
#endif
}

/* --- Minimal GL loader (shader functions are extensions) --- */
void* ra_gl_proc(const char* name) {
#ifdef _WIN32
    return (void*)wglGetProcAddress(name);
#else
    return (void*)glXGetProcAddress((const GLubyte*)name);
#endif
}

typedef GLuint (*RA_PFNGLCreateShaderPROC)(GLenum);
typedef void (*RA_PFNGLShaderSourcePROC)(GLuint, GLsizei, const char* const*,
                                         const GLint*);
typedef void (*RA_PFNGLCompileShaderPROC)(GLuint);
typedef void (*RA_PFNGLGetShaderivPROC)(GLuint, GLenum, GLint*);
typedef GLuint (*RA_PFNGLCreateProgramPROC)(void);
typedef void (*RA_PFNGLAttachShaderPROC)(GLuint, GLuint);
typedef void (*RA_PFNGLLinkProgramPROC)(GLuint);
typedef void (*RA_PFNGLUseProgramPROC)(GLuint);
typedef GLint (*RA_PFNGLGetUniformLocationPROC)(GLuint, const char*);
typedef void (*RA_PFNGLUniform1iPROC)(GLint, GLint);
typedef void (*RA_PFNGLGenBuffersPROC)(GLsizei, GLuint*);
typedef void (*RA_PFNGLBindBufferPROC)(GLenum, GLuint);
typedef void (*RA_PFNGLBufferDataPROC)(GLenum, ptrdiff_t, const void*, GLenum);
typedef void (*RA_PFNGLEnableVertexAttribArrayPROC)(GLuint);
typedef void (*RA_PFNGLVertexAttribPointerPROC)(GLuint, GLint, GLenum,
                                                GLboolean, GLsizei,
                                                const void*);
typedef void (*RA_PFNGLGenVertexArraysPROC)(GLsizei, GLuint*);
typedef void (*RA_PFNGLBindVertexArrayPROC)(GLuint);
typedef void (*RA_PFNGLActiveTexturePROC)(GLenum);

struct RaGl {
    RA_PFNGLCreateShaderPROC CreateShader = nullptr;
    RA_PFNGLShaderSourcePROC ShaderSource = nullptr;
    RA_PFNGLCompileShaderPROC CompileShader = nullptr;
    RA_PFNGLGetShaderivPROC GetShaderiv = nullptr;
    RA_PFNGLCreateProgramPROC CreateProgram = nullptr;
    RA_PFNGLAttachShaderPROC AttachShader = nullptr;
    RA_PFNGLLinkProgramPROC LinkProgram = nullptr;
    RA_PFNGLUseProgramPROC UseProgram = nullptr;
    RA_PFNGLGetUniformLocationPROC GetUniformLocation = nullptr;
    RA_PFNGLUniform1iPROC Uniform1i = nullptr;
    RA_PFNGLGenBuffersPROC GenBuffers = nullptr;
    RA_PFNGLBindBufferPROC BindBuffer = nullptr;
    RA_PFNGLBufferDataPROC BufferData = nullptr;
    RA_PFNGLEnableVertexAttribArrayPROC EnableVertexAttribArray = nullptr;
    RA_PFNGLVertexAttribPointerPROC VertexAttribPointer = nullptr;
    RA_PFNGLGenVertexArraysPROC GenVertexArrays = nullptr;
    RA_PFNGLBindVertexArrayPROC BindVertexArray = nullptr;
    RA_PFNGLActiveTexturePROC ActiveTexture = nullptr;
    GLuint program = 0;
    GLuint vao = 0;
    GLuint vbo = 0;
    GLuint tex = 0;
    bool ready = false;
    bool failed = false;
    unsigned long uploaded_generation = 0;
};

RaGl g_gl;

bool ra_gl_init() {
    if (g_gl.ready || g_gl.failed) return g_gl.ready;
#define RA_LOAD(fn) \
    g_gl.fn = (RA_PFNGL##fn##PROC)ra_gl_proc("gl" #fn); \
    if (!g_gl.fn) { g_gl.failed = true; return false; }
    RA_LOAD(CreateShader)
    RA_LOAD(ShaderSource)
    RA_LOAD(CompileShader)
    RA_LOAD(GetShaderiv)
    RA_LOAD(CreateProgram)
    RA_LOAD(AttachShader)
    RA_LOAD(LinkProgram)
    RA_LOAD(UseProgram)
    RA_LOAD(GetUniformLocation)
    RA_LOAD(Uniform1i)
    RA_LOAD(GenBuffers)
    RA_LOAD(BindBuffer)
    RA_LOAD(BufferData)
    RA_LOAD(EnableVertexAttribArray)
    RA_LOAD(VertexAttribPointer)
    RA_LOAD(GenVertexArrays)
    RA_LOAD(BindVertexArray)
    RA_LOAD(ActiveTexture)
#undef RA_LOAD
    const char* vs_src =
        "#version 330 core\n"
        "layout(location=0) in vec2 a_pos;\n"
        "layout(location=1) in vec2 a_uv;\n"
        "out vec2 v_uv;\n"
        "void main() { v_uv = a_uv; gl_Position = vec4(a_pos, 0.0, 1.0); }\n";
    const char* fs_src =
        "#version 330 core\n"
        "in vec2 v_uv;\n"
        "out vec4 frag;\n"
        "uniform sampler2D u_tex;\n"
        "void main() { frag = texture(u_tex, v_uv); }\n";
    GLuint vs = g_gl.CreateShader(GL_VERTEX_SHADER);
    g_gl.ShaderSource(vs, 1, &vs_src, nullptr);
    g_gl.CompileShader(vs);
    GLint ok = 0;
    g_gl.GetShaderiv(vs, GL_COMPILE_STATUS, &ok);
    if (!ok) { g_gl.failed = true; return false; }
    GLuint fs = g_gl.CreateShader(GL_FRAGMENT_SHADER);
    g_gl.ShaderSource(fs, 1, &fs_src, nullptr);
    g_gl.CompileShader(fs);
    g_gl.GetShaderiv(fs, GL_COMPILE_STATUS, &ok);
    if (!ok) { g_gl.failed = true; return false; }
    g_gl.program = g_gl.CreateProgram();
    g_gl.AttachShader(g_gl.program, vs);
    g_gl.AttachShader(g_gl.program, fs);
    g_gl.LinkProgram(g_gl.program);
    g_gl.GenVertexArrays(1, &g_gl.vao);
    g_gl.GenBuffers(1, &g_gl.vbo);
    glGenTextures(1, &g_gl.tex);
    glBindTexture(GL_TEXTURE_2D, g_gl.tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glBindTexture(GL_TEXTURE_2D, 0);
    g_gl.ready = true;
    return true;
}

/* Toast pixel buffer (software render target). Height 448 gives the
 * launcher's toast renderer scale 2 (height/224), so the 5x7 bitmap font
 * renders at 2x and stays readable on a big screen. */
const int kToastBufW = 640;
const int kToastBufH = 448;
uint32_t g_toast_argb[kToastBufW * kToastBufH];
unsigned char g_toast_rgba[kToastBufW * kToastBufH * 4];

/* This module's own toast UI. The in-game menu model cannot be used: the
 * menu is only created for multi-disc games, so it is NULL for Tekken 3
 * (and every other single-disc title). This ui is never opened; it only
 * carries the toast text + theme for the software toast renderer. */
RecompRuntimeUi* ra_toast_ui() {
    static RecompRuntimeUi* ui = nullptr;
    static bool tried = false;
    if (!ui && !tried) {
        tried = true;
        static const RecompRuntimeUiItem item = {
            "ra_toast", "Achievements", "Achievements", nullptr,
            RECOMP_RUNTIME_UI_ACTION, 0, 0, 0, nullptr, 0, nullptr
        };
        RecompRuntimeUiConfig cfg = {};
        cfg.title = "RetroAchievements";
        cfg.theme = "psx";
        cfg.items = &item;
        cfg.item_count = 1;
        ui = recomp_runtime_ui_create(&cfg);
        if (!ui) {
            fprintf(stdout, "[RA] toast ui create failed\n");
            fflush(stdout);
        }
    }
    return ui;
}

/* Overlay draw callback: runs once per presented frame (GL thread). */
void ra_overlay_draw(int width, int height, void* /*ctx*/) {
    if (width <= 0 || height <= 0) return;
    static bool logged_first = false;
    if (!logged_first) {
        fprintf(stdout, "[RA] overlay draw called (%dx%d)\n", width, height);
        fflush(stdout);
        logged_first = true;
    }
    RecompRuntimeUi* ui = ra_toast_ui();
    if (!ui) return;

    /* Sync toast state (game thread -> GL thread). */
    std::string title, body;
    bool have_toast = false;
    unsigned long generation = 0;
    {
        std::lock_guard<std::mutex> lock(g_toast_mutex);
        auto now = std::chrono::steady_clock::now();
        if (g_toast.pending) {
            recomp_runtime_ui_set_toast(ui, g_toast.title.c_str(),
                                        g_toast.body.c_str());
            g_toast.pending = false;
            g_toast.shown = true;
        }
        if (g_toast.shown && now >= g_toast.until) {
            recomp_runtime_ui_set_toast(ui, nullptr, nullptr);
            g_toast.shown = false;
        }
        if (g_toast.shown && recomp_runtime_ui_toast_visible(ui)) {
            title = g_toast.title;
            body = g_toast.body;
            generation = g_toast.generation;
            have_toast = true;
        }
    }
    if (!have_toast) return;
    (void)title; (void)body;
    if (!ra_gl_init()) {
        static bool logged_gl_fail = false;
        if (!logged_gl_fail) {
            fprintf(stdout, "[RA] overlay draw: GL init failed\n");
            fflush(stdout);
            logged_gl_fail = true;
        }
        return;
    }

    /* Render the toast into the pixel buffer (once per new toast) and
     * upload it as a texture. */
    if (g_gl.uploaded_generation != generation) {
        memset(g_toast_argb, 0, sizeof(g_toast_argb));
        recomp_runtime_ui_render_argb8888(ui, g_toast_argb, kToastBufW,
                                          kToastBufH, kToastBufW * 4);
        for (int i = 0; i < kToastBufW * kToastBufH; ++i) {
            uint32_t a = g_toast_argb[i];
            g_toast_rgba[i * 4 + 0] = (unsigned char)((a >> 16) & 0xff);
            g_toast_rgba[i * 4 + 1] = (unsigned char)((a >> 8) & 0xff);
            g_toast_rgba[i * 4 + 2] = (unsigned char)(a & 0xff);
            g_toast_rgba[i * 4 + 3] = (unsigned char)((a >> 24) & 0xff);
        }
        glBindTexture(GL_TEXTURE_2D, g_gl.tex);
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, kToastBufW, kToastBufH, 0,
                     GL_RGBA, GL_UNSIGNED_BYTE, g_toast_rgba);
        glBindTexture(GL_TEXTURE_2D, 0);
        g_gl.uploaded_generation = generation;
    }

    /* Quad: top-center, 2x the buffer width (shrunk to fit small windows),
     * so the toast stays readable on large displays. */
    float quad_w = (float)kToastBufW * 2.0f;
    if (quad_w > width * 0.9f) quad_w = width * 0.9f;
    float quad_h = quad_w * ((float)kToastBufH / (float)kToastBufW);
    float x0 = -quad_w / (float)width;
    float x1 = quad_w / (float)width;
    float y_top = 1.0f - (12.0f / (float)height) * 2.0f;
    float y_bot = 1.0f - ((12.0f + quad_h) / (float)height) * 2.0f;
    /* UVs: glTexImage2D maps memory row 0 to v=0, and memory row 0 is the
     * buffer's top, so buffer-top = v=0 (top quad verts use v=0). */
    float verts[16] = {
        x0, y_top, 0.0f, 0.0f,
        x1, y_top, 1.0f, 0.0f,
        x0, y_bot, 0.0f, 1.0f,
        x1, y_bot, 1.0f, 1.0f,
    };

    {
        static bool logged_draw = false;
        if (!logged_draw) {
            fprintf(stdout, "[RA] overlay draw: drawing toast quad (%dx%d)\n",
                    width, height);
            fflush(stdout);
            logged_draw = true;
        }
    }

    /* Save GL state, draw, restore (the callback must leave GL as found). */
    GLint prev_program = 0, prev_tex = 0, prev_vao = 0, prev_vbo = 0;
    GLboolean prev_blend = glIsEnabled(GL_BLEND);
    GLboolean prev_depth = glIsEnabled(GL_DEPTH_TEST);
    glGetIntegerv(GL_CURRENT_PROGRAM, &prev_program);
    glGetIntegerv(GL_TEXTURE_BINDING_2D, &prev_tex);
    glGetIntegerv(GL_VERTEX_ARRAY_BINDING, &prev_vao);
    glGetIntegerv(GL_ARRAY_BUFFER_BINDING, &prev_vbo);

    g_gl.UseProgram(g_gl.program);
    GLint loc = g_gl.GetUniformLocation(g_gl.program, "u_tex");
    g_gl.ActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, g_gl.tex);
    if (loc >= 0) g_gl.Uniform1i(loc, 0);
    g_gl.BindVertexArray(g_gl.vao);
    g_gl.BindBuffer(GL_ARRAY_BUFFER, g_gl.vbo);
    g_gl.BufferData(GL_ARRAY_BUFFER, sizeof(verts), verts, GL_STREAM_DRAW);
    g_gl.EnableVertexAttribArray(0);
    g_gl.VertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(float),
                             (const void*)0);
    g_gl.EnableVertexAttribArray(1);
    g_gl.VertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(float),
                             (const void*)(2 * sizeof(float)));
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    glDisable(GL_DEPTH_TEST);
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);

    g_gl.BindVertexArray((GLuint)prev_vao);
    g_gl.BindBuffer(GL_ARRAY_BUFFER, (GLuint)prev_vbo);
    glBindTexture(GL_TEXTURE_2D, (GLuint)prev_tex);
    g_gl.UseProgram((GLuint)prev_program);
    if (!prev_blend) glDisable(GL_BLEND);
    if (prev_depth) glEnable(GL_DEPTH_TEST);
}

/* ------------------------------------------------------------------ */
/* Init + frame hook                                                   */
/* ------------------------------------------------------------------ */

rc_client_t* g_client = nullptr;
bool g_init_done = false;

void ra_init() {
    std::string disc_path;
    std::string exe_dir;
    RaConfig cfg;
    char hash[33];
    rc_hash_iterator_t it;
    bool have_hash = false;
    static bool logged_waiting = false;

    /* Mirror the runtime's own mount fallback: launcher-picked effective
     * path when set, else the committed disc path (CLI --disc / game.toml).
     * Effective alone is empty in --no-launcher CLI runs, which used to
     * defer RA init forever with no log beyond "waiting for disc path". */
    {
        const auto& eff = PSXRecompV4::mod_runtime_effective_disc_path();
        const auto& committed = PSXRecompV4::mod_runtime_disc_path();
        disc_path = !eff.empty() ? eff.string() : committed.string();
    }
    if (disc_path.empty()) {
        /* Not ready yet; retry next frame. Log once so a missing disc
         * path is visible instead of silently deferring forever. */
        if (!logged_waiting) {
            fprintf(stdout, "[RA] waiting for disc path...\n");
            fflush(stdout);
            logged_waiting = true;
        }
        return;
    }
    g_init_done = true;

    exe_dir = ra_exe_dir();
    /* Dev-only test hook (NOT shipped in packages): if ra_test_toast.txt
     * sits next to the exe, pop its contents as a toast for 30 s so the
     * in-game overlay can be verified without a real RA unlock. */
    {
        std::string test_path = exe_dir + "/ra_test_toast.txt";
        FILE* tf = fopen(test_path.c_str(), "r");
        if (tf) {
            char tline[256] = "";
            char bline[512] = "";
            if (fgets(tline, sizeof(tline), tf)) {
                std::string t = ra_trim(tline);
                std::string b;
                if (fgets(bline, sizeof(bline), tf)) b = ra_trim(bline);
                if (t.empty()) t = "Test Achievement";
                if (b.empty()) b = "In-game pop-ups are working!";
                ra_toast_show(t, b, 30000);
                fprintf(stdout, "[RA] test toast armed: %s\n", t.c_str());
                fflush(stdout);
            }
            fclose(tf);
        }
    }
    cfg = ra_read_config(exe_dir);
    if (!cfg.present) {
        fprintf(stdout,
                "[RA] Disabled: no retroachievements.toml next to the exe.\n"
                "[RA] To enable: create retroachievements.toml with your\n"
                "[RA] username and RetroAchievements API token, then restart.\n");
        fflush(stdout);
        return;
    }
    if (cfg.username.empty() || cfg.api_token.empty()) {
        fprintf(stderr,
                "[RA] Disabled: retroachievements.toml is missing username "
                "or api_token.\n");
        return;
    }

    /* Disc hash (identifies the game on RetroAchievements). */
    rc_hash_init_default_cdreader();
    rc_hash_initialize_iterator(&it, disc_path.c_str(), nullptr, 0);
    while (rc_hash_iterate(hash, &it)) {
        have_hash = true;
        break; /* first hash is the disc's */
    }
    rc_hash_destroy_iterator(&it);
    if (!have_hash) {
        fprintf(stderr, "[RA] Disabled: could not hash disc %s\n",
                disc_path.c_str());
        return;
    }
    fprintf(stdout, "[RA] Disc hash: %s\n", hash);

    g_client = rc_client_create(ra_read_memory, ra_server_call);
    if (!g_client) {
        fprintf(stderr, "[RA] Disabled: rc_client_create failed.\n");
        return;
    }
    rc_client_set_event_handler(g_client, ra_event_handler);
    rc_client_set_hardcore_enabled(g_client, cfg.hardcore ? 1 : 0);
    fprintf(stdout, "[RA] Logging in as %s (hardcore %s)...\n",
            cfg.username.c_str(), cfg.hardcore ? "on" : "off");
    fflush(stdout);
    rc_client_begin_login_with_token(g_client, cfg.username.c_str(),
                                     cfg.api_token.c_str(), ra_login_callback,
                                     nullptr);
    rc_client_begin_load_game(g_client, hash, ra_load_callback, nullptr);
}

void ra_frame_hook() {
    static bool logged_hook = false;
    if (!logged_hook) {
        fprintf(stdout, "[RA] frame hook active\n");
        fflush(stdout);
        logged_hook = true;
        /* Register the in-game toast overlay once (OpenGL; harmless if
         * the renderer never calls it, e.g. Vulkan/SDL). */
        psx_host_overlay_set_draw_cb(ra_overlay_draw, nullptr);
    }
    if (!g_init_done) ra_init();
    if (!g_client) return;
    ra_deliver_pending();
    if (g_psx_ram) rc_client_do_frame(g_client);
}

} /* namespace */

/* Self-registration: runs before main(), needs no framework edit. */
#ifdef _WIN32
__attribute__((constructor)) static void ra_register_hook() {
    mod_register_frame_hook(ra_frame_hook);
}
#else
__attribute__((constructor)) static void ra_register_hook() {
    mod_register_frame_hook(ra_frame_hook);
}
#endif
