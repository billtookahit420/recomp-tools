/* ra_http.h — minimal HTTPS POST for RetroAchievements server calls.
 *
 * One function, two backends: OpenSSL on Linux, WinHTTP on Windows.
 * No libcurl dependency (not available for MinGW here).
 *
 * Returns 0 on success (out_body/out_len/out_status filled; caller frees
 * out_body with free()). Returns non-zero on any failure; out_body is NULL.
 */
#pragma once

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

int ra_http_post(const char* url, const char* post_data, const char* content_type,
                 char** out_body, size_t* out_len, int* out_status);

#ifdef __cplusplus
}
#endif
