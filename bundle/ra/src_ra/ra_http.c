/* ra_http.c — minimal HTTPS POST for RetroAchievements server calls.
 *
 * Linux backend: OpenSSL + POSIX sockets. Windows backend: WinHTTP.
 * Only https:// URLs are supported (retroachievements.org is HTTPS-only).
 */
#include "ra_http.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winhttp.h>
#pragma comment(lib, "winhttp.lib")
#else
#include <netdb.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>
#include <openssl/err.h>
#include <openssl/ssl.h>
#endif

/* ------------------------------------------------------------------ */
/* Shared helpers                                                      */
/* ------------------------------------------------------------------ */

/* Split "https://host[:port]/path" into host, port, path.
 * Returns 0 on success. host/path are written into caller buffers. */
static int ra_split_url(const char* url, char* host, size_t host_cap,
                        int* port, const char** path_out) {
    const char* p;
    const char* host_begin;
    const char* host_end;
    const char* path;
    size_t host_len;
    long port_val = 443;

    if (!url || strncmp(url, "https://", 8) != 0) {
        fprintf(stderr, "[RA http] only https:// URLs supported: %s\n",
                url ? url : "(null)");
        return -1;
    }
    host_begin = url + 8;
    path = strchr(host_begin, '/');
    if (!path) path = host_begin + strlen(host_begin);
    host_end = path;
    /* Optional :port before the path. */
    {
        const char* colon = NULL;
        const char* q = host_begin;
        while (q < host_end) {
            if (*q == ':') colon = q;
            q++;
        }
        if (colon) {
            char port_buf[16];
            size_t port_len = (size_t)(host_end - colon - 1);
            if (port_len == 0 || port_len >= sizeof(port_buf)) return -1;
            memcpy(port_buf, colon + 1, port_len);
            port_buf[port_len] = '\0';
            port_val = atol(port_buf);
            if (port_val <= 0 || port_val > 65535) return -1;
            host_end = colon;
        }
    }
    host_len = (size_t)(host_end - host_begin);
    if (host_len == 0 || host_len >= host_cap) return -1;
    memcpy(host, host_begin, host_len);
    host[host_len] = '\0';
    *port = (int)port_val;
    *path_out = (*path == '\0') ? "/" : path;
    (void)p;
    return 0;
}

/* Parse an HTTP response buffer into status + body.
 * Handles Content-Length, chunked transfer-encoding, and read-until-close.
 * Returns 0 on success; body_out is malloc'd (caller frees). */
static int ra_parse_response(const char* buf, size_t len, int* status_out,
                             char** body_out, size_t* body_len_out) {
    const char* hdr_end;
    const char* body;
    size_t body_len;
    int status = 0;
    const char* sp1;
    const char* sp2;

    *body_out = NULL;
    *body_len_out = 0;
    /* Status line: "HTTP/1.1 200 ..." */
    sp1 = strchr(buf, ' ');
    if (!sp1) return -1;
    sp2 = strchr(sp1 + 1, ' ');
    if (!sp2) sp2 = strchr(sp1 + 1, '\r');
    if (!sp2) return -1;
    status = atoi(sp1 + 1);
    hdr_end = strstr(buf, "\r\n\r\n");
    if (!hdr_end) return -1;
    body = hdr_end + 4;
    body_len = len - (size_t)(body - buf);

    /* Chunked? */
    if (strstr(buf, "Transfer-Encoding: chunked") != NULL ||
        strstr(buf, "transfer-encoding: chunked") != NULL) {
        /* De-chunk into a fresh buffer. */
        char* out = (char*)malloc(body_len + 1);
        size_t out_len = 0;
        const char* cur = body;
        const char* end = buf + len;
        if (!out) return -1;
        while (cur < end) {
            char* line_end = strstr(cur, "\r\n");
            long chunk;
            if (!line_end) break;
            chunk = strtol(cur, NULL, 16);
            if (chunk <= 0) break;
            cur = line_end + 2;
            if (cur + chunk > end) chunk = (long)(end - cur);
            memcpy(out + out_len, cur, (size_t)chunk);
            out_len += (size_t)chunk;
            cur += chunk + 2; /* trailing CRLF */
        }
        out[out_len] = '\0';
        *body_out = out;
        *body_len_out = out_len;
        *status_out = status;
        return 0;
    }
    /* Content-Length, or everything after the headers. */
    {
        char* out = (char*)malloc(body_len + 1);
        if (!out) return -1;
        memcpy(out, body, body_len);
        out[body_len] = '\0';
        *body_out = out;
        *body_len_out = body_len;
        *status_out = status;
    }
    return 0;
}

#ifdef _WIN32
/* ------------------------------------------------------------------ */
/* Windows backend (WinHTTP)                                           */
/* ------------------------------------------------------------------ */

static wchar_t* ra_to_wide(const char* utf8) {
    int needed = MultiByteToWideChar(CP_UTF8, 0, utf8, -1, NULL, 0);
    wchar_t* out;
    if (needed <= 0) return NULL;
    out = (wchar_t*)malloc((size_t)needed * sizeof(wchar_t));
    if (!out) return NULL;
    if (MultiByteToWideChar(CP_UTF8, 0, utf8, -1, out, needed) <= 0) {
        free(out);
        return NULL;
    }
    return out;
}

int ra_http_post(const char* url, const char* post_data, const char* content_type,
                 char** out_body, size_t* out_len, int* out_status) {
    char host[256];
    const char* path = NULL;
    int port = 443;
    wchar_t* w_host = NULL;
    wchar_t* w_path = NULL;
    HINTERNET h_session = NULL, h_connect = NULL, h_request = NULL;
    char headers[512];
    DWORD body_len = 0;
    DWORD status = 0;
    DWORD status_len = sizeof(status);
    char* body = NULL;
    size_t body_cap = 0, body_used = 0;
    int rc = -1;

    *out_body = NULL;
    *out_len = 0;
    *out_status = 0;
    if (ra_split_url(url, host, sizeof(host), &port, &path) != 0) return -1;
    if (!post_data) post_data = "";
    body_len = (DWORD)strlen(post_data);

    w_host = ra_to_wide(host);
    w_path = ra_to_wide(path);
    if (!w_host || !w_path) goto done;

    h_session = WinHttpOpen(L"Tekken3-RA/1.0", WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
                            WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!h_session) goto done;
    h_connect = WinHttpConnect(h_session, w_host, (INTERNET_PORT)port, 0);
    if (!h_connect) goto done;
    h_request = WinHttpOpenRequest(h_connect, L"POST", w_path, NULL,
                                   WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES,
                                   WINHTTP_FLAG_SECURE);
    if (!h_request) goto done;
    snprintf(headers, sizeof(headers), "Content-Type: %s\r\n",
             content_type ? content_type : "application/x-www-form-urlencoded");
    {
        wchar_t* w_headers = ra_to_wide(headers);
        BOOL sent;
        if (!w_headers) goto done;
        sent = WinHttpSendRequest(h_request, w_headers, (DWORD)-1,
                                  (LPVOID)post_data, body_len, body_len, 0);
        free(w_headers);
        if (!sent) goto done;
    }
    if (!WinHttpReceiveResponse(h_request, NULL)) goto done;
    if (!WinHttpQueryHeaders(h_request,
                             WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                             NULL, &status, &status_len, NULL)) {
        goto done;
    }
    for (;;) {
        DWORD avail = 0;
        DWORD got = 0;
        if (!WinHttpQueryDataAvailable(h_request, &avail)) goto done;
        if (avail == 0) break;
        if (body_used + avail + 1 > body_cap) {
            size_t new_cap = body_cap ? body_cap * 2 : 8192;
            char* nb;
            while (new_cap < body_used + avail + 1) new_cap *= 2;
            nb = (char*)realloc(body, new_cap);
            if (!nb) goto done;
            body = nb;
            body_cap = new_cap;
        }
        if (!WinHttpReadData(h_request, body + body_used, avail, &got)) goto done;
        body_used += got;
        if (got == 0) break;
    }
    if (body) body[body_used] = '\0';
    *out_body = body;
    body = NULL;
    *out_len = body_used;
    *out_status = (int)status;
    rc = 0;
done:
    free(body);
    free(w_host);
    free(w_path);
    if (h_request) WinHttpCloseHandle(h_request);
    if (h_connect) WinHttpCloseHandle(h_connect);
    if (h_session) WinHttpCloseHandle(h_session);
    if (rc != 0) {
        fprintf(stderr, "[RA http] WinHTTP request failed (err %lu)\n",
                (unsigned long)GetLastError());
    }
    return rc;
}

#else
/* ------------------------------------------------------------------ */
/* Linux backend (OpenSSL + POSIX sockets)                             */
/* ------------------------------------------------------------------ */

int ra_http_post(const char* url, const char* post_data, const char* content_type,
                 char** out_body, size_t* out_len, int* out_status) {
    char host[256];
    const char* path = NULL;
    int port = 443;
    char port_str[16];
    struct addrinfo hints;
    struct addrinfo* res = NULL;
    int sock = -1;
    SSL_CTX* ctx = NULL;
    SSL* ssl = NULL;
    char* req = NULL;
    char* resp = NULL;
    size_t resp_cap = 0, resp_len = 0;
    int rc = -1;

    *out_body = NULL;
    *out_len = 0;
    *out_status = 0;
    if (ra_split_url(url, host, sizeof(host), &port, &path) != 0) return -1;
    if (!post_data) post_data = "";
    snprintf(port_str, sizeof(port_str), "%d", port);

    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(host, port_str, &hints, &res) != 0 || !res) {
        fprintf(stderr, "[RA http] DNS lookup failed for %s\n", host);
        return -1;
    }
    sock = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (sock < 0) goto done;
    if (connect(sock, res->ai_addr, res->ai_addrlen) != 0) {
        fprintf(stderr, "[RA http] connect failed for %s:%d\n", host, port);
        goto done;
    }
    SSL_library_init();
    SSL_load_error_strings();
    ctx = SSL_CTX_new(TLS_client_method());
    if (!ctx) goto done;
    ssl = SSL_new(ctx);
    if (!ssl) goto done;
    SSL_set_tlsext_host_name(ssl, host);
    SSL_set_fd(ssl, sock);
    if (SSL_connect(ssl) != 1) {
        fprintf(stderr, "[RA http] TLS handshake failed for %s\n", host);
        goto done;
    }
    {
        size_t req_cap = strlen(path) + strlen(host) +
                         (content_type ? strlen(content_type) : 0) +
                         strlen(post_data) + 256;
        int req_len;
        req = (char*)malloc(req_cap);
        if (!req) goto done;
        req_len = snprintf(req, req_cap,
                           "POST %s HTTP/1.1\r\n"
                           "Host: %s\r\n"
                           "Content-Type: %s\r\n"
                           "Content-Length: %zu\r\n"
                           "Connection: close\r\n"
                           "\r\n"
                           "%s",
                           path, host,
                           content_type ? content_type
                                        : "application/x-www-form-urlencoded",
                           strlen(post_data), post_data);
        if (req_len < 0 || (size_t)req_len >= req_cap) goto done;
        if (SSL_write(ssl, req, req_len) <= 0) goto done;
    }
    resp_cap = 8192;
    resp = (char*)malloc(resp_cap);
    if (!resp) goto done;
    for (;;) {
        int n;
        if (resp_len + 4096 + 1 > resp_cap) {
            char* nb;
            resp_cap *= 2;
            nb = (char*)realloc(resp, resp_cap);
            if (!nb) goto done;
            resp = nb;
        }
        n = SSL_read(ssl, resp + resp_len, 4096);
        if (n <= 0) break;
        resp_len += (size_t)n;
    }
    resp[resp_len] = '\0';
    if (ra_parse_response(resp, resp_len, out_status, out_body, out_len) != 0) {
        fprintf(stderr, "[RA http] could not parse response from %s\n", host);
        goto done;
    }
    rc = 0;
done:
    free(req);
    free(resp);
    if (ssl) {
        SSL_shutdown(ssl);
        SSL_free(ssl);
    }
    if (ctx) SSL_CTX_free(ctx);
    if (sock >= 0) close(sock);
    if (res) freeaddrinfo(res);
    return rc;
}
#endif
