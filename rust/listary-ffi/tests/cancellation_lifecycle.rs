use listary_core::{CoreConfig, SearchRequest};
use listary_ffi::{
    ListaryBuffer, ListaryCoreHandle, ListaryStatus, listary_buffer_free, listary_core_add_root,
    listary_core_cancel, listary_core_close, listary_core_get_diagnostics, listary_core_get_item,
    listary_core_open, listary_core_rebuild_all, listary_core_search,
};
use serde_json::{Value, json};
use std::{
    fs,
    path::Path,
    ptr, slice,
    sync::{Arc, Barrier},
    thread,
    time::{Duration, Instant},
};
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

fn add_root(handle: Handle, root: &Path) {
    let request = serde_json::to_vec(&json!({
        "path": root,
        "kind": "local"
    }))
    .unwrap();
    let mut response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_add_root(
            handle,
            request.as_ptr(),
            request.len(),
            &mut response,
            &mut error,
        )
    };
    let message = take_optional_utf8(error);
    assert_eq!(
        status,
        ListaryStatus::Ok as i32,
        "add_root failed: {message:?}"
    );
    let payload: Value = serde_json::from_slice(&unsafe { take_buffer(response) }).unwrap();
    assert!(payload["root_id"].as_str().is_some());
}

fn populate_tree(root: &Path, files: usize) {
    const FOLDERS: usize = 32;
    for folder in 0..FOLDERS {
        fs::create_dir_all(root.join(format!("folder_{folder:02}"))).unwrap();
    }
    for index in 0..files {
        let folder = root.join(format!("folder_{:02}", index % FOLDERS));
        fs::write(
            folder.join(format!("scene_{index:05}_common.mov")),
            [index as u8],
        )
        .unwrap();
    }
}

fn rebuild(handle: Handle, request_id: u64) -> (i32, Option<String>) {
    let mut response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe { listary_core_rebuild_all(handle, request_id, &mut response, &mut error) };
    let message = take_optional_utf8(error);
    if !response.ptr.is_null() || response.len != 0 {
        let _: Value = serde_json::from_slice(&unsafe { take_buffer(response) }).unwrap();
    }
    (status, message)
}

fn diagnostics(handle: Handle) -> Value {
    let mut response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe { listary_core_get_diagnostics(handle, &mut response, &mut error) };
    let message = take_optional_utf8(error);
    assert_eq!(
        status,
        ListaryStatus::Ok as i32,
        "diagnostics failed: {message:?}"
    );
    serde_json::from_slice(&unsafe { take_buffer(response) }).unwrap()
}

#[test]
fn duplicate_active_request_ids_fail_closed() {
    const FILES: usize = 8_192;
    const REQUEST_ID: u64 = 70_001;

    let state = TempDir::new().unwrap();
    let files = TempDir::new().unwrap();
    populate_tree(files.path(), FILES);
    let handle = open_handle(state.path());
    add_root(handle.0, files.path());

    let barrier = Arc::new(Barrier::new(4));
    let address = handle.0 as usize;
    let mut workers = Vec::new();
    for _ in 0..3 {
        let barrier = barrier.clone();
        workers.push(thread::spawn(move || {
            barrier.wait();
            rebuild(address as Handle, REQUEST_ID)
        }));
    }
    barrier.wait();

    let outcomes: Vec<_> = workers
        .into_iter()
        .map(|worker| worker.join().expect("rebuild worker did not panic"))
        .collect();
    let ok_count = outcomes
        .iter()
        .filter(|(status, _)| *status == ListaryStatus::Ok as i32)
        .count();
    let duplicate_count = outcomes
        .iter()
        .filter(|(status, _)| *status == ListaryStatus::InvalidArgument as i32)
        .count();

    assert_eq!(
        ok_count, 1,
        "unexpected duplicate-ID outcomes: {outcomes:?}"
    );
    assert_eq!(
        duplicate_count, 2,
        "concurrent duplicate request IDs did not fail closed: {outcomes:?}"
    );
    for (status, message) in outcomes {
        match status {
            status if status == ListaryStatus::Ok as i32 => assert!(message.is_none()),
            status if status == ListaryStatus::InvalidArgument as i32 => assert!(
                message
                    .as_deref()
                    .is_some_and(|text| text.contains("request_id is already active")),
                "duplicate-ID error text was missing: {message:?}"
            ),
            other => panic!("unexpected duplicate-ID status {other}: {message:?}"),
        }
    }

    assert_eq!(
        unsafe { listary_core_cancel(handle.0, REQUEST_ID) },
        ListaryStatus::NotFound as i32,
        "request ID remained registered after rebuild completion"
    );
}

#[test]
fn cancel_active_call_then_join_before_close_and_cancel_after_completion_is_not_found() {
    const FILES: usize = 8_192;
    const REQUEST_ID: u64 = 70_002;

    let state = TempDir::new().unwrap();
    let files = TempDir::new().unwrap();
    populate_tree(files.path(), FILES);
    let handle = open_handle(state.path());
    add_root(handle.0, files.path());

    assert_eq!(
        unsafe { listary_core_cancel(handle.0, REQUEST_ID) },
        ListaryStatus::NotFound as i32,
        "cancel-before-start should not pre-cancel a future request"
    );

    let address = handle.0 as usize;
    let started = Arc::new(Barrier::new(2));
    let worker_started = started.clone();
    let worker = thread::spawn(move || {
        worker_started.wait();
        rebuild(address as Handle, REQUEST_ID)
    });
    started.wait();

    let deadline = Instant::now() + Duration::from_secs(5);
    let cancel_status = loop {
        let status = unsafe { listary_core_cancel(handle.0, REQUEST_ID) };
        if status == ListaryStatus::Ok as i32 {
            break status;
        }
        assert_eq!(
            status,
            ListaryStatus::NotFound as i32,
            "unexpected status while waiting for request registration"
        );
        assert!(
            Instant::now() < deadline,
            "rebuild completed or failed to register before cancellation could observe it"
        );
        thread::yield_now();
    };
    assert_eq!(cancel_status, ListaryStatus::Ok as i32);

    let (operation_status, operation_error) = worker.join().expect("rebuild worker did not panic");
    assert!(
        operation_status == ListaryStatus::Cancelled as i32
            || operation_status == ListaryStatus::Ok as i32,
        "active cancellation produced unexpected status {operation_status}: {operation_error:?}"
    );
    if operation_status == ListaryStatus::Cancelled as i32 {
        assert!(
            operation_error
                .as_deref()
                .is_some_and(|text| text.contains("cancel")),
            "cancelled operation did not return cancellation text: {operation_error:?}"
        );
    }

    assert_eq!(
        unsafe { listary_core_cancel(handle.0, REQUEST_ID) },
        ListaryStatus::NotFound as i32,
        "cancel-after-completion must report that the registration is gone"
    );

    // The documented close rule is exercised only at its safe boundary: all active work has
    // completed and the worker has been joined before the owning handle is dropped/closed.
    drop(handle);
}

#[test]
fn repeated_open_close_releases_state_lock_and_null_close_is_safe() {
    let state = TempDir::new().unwrap();

    unsafe { listary_core_close(ptr::null_mut()) };
    for _ in 0..8 {
        let handle = open_handle(state.path());
        let report = diagnostics(handle.0);
        assert_eq!(report["root_count"].as_u64(), Some(0));
        drop(handle);
    }
}

#[test]
fn error_and_response_buffers_are_owned_until_freed_and_handle_remains_usable() {
    let state = TempDir::new().unwrap();

    let invalid_config = b"{this is not json";
    let mut rejected_handle: Handle = ptr::null_mut();
    let mut open_error = ListaryBuffer::default();
    let open_status = unsafe {
        listary_core_open(
            invalid_config.as_ptr(),
            invalid_config.len(),
            &mut rejected_handle,
            &mut open_error,
        )
    };
    assert_eq!(open_status, ListaryStatus::Serialization as i32);
    assert!(rejected_handle.is_null());
    let open_message = take_optional_utf8(open_error).expect("open error buffer");
    assert!(!open_message.is_empty());

    let handle = open_handle(state.path());
    let request = serde_json::to_vec(&SearchRequest {
        query: "anything".into(),
        ..SearchRequest::default()
    })
    .unwrap();
    let mut response = ListaryBuffer::default();
    let mut search_error = ListaryBuffer::default();
    let search_status = unsafe {
        listary_core_search(
            handle.0,
            0,
            request.as_ptr(),
            request.len(),
            &mut response,
            &mut search_error,
        )
    };
    assert_eq!(search_status, ListaryStatus::InvalidArgument as i32);
    assert!(response.ptr.is_null());
    assert_eq!(response.len, 0);
    let search_message = take_optional_utf8(search_error).expect("search error buffer");
    assert!(search_message.contains("request_id must be nonzero"));

    // Freeing a zero/null buffer is explicitly supported by the ABI.
    unsafe { listary_buffer_free(response) };

    let report = diagnostics(handle.0);
    assert_eq!(report["item_count"].as_u64(), Some(0));
}

#[test]
fn ffi_initializes_outputs_and_distinguishes_missing_items_from_malformed_requests() {
    let state = TempDir::new().unwrap();

    let invalid_config = b"{this is not json";
    let mut rejected_handle = ptr::NonNull::<ListaryCoreHandle>::dangling().as_ptr();
    let mut open_error = dirty_buffer();
    let status = unsafe {
        listary_core_open(
            invalid_config.as_ptr(),
            invalid_config.len(),
            &mut rejected_handle,
            &mut open_error,
        )
    };
    assert_eq!(status, ListaryStatus::Serialization as i32);
    assert!(rejected_handle.is_null());
    assert!(
        take_optional_utf8(open_error)
            .as_deref()
            .is_some_and(|message| message.contains("serialization"))
    );

    let handle = open_handle(state.path());
    let missing_request = serde_json::to_vec(&json!({ "item_id": u32::MAX })).unwrap();
    let mut response = dirty_buffer();
    let mut error = dirty_buffer();
    let status = unsafe {
        listary_core_get_item(
            handle.0,
            missing_request.as_ptr(),
            missing_request.len(),
            &mut response,
            &mut error,
        )
    };
    assert_eq!(status, ListaryStatus::NotFound as i32);
    assert!(response.ptr.is_null());
    assert_eq!(response.len, 0);
    assert_eq!(
        take_optional_utf8(error).as_deref(),
        Some("item not found: 4294967295")
    );

    let malformed_request = br#"{"item_id":"not-a-number"}"#;
    let mut response = dirty_buffer();
    let mut error = dirty_buffer();
    let status = unsafe {
        listary_core_get_item(
            handle.0,
            malformed_request.as_ptr(),
            malformed_request.len(),
            &mut response,
            &mut error,
        )
    };
    assert_eq!(status, ListaryStatus::InvalidArgument as i32);
    assert!(response.ptr.is_null());
    assert_eq!(response.len, 0);
    assert!(
        take_optional_utf8(error)
            .as_deref()
            .is_some_and(|message| message.contains("invalid item lookup request"))
    );

    let mut response = dirty_buffer();
    let mut error = dirty_buffer();
    let status = unsafe { listary_core_get_diagnostics(handle.0, &mut response, &mut error) };
    assert_eq!(status, ListaryStatus::Ok as i32);
    assert!(error.ptr.is_null());
    assert_eq!(error.len, 0);
    let report: Value = serde_json::from_slice(&unsafe { take_buffer(response) }).unwrap();
    assert_eq!(report["item_count"].as_u64(), Some(0));
}
