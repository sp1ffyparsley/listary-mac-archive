use listary_core::CoreConfig;
use listary_ffi::{
    LISTARY_CORE_MAX_INPUT_BYTES, ListaryBuffer, ListaryCoreHandle, ListaryStatus,
    listary_buffer_free, listary_core_add_root, listary_core_cancel, listary_core_close,
    listary_core_get_item, listary_core_list_roots, listary_core_open, listary_core_reconcile_root,
};
use serde_json::Value;
use std::{path::Path, ptr, slice};
use tempfile::TempDir;

type Handle = *mut ListaryCoreHandle;

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

fn take_optional_utf8(buffer: ListaryBuffer) -> Option<String> {
    if buffer.ptr.is_null() || buffer.len == 0 {
        return None;
    }
    Some(String::from_utf8(unsafe { take_buffer(buffer) }).expect("FFI buffer is UTF-8"))
}

fn dirty_buffer() -> ListaryBuffer {
    ListaryBuffer {
        ptr: ptr::NonNull::<u8>::dangling().as_ptr(),
        len: usize::MAX,
    }
}

fn open_handle(state_dir: &Path) -> HandleGuard {
    let config = serde_json::to_vec(&CoreConfig::new(state_dir.to_path_buf())).unwrap();
    let mut handle: Handle = ptr::null_mut();
    let mut error = ListaryBuffer::default();
    let status =
        unsafe { listary_core_open(config.as_ptr(), config.len(), &mut handle, &mut error) };
    let message = take_optional_utf8(error);
    assert_eq!(status, ListaryStatus::Ok as i32, "open failed: {message:?}");
    assert!(!handle.is_null());
    HandleGuard(handle)
}

fn assert_empty_buffer(buffer: &ListaryBuffer) {
    assert!(buffer.ptr.is_null());
    assert_eq!(buffer.len, 0);
}

#[test]
fn exact_input_limit_is_accepted_and_parsed() {
    assert_eq!(LISTARY_CORE_MAX_INPUT_BYTES, 8 * 1024 * 1024);
    let state = TempDir::new().unwrap();
    let handle = open_handle(state.path());

    let mut request = br#"{"item_id":4294967295}"#.to_vec();
    request.resize(LISTARY_CORE_MAX_INPUT_BYTES, b' ');
    assert_eq!(request.len(), LISTARY_CORE_MAX_INPUT_BYTES);

    let mut response = dirty_buffer();
    let mut error = dirty_buffer();
    let status = unsafe {
        listary_core_get_item(
            handle.0,
            request.as_ptr(),
            request.len(),
            &mut response,
            &mut error,
        )
    };

    assert_eq!(status, ListaryStatus::NotFound as i32);
    assert_empty_buffer(&response);
    assert_eq!(
        take_optional_utf8(error).as_deref(),
        Some("item not found: 4294967295")
    );
}

#[test]
fn oversized_json_length_is_rejected_before_pointer_access_or_mutation() {
    let state = TempDir::new().unwrap();
    let handle = open_handle(state.path());
    let unreadable = ptr::NonNull::<u8>::dangling().as_ptr();
    let oversized_len = LISTARY_CORE_MAX_INPUT_BYTES + 1;

    let mut response = dirty_buffer();
    let mut error = dirty_buffer();
    let status = unsafe {
        listary_core_add_root(
            handle.0,
            unreadable,
            oversized_len,
            &mut response,
            &mut error,
        )
    };

    assert_eq!(status, ListaryStatus::InvalidArgument as i32);
    assert_empty_buffer(&response);
    assert_eq!(
        take_optional_utf8(error).as_deref(),
        Some("invalid request: input length 8388609 exceeds maximum 8388608 bytes")
    );

    let mut roots_response = ListaryBuffer::default();
    let mut roots_error = ListaryBuffer::default();
    let status =
        unsafe { listary_core_list_roots(handle.0, &mut roots_response, &mut roots_error) };
    assert_eq!(
        status,
        ListaryStatus::Ok as i32,
        "list roots failed: {:?}",
        take_optional_utf8(roots_error)
    );
    let roots: Value = serde_json::from_slice(&unsafe { take_buffer(roots_response) }).unwrap();
    assert_eq!(roots, serde_json::json!([]));
}

#[test]
fn oversized_raw_utf8_length_is_rejected_before_pointer_access_or_registration() {
    let state = TempDir::new().unwrap();
    let handle = open_handle(state.path());
    let unreadable = ptr::NonNull::<u8>::dangling().as_ptr();
    let oversized_len = LISTARY_CORE_MAX_INPUT_BYTES + 1;
    let request_id = 91;

    let mut response = dirty_buffer();
    let mut error = dirty_buffer();
    let status = unsafe {
        listary_core_reconcile_root(
            handle.0,
            request_id,
            unreadable,
            oversized_len,
            &mut response,
            &mut error,
        )
    };

    assert_eq!(status, ListaryStatus::InvalidArgument as i32);
    assert_empty_buffer(&response);
    assert_eq!(
        take_optional_utf8(error).as_deref(),
        Some("invalid request: input length 8388609 exceeds maximum 8388608 bytes")
    );
    assert_eq!(
        unsafe { listary_core_cancel(handle.0, request_id) },
        ListaryStatus::NotFound as i32
    );
}
