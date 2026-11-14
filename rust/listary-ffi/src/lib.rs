use listary_core::{
    ActionContext, ActivationSource, CancellationToken, CommandContext, Core, CoreConfig,
    CoreError, CustomAction, CustomCommand, FacetCountsRequest, FavoriteMenu, FileSearchRequest,
    FilterPreset, OpenFolderCandidate, PriorityRule, RootKind, RootRecord, SearchRequest,
    WebSearchProvider, WebSearchRequest,
};
use parking_lot::Mutex;
use serde::{Deserialize, Serialize};
use std::{
    collections::HashMap,
    ffi::c_void,
    fmt,
    panic::{AssertUnwindSafe, catch_unwind},
    path::PathBuf,
    ptr, slice, str,
};
use uuid::Uuid;

#[repr(C)]
pub struct ListaryBuffer {
    pub ptr: *mut u8,
    pub len: usize,
}

impl Default for ListaryBuffer {
    fn default() -> Self {
        Self {
            ptr: ptr::null_mut(),
            len: 0,
        }
    }
}

pub struct ListaryCoreHandle {
    core: Core,
    searches: Mutex<HashMap<u64, CancellationToken>>,
}

struct ActiveRequest<'a> {
    handle: &'a ListaryCoreHandle,
    request_id: u64,
    token: CancellationToken,
}

impl ActiveRequest<'_> {
    fn token(&self) -> &CancellationToken {
        &self.token
    }
}

impl Drop for ActiveRequest<'_> {
    fn drop(&mut self) {
        self.handle.searches.lock().remove(&self.request_id);
    }
}

#[repr(i32)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ListaryStatus {
    Ok = 0,
    InvalidArgument = 1,
    AlreadyOpen = 2,
    Cancelled = 3,
    NotFound = 4,
    Io = 5,
    Index = 6,
    Serialization = 7,
    StaleCatalog = 8,
    Panic = 255,
}

#[derive(Debug, Deserialize)]
struct AddRootRequest {
    path: PathBuf,
    kind: RootKind,
}

#[derive(Debug, Deserialize)]
struct RecordActivationRequest {
    item_id: u32,
    now_unix_ms: i64,
    #[serde(default)]
    source: ActivationSource,
}

#[derive(Debug, Deserialize)]
struct RootIdRequest {
    root_id: Uuid,
}

#[derive(Debug, Deserialize)]
struct FilterIdRequest {
    filter_id: Uuid,
}

#[derive(Debug, Deserialize)]
struct CommandIdRequest {
    command_id: Uuid,
}

#[derive(Debug, Deserialize)]
struct ExpandCommandRequest {
    command_id: Uuid,
    #[serde(default)]
    query: String,
    #[serde(default)]
    current_folder: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ActionIdRequest {
    action_id: Uuid,
}

#[derive(Debug, Deserialize)]
struct ExpandActionRequest {
    action_id: Uuid,
    action_path: String,
}

#[derive(Debug, Deserialize)]
struct ResolveFavoritesRequest {
    now_unix_ms: i64,
    #[serde(default)]
    open_folders: Vec<OpenFolderCandidate>,
}

#[derive(Debug, Deserialize)]
struct ReconcilePathsRequest {
    root_id: Uuid,
    paths: Vec<PathBuf>,
    #[serde(default)]
    expected_catalog_generation: Option<u64>,
}

#[derive(Debug, Deserialize)]
struct RecommendationRequest {
    now_unix_ms: i64,
    limit: usize,
}

#[derive(Debug, Deserialize)]
struct PathPrefixRequest {
    prefix: String,
}

#[derive(Debug, Deserialize)]
struct ItemIdRequest {
    item_id: u32,
}

#[derive(Debug, Serialize)]
struct TogglePinResponse {
    item_id: u32,
    pinned: bool,
}

#[derive(Debug, Serialize)]
struct AddRootResponse {
    root_id: Uuid,
}

pub const LISTARY_CORE_API_VERSION: u32 = 9;

/// Inclusive size limit for every caller-owned JSON or raw UTF-8 input buffer.
///
/// C callers must keep each `*_len` argument at or below this limit. The FFI validates the
/// declared length before reading the input pointer, constructing a Rust slice, or parsing UTF-8
/// or JSON. Eight MiB leaves ample room for coalesced dirty-path batches and complete settings
/// documents while bounding work and preventing untrusted lengths from reaching slice creation.
pub const LISTARY_CORE_MAX_INPUT_BYTES: usize = 8 * 1024 * 1024;

#[unsafe(no_mangle)]
pub extern "C" fn listary_core_api_version() -> u32 {
    LISTARY_CORE_API_VERSION
}

#[unsafe(no_mangle)]
/// Opens one core instance for the supplied state directory.
///
/// # Safety
/// `config_json` must reference `config_len` readable UTF-8 bytes for the duration of the call.
/// `out_handle` must be a valid writable pointer. `out_error`, when non-null, must be a valid
/// writable `ListaryBuffer`. A successful handle must later be passed exactly once to
/// `listary_core_close`.
pub unsafe extern "C" fn listary_core_open(
    config_json: *const u8,
    config_len: usize,
    out_handle: *mut *mut ListaryCoreHandle,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_handle_output(out_handle)? };
        let config: CoreConfig = unsafe { decode_json(config_json, config_len)? };
        let core = Core::open(config)?;
        let handle = Box::new(ListaryCoreHandle {
            core,
            searches: Mutex::new(HashMap::new()),
        });
        unsafe {
            out_handle.write(Box::into_raw(handle));
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Closes a handle returned by `listary_core_open`.
///
/// # Safety
/// `handle` must be null or a live handle returned by `listary_core_open`. It must not be used
/// concurrently while this function runs and must never be used again after this call.
pub unsafe extern "C" fn listary_core_close(handle: *mut ListaryCoreHandle) {
    if handle.is_null() {
        return;
    }
    let _ = catch_unwind(AssertUnwindSafe(|| unsafe {
        drop(Box::from_raw(handle));
    }));
}

#[unsafe(no_mangle)]
/// Executes a search synchronously on the calling thread.
///
/// # Safety
/// `handle` must be live. `request_json` must reference `request_len` readable UTF-8 bytes.
/// `out_response` must be writable. `out_error`, when non-null, must be writable. Returned
/// buffers are owned by the caller and must be released with `listary_buffer_free` exactly once.
pub unsafe extern "C" fn listary_core_search(
    handle: *mut ListaryCoreHandle,
    request_id: u64,
    request_json: *const u8,
    request_len: usize,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        let request: SearchRequest = unsafe { decode_json(request_json, request_len)? };
        let request_registration = register_request(handle, request_id)?;
        let response = handle.core.search(&request, request_registration.token())?;
        unsafe {
            *out_response = encode_json(&response)?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Executes exact, pageable File Search synchronously on the calling thread.
///
/// # Safety
/// `handle` must be live. `request_json` must reference `request_len` readable UTF-8 bytes.
/// `out_response` must be writable. `out_error`, when non-null, must be writable. Returned
/// buffers are owned by the caller and must be released with `listary_buffer_free` exactly once.
/// Run this potentially long operation on a worker queue; `listary_core_cancel` may be called
/// from another thread with the same nonzero `request_id`.
pub unsafe extern "C" fn listary_core_file_search(
    handle: *mut ListaryCoreHandle,
    request_id: u64,
    request_json: *const u8,
    request_len: usize,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        let request: FileSearchRequest = unsafe { decode_json(request_json, request_len)? };
        let request_registration = register_request(handle, request_id)?;
        let response = handle
            .core
            .file_search(&request, request_registration.token())?;
        unsafe {
            *out_response = encode_json(&response)?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Computes exact File Search facet counts synchronously on the calling thread.
///
/// # Safety
/// The handle must be live, request_json must reference request_len readable UTF-8 bytes,
/// and out_response must be writable. Returned buffers must be released exactly once with
/// listary_buffer_free.
pub unsafe extern "C" fn listary_core_facet_counts(
    handle: *mut ListaryCoreHandle,
    request_id: u64,
    request_json: *const u8,
    request_len: usize,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        let request: FacetCountsRequest = unsafe { decode_json(request_json, request_len)? };
        let request_registration = register_request(handle, request_id)?;
        let response = handle
            .core
            .facet_counts(&request, request_registration.token())?;
        unsafe {
            *out_response = encode_json(&response)?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Requests cancellation of a currently registered search/facet/reconcile/rebuild operation.
///
/// # Safety
/// `handle` must remain live for the duration of this call. The function may be called from a
/// different thread than the operation being cancelled.
pub unsafe extern "C" fn listary_core_cancel(
    handle: *mut ListaryCoreHandle,
    request_id: u64,
) -> i32 {
    if handle.is_null() {
        return ListaryStatus::InvalidArgument as i32;
    }
    let result = catch_unwind(AssertUnwindSafe(|| unsafe {
        let handle = &*handle;
        if let Some(token) = handle.searches.lock().get(&request_id) {
            token.cancel();
            ListaryStatus::Ok as i32
        } else {
            ListaryStatus::NotFound as i32
        }
    }));
    result.unwrap_or(ListaryStatus::Panic as i32)
}

#[unsafe(no_mangle)]
/// Adds an indexed root using a JSON request.
///
/// # Safety
/// `handle` must be live. `request_json` must reference `request_len` readable UTF-8 bytes.
/// `out_response` must be writable. `out_error`, when non-null, must be writable. Returned
/// buffers must be released with `listary_buffer_free` exactly once.
pub unsafe extern "C" fn listary_core_add_root(
    handle: *mut ListaryCoreHandle,
    request_json: *const u8,
    request_len: usize,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        let request: AddRootRequest = unsafe { decode_json(request_json, request_len)? };
        let root = handle.core.add_root(request.path, request.kind)?;
        unsafe {
            *out_response = encode_json(&AddRootResponse { root_id: root.id })?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Returns all configured indexed roots as UTF-8 JSON.
///
/// # Safety
/// handle must be live. out_response must be writable. out_error, when non-null, must be
/// writable. Returned buffers must be released with listary_buffer_free exactly once.
pub unsafe extern "C" fn listary_core_list_roots(
    handle: *mut ListaryCoreHandle,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        unsafe {
            *out_response = encode_json(&handle.core.list_roots())?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Replaces one root configuration using RootRecord JSON.
///
/// # Safety
/// handle must be live. root_json must reference root_len readable UTF-8 bytes.
/// out_error, when non-null, must be writable.
pub unsafe extern "C" fn listary_core_put_root(
    handle: *mut ListaryCoreHandle,
    root_json: *const u8,
    root_len: usize,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        let handle = unsafe { handle_ref(handle)? };
        let root: RootRecord = unsafe { decode_json(root_json, root_len)? };
        handle.core.put_root(root)
    })
}

#[unsafe(no_mangle)]
/// Removes one configured root and its indexed records.
///
/// # Safety
/// handle must be live. request_json must reference request_len readable UTF-8 bytes.
/// out_error, when non-null, must be writable.
pub unsafe extern "C" fn listary_core_remove_root(
    handle: *mut ListaryCoreHandle,
    request_json: *const u8,
    request_len: usize,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        let handle = unsafe { handle_ref(handle)? };
        let request: RootIdRequest = unsafe { decode_json(request_json, request_len)? };
        handle.core.remove_root(request.root_id)
    })
}

#[unsafe(no_mangle)]
/// Returns all saved custom search filters as UTF-8 JSON.
///
/// # Safety
/// handle must be live. out_response must be writable. out_error, when non-null, must be
/// writable. Returned buffers must be released with listary_buffer_free exactly once.
pub unsafe extern "C" fn listary_core_list_filters(
    handle: *mut ListaryCoreHandle,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        unsafe {
            *out_response = encode_json(&handle.core.list_filters())?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Creates or replaces one saved custom search filter and returns its canonical JSON form.
///
/// # Safety
/// handle must be live. filter_json must reference filter_len readable UTF-8 bytes.
/// out_response must be writable. out_error, when non-null, must be writable. Returned buffers
/// must be released with listary_buffer_free exactly once.
pub unsafe extern "C" fn listary_core_put_filter(
    handle: *mut ListaryCoreHandle,
    filter_json: *const u8,
    filter_len: usize,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        let preset: FilterPreset = unsafe { decode_json(filter_json, filter_len)? };
        let preset = handle.core.put_filter(preset)?;
        unsafe {
            *out_response = encode_json(&preset)?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Removes one saved custom search filter.
///
/// # Safety
/// handle must be live. request_json must reference request_len readable UTF-8 bytes.
/// out_error, when non-null, must be writable.
pub unsafe extern "C" fn listary_core_remove_filter(
    handle: *mut ListaryCoreHandle,
    request_json: *const u8,
    request_len: usize,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        let handle = unsafe { handle_ref(handle)? };
        let request: FilterIdRequest = unsafe { decode_json(request_json, request_len)? };
        handle.core.remove_filter(request.filter_id)
    })
}

#[unsafe(no_mangle)]
/// Returns all saved custom commands as UTF-8 JSON.
///
/// # Safety
/// handle must be live. out_response must be writable. out_error, when non-null, must be
/// writable. Returned buffers must be released with listary_buffer_free exactly once.
pub unsafe extern "C" fn listary_core_list_commands(
    handle: *mut ListaryCoreHandle,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        unsafe {
            *out_response = encode_json(&handle.core.list_commands())?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Creates or replaces one custom command and returns its canonical JSON form.
///
/// # Safety
/// handle must be live. command_json must reference command_len readable UTF-8 bytes.
/// out_response must be writable. out_error, when non-null, must be writable. Returned buffers
/// must be released with listary_buffer_free exactly once.
pub unsafe extern "C" fn listary_core_put_command(
    handle: *mut ListaryCoreHandle,
    command_json: *const u8,
    command_len: usize,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        let command: CustomCommand = unsafe { decode_json(command_json, command_len)? };
        let command = handle.core.put_command(command)?;
        unsafe {
            *out_response = encode_json(&command)?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Removes one custom command.
///
/// # Safety
/// handle must be live. request_json must reference request_len readable UTF-8 bytes.
/// out_error, when non-null, must be writable.
pub unsafe extern "C" fn listary_core_remove_command(
    handle: *mut ListaryCoreHandle,
    request_json: *const u8,
    request_len: usize,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        let handle = unsafe { handle_ref(handle)? };
        let request: CommandIdRequest = unsafe { decode_json(request_json, request_len)? };
        handle.core.remove_command(request.command_id)
    })
}

#[unsafe(no_mangle)]
/// Expands one command's documented placeholders into an argv-style invocation.
///
/// # Safety
/// handle must be live. request_json must reference request_len readable UTF-8 bytes.
/// out_response must be writable. out_error, when non-null, must be writable. Returned buffers
/// must be released with listary_buffer_free exactly once.
pub unsafe extern "C" fn listary_core_expand_command(
    handle: *mut ListaryCoreHandle,
    request_json: *const u8,
    request_len: usize,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        let request: ExpandCommandRequest = unsafe { decode_json(request_json, request_len)? };
        let invocation = handle.core.expand_command(
            request.command_id,
            &CommandContext {
                query: request.query,
                current_folder: request.current_folder,
            },
        )?;
        unsafe {
            *out_response = encode_json(&invocation)?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Returns all saved custom actions as UTF-8 JSON.
///
/// # Safety
/// handle must be live. out_response must be writable. out_error, when non-null, must be
/// writable. Returned buffers must be released with listary_buffer_free exactly once.
pub unsafe extern "C" fn listary_core_list_actions(
    handle: *mut ListaryCoreHandle,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        unsafe {
            *out_response = encode_json(&handle.core.list_actions())?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Creates or replaces one custom action and returns its canonical JSON form.
///
/// # Safety
/// handle must be live. action_json must reference action_len readable UTF-8 bytes.
/// out_response must be writable. out_error, when non-null, must be writable. Returned buffers
/// must be released with listary_buffer_free exactly once.
pub unsafe extern "C" fn listary_core_put_action(
    handle: *mut ListaryCoreHandle,
    action_json: *const u8,
    action_len: usize,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        let action: CustomAction = unsafe { decode_json(action_json, action_len)? };
        let action = handle.core.put_action(action)?;
        unsafe {
            *out_response = encode_json(&action)?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Removes one custom action.
///
/// # Safety
/// handle must be live. request_json must reference request_len readable UTF-8 bytes.
/// out_error, when non-null, must be writable.
pub unsafe extern "C" fn listary_core_remove_action(
    handle: *mut ListaryCoreHandle,
    request_json: *const u8,
    request_len: usize,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        let handle = unsafe { handle_ref(handle)? };
        let request: ActionIdRequest = unsafe { decode_json(request_json, request_len)? };
        handle.core.remove_action(request.action_id)
    })
}

#[unsafe(no_mangle)]
/// Expands one action's documented placeholders into an argv-style invocation.
///
/// # Safety
/// handle must be live. request_json must reference request_len readable UTF-8 bytes.
/// out_response must be writable. out_error, when non-null, must be writable. Returned buffers
/// must be released with listary_buffer_free exactly once.
pub unsafe extern "C" fn listary_core_expand_action(
    handle: *mut ListaryCoreHandle,
    request_json: *const u8,
    request_len: usize,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        let request: ExpandActionRequest = unsafe { decode_json(request_json, request_len)? };
        let invocation = handle.core.expand_action(
            request.action_id,
            &ActionContext {
                action_path: request.action_path,
            },
        )?;
        unsafe {
            *out_response = encode_json(&invocation)?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Returns the persisted ordered Favorites/Popup Menu tree as UTF-8 JSON.
///
/// # Safety
/// handle must be live. out_response must be writable. out_error, when non-null, must be
/// writable. Returned buffers must be released with listary_buffer_free exactly once.
pub unsafe extern "C" fn listary_core_get_favorites(
    handle: *mut ListaryCoreHandle,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        unsafe {
            *out_response = encode_json(&handle.core.get_favorites())?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Atomically replaces the ordered Favorites/Popup Menu tree and returns its canonical JSON form.
///
/// # Safety
/// handle must be live. menu_json must reference menu_len readable UTF-8 bytes.
/// out_response must be writable. out_error, when non-null, must be writable. Returned buffers
/// must be released with listary_buffer_free exactly once.
pub unsafe extern "C" fn listary_core_put_favorites(
    handle: *mut ListaryCoreHandle,
    menu_json: *const u8,
    menu_len: usize,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        let menu: FavoriteMenu = unsafe { decode_json(menu_json, menu_len)? };
        let menu = handle.core.put_favorites(menu)?;
        unsafe {
            *out_response = encode_json(&menu)?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Toggles the first-class pin for one current catalog item and returns the resulting pin state.
///
/// # Safety
/// handle must be live. request_json must reference request_len readable UTF-8 bytes.
/// out_response must be writable. out_error, when non-null, must be writable. Returned buffers
/// must be released with listary_buffer_free exactly once.
pub unsafe extern "C" fn listary_core_toggle_pin(
    handle: *mut ListaryCoreHandle,
    request_json: *const u8,
    request_len: usize,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        let request: ItemIdRequest = unsafe { decode_json(request_json, request_len)? };
        let pinned = handle.core.toggle_pin(request.item_id)?;
        unsafe {
            *out_response = encode_json(&TogglePinResponse {
                item_id: request.item_id,
                pinned,
            })?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Resolves Favorites dynamic groups using canonical history plus caller-supplied open folders.
///
/// # Safety
/// handle must be live. request_json must reference request_len readable UTF-8 bytes.
/// out_response must be writable. out_error, when non-null, must be writable. Returned buffers
/// must be released with listary_buffer_free exactly once.
pub unsafe extern "C" fn listary_core_resolve_favorites(
    handle: *mut ListaryCoreHandle,
    request_json: *const u8,
    request_len: usize,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        let request: ResolveFavoritesRequest = unsafe { decode_json(request_json, request_len)? };
        let menu = handle
            .core
            .resolve_favorites(request.now_unix_ms, request.open_folders)?;
        unsafe {
            *out_response = encode_json(&menu)?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Returns the persisted ordered Web Search provider list as UTF-8 JSON.
///
/// # Safety
/// handle must be live. out_response must be writable. out_error, when non-null, must be
/// writable. Returned buffers must be released with listary_buffer_free exactly once.
pub unsafe extern "C" fn listary_core_list_web_search_providers(
    handle: *mut ListaryCoreHandle,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        unsafe {
            *out_response = encode_json(&handle.core.list_web_search_providers())?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Replaces the complete ordered Web Search provider list and returns its canonical JSON form.
///
/// # Safety
/// handle must be live. providers_json must reference providers_len readable UTF-8 bytes.
/// out_response must be writable. out_error, when non-null, must be writable. Returned buffers
/// must be released with listary_buffer_free exactly once.
pub unsafe extern "C" fn listary_core_replace_web_search_providers(
    handle: *mut ListaryCoreHandle,
    providers_json: *const u8,
    providers_len: usize,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        let providers: Vec<WebSearchProvider> =
            unsafe { decode_json(providers_json, providers_len)? };
        let providers = handle.core.replace_web_search_providers(providers)?;
        unsafe {
            *out_response = encode_json(&providers)?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Returns the persisted ordered search-priority rule list as UTF-8 JSON.
///
/// # Safety
/// handle must be live. out_response must be writable. out_error, when non-null, must be
/// writable. Returned buffers must be released with listary_buffer_free exactly once.
pub unsafe extern "C" fn listary_core_list_priority_rules(
    handle: *mut ListaryCoreHandle,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        unsafe {
            *out_response = encode_json(&handle.core.list_priority_rules())?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Atomically replaces the complete ordered search-priority rule list.
///
/// # Safety
/// handle must be live. rules_json must reference rules_len readable UTF-8 bytes. out_response
/// must be writable. out_error, when non-null, must be writable. Returned buffers must be
/// released with listary_buffer_free exactly once.
pub unsafe extern "C" fn listary_core_replace_priority_rules(
    handle: *mut ListaryCoreHandle,
    rules_json: *const u8,
    rules_len: usize,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        let rules: Vec<PriorityRule> = unsafe { decode_json(rules_json, rules_len)? };
        let rules = handle.core.replace_priority_rules(rules)?;
        unsafe {
            *out_response = encode_json(&rules)?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Resolves a Web Search activation into percent-encoded browser/suggestion descriptors.
///
/// This call performs no browser launch and no network I/O.
///
/// # Safety
/// handle must be live. request_json must reference request_len readable UTF-8 bytes.
/// out_response must be writable. out_error, when non-null, must be writable. Returned buffers
/// must be released with listary_buffer_free exactly once.
pub unsafe extern "C" fn listary_core_resolve_web_search(
    handle: *mut ListaryCoreHandle,
    request_json: *const u8,
    request_len: usize,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        let request: WebSearchRequest = unsafe { decode_json(request_json, request_len)? };
        let resolution = handle.core.resolve_web_search(&request)?;
        unsafe {
            *out_response = encode_json(&resolution)?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Reconciles one root synchronously on the calling thread.
///
/// # Safety
/// `handle` must be live. `root_id_utf8` must reference `root_id_len` readable UTF-8 bytes.
/// `out_response` must be writable. `out_error`, when non-null, must be writable. Returned
/// buffers must be released with `listary_buffer_free` exactly once.
pub unsafe extern "C" fn listary_core_reconcile_root(
    handle: *mut ListaryCoreHandle,
    request_id: u64,
    root_id_utf8: *const u8,
    root_id_len: usize,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        let id_text = unsafe { decode_utf8(root_id_utf8, root_id_len)? };
        let root_id = Uuid::parse_str(id_text)
            .map_err(|e| CoreError::InvalidRequest(format!("invalid root id: {e}")))?;
        let request_registration = register_request(handle, request_id)?;
        let report = handle
            .core
            .reconcile_root(root_id, request_registration.token())?;
        unsafe {
            *out_response = encode_json(&report)?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Reconciles only the supplied changed paths or subtrees for one root.
///
/// # Safety
/// handle must be live. request_json must reference request_len readable UTF-8 bytes.
/// out_response must be writable. out_error, when non-null, must be writable. Returned
/// buffers must be released with listary_buffer_free exactly once.
pub unsafe extern "C" fn listary_core_reconcile_paths(
    handle: *mut ListaryCoreHandle,
    request_id: u64,
    request_json: *const u8,
    request_len: usize,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        let request: ReconcilePathsRequest = unsafe { decode_json(request_json, request_len)? };
        let request_registration = register_request(handle, request_id)?;
        let report = handle.core.reconcile_paths_at_generation(
            request.root_id,
            request.paths,
            request.expected_catalog_generation,
            request_registration.token(),
        )?;
        unsafe {
            *out_response = encode_json(&report)?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Records a result activation for history/recommendation ranking.
///
/// # Safety
/// `handle` must be live. `request_json` must reference `request_len` readable UTF-8 bytes.
/// `out_error`, when non-null, must be a valid writable `ListaryBuffer`.
pub unsafe extern "C" fn listary_core_record_activation(
    handle: *mut ListaryCoreHandle,
    request_json: *const u8,
    request_len: usize,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        let handle = unsafe { handle_ref(handle)? };
        let request: RecordActivationRequest = unsafe { decode_json(request_json, request_len)? };
        handle.core.record_activation_with_source(
            request.item_id,
            request.now_unix_ms,
            request.source,
        )
    })
}

#[unsafe(no_mangle)]
/// Returns zero-query recommendations as UTF-8 JSON.
///
/// # Safety
/// handle must be live. request_json must reference request_len readable UTF-8 bytes.
/// out_response must be writable. out_error, when non-null, must be writable. Returned
/// buffers must be released with listary_buffer_free exactly once.
pub unsafe extern "C" fn listary_core_recommendations(
    handle: *mut ListaryCoreHandle,
    request_json: *const u8,
    request_len: usize,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        let request: RecommendationRequest = unsafe { decode_json(request_json, request_len)? };
        unsafe {
            *out_response = encode_json(
                &handle
                    .core
                    .recommendations(request.now_unix_ms, request.limit),
            )?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Hides one item from zero-query recommendations.
///
/// # Safety
/// handle must be live. request_json must reference request_len readable UTF-8 bytes.
/// out_error, when non-null, must be writable.
pub unsafe extern "C" fn listary_core_hide_recommendation(
    handle: *mut ListaryCoreHandle,
    request_json: *const u8,
    request_len: usize,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        let handle = unsafe { handle_ref(handle)? };
        let request: ItemIdRequest = unsafe { decode_json(request_json, request_len)? };
        handle.core.hide_recommendation(request.item_id)
    })
}

#[unsafe(no_mangle)]
/// Excludes a path prefix from recommendations and normal search results.
///
/// # Safety
/// handle must be live. request_json must reference request_len readable UTF-8 bytes.
/// out_error, when non-null, must be writable.
pub unsafe extern "C" fn listary_core_exclude_path(
    handle: *mut ListaryCoreHandle,
    request_json: *const u8,
    request_len: usize,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        let handle = unsafe { handle_ref(handle)? };
        let request: PathPrefixRequest = unsafe { decode_json(request_json, request_len)? };
        handle.core.exclude_recommendation_path(request.prefix)
    })
}

#[unsafe(no_mangle)]
/// Returns one indexed item by stable catalog ID as UTF-8 JSON.
///
/// # Safety
/// handle must be live. request_json must reference request_len readable UTF-8 bytes.
/// out_response must be writable. out_error, when non-null, must be writable. Returned
/// buffers must be released with listary_buffer_free exactly once.
pub unsafe extern "C" fn listary_core_get_item(
    handle: *mut ListaryCoreHandle,
    request_json: *const u8,
    request_len: usize,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    item_lookup_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        let request_text = unsafe { decode_utf8(request_json, request_len)? };
        let request: ItemIdRequest = serde_json::from_str(request_text).map_err(|error| {
            CoreError::InvalidRequest(format!("invalid item lookup request: {error}"))
        })?;
        let item = handle
            .core
            .get_item(request.item_id)
            .ok_or(ItemLookupError::NotFound(request.item_id))?;
        unsafe {
            *out_response = encode_json(&item)?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Returns current core diagnostics as UTF-8 JSON.
///
/// # Safety
/// `handle` must be live. `out_response` must be writable. `out_error`, when non-null, must be
/// writable. Returned buffers must be released with `listary_buffer_free` exactly once.
pub unsafe extern "C" fn listary_core_get_diagnostics(
    handle: *mut ListaryCoreHandle,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        let diagnostics = handle.core.diagnostics()?;
        unsafe {
            *out_response = encode_json(&diagnostics)?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Flushes RocksDB and writes the generation-bound derived search-cache checkpoint.
///
/// # Safety
/// handle must be live. out_error, when non-null, must be writable. The caller must not close
/// the handle concurrently with this operation.
pub unsafe extern "C" fn listary_core_flush(
    handle: *mut ListaryCoreHandle,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        let handle = unsafe { handle_ref(handle)? };
        handle.core.flush()
    })
}

#[unsafe(no_mangle)]
/// Reconciles every enabled root, returning per-root results as UTF-8 JSON.
///
/// # Safety
/// `handle` must be live. `out_response` must be writable. `out_error`, when non-null, must be
/// writable. Returned buffers must be released with `listary_buffer_free` exactly once.
pub unsafe extern "C" fn listary_core_rebuild_all(
    handle: *mut ListaryCoreHandle,
    request_id: u64,
    out_response: *mut ListaryBuffer,
    out_error: *mut ListaryBuffer,
) -> i32 {
    ffi_guard(out_error, || {
        unsafe { initialize_response_output(out_response)? };
        let handle = unsafe { handle_ref(handle)? };
        let request_registration = register_request(handle, request_id)?;
        let reports = handle.core.rebuild_all(request_registration.token())?;
        unsafe {
            *out_response = encode_json(&reports)?;
        }
        Ok(())
    })
}

#[unsafe(no_mangle)]
/// Releases a buffer returned by this library.
///
/// # Safety
/// `buffer` must either be the zero/null buffer or a buffer returned by this library that has not
/// already been freed. Its `ptr` and `len` fields must not be modified by the caller.
pub unsafe extern "C" fn listary_buffer_free(buffer: ListaryBuffer) {
    if buffer.ptr.is_null() || buffer.len == 0 {
        return;
    }
    let _ = catch_unwind(AssertUnwindSafe(|| unsafe {
        let raw = ptr::slice_from_raw_parts_mut(buffer.ptr, buffer.len);
        drop(Box::<[u8]>::from_raw(raw));
    }));
}

trait BoundaryError: fmt::Display {
    fn status(&self) -> ListaryStatus;
}

impl BoundaryError for CoreError {
    fn status(&self) -> ListaryStatus {
        status_for_error(self)
    }
}

#[derive(Debug)]
enum ItemLookupError {
    Core(CoreError),
    NotFound(u32),
}

impl From<CoreError> for ItemLookupError {
    fn from(error: CoreError) -> Self {
        Self::Core(error)
    }
}

impl fmt::Display for ItemLookupError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Core(error) => error.fmt(formatter),
            Self::NotFound(item_id) => write!(formatter, "item not found: {item_id}"),
        }
    }
}

impl BoundaryError for ItemLookupError {
    fn status(&self) -> ListaryStatus {
        match self {
            Self::Core(error) => status_for_error(error),
            Self::NotFound(_) => ListaryStatus::NotFound,
        }
    }
}

fn ffi_guard<F>(out_error: *mut ListaryBuffer, operation: F) -> i32
where
    F: FnOnce() -> Result<(), CoreError>,
{
    boundary_guard(out_error, operation)
}

fn item_lookup_guard<F>(out_error: *mut ListaryBuffer, operation: F) -> i32
where
    F: FnOnce() -> Result<(), ItemLookupError>,
{
    boundary_guard(out_error, operation)
}

fn boundary_guard<F, E>(out_error: *mut ListaryBuffer, operation: F) -> i32
where
    F: FnOnce() -> Result<(), E>,
    E: BoundaryError,
{
    unsafe {
        initialize_optional_output(out_error);
    }
    match catch_unwind(AssertUnwindSafe(operation)) {
        Ok(Ok(())) => ListaryStatus::Ok as i32,
        Ok(Err(error)) => {
            unsafe {
                write_error(out_error, &error.to_string());
            }
            error.status() as i32
        }
        Err(_) => {
            unsafe {
                write_error(out_error, "panic contained at FFI boundary");
            }
            ListaryStatus::Panic as i32
        }
    }
}

fn status_for_error(error: &CoreError) -> ListaryStatus {
    match error {
        CoreError::AlreadyOpen => ListaryStatus::AlreadyOpen,
        CoreError::Cancelled => ListaryStatus::Cancelled,
        CoreError::StaleCatalog { .. } => ListaryStatus::StaleCatalog,
        CoreError::RootNotFound(_)
        | CoreError::FilterNotFound(_)
        | CoreError::CommandNotFound(_)
        | CoreError::ActionNotFound(_) => ListaryStatus::NotFound,
        CoreError::InvalidRequest(_) => ListaryStatus::InvalidArgument,
        CoreError::Filesystem { .. } => ListaryStatus::Io,
        CoreError::Index(_) => ListaryStatus::Index,
        CoreError::Serialization(_) => ListaryStatus::Serialization,
    }
}

fn register_request(
    handle: &ListaryCoreHandle,
    request_id: u64,
) -> Result<ActiveRequest<'_>, CoreError> {
    if request_id == 0 {
        return Err(CoreError::InvalidRequest(
            "request_id must be nonzero".into(),
        ));
    }
    let mut searches = handle.searches.lock();
    if searches.contains_key(&request_id) {
        return Err(CoreError::InvalidRequest(format!(
            "request_id is already active: {request_id}"
        )));
    }
    let token = CancellationToken::new();
    searches.insert(request_id, token.clone());
    Ok(ActiveRequest {
        handle,
        request_id,
        token,
    })
}

unsafe fn initialize_handle_output(
    out_handle: *mut *mut ListaryCoreHandle,
) -> Result<(), CoreError> {
    if out_handle.is_null() {
        return Err(CoreError::InvalidRequest("out_handle is null".into()));
    }
    unsafe {
        out_handle.write(ptr::null_mut());
    }
    Ok(())
}

unsafe fn initialize_response_output(out_response: *mut ListaryBuffer) -> Result<(), CoreError> {
    if out_response.is_null() {
        return Err(CoreError::InvalidRequest("out_response is null".into()));
    }
    unsafe {
        out_response.write(ListaryBuffer::default());
    }
    Ok(())
}

unsafe fn initialize_optional_output(output: *mut ListaryBuffer) {
    if !output.is_null() {
        unsafe {
            output.write(ListaryBuffer::default());
        }
    }
}

unsafe fn handle_ref<'a>(
    handle: *mut ListaryCoreHandle,
) -> Result<&'a ListaryCoreHandle, CoreError> {
    if handle.is_null() {
        return Err(CoreError::InvalidRequest("handle is null".into()));
    }
    Ok(unsafe { &*handle })
}

unsafe fn decode_json<T: for<'de> Deserialize<'de>>(
    ptr: *const u8,
    len: usize,
) -> Result<T, CoreError> {
    let text = unsafe { decode_utf8(ptr, len)? };
    serde_json::from_str(text).map_err(CoreError::from)
}

unsafe fn decode_utf8<'a>(ptr: *const u8, len: usize) -> Result<&'a str, CoreError> {
    if len > LISTARY_CORE_MAX_INPUT_BYTES {
        return Err(CoreError::InvalidRequest(format!(
            "input length {len} exceeds maximum {LISTARY_CORE_MAX_INPUT_BYTES} bytes"
        )));
    }
    if ptr.is_null() && len != 0 {
        return Err(CoreError::InvalidRequest("input pointer is null".into()));
    }
    let bytes = if len == 0 {
        &[]
    } else {
        unsafe { slice::from_raw_parts(ptr, len) }
    };
    str::from_utf8(bytes)
        .map_err(|e| CoreError::InvalidRequest(format!("input is not valid UTF-8: {e}")))
}

fn encode_json<T: Serialize>(value: &T) -> Result<ListaryBuffer, CoreError> {
    let bytes = serde_json::to_vec(value).map_err(CoreError::from)?;
    Ok(buffer_from_bytes(bytes))
}

fn buffer_from_bytes(bytes: Vec<u8>) -> ListaryBuffer {
    let mut boxed = bytes.into_boxed_slice();
    let buffer = ListaryBuffer {
        ptr: boxed.as_mut_ptr(),
        len: boxed.len(),
    };
    std::mem::forget(boxed);
    buffer
}

unsafe fn write_error(out_error: *mut ListaryBuffer, message: &str) {
    if out_error.is_null() {
        return;
    }
    unsafe {
        out_error.write(buffer_from_bytes(message.as_bytes().to_vec()));
    }
}

#[allow(dead_code)]
fn _assert_opaque_handle_not_c_void(_: *mut c_void) {}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};
    use std::fs;
    use tempfile::TempDir;

    unsafe fn take_buffer(buffer: ListaryBuffer) -> Vec<u8> {
        if buffer.ptr.is_null() || buffer.len == 0 {
            return Vec::new();
        }
        let bytes = unsafe { slice::from_raw_parts(buffer.ptr, buffer.len) }.to_vec();
        unsafe { listary_buffer_free(buffer) };
        bytes
    }

    #[test]
    fn c_abi_open_add_reconcile_search_diagnostics_and_close() {
        assert_eq!(listary_core_api_version(), 8);
        let state = TempDir::new().unwrap();
        let files = TempDir::new().unwrap();
        let original = files.path().join("MacPortReference.mov");
        fs::write(&original, b"x").unwrap();

        let config = serde_json::to_vec(&json!({
            "state_dir": state.path(),
            "log_max_bytes": 1048576,
            "log_generations": 2
        }))
        .unwrap();
        let mut handle: *mut ListaryCoreHandle = ptr::null_mut();
        let mut error = ListaryBuffer::default();
        let status =
            unsafe { listary_core_open(config.as_ptr(), config.len(), &mut handle, &mut error) };
        assert_eq!(status, ListaryStatus::Ok as i32);
        assert!(!handle.is_null());

        let add = serde_json::to_vec(&json!({
            "path": files.path(),
            "kind": "local"
        }))
        .unwrap();
        let mut add_response = ListaryBuffer::default();
        let status = unsafe {
            listary_core_add_root(
                handle,
                add.as_ptr(),
                add.len(),
                &mut add_response,
                &mut error,
            )
        };
        assert_eq!(status, ListaryStatus::Ok as i32);
        let add_json: Value =
            serde_json::from_slice(&unsafe { take_buffer(add_response) }).unwrap();
        let root_id_text = add_json["root_id"].as_str().unwrap().to_string();

        let mut roots_response = ListaryBuffer::default();
        let status = unsafe { listary_core_list_roots(handle, &mut roots_response, &mut error) };
        assert_eq!(status, ListaryStatus::Ok as i32);
        let roots_json: Value =
            serde_json::from_slice(&unsafe { take_buffer(roots_response) }).unwrap();
        assert_eq!(roots_json.as_array().unwrap().len(), 1);

        let mut reconcile_response = ListaryBuffer::default();
        let status = unsafe {
            listary_core_reconcile_root(
                handle,
                41,
                root_id_text.as_ptr(),
                root_id_text.len(),
                &mut reconcile_response,
                &mut error,
            )
        };
        assert_eq!(status, ListaryStatus::Ok as i32);
        let _: Value = serde_json::from_slice(&unsafe { take_buffer(reconcile_response) }).unwrap();

        let oversized_paths = (0..=listary_core::MAX_RECONCILE_PATHS)
            .map(|index| files.path().join(format!("oversized-{index}.txt")))
            .collect::<Vec<_>>();
        let oversized_reconcile = serde_json::to_vec(&json!({
            "root_id": root_id_text,
            "paths": oversized_paths
        }))
        .unwrap();
        let mut oversized_response = ListaryBuffer::default();
        let mut oversized_error = ListaryBuffer::default();
        let status = unsafe {
            listary_core_reconcile_paths(
                handle,
                43,
                oversized_reconcile.as_ptr(),
                oversized_reconcile.len(),
                &mut oversized_response,
                &mut oversized_error,
            )
        };
        assert_eq!(status, ListaryStatus::InvalidArgument as i32);
        assert!(unsafe { take_buffer(oversized_response) }.is_empty());
        let oversized_message = String::from_utf8(unsafe { take_buffer(oversized_error) }).unwrap();
        assert!(oversized_message.contains("batch limit"));

        let search_request = serde_json::to_vec(&SearchRequest {
            query: "macportreference".into(),
            ..SearchRequest::default()
        })
        .unwrap();
        let mut search_response = ListaryBuffer::default();
        let status = unsafe {
            listary_core_search(
                handle,
                42,
                search_request.as_ptr(),
                search_request.len(),
                &mut search_response,
                &mut error,
            )
        };
        assert_eq!(status, ListaryStatus::Ok as i32);
        let search_json: Value =
            serde_json::from_slice(&unsafe { take_buffer(search_response) }).unwrap();
        assert_eq!(search_json["results"].as_array().unwrap().len(), 1);
        let item_id = search_json["results"][0]["item"]["id"].as_u64().unwrap() as u32;

        let activation = serde_json::to_vec(&json!({
            "item_id": item_id,
            "now_unix_ms": 1800000000000i64
        }))
        .unwrap();
        let status = unsafe {
            listary_core_record_activation(
                handle,
                activation.as_ptr(),
                activation.len(),
                &mut error,
            )
        };
        assert_eq!(status, ListaryStatus::Ok as i32);

        let recommendation_request = serde_json::to_vec(&json!({
            "now_unix_ms": 1800000000100i64,
            "limit": 10
        }))
        .unwrap();
        let mut recommendations = ListaryBuffer::default();
        let status = unsafe {
            listary_core_recommendations(
                handle,
                recommendation_request.as_ptr(),
                recommendation_request.len(),
                &mut recommendations,
                &mut error,
            )
        };
        assert_eq!(status, ListaryStatus::Ok as i32);
        let recommendation_json: Value =
            serde_json::from_slice(&unsafe { take_buffer(recommendations) }).unwrap();
        assert_eq!(recommendation_json.as_array().unwrap().len(), 1);

        let item_request = serde_json::to_vec(&json!({ "item_id": item_id })).unwrap();
        let mut item_response = ListaryBuffer::default();
        let status = unsafe {
            listary_core_get_item(
                handle,
                item_request.as_ptr(),
                item_request.len(),
                &mut item_response,
                &mut error,
            )
        };
        assert_eq!(status, ListaryStatus::Ok as i32);
        let item_json: Value =
            serde_json::from_slice(&unsafe { take_buffer(item_response) }).unwrap();
        assert_eq!(item_json["name"], "MacPortReference.mov");

        let mut diagnostics_before_rename = ListaryBuffer::default();
        let status = unsafe {
            listary_core_get_diagnostics(handle, &mut diagnostics_before_rename, &mut error)
        };
        assert_eq!(status, ListaryStatus::Ok as i32);
        let diagnostic_before_rename: Value =
            serde_json::from_slice(&unsafe { take_buffer(diagnostics_before_rename) }).unwrap();
        let generation_before_rename = diagnostic_before_rename["catalog_generation"]
            .as_u64()
            .unwrap();

        let renamed = files.path().join("RenamedReference.mov");
        fs::rename(&original, &renamed).unwrap();
        let reconcile_paths_request = serde_json::to_vec(&json!({
            "root_id": root_id_text,
            "paths": [original, renamed],
            "expected_catalog_generation": generation_before_rename
        }))
        .unwrap();
        let mut reconcile_paths_response = ListaryBuffer::default();
        let status = unsafe {
            listary_core_reconcile_paths(
                handle,
                43,
                reconcile_paths_request.as_ptr(),
                reconcile_paths_request.len(),
                &mut reconcile_paths_response,
                &mut error,
            )
        };
        assert_eq!(status, ListaryStatus::Ok as i32);
        let _: Value =
            serde_json::from_slice(&unsafe { take_buffer(reconcile_paths_response) }).unwrap();

        let mut stale_reconcile_response = ListaryBuffer::default();
        let mut stale_reconcile_error = ListaryBuffer::default();
        let status = unsafe {
            listary_core_reconcile_paths(
                handle,
                45,
                reconcile_paths_request.as_ptr(),
                reconcile_paths_request.len(),
                &mut stale_reconcile_response,
                &mut stale_reconcile_error,
            )
        };
        assert_eq!(status, ListaryStatus::StaleCatalog as i32);
        assert!(!unsafe { take_buffer(stale_reconcile_error) }.is_empty());

        let renamed_search = serde_json::to_vec(&SearchRequest {
            query: "renamedreference".into(),
            ..SearchRequest::default()
        })
        .unwrap();
        let mut renamed_response = ListaryBuffer::default();
        let status = unsafe {
            listary_core_search(
                handle,
                44,
                renamed_search.as_ptr(),
                renamed_search.len(),
                &mut renamed_response,
                &mut error,
            )
        };
        assert_eq!(status, ListaryStatus::Ok as i32);
        let renamed_json: Value =
            serde_json::from_slice(&unsafe { take_buffer(renamed_response) }).unwrap();
        assert_eq!(renamed_json["results"].as_array().unwrap().len(), 1);

        let mut diagnostics = ListaryBuffer::default();
        let status = unsafe { listary_core_get_diagnostics(handle, &mut diagnostics, &mut error) };
        assert_eq!(status, ListaryStatus::Ok as i32);
        let diagnostic_json: Value =
            serde_json::from_slice(&unsafe { take_buffer(diagnostics) }).unwrap();
        assert_eq!(diagnostic_json["item_count"].as_u64(), Some(1));
        assert!(diagnostic_json["catalog_generation"].as_u64().unwrap() > generation_before_rename);

        unsafe { listary_core_close(handle) };
    }

    #[test]
    fn contained_panic_drops_active_request_registration() {
        const REQUEST_ID: u64 = 91_001;

        let state = TempDir::new().unwrap();
        let handle = ListaryCoreHandle {
            core: Core::open(CoreConfig::new(state.path().to_path_buf())).unwrap(),
            searches: Mutex::new(HashMap::new()),
        };
        let mut error = ListaryBuffer::default();

        let status = ffi_guard(&mut error, || -> Result<(), CoreError> {
            let _request_registration = register_request(&handle, REQUEST_ID)?;
            panic!("test-only panic after active-request registration");
        });

        assert_eq!(status, ListaryStatus::Panic as i32);
        let message = String::from_utf8(unsafe { take_buffer(error) }).unwrap();
        assert_eq!(message, "panic contained at FFI boundary");
        assert!(!handle.searches.lock().contains_key(&REQUEST_ID));

        let reused_registration = register_request(&handle, REQUEST_ID)
            .expect("contained panic must leave the request ID reusable");
        drop(reused_registration);
        assert!(!handle.searches.lock().contains_key(&REQUEST_ID));
    }
}
