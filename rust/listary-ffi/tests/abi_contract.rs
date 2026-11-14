use listary_core::{
    ActivationSource, CORE_SCHEMA_VERSION, CoreConfig, CustomAction, CustomCommand,
    FacetCountsRequest, FileFacetCategory, FileSearchRequest, FileSearchSortField, FilterPreset,
    FilterTarget, ItemKind, ModifiedTimeFacet, Priority, PriorityRule, RefreshPolicy,
    RootAvailability, RootKind, SearchContext, SearchRequest, SortDirection, WebSearchProvider,
    WebSearchRequest, WebSuggestionProvider,
};
use listary_ffi::{
    LISTARY_CORE_API_VERSION, ListaryBuffer, ListaryCoreHandle, ListaryStatus, listary_buffer_free,
    listary_core_add_root, listary_core_api_version, listary_core_cancel, listary_core_close,
    listary_core_exclude_path, listary_core_expand_action, listary_core_expand_command,
    listary_core_facet_counts, listary_core_file_search, listary_core_flush,
    listary_core_get_diagnostics, listary_core_get_favorites, listary_core_get_item,
    listary_core_hide_recommendation, listary_core_list_actions, listary_core_list_commands,
    listary_core_list_filters, listary_core_list_priority_rules, listary_core_list_roots,
    listary_core_list_web_search_providers, listary_core_open, listary_core_put_action,
    listary_core_put_command, listary_core_put_favorites, listary_core_put_filter,
    listary_core_put_root, listary_core_rebuild_all, listary_core_recommendations,
    listary_core_reconcile_paths, listary_core_reconcile_root, listary_core_record_activation,
    listary_core_remove_action, listary_core_remove_command, listary_core_remove_filter,
    listary_core_remove_root, listary_core_replace_priority_rules,
    listary_core_replace_web_search_providers, listary_core_resolve_favorites,
    listary_core_resolve_web_search, listary_core_search, listary_core_toggle_pin,
};
use serde_json::{Value, json};
use std::{collections::BTreeSet, fs, path::PathBuf, ptr, slice};
use tempfile::TempDir;

type Handle = *mut ListaryCoreHandle;
type JsonResponseFn =
    unsafe extern "C" fn(Handle, *const u8, usize, *mut ListaryBuffer, *mut ListaryBuffer) -> i32;
type JsonNoResponseFn = unsafe extern "C" fn(Handle, *const u8, usize, *mut ListaryBuffer) -> i32;
type RequestIdJsonResponseFn = unsafe extern "C" fn(
    Handle,
    u64,
    *const u8,
    usize,
    *mut ListaryBuffer,
    *mut ListaryBuffer,
) -> i32;

struct HandleGuard(Handle);

impl Drop for HandleGuard {
    fn drop(&mut self) {
        unsafe { listary_core_close(self.0) };
    }
}

unsafe fn take_buffer(buffer: ListaryBuffer) -> Vec<u8> {
    if buffer.ptr.is_null() || buffer.len == 0 {
        return Vec::new();
    }
    let bytes = unsafe { slice::from_raw_parts(buffer.ptr, buffer.len) }.to_vec();
    unsafe { listary_buffer_free(buffer) };
    bytes
}

fn assert_ok(status: i32, error: ListaryBuffer) {
    if status == ListaryStatus::Ok as i32 {
        assert!(
            error.ptr.is_null() && error.len == 0,
            "successful FFI call unexpectedly populated the error buffer"
        );
        return;
    }
    let message = String::from_utf8_lossy(&unsafe { take_buffer(error) }).into_owned();
    panic!("FFI call failed with status {status}: {message}");
}

fn keys(value: &Value) -> BTreeSet<String> {
    value
        .as_object()
        .expect("JSON object")
        .keys()
        .cloned()
        .collect()
}

fn assert_keys(value: &Value, expected: &[&str]) {
    let expected: BTreeSet<String> = expected.iter().map(|key| (*key).to_string()).collect();
    assert_eq!(keys(value), expected);
}

fn normalized_c_header(value: &str) -> String {
    value.split_whitespace().collect::<Vec<_>>().join(" ")
}

#[test]
fn abi_v9_symbols_status_values_and_header_remain_in_lockstep() {
    assert_eq!(LISTARY_CORE_API_VERSION, 9);
    assert_eq!(listary_core_api_version(), 9);
    assert_eq!(ListaryStatus::Ok as i32, 0);
    assert_eq!(ListaryStatus::InvalidArgument as i32, 1);
    assert_eq!(ListaryStatus::AlreadyOpen as i32, 2);
    assert_eq!(ListaryStatus::Cancelled as i32, 3);
    assert_eq!(ListaryStatus::NotFound as i32, 4);
    assert_eq!(ListaryStatus::Io as i32, 5);
    assert_eq!(ListaryStatus::Index as i32, 6);
    assert_eq!(ListaryStatus::Serialization as i32, 7);
    assert_eq!(ListaryStatus::StaleCatalog as i32, 8);
    assert_eq!(ListaryStatus::Panic as i32, 255);
    assert_eq!(
        std::mem::size_of::<ListaryBuffer>(),
        std::mem::size_of::<*mut u8>() + std::mem::size_of::<usize>()
    );
    assert_eq!(
        std::mem::align_of::<ListaryBuffer>(),
        std::mem::align_of::<usize>()
    );

    let _: extern "C" fn() -> u32 = listary_core_api_version;
    let _: unsafe extern "C" fn(*const u8, usize, *mut Handle, *mut ListaryBuffer) -> i32 =
        listary_core_open;
    let _: unsafe extern "C" fn(Handle) = listary_core_close;
    let _: RequestIdJsonResponseFn = listary_core_search;
    let _: RequestIdJsonResponseFn = listary_core_file_search;
    let _: RequestIdJsonResponseFn = listary_core_facet_counts;
    let _: unsafe extern "C" fn(Handle, u64) -> i32 = listary_core_cancel;
    let _: JsonResponseFn = listary_core_add_root;
    let _: unsafe extern "C" fn(Handle, *mut ListaryBuffer, *mut ListaryBuffer) -> i32 =
        listary_core_list_roots;
    let _: JsonNoResponseFn = listary_core_put_root;
    let _: JsonNoResponseFn = listary_core_remove_root;
    let _: unsafe extern "C" fn(Handle, *mut ListaryBuffer, *mut ListaryBuffer) -> i32 =
        listary_core_list_filters;
    let _: JsonResponseFn = listary_core_put_filter;
    let _: JsonNoResponseFn = listary_core_remove_filter;
    let _: unsafe extern "C" fn(Handle, *mut ListaryBuffer, *mut ListaryBuffer) -> i32 =
        listary_core_list_commands;
    let _: JsonResponseFn = listary_core_put_command;
    let _: JsonNoResponseFn = listary_core_remove_command;
    let _: JsonResponseFn = listary_core_expand_command;
    let _: unsafe extern "C" fn(Handle, *mut ListaryBuffer, *mut ListaryBuffer) -> i32 =
        listary_core_list_actions;
    let _: JsonResponseFn = listary_core_put_action;
    let _: JsonNoResponseFn = listary_core_remove_action;
    let _: JsonResponseFn = listary_core_expand_action;
    let _: unsafe extern "C" fn(Handle, *mut ListaryBuffer, *mut ListaryBuffer) -> i32 =
        listary_core_get_favorites;
    let _: JsonResponseFn = listary_core_put_favorites;
    let _: JsonResponseFn = listary_core_toggle_pin;
    let _: JsonResponseFn = listary_core_resolve_favorites;
    let _: unsafe extern "C" fn(Handle, *mut ListaryBuffer, *mut ListaryBuffer) -> i32 =
        listary_core_list_web_search_providers;
    let _: JsonResponseFn = listary_core_replace_web_search_providers;
    let _: unsafe extern "C" fn(Handle, *mut ListaryBuffer, *mut ListaryBuffer) -> i32 =
        listary_core_list_priority_rules;
    let _: JsonResponseFn = listary_core_replace_priority_rules;
    let _: JsonResponseFn = listary_core_resolve_web_search;
    let _: RequestIdJsonResponseFn = listary_core_reconcile_root;
    let _: RequestIdJsonResponseFn = listary_core_reconcile_paths;
    let _: JsonNoResponseFn = listary_core_record_activation;
    let _: JsonResponseFn = listary_core_recommendations;
    let _: JsonNoResponseFn = listary_core_hide_recommendation;
    let _: JsonNoResponseFn = listary_core_exclude_path;
    let _: JsonResponseFn = listary_core_get_item;
    let _: unsafe extern "C" fn(Handle, *mut ListaryBuffer, *mut ListaryBuffer) -> i32 =
        listary_core_get_diagnostics;
    let _: unsafe extern "C" fn(Handle, *mut ListaryBuffer) -> i32 = listary_core_flush;
    let _: unsafe extern "C" fn(Handle, u64, *mut ListaryBuffer, *mut ListaryBuffer) -> i32 =
        listary_core_rebuild_all;
    let _: unsafe extern "C" fn(ListaryBuffer) = listary_buffer_free;

    let header = include_str!("../include/listary_core.h");
    let normalized_header = normalized_c_header(header);
    assert!(
        normalized_header.contains("#define LISTARY_CORE_API_VERSION 9u"),
        "C header must publish the compile-time ABI v9 version"
    );
    let symbols = [
        "listary_core_api_version",
        "listary_core_open",
        "listary_core_close",
        "listary_core_search",
        "listary_core_file_search",
        "listary_core_facet_counts",
        "listary_core_cancel",
        "listary_core_add_root",
        "listary_core_list_roots",
        "listary_core_put_root",
        "listary_core_remove_root",
        "listary_core_list_filters",
        "listary_core_put_filter",
        "listary_core_remove_filter",
        "listary_core_list_commands",
        "listary_core_put_command",
        "listary_core_remove_command",
        "listary_core_expand_command",
        "listary_core_list_actions",
        "listary_core_put_action",
        "listary_core_remove_action",
        "listary_core_expand_action",
        "listary_core_get_favorites",
        "listary_core_put_favorites",
        "listary_core_toggle_pin",
        "listary_core_resolve_favorites",
        "listary_core_list_web_search_providers",
        "listary_core_replace_web_search_providers",
        "listary_core_list_priority_rules",
        "listary_core_replace_priority_rules",
        "listary_core_resolve_web_search",
        "listary_core_reconcile_root",
        "listary_core_reconcile_paths",
        "listary_core_record_activation",
        "listary_core_recommendations",
        "listary_core_hide_recommendation",
        "listary_core_exclude_path",
        "listary_core_get_item",
        "listary_core_get_diagnostics",
        "listary_core_flush",
        "listary_core_rebuild_all",
        "listary_buffer_free",
    ];
    assert_eq!(symbols.len(), 42, "ABI v9 public symbol count drifted");
    for symbol in symbols {
        assert!(
            header.contains(&format!("{symbol}(")),
            "C header lost ABI v9 symbol {symbol}"
        );
    }
    for declaration in [
        "LISTARY_STATUS_OK = 0",
        "LISTARY_STATUS_INVALID_ARGUMENT = 1",
        "LISTARY_STATUS_ALREADY_OPEN = 2",
        "LISTARY_STATUS_CANCELLED = 3",
        "LISTARY_STATUS_NOT_FOUND = 4",
        "LISTARY_STATUS_IO = 5",
        "LISTARY_STATUS_INDEX = 6",
        "LISTARY_STATUS_SERIALIZATION = 7",
        "LISTARY_STATUS_STALE_CATALOG = 8",
        "LISTARY_STATUS_PANIC = 255",
    ] {
        assert!(
            header.contains(declaration),
            "C header changed ABI v9 status declaration {declaration:?}"
        );
    }

    for declaration in [
        "uint32_t listary_core_api_version(void);",
        "int32_t listary_core_open( const uint8_t *config_json, size_t config_len, ListaryCoreHandle **out_handle, ListaryBuffer *out_error);",
        "void listary_core_close(ListaryCoreHandle *handle);",
        "int32_t listary_core_search( ListaryCoreHandle *handle, uint64_t request_id, const uint8_t *request_json, size_t request_len, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_file_search( ListaryCoreHandle *handle, uint64_t request_id, const uint8_t *request_json, size_t request_len, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_facet_counts( ListaryCoreHandle *handle, uint64_t request_id, const uint8_t *request_json, size_t request_len, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_cancel( ListaryCoreHandle *handle, uint64_t request_id);",
        "int32_t listary_core_add_root( ListaryCoreHandle *handle, const uint8_t *request_json, size_t request_len, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_list_roots( ListaryCoreHandle *handle, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_put_root( ListaryCoreHandle *handle, const uint8_t *root_json, size_t root_len, ListaryBuffer *out_error);",
        "int32_t listary_core_remove_root( ListaryCoreHandle *handle, const uint8_t *request_json, size_t request_len, ListaryBuffer *out_error);",
        "int32_t listary_core_list_filters( ListaryCoreHandle *handle, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_put_filter( ListaryCoreHandle *handle, const uint8_t *filter_json, size_t filter_len, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_remove_filter( ListaryCoreHandle *handle, const uint8_t *request_json, size_t request_len, ListaryBuffer *out_error);",
        "int32_t listary_core_list_commands( ListaryCoreHandle *handle, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_put_command( ListaryCoreHandle *handle, const uint8_t *command_json, size_t command_len, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_remove_command( ListaryCoreHandle *handle, const uint8_t *request_json, size_t request_len, ListaryBuffer *out_error);",
        "int32_t listary_core_expand_command( ListaryCoreHandle *handle, const uint8_t *request_json, size_t request_len, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_list_actions( ListaryCoreHandle *handle, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_put_action( ListaryCoreHandle *handle, const uint8_t *action_json, size_t action_len, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_remove_action( ListaryCoreHandle *handle, const uint8_t *request_json, size_t request_len, ListaryBuffer *out_error);",
        "int32_t listary_core_expand_action( ListaryCoreHandle *handle, const uint8_t *request_json, size_t request_len, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_get_favorites( ListaryCoreHandle *handle, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_put_favorites( ListaryCoreHandle *handle, const uint8_t *menu_json, size_t menu_len, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_toggle_pin( ListaryCoreHandle *handle, const uint8_t *request_json, size_t request_len, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_resolve_favorites( ListaryCoreHandle *handle, const uint8_t *request_json, size_t request_len, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_list_web_search_providers( ListaryCoreHandle *handle, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_replace_web_search_providers( ListaryCoreHandle *handle, const uint8_t *providers_json, size_t providers_len, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_list_priority_rules( ListaryCoreHandle *handle, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_replace_priority_rules( ListaryCoreHandle *handle, const uint8_t *rules_json, size_t rules_len, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_resolve_web_search( ListaryCoreHandle *handle, const uint8_t *request_json, size_t request_len, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_reconcile_root( ListaryCoreHandle *handle, uint64_t request_id, const uint8_t *root_id_utf8, size_t root_id_len, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_reconcile_paths( ListaryCoreHandle *handle, uint64_t request_id, const uint8_t *request_json, size_t request_len, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_record_activation( ListaryCoreHandle *handle, const uint8_t *request_json, size_t request_len, ListaryBuffer *out_error);",
        "int32_t listary_core_recommendations( ListaryCoreHandle *handle, const uint8_t *request_json, size_t request_len, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_hide_recommendation( ListaryCoreHandle *handle, const uint8_t *request_json, size_t request_len, ListaryBuffer *out_error);",
        "int32_t listary_core_exclude_path( ListaryCoreHandle *handle, const uint8_t *request_json, size_t request_len, ListaryBuffer *out_error);",
        "int32_t listary_core_get_item( ListaryCoreHandle *handle, const uint8_t *request_json, size_t request_len, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_get_diagnostics( ListaryCoreHandle *handle, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "int32_t listary_core_flush( ListaryCoreHandle *handle, ListaryBuffer *out_error);",
        "int32_t listary_core_rebuild_all( ListaryCoreHandle *handle, uint64_t request_id, ListaryBuffer *out_response, ListaryBuffer *out_error);",
        "void listary_buffer_free(ListaryBuffer buffer);",
    ] {
        assert!(
            normalized_header.contains(declaration),
            "C header prototype drifted from the Rust ABI v9 contract: {declaration}"
        );
    }
}

#[test]
fn mutating_response_calls_validate_output_pointer_before_side_effects() {
    let state = TempDir::new().unwrap();
    let files = TempDir::new().unwrap();
    fs::write(files.path().join("should-not-index.txt"), b"fixture").unwrap();

    let config = serde_json::to_vec(&json!({
        "state_dir": state.path(),
        "log_max_bytes": 1048576,
        "log_generations": 2
    }))
    .unwrap();
    let mut handle: Handle = ptr::null_mut();
    let mut error = ListaryBuffer::default();
    let status =
        unsafe { listary_core_open(config.as_ptr(), config.len(), &mut handle, &mut error) };
    assert_ok(status, error);
    let handle = HandleGuard(handle);

    let add_request = serde_json::to_vec(&json!({
        "path": files.path(),
        "kind": "local"
    }))
    .unwrap();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_add_root(
            handle.0,
            add_request.as_ptr(),
            add_request.len(),
            ptr::null_mut(),
            &mut error,
        )
    };
    assert_eq!(status, ListaryStatus::InvalidArgument as i32);
    let message = String::from_utf8(unsafe { take_buffer(error) }).unwrap();
    assert!(message.contains("out_response is null"));

    let mut roots_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe { listary_core_list_roots(handle.0, &mut roots_response, &mut error) };
    assert_ok(status, error);
    let roots: Value = serde_json::from_slice(&unsafe { take_buffer(roots_response) }).unwrap();
    assert!(roots.as_array().unwrap().is_empty());

    let command_request = serde_json::to_vec(&CustomCommand::new(
        "nullout",
        "Null output",
        "/usr/bin/tool",
    ))
    .unwrap();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_put_command(
            handle.0,
            command_request.as_ptr(),
            command_request.len(),
            ptr::null_mut(),
            &mut error,
        )
    };
    assert_eq!(status, ListaryStatus::InvalidArgument as i32);
    let message = String::from_utf8(unsafe { take_buffer(error) }).unwrap();
    assert!(message.contains("out_response is null"));
    let mut commands_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status =
        unsafe { listary_core_list_commands(handle.0, &mut commands_response, &mut error) };
    assert_ok(status, error);
    let commands: Value =
        serde_json::from_slice(&unsafe { take_buffer(commands_response) }).unwrap();
    assert!(commands.as_array().unwrap().is_empty());

    let action_request =
        serde_json::to_vec(&CustomAction::new("Null output", "/usr/bin/tool")).unwrap();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_put_action(
            handle.0,
            action_request.as_ptr(),
            action_request.len(),
            ptr::null_mut(),
            &mut error,
        )
    };
    assert_eq!(status, ListaryStatus::InvalidArgument as i32);
    let message = String::from_utf8(unsafe { take_buffer(error) }).unwrap();
    assert!(message.contains("out_response is null"));
    let mut actions_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe { listary_core_list_actions(handle.0, &mut actions_response, &mut error) };
    assert_ok(status, error);
    let actions: Value = serde_json::from_slice(&unsafe { take_buffer(actions_response) }).unwrap();
    assert!(actions.as_array().unwrap().is_empty());

    let favorites_request = serde_json::to_vec(&json!({
        "items": [{
            "type": "folder",
            "id": uuid::Uuid::new_v4(),
            "title": "Should not persist",
            "path": "/tmp/should-not-persist"
        }]
    }))
    .unwrap();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_put_favorites(
            handle.0,
            favorites_request.as_ptr(),
            favorites_request.len(),
            ptr::null_mut(),
            &mut error,
        )
    };
    assert_eq!(status, ListaryStatus::InvalidArgument as i32);
    let message = String::from_utf8(unsafe { take_buffer(error) }).unwrap();
    assert!(message.contains("out_response is null"));
    let mut favorites_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status =
        unsafe { listary_core_get_favorites(handle.0, &mut favorites_response, &mut error) };
    assert_ok(status, error);
    let favorites: Value =
        serde_json::from_slice(&unsafe { take_buffer(favorites_response) }).unwrap();
    assert_eq!(favorites, json!({"items": [], "pins": []}));

    let web_search_request = serde_json::to_vec(&json!([{
        "id": uuid::Uuid::new_v4(),
        "enabled": true,
        "keyword": "web",
        "hotkey": null,
        "title": "Should not persist",
        "url_template": "https://example.test/?q={query}",
        "suggestion_provider": {"kind": "none"},
        "icon_path": null
    }]))
    .unwrap();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_replace_web_search_providers(
            handle.0,
            web_search_request.as_ptr(),
            web_search_request.len(),
            ptr::null_mut(),
            &mut error,
        )
    };
    assert_eq!(status, ListaryStatus::InvalidArgument as i32);
    let message = String::from_utf8(unsafe { take_buffer(error) }).unwrap();
    assert!(message.contains("out_response is null"));
    let mut providers_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_list_web_search_providers(handle.0, &mut providers_response, &mut error)
    };
    assert_ok(status, error);
    let providers: Value =
        serde_json::from_slice(&unsafe { take_buffer(providers_response) }).unwrap();
    assert!(providers.as_array().unwrap().is_empty());

    let priority_request = serde_json::to_vec(&vec![PriorityRule::path(
        "/tmp/should-not-persist",
        true,
        Priority::High,
    )])
    .unwrap();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_replace_priority_rules(
            handle.0,
            priority_request.as_ptr(),
            priority_request.len(),
            ptr::null_mut(),
            &mut error,
        )
    };
    assert_eq!(status, ListaryStatus::InvalidArgument as i32);
    let message = String::from_utf8(unsafe { take_buffer(error) }).unwrap();
    assert!(message.contains("out_response is null"));
    let mut priority_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status =
        unsafe { listary_core_list_priority_rules(handle.0, &mut priority_response, &mut error) };
    assert_ok(status, error);
    let priority_rules: Vec<PriorityRule> =
        serde_json::from_slice(&unsafe { take_buffer(priority_response) }).unwrap();
    assert!(priority_rules.is_empty());

    let mut add_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_add_root(
            handle.0,
            add_request.as_ptr(),
            add_request.len(),
            &mut add_response,
            &mut error,
        )
    };
    assert_ok(status, error);
    let add_json: Value = serde_json::from_slice(&unsafe { take_buffer(add_response) }).unwrap();
    let root_id = add_json["root_id"].as_str().unwrap().to_string();

    let mut diagnostics_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status =
        unsafe { listary_core_get_diagnostics(handle.0, &mut diagnostics_response, &mut error) };
    assert_ok(status, error);
    let diagnostics_before: Value =
        serde_json::from_slice(&unsafe { take_buffer(diagnostics_response) }).unwrap();
    assert_eq!(diagnostics_before["item_count"].as_u64(), Some(0));

    let mut roots_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe { listary_core_list_roots(handle.0, &mut roots_response, &mut error) };
    assert_ok(status, error);
    let roots_before: Value =
        serde_json::from_slice(&unsafe { take_buffer(roots_response) }).unwrap();
    let last_reconciled_before = roots_before[0]["last_reconciled_unix_ms"].clone();

    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_reconcile_root(
            handle.0,
            77,
            root_id.as_ptr(),
            root_id.len(),
            ptr::null_mut(),
            &mut error,
        )
    };
    assert_eq!(status, ListaryStatus::InvalidArgument as i32);
    let message = String::from_utf8(unsafe { take_buffer(error) }).unwrap();
    assert!(message.contains("out_response is null"));
    assert_eq!(
        unsafe { listary_core_cancel(handle.0, 77) },
        ListaryStatus::NotFound as i32
    );

    let mut diagnostics_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status =
        unsafe { listary_core_get_diagnostics(handle.0, &mut diagnostics_response, &mut error) };
    assert_ok(status, error);
    let diagnostics_after: Value =
        serde_json::from_slice(&unsafe { take_buffer(diagnostics_response) }).unwrap();
    assert_eq!(diagnostics_after["item_count"].as_u64(), Some(0));

    let mut roots_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe { listary_core_list_roots(handle.0, &mut roots_response, &mut error) };
    assert_ok(status, error);
    let roots_after: Value =
        serde_json::from_slice(&unsafe { take_buffer(roots_response) }).unwrap();
    assert_eq!(
        roots_after[0]["last_reconciled_unix_ms"],
        last_reconciled_before
    );

    let mut reconcile_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_reconcile_root(
            handle.0,
            77,
            root_id.as_ptr(),
            root_id.len(),
            &mut reconcile_response,
            &mut error,
        )
    };
    assert_ok(status, error);
    let _: Value = serde_json::from_slice(&unsafe { take_buffer(reconcile_response) }).unwrap();

    let mut diagnostics_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status =
        unsafe { listary_core_get_diagnostics(handle.0, &mut diagnostics_response, &mut error) };
    assert_ok(status, error);
    let diagnostics_final: Value =
        serde_json::from_slice(&unsafe { take_buffer(diagnostics_response) }).unwrap();
    assert_eq!(diagnostics_final["item_count"].as_u64(), Some(1));
}

#[test]
fn facet_count_output_validation_does_not_leak_request_registration() {
    const REQUEST_ID: u64 = 8_806;
    let state = TempDir::new().unwrap();
    let config = serde_json::to_vec(&CoreConfig::new(state.path().to_path_buf())).unwrap();
    let mut handle: Handle = ptr::null_mut();
    let mut error = ListaryBuffer::default();
    let status =
        unsafe { listary_core_open(config.as_ptr(), config.len(), &mut handle, &mut error) };
    assert_ok(status, error);
    let handle = HandleGuard(handle);

    let request = serde_json::to_vec(&FacetCountsRequest {
        now_unix_ms: 1_800_000_000_000,
        ..FacetCountsRequest::default()
    })
    .unwrap();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_facet_counts(
            handle.0,
            REQUEST_ID,
            request.as_ptr(),
            request.len(),
            ptr::null_mut(),
            &mut error,
        )
    };
    assert_eq!(status, ListaryStatus::InvalidArgument as i32);
    let message = String::from_utf8(unsafe { take_buffer(error) }).unwrap();
    assert!(message.contains("out_response is null"));
    assert_eq!(
        unsafe { listary_core_cancel(handle.0, REQUEST_ID) },
        ListaryStatus::NotFound as i32
    );

    for _ in 0..2 {
        let mut response = ListaryBuffer::default();
        let mut error = ListaryBuffer::default();
        let status = unsafe {
            listary_core_facet_counts(
                handle.0,
                REQUEST_ID,
                request.as_ptr(),
                request.len(),
                &mut response,
                &mut error,
            )
        };
        assert_ok(status, error);
        let value: Value = serde_json::from_slice(&unsafe { take_buffer(response) }).unwrap();
        assert_eq!(value["matched_total"].as_u64(), Some(0));
        assert_eq!(
            unsafe { listary_core_cancel(handle.0, REQUEST_ID) },
            ListaryStatus::NotFound as i32
        );
    }
}

#[test]
fn json_request_and_enum_contract_is_exact_for_swift_codable_models() {
    let request = SearchRequest {
        query: "contractscene".into(),
        limit: 10,
        candidate_limit: 64,
        include_unavailable: true,
        root_ids: Vec::new(),
        kinds: vec![ItemKind::File],
        categories: vec![FileFacetCategory::Video],
        modified_time: Some(ModifiedTimeFacet::Last30Days),
        context: SearchContext {
            current_folder: Some("X:/contract".into()),
            contextual: true,
        },
        now_unix_ms: 1_800_000_000_000,
        timezone_offset_minutes: -240,
    };
    let value = serde_json::to_value(&request).unwrap();
    assert_keys(
        &value,
        &[
            "query",
            "limit",
            "candidate_limit",
            "include_unavailable",
            "root_ids",
            "kinds",
            "categories",
            "modified_time",
            "context",
            "now_unix_ms",
            "timezone_offset_minutes",
        ],
    );
    assert_keys(&value["context"], &["current_folder", "contextual"]);
    assert_eq!(value["kinds"], json!(["file"]));
    assert_eq!(value["categories"], json!(["video"]));
    assert_eq!(value["modified_time"], json!("last_30_days"));

    let file_search = FileSearchRequest {
        query: "contractscene".into(),
        include_unavailable: false,
        categories: vec![FileFacetCategory::Video],
        modified_time: Some(ModifiedTimeFacet::Last7Days),
        now_unix_ms: 1_800_000_000_000,
        timezone_offset_minutes: -240,
        offset: 200,
        limit: 100,
        sort_field: FileSearchSortField::Modified,
        sort_direction: SortDirection::Ascending,
        expected_catalog_generation: Some(42),
        ..FileSearchRequest::default()
    };
    let file_search_json = serde_json::to_value(file_search).unwrap();
    assert_keys(
        &file_search_json,
        &[
            "query",
            "include_unavailable",
            "root_ids",
            "categories",
            "modified_time",
            "now_unix_ms",
            "timezone_offset_minutes",
            "offset",
            "limit",
            "sort_field",
            "sort_direction",
            "expected_catalog_generation",
        ],
    );
    assert_eq!(file_search_json["sort_field"], json!("modified"));
    assert_eq!(file_search_json["sort_direction"], json!("ascending"));
    assert_eq!(file_search_json["expected_catalog_generation"], json!(42));
    assert_eq!(
        serde_json::to_value([
            FileSearchSortField::Relevance,
            FileSearchSortField::Name,
            FileSearchSortField::Path,
            FileSearchSortField::Kind,
            FileSearchSortField::Modified,
            FileSearchSortField::Size,
        ])
        .unwrap(),
        json!(["relevance", "name", "path", "kind", "modified", "size"])
    );
    assert_eq!(
        serde_json::to_value([SortDirection::Ascending, SortDirection::Descending]).unwrap(),
        json!(["ascending", "descending"])
    );

    assert_eq!(
        serde_json::to_value([
            ItemKind::File,
            ItemKind::Folder,
            ItemKind::App,
            ItemKind::Package,
        ])
        .unwrap(),
        json!(["file", "folder", "app", "package"])
    );
    assert_eq!(
        serde_json::to_value([
            FileFacetCategory::Folder,
            FileFacetCategory::Document,
            FileFacetCategory::Picture,
            FileFacetCategory::Video,
            FileFacetCategory::Audio,
            FileFacetCategory::Other,
        ])
        .unwrap(),
        json!(["folder", "document", "picture", "video", "audio", "other"])
    );
    assert_eq!(
        serde_json::to_value([
            ModifiedTimeFacet::Last1Day,
            ModifiedTimeFacet::Last7Days,
            ModifiedTimeFacet::Last30Days,
            ModifiedTimeFacet::Last365Days,
            ModifiedTimeFacet::Older,
        ])
        .unwrap(),
        json!([
            "last_1_day",
            "last_7_days",
            "last_30_days",
            "last_365_days",
            "older"
        ])
    );
    let facets = FacetCountsRequest {
        query: "scene".into(),
        include_unavailable: false,
        root_ids: Vec::new(),
        categories: vec![FileFacetCategory::Video, FileFacetCategory::Audio],
        modified_time: Some(ModifiedTimeFacet::Last7Days),
        now_unix_ms: 1_800_000_000_000,
        timezone_offset_minutes: -240,
    };
    let facets_json = serde_json::to_value(facets).unwrap();
    assert_keys(
        &facets_json,
        &[
            "query",
            "include_unavailable",
            "root_ids",
            "categories",
            "modified_time",
            "now_unix_ms",
            "timezone_offset_minutes",
        ],
    );
    assert_eq!(facets_json["categories"], json!(["video", "audio"]));
    assert_eq!(facets_json["modified_time"], json!("last_7_days"));
    assert_eq!(
        serde_json::to_value([
            RootKind::Local,
            RootKind::Network,
            RootKind::Removable,
            RootKind::Cloud,
        ])
        .unwrap(),
        json!(["local", "network", "removable", "cloud"])
    );
    assert_eq!(
        serde_json::to_value([
            RootAvailability::Available,
            RootAvailability::Unavailable,
            RootAvailability::Stale,
        ])
        .unwrap(),
        json!(["available", "unavailable", "stale"])
    );
    assert_eq!(
        serde_json::to_value([
            Priority::Ignored,
            Priority::Low,
            Priority::Normal,
            Priority::High,
        ])
        .unwrap(),
        json!(["ignored", "low", "normal", "high"])
    );
    assert_eq!(
        serde_json::to_value([
            ActivationSource::Launcher,
            ActivationSource::FileSearch,
            ActivationSource::FileManager,
            ActivationSource::QuickSwitch,
            ActivationSource::Api,
        ])
        .unwrap(),
        json!([
            "launcher",
            "file_search",
            "file_manager",
            "quick_switch",
            "api"
        ])
    );
    assert_eq!(
        serde_json::to_value(RefreshPolicy::Never).unwrap(),
        json!("never")
    );
    assert_eq!(
        serde_json::to_value(RefreshPolicy::Automatic).unwrap(),
        json!("automatic")
    );
    assert_eq!(
        serde_json::to_value(RefreshPolicy::Interval { seconds: 30 }).unwrap(),
        json!({"interval": {"seconds": 30}})
    );
    assert_eq!(
        serde_json::to_value([
            FilterTarget::Any,
            FilterTarget::Files,
            FilterTarget::Folders,
        ])
        .unwrap(),
        json!(["any", "files", "folders"])
    );

    let filter = FilterPreset::new("movie", "Movies");
    assert_keys(
        &serde_json::to_value(filter).unwrap(),
        &[
            "id",
            "keyword",
            "name",
            "extra_query",
            "search_paths",
            "target",
            "extensions",
        ],
    );

    let mut web = WebSearchProvider::new("web", "Web", "https://example.test/?q={query}");
    web.hotkey = Some("Ctrl+W".into());
    web.suggestion_provider = WebSuggestionProvider::Custom {
        url_template: "https://suggest.test/?q={query}".into(),
    };
    web.icon_path = Some("/tmp/web.png".into());
    let web_json = serde_json::to_value(&web).unwrap();
    assert_keys(
        &web_json,
        &[
            "id",
            "enabled",
            "keyword",
            "hotkey",
            "title",
            "url_template",
            "suggestion_provider",
            "icon_path",
        ],
    );
    assert_eq!(
        web_json["suggestion_provider"],
        json!({
            "kind": "custom",
            "url_template": "https://suggest.test/?q={query}"
        })
    );
    assert_eq!(
        serde_json::to_value(WebSearchRequest::Input {
            input: "web query".into(),
        })
        .unwrap(),
        json!({"mode": "input", "input": "web query"})
    );
    assert_eq!(
        serde_json::to_value(WebSearchRequest::Provider {
            provider_id: web.id,
            query: "direct query".into(),
        })
        .unwrap(),
        json!({
            "mode": "provider",
            "provider_id": web.id,
            "query": "direct query"
        })
    );

    let config = CoreConfig {
        state_dir: PathBuf::from("X:/state"),
        log_max_bytes: 1_048_576,
        log_generations: 2,
    };
    assert_keys(
        &serde_json::to_value(config).unwrap(),
        &["state_dir", "log_max_bytes", "log_generations"],
    );
}

#[test]
fn ffi_json_responses_match_abi_v9_contract_and_publish_schema_version() {
    assert_eq!(CORE_SCHEMA_VERSION, 1);
    let state = TempDir::new().unwrap();
    let files = TempDir::new().unwrap();
    let file = files.path().join("ContractScene.mov");
    fs::write(&file, b"contract").unwrap();

    let config = serde_json::to_vec(&json!({
        "state_dir": state.path(),
        "log_max_bytes": 1048576,
        "log_generations": 2
    }))
    .unwrap();
    let mut handle: Handle = ptr::null_mut();
    let mut error = ListaryBuffer::default();
    let status =
        unsafe { listary_core_open(config.as_ptr(), config.len(), &mut handle, &mut error) };
    assert_ok(status, error);
    assert!(!handle.is_null());
    let handle = HandleGuard(handle);

    let add_request = serde_json::to_vec(&json!({
        "path": files.path(),
        "kind": "local"
    }))
    .unwrap();
    let mut add_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_add_root(
            handle.0,
            add_request.as_ptr(),
            add_request.len(),
            &mut add_response,
            &mut error,
        )
    };
    assert_ok(status, error);
    let add_json: Value = serde_json::from_slice(&unsafe { take_buffer(add_response) }).unwrap();
    assert_keys(&add_json, &["root_id"]);
    let root_id = add_json["root_id"].as_str().unwrap().to_string();

    let mut roots_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe { listary_core_list_roots(handle.0, &mut roots_response, &mut error) };
    assert_ok(status, error);
    let roots_json: Value =
        serde_json::from_slice(&unsafe { take_buffer(roots_response) }).unwrap();
    let root = &roots_json.as_array().unwrap()[0];
    assert_keys(
        root,
        &[
            "id",
            "path",
            "kind",
            "availability",
            "refresh_policy",
            "priority",
            "enabled",
            "last_reconciled_unix_ms",
            "last_error",
        ],
    );
    assert_eq!(root["kind"], "local");
    assert_eq!(root["availability"], "available");
    assert_eq!(root["refresh_policy"], "automatic");
    assert_eq!(root["priority"], "normal");

    let mut reconcile_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_reconcile_root(
            handle.0,
            41,
            root_id.as_ptr(),
            root_id.len(),
            &mut reconcile_response,
            &mut error,
        )
    };
    assert_ok(status, error);
    let _: Value = serde_json::from_slice(&unsafe { take_buffer(reconcile_response) }).unwrap();

    let filter_request = serde_json::to_vec(&json!({
        "id": uuid::Uuid::new_v4(),
        "keyword": "movie",
        "name": "MOV files",
        "extra_query": "contractscene",
        "search_paths": [files.path()],
        "target": "files",
        "extensions": [".MOV"]
    }))
    .unwrap();
    let mut filter_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_put_filter(
            handle.0,
            filter_request.as_ptr(),
            filter_request.len(),
            &mut filter_response,
            &mut error,
        )
    };
    assert_ok(status, error);
    let filter_json: Value =
        serde_json::from_slice(&unsafe { take_buffer(filter_response) }).unwrap();
    assert_eq!(filter_json["keyword"], "movie");
    assert_eq!(filter_json["extensions"], json!(["mov"]));
    let filter_id = filter_json["id"].as_str().unwrap().to_string();

    let mut filters_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe { listary_core_list_filters(handle.0, &mut filters_response, &mut error) };
    assert_ok(status, error);
    let filters_json: Value =
        serde_json::from_slice(&unsafe { take_buffer(filters_response) }).unwrap();
    assert_eq!(filters_json.as_array().unwrap().len(), 1);

    let mut command = CustomCommand::new("project", "Project tool", "/usr/bin/tool");
    command.arguments = vec!["--query={query}".into(), "{current_folder}".into()];
    command.working_directory = Some("{current_folder}".into());
    let command_request = serde_json::to_vec(&command).unwrap();
    let mut command_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_put_command(
            handle.0,
            command_request.as_ptr(),
            command_request.len(),
            &mut command_response,
            &mut error,
        )
    };
    assert_ok(status, error);
    let command_json: Value =
        serde_json::from_slice(&unsafe { take_buffer(command_response) }).unwrap();
    let command_id = command_json["id"].as_str().unwrap().to_string();
    assert_eq!(command_json["keyword"], "project");

    let mut commands_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status =
        unsafe { listary_core_list_commands(handle.0, &mut commands_response, &mut error) };
    assert_ok(status, error);
    let commands_json: Value =
        serde_json::from_slice(&unsafe { take_buffer(commands_response) }).unwrap();
    assert_eq!(commands_json.as_array().unwrap().len(), 1);

    let favorites_menu = json!({
        "items": [
            {
                "type": "folder",
                "id": uuid::Uuid::new_v4(),
                "title": "Projects",
                "path": "/Users/test/Projects"
            },
            {
                "type": "submenu",
                "id": uuid::Uuid::new_v4(),
                "title": "Tools",
                "items": [
                    {"type": "separator", "id": uuid::Uuid::new_v4()},
                    {
                        "type": "command",
                        "id": uuid::Uuid::new_v4(),
                        "command_id": command_id
                    }
                ]
            },
            {
                "type": "dynamic_group",
                "id": uuid::Uuid::new_v4(),
                "title": null,
                "kind": "recent_folders",
                "limit": 5
            },
            {
                "type": "dynamic_group",
                "id": uuid::Uuid::new_v4(),
                "title": "Open folders",
                "kind": "open_folders",
                "limit": 5
            }
        ],
        "pins": [
            {
                "id": uuid::Uuid::new_v4(),
                "title": "Contract Scene",
                "path": file
            }
        ]
    });
    let favorites_request = serde_json::to_vec(&favorites_menu).unwrap();
    let mut favorites_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_put_favorites(
            handle.0,
            favorites_request.as_ptr(),
            favorites_request.len(),
            &mut favorites_response,
            &mut error,
        )
    };
    assert_ok(status, error);
    let put_favorites: Value =
        serde_json::from_slice(&unsafe { take_buffer(favorites_response) }).unwrap();
    assert_eq!(put_favorites, favorites_menu);

    let mut favorites_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status =
        unsafe { listary_core_get_favorites(handle.0, &mut favorites_response, &mut error) };
    assert_ok(status, error);
    let persisted_favorites: Value =
        serde_json::from_slice(&unsafe { take_buffer(favorites_response) }).unwrap();
    assert_eq!(persisted_favorites, favorites_menu);

    let pinned_recommendation_request = serde_json::to_vec(&json!({
        "now_unix_ms": 1_800_000_000_050i64,
        "limit": 10
    }))
    .unwrap();
    let mut pinned_recommendation_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_recommendations(
            handle.0,
            pinned_recommendation_request.as_ptr(),
            pinned_recommendation_request.len(),
            &mut pinned_recommendation_response,
            &mut error,
        )
    };
    assert_ok(status, error);
    let pinned_recommendations: Value =
        serde_json::from_slice(&unsafe { take_buffer(pinned_recommendation_response) }).unwrap();
    assert_eq!(
        pinned_recommendations[0]["item"]["name"],
        "ContractScene.mov"
    );
    assert_eq!(
        pinned_recommendations[0]["activation_count"].as_u64(),
        Some(0)
    );

    let resolve_favorites_request = serde_json::to_vec(&json!({
        "now_unix_ms": 1_800_000_000_000i64,
        "open_folders": [
            {"path": "/Users/test/Open", "title": "Open Project"},
            {"path": "/Users/test/Open/", "title": "Duplicate Open Project"}
        ]
    }))
    .unwrap();
    let mut resolved_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_resolve_favorites(
            handle.0,
            resolve_favorites_request.as_ptr(),
            resolve_favorites_request.len(),
            &mut resolved_response,
            &mut error,
        )
    };
    assert_ok(status, error);
    let resolved_favorites: Value =
        serde_json::from_slice(&unsafe { take_buffer(resolved_response) }).unwrap();
    let resolved_items = resolved_favorites["items"].as_array().unwrap();
    assert_eq!(resolved_items[0]["source"], "favorite");
    assert_eq!(resolved_items[1]["type"], "submenu");
    assert_eq!(resolved_items[1]["items"][1]["type"], "command");
    assert_eq!(resolved_items[1]["items"][1]["command_id"], command_id);
    let open_group = resolved_items
        .iter()
        .find(|item| item["title"] == "Open folders")
        .unwrap();
    assert_eq!(open_group["items"].as_array().unwrap().len(), 1);
    assert_eq!(open_group["items"][0]["path"], "/Users/test/Open");
    assert_eq!(open_group["items"][0]["source"], "open_folder");

    let provider_one = uuid::Uuid::new_v4();
    let provider_two = uuid::Uuid::new_v4();
    let web_search_providers = json!([
        {
            "id": provider_one,
            "enabled": true,
            "keyword": "Store",
            "hotkey": "Ctrl+1",
            "title": "First store",
            "url_template": "https://first.test/?q={query}",
            "suggestion_provider": {"kind": "google"},
            "icon_path": null
        },
        {
            "id": provider_two,
            "enabled": true,
            "keyword": "store",
            "hotkey": null,
            "title": "Second store",
            "url_template": "https://second.test/?q={query}",
            "suggestion_provider": {
                "kind": "custom",
                "url_template": "https://suggest.test/?term={query}"
            },
            "icon_path": null
        }
    ]);
    let replace_web_search_request = serde_json::to_vec(&web_search_providers).unwrap();
    let mut web_search_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_replace_web_search_providers(
            handle.0,
            replace_web_search_request.as_ptr(),
            replace_web_search_request.len(),
            &mut web_search_response,
            &mut error,
        )
    };
    assert_ok(status, error);
    let canonical_web_search: Value =
        serde_json::from_slice(&unsafe { take_buffer(web_search_response) }).unwrap();
    assert_eq!(canonical_web_search[0]["keyword"], "store");
    assert_eq!(canonical_web_search[1]["keyword"], "store");
    assert_eq!(canonical_web_search[0]["id"], provider_one.to_string());
    assert_eq!(canonical_web_search[1]["id"], provider_two.to_string());

    let mut web_search_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_list_web_search_providers(handle.0, &mut web_search_response, &mut error)
    };
    assert_ok(status, error);
    let persisted_web_search: Value =
        serde_json::from_slice(&unsafe { take_buffer(web_search_response) }).unwrap();
    assert_eq!(persisted_web_search, canonical_web_search);

    let priority_rule = PriorityRule::path("/Users/test/Projects", true, Priority::High);
    let priority_request = serde_json::to_vec(&vec![priority_rule.clone()]).unwrap();
    let mut priority_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_replace_priority_rules(
            handle.0,
            priority_request.as_ptr(),
            priority_request.len(),
            &mut priority_response,
            &mut error,
        )
    };
    assert_ok(status, error);
    let canonical_priority_rules: Vec<PriorityRule> =
        serde_json::from_slice(&unsafe { take_buffer(priority_response) }).unwrap();
    assert_eq!(canonical_priority_rules, vec![priority_rule]);

    let mut priority_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status =
        unsafe { listary_core_list_priority_rules(handle.0, &mut priority_response, &mut error) };
    assert_ok(status, error);
    let persisted_priority_rules: Vec<PriorityRule> =
        serde_json::from_slice(&unsafe { take_buffer(priority_response) }).unwrap();
    assert_eq!(persisted_priority_rules, canonical_priority_rules);

    let resolve_web_search_request = serde_json::to_vec(&json!({
        "mode": "input",
        "input": "STORE café / test"
    }))
    .unwrap();
    let mut web_search_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_resolve_web_search(
            handle.0,
            resolve_web_search_request.as_ptr(),
            resolve_web_search_request.len(),
            &mut web_search_response,
            &mut error,
        )
    };
    assert_ok(status, error);
    let resolved_web_search: Value =
        serde_json::from_slice(&unsafe { take_buffer(web_search_response) }).unwrap();
    assert_eq!(resolved_web_search["query"], "café / test");
    assert_eq!(resolved_web_search["matches"].as_array().unwrap().len(), 2);
    assert_eq!(
        resolved_web_search["matches"][0]["url"],
        "https://first.test/?q=caf%C3%A9%20%2F%20test"
    );
    assert_eq!(
        resolved_web_search["matches"][1]["suggestion"]["url"],
        "https://suggest.test/?term=caf%C3%A9%20%2F%20test"
    );

    let expand_command_request = serde_json::to_vec(&json!({
        "command_id": command_id,
        "query": "snow fall",
        "current_folder": "/Users/test/Scenes"
    }))
    .unwrap();
    let mut expand_command_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_expand_command(
            handle.0,
            expand_command_request.as_ptr(),
            expand_command_request.len(),
            &mut expand_command_response,
            &mut error,
        )
    };
    assert_ok(status, error);
    let expanded_command: Value =
        serde_json::from_slice(&unsafe { take_buffer(expand_command_response) }).unwrap();
    assert_eq!(
        expanded_command["arguments"],
        json!(["--query=snow fall", "/Users/test/Scenes"])
    );
    assert_eq!(expanded_command["working_directory"], "/Users/test/Scenes");

    let mut action = CustomAction::new("Process result", "/usr/bin/tool");
    action.arguments = vec!["{action_path}".into(), "{action_path:parent}".into()];
    let action_request = serde_json::to_vec(&action).unwrap();
    let mut action_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_put_action(
            handle.0,
            action_request.as_ptr(),
            action_request.len(),
            &mut action_response,
            &mut error,
        )
    };
    assert_ok(status, error);
    let action_json: Value =
        serde_json::from_slice(&unsafe { take_buffer(action_response) }).unwrap();
    let action_id = action_json["id"].as_str().unwrap().to_string();

    let mut actions_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe { listary_core_list_actions(handle.0, &mut actions_response, &mut error) };
    assert_ok(status, error);
    let actions_json: Value =
        serde_json::from_slice(&unsafe { take_buffer(actions_response) }).unwrap();
    assert_eq!(actions_json.as_array().unwrap().len(), 1);

    let expand_action_request = serde_json::to_vec(&json!({
        "action_id": action_id,
        "action_path": "/Users/test/Scenes/clip.mov"
    }))
    .unwrap();
    let mut expand_action_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_expand_action(
            handle.0,
            expand_action_request.as_ptr(),
            expand_action_request.len(),
            &mut expand_action_response,
            &mut error,
        )
    };
    assert_ok(status, error);
    let expanded_action: Value =
        serde_json::from_slice(&unsafe { take_buffer(expand_action_response) }).unwrap();
    assert_eq!(
        expanded_action["arguments"],
        json!(["/Users/test/Scenes/clip.mov", "/Users/test/Scenes"])
    );

    let search_request = serde_json::to_vec(&json!({
        "query": "movie:",
        "limit": 10,
        "candidate_limit": 64,
        "include_unavailable": true,
        "root_ids": [],
        "kinds": [],
        "context": {
            "current_folder": null,
            "contextual": false
        },
        "now_unix_ms": 1800000000000i64,
        "timezone_offset_minutes": 0
    }))
    .unwrap();
    let mut search_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_search(
            handle.0,
            42,
            search_request.as_ptr(),
            search_request.len(),
            &mut search_response,
            &mut error,
        )
    };
    assert_ok(status, error);
    let search_json: Value =
        serde_json::from_slice(&unsafe { take_buffer(search_response) }).unwrap();
    assert_keys(
        &search_json,
        &[
            "results",
            "parsed",
            "considered_candidates",
            "cancelled",
            "elapsed_micros",
        ],
    );
    assert_keys(
        &search_json["parsed"],
        &[
            "positive_terms",
            "negative_terms",
            "include_extensions",
            "exclude_extensions",
            "modified_unix_ms",
            "size_bytes",
            "unparsed_operators",
        ],
    );
    let first_term = &search_json["parsed"]["positive_terms"][0];
    assert_keys(first_term, &["text", "exact", "path_term"]);
    let result = &search_json["results"][0];
    assert_keys(result, &["item", "score"]);
    assert_keys(
        &result["item"],
        &[
            "id",
            "root_id",
            "path",
            "parent_path",
            "name",
            "extension",
            "kind",
            "size",
            "modified_unix_ms",
            "available",
            "priority",
            "stable_file_id",
            "preview_hint",
        ],
    );
    assert_eq!(result["item"]["kind"], "file");
    assert_eq!(result["item"]["priority"], "normal");
    assert_keys(
        &result["score"],
        &[
            "text_tier",
            "text_quality",
            "priority_boost",
            "context_boost",
            "history_boost",
            "recency_boost",
            "combined",
        ],
    );

    let facet_request = serde_json::to_vec(&json!({
        "query": "",
        "include_unavailable": true,
        "root_ids": [],
        "categories": ["video"],
        "modified_time": null,
        "now_unix_ms": 1_800_000_000_000i64,
        "timezone_offset_minutes": 0
    }))
    .unwrap();
    let mut facet_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_facet_counts(
            handle.0,
            142,
            facet_request.as_ptr(),
            facet_request.len(),
            &mut facet_response,
            &mut error,
        )
    };
    assert_ok(status, error);
    let facets: Value = serde_json::from_slice(&unsafe { take_buffer(facet_response) }).unwrap();
    assert_keys(
        &facets,
        &[
            "matched_total",
            "categories",
            "modified_time",
            "considered_candidates",
            "elapsed_micros",
        ],
    );
    assert_eq!(facets["matched_total"].as_u64(), Some(1));
    assert_eq!(facets["considered_candidates"].as_u64(), Some(1));
    assert_eq!(facets["categories"].as_array().unwrap().len(), 6);
    assert_eq!(facets["modified_time"].as_array().unwrap().len(), 5);
    assert_eq!(facets["categories"][0]["category"], "folder");
    assert_eq!(facets["categories"][3]["category"], "video");
    assert_eq!(facets["categories"][3]["count"].as_u64(), Some(1));
    assert_keys(&facets["categories"][3], &["category", "count"]);
    assert_eq!(facets["modified_time"][0]["range"], "last_1_day");
    assert_keys(&facets["modified_time"][0], &["range", "count"]);

    let item_id = result["item"]["id"].as_u64().unwrap();
    let toggle_pin_request = serde_json::to_vec(&json!({"item_id": item_id})).unwrap();
    let mut toggle_pin_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_toggle_pin(
            handle.0,
            toggle_pin_request.as_ptr(),
            toggle_pin_request.len(),
            &mut toggle_pin_response,
            &mut error,
        )
    };
    assert_ok(status, error);
    let unpinned: Value =
        serde_json::from_slice(&unsafe { take_buffer(toggle_pin_response) }).unwrap();
    assert_eq!(unpinned, json!({"item_id": item_id, "pinned": false}));

    let mut favorites_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status =
        unsafe { listary_core_get_favorites(handle.0, &mut favorites_response, &mut error) };
    assert_ok(status, error);
    let after_unpin: Value =
        serde_json::from_slice(&unsafe { take_buffer(favorites_response) }).unwrap();
    assert_eq!(after_unpin["items"], favorites_menu["items"]);
    assert_eq!(after_unpin["pins"], json!([]));

    let mut toggle_pin_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_toggle_pin(
            handle.0,
            toggle_pin_request.as_ptr(),
            toggle_pin_request.len(),
            &mut toggle_pin_response,
            &mut error,
        )
    };
    assert_ok(status, error);
    let repinned: Value =
        serde_json::from_slice(&unsafe { take_buffer(toggle_pin_response) }).unwrap();
    assert_eq!(repinned, json!({"item_id": item_id, "pinned": true}));

    let mut favorites_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status =
        unsafe { listary_core_get_favorites(handle.0, &mut favorites_response, &mut error) };
    assert_ok(status, error);
    let after_repin: Value =
        serde_json::from_slice(&unsafe { take_buffer(favorites_response) }).unwrap();
    assert_eq!(after_repin["items"], favorites_menu["items"]);
    assert_eq!(after_repin["pins"].as_array().unwrap().len(), 1);
    assert_eq!(
        after_repin["pins"][0]["path"],
        favorites_menu["pins"][0]["path"]
    );
    assert_eq!(after_repin["pins"][0]["title"], "ContractScene.mov");

    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_toggle_pin(
            handle.0,
            toggle_pin_request.as_ptr(),
            toggle_pin_request.len(),
            ptr::null_mut(),
            &mut error,
        )
    };
    assert_eq!(status, ListaryStatus::InvalidArgument as i32);
    let message = String::from_utf8(unsafe { take_buffer(error) }).unwrap();
    assert!(message.contains("out_response is null"));

    let mut favorites_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status =
        unsafe { listary_core_get_favorites(handle.0, &mut favorites_response, &mut error) };
    assert_ok(status, error);
    let after_rejected_toggle: Value =
        serde_json::from_slice(&unsafe { take_buffer(favorites_response) }).unwrap();
    assert_eq!(after_rejected_toggle["pins"].as_array().unwrap().len(), 1);

    let legacy_activation = serde_json::to_vec(&json!({
        "item_id": item_id,
        "now_unix_ms": 1_800_000_000_100i64
    }))
    .unwrap();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_record_activation(
            handle.0,
            legacy_activation.as_ptr(),
            legacy_activation.len(),
            &mut error,
        )
    };
    assert_ok(status, error);

    let recommendation_request = serde_json::to_vec(&json!({
        "now_unix_ms": 1_800_000_000_200i64,
        "limit": 10
    }))
    .unwrap();
    let mut recommendation_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_recommendations(
            handle.0,
            recommendation_request.as_ptr(),
            recommendation_request.len(),
            &mut recommendation_response,
            &mut error,
        )
    };
    assert_ok(status, error);
    let recommendations: Value =
        serde_json::from_slice(&unsafe { take_buffer(recommendation_response) }).unwrap();
    assert_eq!(recommendations[0]["last_source"], "launcher");
    assert_eq!(recommendations[0]["activation_count"].as_u64(), Some(1));

    let incompatible_activation = serde_json::to_vec(&json!({
        "item_id": item_id,
        "now_unix_ms": 1_800_000_000_300i64,
        "source": "future_shell_mode"
    }))
    .unwrap();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_record_activation(
            handle.0,
            incompatible_activation.as_ptr(),
            incompatible_activation.len(),
            &mut error,
        )
    };
    assert_eq!(status, ListaryStatus::Serialization as i32);
    let message = String::from_utf8(unsafe { take_buffer(error) }).unwrap();
    assert!(!message.is_empty());

    let mut diagnostics_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status =
        unsafe { listary_core_get_diagnostics(handle.0, &mut diagnostics_response, &mut error) };
    assert_ok(status, error);
    let diagnostics: Value =
        serde_json::from_slice(&unsafe { take_buffer(diagnostics_response) }).unwrap();
    assert_keys(
        &diagnostics,
        &[
            "schema_version",
            "catalog_generation",
            "item_count",
            "root_count",
            "available_root_count",
            "unavailable_root_count",
            "index_bytes",
            "history_entries",
            "recovery_count",
            "last_recovery_path",
        ],
    );
    assert_eq!(
        diagnostics["schema_version"].as_u64(),
        Some(CORE_SCHEMA_VERSION as u64)
    );
    assert!(diagnostics["catalog_generation"].as_u64().is_some());

    let remove_filter_request = serde_json::to_vec(&json!({"filter_id": filter_id})).unwrap();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_remove_filter(
            handle.0,
            remove_filter_request.as_ptr(),
            remove_filter_request.len(),
            &mut error,
        )
    };
    assert_ok(status, error);

    let remove_command_request = serde_json::to_vec(&json!({"command_id": command_id})).unwrap();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_remove_command(
            handle.0,
            remove_command_request.as_ptr(),
            remove_command_request.len(),
            &mut error,
        )
    };
    assert_eq!(status, ListaryStatus::InvalidArgument as i32);
    let message = String::from_utf8(unsafe { take_buffer(error) }).unwrap();
    assert!(message.contains("referenced by favorites menu"));

    let empty_favorites = serde_json::to_vec(&json!({"items": []})).unwrap();
    let mut favorites_response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_put_favorites(
            handle.0,
            empty_favorites.as_ptr(),
            empty_favorites.len(),
            &mut favorites_response,
            &mut error,
        )
    };
    assert_ok(status, error);
    let cleared_favorites: Value =
        serde_json::from_slice(&unsafe { take_buffer(favorites_response) }).unwrap();
    assert_eq!(cleared_favorites, json!({"items": [], "pins": []}));

    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_remove_command(
            handle.0,
            remove_command_request.as_ptr(),
            remove_command_request.len(),
            &mut error,
        )
    };
    assert_ok(status, error);

    let remove_action_request = serde_json::to_vec(&json!({"action_id": action_id})).unwrap();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_remove_action(
            handle.0,
            remove_action_request.as_ptr(),
            remove_action_request.len(),
            &mut error,
        )
    };
    assert_ok(status, error);
}
