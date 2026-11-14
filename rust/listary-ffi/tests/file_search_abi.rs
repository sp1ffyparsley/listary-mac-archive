use listary_core::{
    Core, CoreConfig, DiscoveredItem, FileSearchRequest, FileSearchResponse, FileSearchSortField,
    ItemKind, RootKind, SortDirection,
};
use listary_ffi::{
    ListaryBuffer, ListaryCoreHandle, ListaryStatus, listary_buffer_free, listary_core_add_root,
    listary_core_cancel, listary_core_close, listary_core_file_search, listary_core_open,
    listary_core_rebuild_all,
};
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
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

fn seed_state(state_dir: &Path, item_count: usize) {
    let core = Core::open(CoreConfig::new(state_dir.to_path_buf())).unwrap();
    let root = core
        .add_root(PathBuf::from("X:/ffi-exact"), RootKind::Local)
        .unwrap();
    core.replace_root_snapshot(
        root.id,
        (0..item_count)
            .map(|index| {
                DiscoveredItem::new(
                    PathBuf::from(format!("X:/ffi-exact/result_{index:05}.txt")),
                    ItemKind::File,
                    index as u64,
                    index as i64,
                )
            })
            .collect(),
    )
    .unwrap();
    core.flush().unwrap();
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

fn call_file_search(
    handle: Handle,
    request_id: u64,
    request: &[u8],
) -> (i32, Option<FileSearchResponse>, Option<String>) {
    let mut response = ListaryBuffer::default();
    let mut error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_file_search(
            handle,
            request_id,
            request.as_ptr(),
            request.len(),
            &mut response,
            &mut error,
        )
    };
    let response = if response.ptr.is_null() {
        None
    } else {
        Some(serde_json::from_slice(&unsafe { take_buffer(response) }).unwrap())
    };
    (status, response, take_optional_utf8(error))
}

#[test]
fn file_search_ffi_initializes_outputs_maps_stale_catalog_and_reuses_completed_ids() {
    let state = TempDir::new().unwrap();
    seed_state(state.path(), 32);
    let handle = open_handle(state.path());

    let malformed = br#"{"limit":"not-a-number"}"#;
    let mut response = dirty_buffer();
    let mut error = dirty_buffer();
    let status = unsafe {
        listary_core_file_search(
            handle.0,
            81_001,
            malformed.as_ptr(),
            malformed.len(),
            &mut response,
            &mut error,
        )
    };
    assert_eq!(status, ListaryStatus::Serialization as i32);
    assert!(response.ptr.is_null());
    assert_eq!(response.len, 0);
    assert!(
        take_optional_utf8(error)
            .as_deref()
            .is_some_and(|message| message.contains("serialization"))
    );
    assert_eq!(
        unsafe { listary_core_cancel(handle.0, 81_001) },
        ListaryStatus::NotFound as i32
    );

    let request = serde_json::to_vec(&FileSearchRequest {
        query: "result".into(),
        limit: 7,
        sort_field: FileSearchSortField::Name,
        sort_direction: SortDirection::Ascending,
        ..FileSearchRequest::default()
    })
    .unwrap();
    let (status, first, error) = call_file_search(handle.0, 81_002, &request);
    assert_eq!(status, ListaryStatus::Ok as i32, "{error:?}");
    let first = first.unwrap();
    assert_eq!(first.total_matches, 32);
    assert_eq!(first.results.len(), 7);
    assert!(first.has_more);
    assert_eq!(
        unsafe { listary_core_cancel(handle.0, 81_002) },
        ListaryStatus::NotFound as i32
    );

    // A completed request ID is reusable; ActiveRequest owns cleanup on every return path.
    let (status, repeated, error) = call_file_search(handle.0, 81_002, &request);
    assert_eq!(status, ListaryStatus::Ok as i32, "{error:?}");
    assert_eq!(
        repeated.unwrap().catalog_generation,
        first.catalog_generation
    );

    let changed_files = TempDir::new().unwrap();
    fs::write(
        changed_files.path().join("generation-change.txt"),
        b"change",
    )
    .unwrap();
    let add_root = serde_json::to_vec(&json!({
        "path": changed_files.path(),
        "kind": "local"
    }))
    .unwrap();
    let mut add_response = ListaryBuffer::default();
    let mut add_error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_add_root(
            handle.0,
            add_root.as_ptr(),
            add_root.len(),
            &mut add_response,
            &mut add_error,
        )
    };
    assert_eq!(
        status,
        ListaryStatus::Ok as i32,
        "{:?}",
        take_optional_utf8(add_error)
    );
    let _: Value = serde_json::from_slice(&unsafe { take_buffer(add_response) }).unwrap();
    let mut rebuild_response = ListaryBuffer::default();
    let mut rebuild_error = ListaryBuffer::default();
    let status = unsafe {
        listary_core_rebuild_all(handle.0, 81_004, &mut rebuild_response, &mut rebuild_error)
    };
    assert_eq!(
        status,
        ListaryStatus::Ok as i32,
        "{:?}",
        take_optional_utf8(rebuild_error)
    );
    let _: Value = serde_json::from_slice(&unsafe { take_buffer(rebuild_response) }).unwrap();

    let stale_request = serde_json::to_vec(&FileSearchRequest {
        expected_catalog_generation: Some(first.catalog_generation),
        ..serde_json::from_slice::<FileSearchRequest>(&request).unwrap()
    })
    .unwrap();
    let (status, response, error) = call_file_search(handle.0, 81_003, &stale_request);
    assert_eq!(status, ListaryStatus::StaleCatalog as i32);
    assert!(response.is_none());
    assert!(
        error
            .as_deref()
            .is_some_and(|message| message.contains("catalog generation is stale"))
    );
    assert_eq!(
        unsafe { listary_core_cancel(handle.0, 81_003) },
        ListaryStatus::NotFound as i32
    );
}

#[test]
fn file_search_ffi_is_cancellable_and_always_cleans_request_registration() {
    const REQUEST_ID: u64 = 81_100;
    let state = TempDir::new().unwrap();
    seed_state(state.path(), 12_000);
    let handle = open_handle(state.path());
    let request = serde_json::to_vec(&FileSearchRequest {
        query: "result".into(),
        limit: 1_000,
        sort_field: FileSearchSortField::Path,
        sort_direction: SortDirection::Descending,
        ..FileSearchRequest::default()
    })
    .unwrap();

    let address = handle.0 as usize;
    let started = Arc::new(Barrier::new(2));
    let worker_started = started.clone();
    let worker = thread::spawn(move || {
        worker_started.wait();
        call_file_search(address as Handle, REQUEST_ID, &request)
    });
    started.wait();

    let deadline = Instant::now() + Duration::from_secs(5);
    let cancellation_observed = loop {
        let status = unsafe { listary_core_cancel(handle.0, REQUEST_ID) };
        if status == ListaryStatus::Ok as i32 {
            break true;
        }
        assert_eq!(status, ListaryStatus::NotFound as i32);
        if worker.is_finished() || Instant::now() >= deadline {
            break false;
        }
        thread::yield_now();
    };

    let (status, response, error) = worker.join().expect("file search worker did not panic");
    assert!(
        cancellation_observed,
        "exact File Search completed before its active request could be cancelled"
    );
    assert_eq!(
        status,
        ListaryStatus::Cancelled as i32,
        "accepted cancellation was not honored: {error:?}"
    );
    assert!(response.is_none());
    assert!(
        error
            .as_deref()
            .is_some_and(|message| message.contains("cancel"))
    );
    assert_eq!(
        unsafe { listary_core_cancel(handle.0, REQUEST_ID) },
        ListaryStatus::NotFound as i32,
        "request registration leaked after completion/cancellation"
    );
}
