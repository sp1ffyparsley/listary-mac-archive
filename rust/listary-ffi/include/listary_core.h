#ifndef LISTARY_CORE_H
#define LISTARY_CORE_H

#include <stddef.h>
#include <stdint.h>

#define LISTARY_CORE_API_VERSION 9u

#ifdef __cplusplus
extern "C" {
#endif

typedef struct ListaryCoreHandle ListaryCoreHandle;

typedef struct ListaryBuffer {
    uint8_t *ptr;
    size_t len;
} ListaryBuffer;

enum ListaryStatus {
    LISTARY_STATUS_OK = 0,
    LISTARY_STATUS_INVALID_ARGUMENT = 1,
    LISTARY_STATUS_ALREADY_OPEN = 2,
    LISTARY_STATUS_CANCELLED = 3,
    LISTARY_STATUS_NOT_FOUND = 4,
    LISTARY_STATUS_IO = 5,
    LISTARY_STATUS_INDEX = 6,
    LISTARY_STATUS_SERIALIZATION = 7,
    LISTARY_STATUS_STALE_CATALOG = 8,
    LISTARY_STATUS_PANIC = 255
};

uint32_t listary_core_api_version(void);

/*
 * Complex request/response payloads use versioned UTF-8 JSON so the Swift
 * bridge can evolve independently from the Rust ABI. Search, facet counts, and reconcile
 * are synchronous on the calling thread; callers run them on a worker queue.
 * listary_core_cancel is safe to call from another thread with the same
 * request_id.
 */
int32_t listary_core_open(
    const uint8_t *config_json,
    size_t config_len,
    ListaryCoreHandle **out_handle,
    ListaryBuffer *out_error);

void listary_core_close(ListaryCoreHandle *handle);

int32_t listary_core_search(
    ListaryCoreHandle *handle,
    uint64_t request_id,
    const uint8_t *request_json,
    size_t request_len,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_file_search(
    ListaryCoreHandle *handle,
    uint64_t request_id,
    const uint8_t *request_json,
    size_t request_len,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_facet_counts(
    ListaryCoreHandle *handle,
    uint64_t request_id,
    const uint8_t *request_json,
    size_t request_len,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_cancel(
    ListaryCoreHandle *handle,
    uint64_t request_id);

int32_t listary_core_add_root(
    ListaryCoreHandle *handle,
    const uint8_t *request_json,
    size_t request_len,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_list_roots(
    ListaryCoreHandle *handle,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_put_root(
    ListaryCoreHandle *handle,
    const uint8_t *root_json,
    size_t root_len,
    ListaryBuffer *out_error);

int32_t listary_core_remove_root(
    ListaryCoreHandle *handle,
    const uint8_t *request_json,
    size_t request_len,
    ListaryBuffer *out_error);

int32_t listary_core_list_filters(
    ListaryCoreHandle *handle,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_put_filter(
    ListaryCoreHandle *handle,
    const uint8_t *filter_json,
    size_t filter_len,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_remove_filter(
    ListaryCoreHandle *handle,
    const uint8_t *request_json,
    size_t request_len,
    ListaryBuffer *out_error);

int32_t listary_core_list_commands(
    ListaryCoreHandle *handle,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_put_command(
    ListaryCoreHandle *handle,
    const uint8_t *command_json,
    size_t command_len,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_remove_command(
    ListaryCoreHandle *handle,
    const uint8_t *request_json,
    size_t request_len,
    ListaryBuffer *out_error);

int32_t listary_core_expand_command(
    ListaryCoreHandle *handle,
    const uint8_t *request_json,
    size_t request_len,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_list_actions(
    ListaryCoreHandle *handle,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_put_action(
    ListaryCoreHandle *handle,
    const uint8_t *action_json,
    size_t action_len,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_remove_action(
    ListaryCoreHandle *handle,
    const uint8_t *request_json,
    size_t request_len,
    ListaryBuffer *out_error);

int32_t listary_core_expand_action(
    ListaryCoreHandle *handle,
    const uint8_t *request_json,
    size_t request_len,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_get_favorites(
    ListaryCoreHandle *handle,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_put_favorites(
    ListaryCoreHandle *handle,
    const uint8_t *menu_json,
    size_t menu_len,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_toggle_pin(
    ListaryCoreHandle *handle,
    const uint8_t *request_json,
    size_t request_len,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_resolve_favorites(
    ListaryCoreHandle *handle,
    const uint8_t *request_json,
    size_t request_len,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_list_web_search_providers(
    ListaryCoreHandle *handle,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_replace_web_search_providers(
    ListaryCoreHandle *handle,
    const uint8_t *providers_json,
    size_t providers_len,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_list_priority_rules(
    ListaryCoreHandle *handle,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_replace_priority_rules(
    ListaryCoreHandle *handle,
    const uint8_t *rules_json,
    size_t rules_len,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_resolve_web_search(
    ListaryCoreHandle *handle,
    const uint8_t *request_json,
    size_t request_len,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_reconcile_root(
    ListaryCoreHandle *handle,
    uint64_t request_id,
    const uint8_t *root_id_utf8,
    size_t root_id_len,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_reconcile_paths(
    ListaryCoreHandle *handle,
    uint64_t request_id,
    const uint8_t *request_json,
    size_t request_len,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_record_activation(
    ListaryCoreHandle *handle,
    const uint8_t *request_json,
    size_t request_len,
    ListaryBuffer *out_error);

int32_t listary_core_recommendations(
    ListaryCoreHandle *handle,
    const uint8_t *request_json,
    size_t request_len,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_hide_recommendation(
    ListaryCoreHandle *handle,
    const uint8_t *request_json,
    size_t request_len,
    ListaryBuffer *out_error);

int32_t listary_core_exclude_path(
    ListaryCoreHandle *handle,
    const uint8_t *request_json,
    size_t request_len,
    ListaryBuffer *out_error);

int32_t listary_core_get_item(
    ListaryCoreHandle *handle,
    const uint8_t *request_json,
    size_t request_len,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_get_diagnostics(
    ListaryCoreHandle *handle,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

int32_t listary_core_flush(
    ListaryCoreHandle *handle,
    ListaryBuffer *out_error);

int32_t listary_core_rebuild_all(
    ListaryCoreHandle *handle,
    uint64_t request_id,
    ListaryBuffer *out_response,
    ListaryBuffer *out_error);

void listary_buffer_free(ListaryBuffer buffer);

#ifdef __cplusplus
}
#endif

#endif
