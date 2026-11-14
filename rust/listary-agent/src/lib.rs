use listary_core::{
    ActivationSource, CancellationToken, Core, CoreConfig, CoreError, FacetCountsRequest,
    FileSearchRequest, FilterPreset, MAX_RECONCILE_PATHS, RefreshPolicy, RootAvailability,
    RootKind, RootRecord, SearchRequest, WorkspaceCatalogPlatform, WorkspaceCatalogRefresh,
    load_workspace_catalog, scheduled_refresh_due,
};
use notify::{Event as NotifyEvent, RecommendedWatcher, RecursiveMode, Watcher};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet},
    fs,
    io::{self, Read, Write},
    net::{Ipv4Addr, SocketAddr, SocketAddrV4, TcpListener, TcpStream},
    path::{Path, PathBuf},
    sync::{
        Arc, Condvar, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use uuid::Uuid;

pub const AGENT_PROTOCOL_VERSION: u32 = 1;
pub const DEFAULT_ENDPOINT_FILE: &str = "integration-endpoint-v1.json";
const MAX_REQUEST_BYTES: usize = 8 * 1024 * 1024;
const HTTP_RPC_PATH: &str = "/v1/rpc";
const MAX_HTTP_HEADER_BYTES: usize = 32 * 1024;
const MAX_HTTP_LINE_BYTES: usize = 8 * 1024;
const IO_TIMEOUT: Duration = Duration::from_secs(15);
const CONNECTION_READ_POLL: Duration = Duration::from_secs(1);
const WATCH_DEBOUNCE: Duration = Duration::from_millis(75);
const WATCH_MAX_COALESCE: Duration = Duration::from_millis(250);
const WATCH_EVENT_QUEUE_CAPACITY: usize = 256;
const WATCH_ROOT_RETRY: Duration = Duration::from_secs(1);
const WATCH_LOOP_TICK: Duration = Duration::from_millis(25);
const SCHEDULE_POLL: Duration = Duration::from_millis(250);
const SCHEDULE_AVAILABILITY_PROBE: Duration = Duration::from_secs(1);
const SCHEDULE_UNAVAILABLE_RETRY: Duration = Duration::from_secs(1);
const SCHEDULE_STALE_RETRY: Duration = Duration::from_millis(250);
const SCHEDULE_FAILURE_RETRY: Duration = Duration::from_secs(5);

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentEndpoint {
    pub schema_version: u32,
    pub protocol_version: u32,
    pub process_id: u32,
    pub address: SocketAddr,
    pub token: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct AgentRequest {
    token: String,
    #[serde(default)]
    id: Option<Value>,
    method: String,
    #[serde(default)]
    request_id: Option<u64>,
    #[serde(default)]
    params: Value,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct HttpRpcRequest {
    #[serde(default)]
    id: Option<Value>,
    method: String,
    #[serde(default)]
    request_id: Option<u64>,
    #[serde(default)]
    params: Value,
}

#[derive(Debug, Default)]
struct HttpHeaders {
    authorization: Option<String>,
    content_length: Option<u64>,
    content_type: Option<String>,
    origin_present: bool,
    transfer_encoding_present: bool,
    expect_present: bool,
}

#[derive(Debug, Serialize)]
struct AgentResponse {
    #[serde(skip_serializing_if = "Option::is_none")]
    id: Option<Value>,
    ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    data: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<AgentError>,
}

#[derive(Debug, Serialize)]
struct AgentError {
    code: &'static str,
    message: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RecordActivationParams {
    item_id: u32,
    now_unix_ms: i64,
    #[serde(default)]
    source: ActivationSource,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ItemIdParams {
    item_id: u32,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct AddRootParams {
    path: PathBuf,
    #[serde(default = "default_root_kind")]
    kind: RootKind,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RootIdParams {
    root_id: Uuid,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct FilterIdParams {
    filter_id: Uuid,
}

fn default_root_kind() -> RootKind {
    RootKind::Local
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct WorkspaceCatalogStatus {
    pub configured: bool,
    pub loaded: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_refresh: Option<WorkspaceCatalogRefresh>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_error: Option<String>,
}

struct WorkspaceCatalogState {
    path: Option<PathBuf>,
    status: WorkspaceCatalogStatus,
}

impl WorkspaceCatalogState {
    fn new(path: Option<PathBuf>) -> Self {
        Self {
            status: WorkspaceCatalogStatus {
                configured: path.is_some(),
                loaded: false,
                last_refresh: None,
                last_error: None,
            },
            path,
        }
    }

    fn record_success(&mut self, refresh: WorkspaceCatalogRefresh) {
        self.status.loaded = true;
        self.status.last_refresh = Some(refresh);
        self.status.last_error = None;
    }

    fn record_failure(&mut self, error: &CoreError) {
        self.status.last_error = Some(error.to_string());
    }
}

#[derive(Debug, Clone, Serialize, Default)]
struct RootWatcherStatus {
    watched_root_count: usize,
    pending_root_count: usize,
    scheduled_root_count: usize,
    scheduled_in_flight: usize,
    event_count: u64,
    reconcile_batches: u64,
    reconciled_paths: u64,
    full_reconciles: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    last_full_reconcile_micros: Option<u64>,
    queue_overflow_recoveries: u64,
    stale_batches_rejected: u64,
    scheduled_reconciles: u64,
    scheduled_failures: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    last_error: Option<String>,
}

enum ScheduledRefreshOutcome {
    Reconciled,
    Unavailable,
    ProbeHealthy,
    StaleCatalog,
    Cancelled,
    Failed(String),
}

struct ScheduledRefreshTask {
    root_id: Uuid,
    runner: thread::JoinHandle<()>,
}

struct PendingRootChanges {
    expected_catalog_generation: u64,
    paths: HashSet<PathBuf>,
}

enum RootWatcherMessage {
    Sync {
        roots: Vec<RootRecord>,
        recover_untrusted: bool,
    },
    Event(notify::Result<NotifyEvent>),
    ScheduledRefreshComplete {
        root_id: Uuid,
        outcome: ScheduledRefreshOutcome,
    },
    Shutdown,
}

#[derive(Clone)]
struct RootWatcherHandle {
    sender: mpsc::SyncSender<RootWatcherMessage>,
    cancellation: CancellationToken,
    status: Arc<Mutex<RootWatcherStatus>>,
    #[cfg(test)]
    event_queue_overflowed: Arc<AtomicBool>,
}

impl RootWatcherHandle {
    fn sync(&self, roots: Vec<RootRecord>, recover_untrusted: bool) {
        let _ = self.sender.send(RootWatcherMessage::Sync {
            roots,
            recover_untrusted,
        });
    }

    fn stop(&self) {
        self.cancellation.cancel();
        // Cancellation is the authoritative stop signal. The queue message is only a wakeup;
        // never let a saturated bounded event queue make the shutdown caller block on watcher
        // progress.
        let _ = self.sender.try_send(RootWatcherMessage::Shutdown);
    }

    fn status(&self) -> RootWatcherStatus {
        self.status
            .lock()
            .map(|status| status.clone())
            .unwrap_or_else(|_| RootWatcherStatus {
                last_error: Some("root watcher status lock poisoned".into()),
                ..Default::default()
            })
    }
}

struct SharedState {
    core: Arc<Core>,
    token: String,
    listener_address: SocketAddr,
    http_api_enabled: bool,
    requests: Mutex<HashMap<u64, CancellationToken>>,
    active_connections: Mutex<usize>,
    connections_drained: Condvar,
    workspace_catalog: Mutex<WorkspaceCatalogState>,
    root_watcher: RootWatcherHandle,
    stopping: AtomicBool,
}

impl SharedState {
    fn connection_started(&self) -> io::Result<()> {
        let mut active = self
            .active_connections
            .lock()
            .map_err(|_| io::Error::other("agent connection counter lock poisoned"))?;
        *active = active.saturating_add(1);
        Ok(())
    }

    fn connection_finished(&self) {
        if let Ok(mut active) = self.active_connections.lock() {
            *active = active.saturating_sub(1);
            if *active == 0 {
                self.connections_drained.notify_all();
            }
        }
    }

    fn cancel_active_requests(&self) {
        let tokens = self
            .requests
            .lock()
            .map(|requests| requests.values().cloned().collect::<Vec<_>>())
            .unwrap_or_default();
        for token in tokens {
            token.cancel();
        }
    }

    fn wait_for_connections(&self, timeout: Duration) -> io::Result<()> {
        let active = self
            .active_connections
            .lock()
            .map_err(|_| io::Error::other("agent connection counter lock poisoned"))?;
        let (active, wait) = self
            .connections_drained
            .wait_timeout_while(active, timeout, |count| *count != 0)
            .map_err(|_| io::Error::other("agent connection counter lock poisoned"))?;
        if *active != 0 && wait.timed_out() {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                format!(
                    "timed out waiting for {} active Listary connection(s) to drain",
                    *active
                ),
            ));
        }
        Ok(())
    }
}

fn watcher_error(error: notify::Error) -> AgentStartError {
    AgentStartError::Io(io::Error::other(format!(
        "filesystem watcher initialization failed: {error}"
    )))
}

fn enqueue_watcher_event(
    sender: &mpsc::SyncSender<RootWatcherMessage>,
    overflowed: &AtomicBool,
    event: notify::Result<NotifyEvent>,
) {
    match sender.try_send(RootWatcherMessage::Event(event)) {
        Ok(()) => {}
        Err(mpsc::TrySendError::Full(_)) => {
            overflowed.store(true, Ordering::Release);
        }
        Err(mpsc::TrySendError::Disconnected(_)) => {}
    }
}

fn start_root_watcher(
    core: Arc<Core>,
) -> Result<(RootWatcherHandle, thread::JoinHandle<()>), AgentStartError> {
    let (sender, receiver) = mpsc::sync_channel::<RootWatcherMessage>(WATCH_EVENT_QUEUE_CAPACITY);
    let cancellation = CancellationToken::new();
    let status = Arc::new(Mutex::new(RootWatcherStatus::default()));
    let event_queue_overflowed = Arc::new(AtomicBool::new(false));
    let event_sender = sender.clone();
    let event_overflow = Arc::clone(&event_queue_overflowed);
    let watcher = notify::recommended_watcher(move |event| {
        enqueue_watcher_event(&event_sender, &event_overflow, event);
    })
    .map_err(watcher_error)?;
    let watcher_sender = sender.clone();
    let handle = RootWatcherHandle {
        sender,
        cancellation: cancellation.clone(),
        status: Arc::clone(&status),
        #[cfg(test)]
        event_queue_overflowed: Arc::clone(&event_queue_overflowed),
    };
    let runner = thread::spawn(move || {
        run_root_watcher(
            core,
            watcher,
            receiver,
            watcher_sender,
            status,
            cancellation,
            event_queue_overflowed,
        );
    });
    Ok((handle, runner))
}

fn root_is_automatically_watched(root: &RootRecord) -> bool {
    root.enabled && matches!(root.refresh_policy, RefreshPolicy::Automatic)
}

fn root_uses_timed_schedule(root: &RootRecord) -> bool {
    root.enabled && root.refresh_policy.is_timed()
}

fn unix_time_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis().min(i64::MAX as u128) as i64)
        .unwrap_or(0)
}

fn record_watcher_error(status: &Arc<Mutex<RootWatcherStatus>>, message: impl Into<String>) {
    if let Ok(mut status) = status.lock() {
        status.last_error = Some(message.into());
    }
}

fn update_watcher_counts(
    status: &Arc<Mutex<RootWatcherStatus>>,
    active: &HashMap<Uuid, PathBuf>,
    pending: &HashMap<Uuid, PendingRootChanges>,
    full_recovery: &HashSet<Uuid>,
) {
    if let Ok(mut status) = status.lock() {
        status.watched_root_count = active.len();
        status.pending_root_count = pending
            .keys()
            .copied()
            .chain(full_recovery.iter().copied())
            .collect::<HashSet<_>>()
            .len();
    }
}

fn sync_watched_roots(
    watcher: &mut RecommendedWatcher,
    desired: &mut HashMap<Uuid, RootRecord>,
    active: &mut HashMap<Uuid, PathBuf>,
    full_recovery: &mut HashSet<Uuid>,
    roots: Vec<RootRecord>,
    recover_untrusted: bool,
    status: &Arc<Mutex<RootWatcherStatus>>,
) {
    let next = roots
        .into_iter()
        .filter(root_is_automatically_watched)
        .map(|root| (root.id, root))
        .collect::<HashMap<_, _>>();

    let stale_ids = active
        .iter()
        .filter_map(|(root_id, path)| {
            let keep = next.get(root_id).is_some_and(|root| root.path == *path);
            (!keep).then_some(*root_id)
        })
        .collect::<Vec<_>>();
    for root_id in stale_ids {
        if let Some(path) = active.remove(&root_id) {
            let _ = watcher.unwatch(&path);
        }
    }

    full_recovery.retain(|root_id| next.contains_key(root_id));
    if recover_untrusted {
        full_recovery.extend(
            next.values()
                .filter(|root| root.last_reconciled_unix_ms.is_some())
                .map(|root| root.id),
        );
    }
    *desired = next;

    for root in desired.values() {
        if active.contains_key(&root.id) {
            continue;
        }
        if !root.path.exists() {
            if root.last_reconciled_unix_ms.is_some() {
                full_recovery.insert(root.id);
            }
            continue;
        }
        match watcher.watch(&root.path, RecursiveMode::Recursive) {
            Ok(()) => {
                active.insert(root.id, root.path.clone());
                if root.last_reconciled_unix_ms.is_some() {
                    full_recovery.insert(root.id);
                }
            }
            Err(error) => {
                record_watcher_error(status, format!("could not watch an indexed root: {error}"))
            }
        }
    }
}

fn root_for_changed_path(desired: &HashMap<Uuid, RootRecord>, path: &Path) -> Option<Uuid> {
    desired
        .values()
        .filter(|root| path.starts_with(&root.path))
        .max_by_key(|root| root.path.components().count())
        .map(|root| root.id)
}

fn run_due_scheduled_refreshes(
    core: &Arc<Core>,
    cancellation: &CancellationToken,
    status: &Arc<Mutex<RootWatcherStatus>>,
    next_attempt_at: &mut HashMap<Uuid, Instant>,
    next_probe_at: &mut HashMap<Uuid, Instant>,
    scheduled_task: &mut Option<ScheduledRefreshTask>,
    sender: &mpsc::SyncSender<RootWatcherMessage>,
) {
    let roots = core.list_roots();
    let scheduled_ids = roots
        .iter()
        .filter(|root| root_uses_timed_schedule(root))
        .map(|root| root.id)
        .collect::<HashSet<_>>();
    next_attempt_at.retain(|root_id, _| scheduled_ids.contains(root_id));
    next_probe_at.retain(|root_id, _| scheduled_ids.contains(root_id));
    if let Ok(mut watcher_status) = status.lock() {
        watcher_status.scheduled_root_count = scheduled_ids.len();
    }
    if scheduled_task.is_some() {
        return;
    }

    let timed_roots = roots
        .into_iter()
        .filter(root_uses_timed_schedule)
        .collect::<Vec<_>>();
    let now = Instant::now();
    let now_unix_ms = unix_time_ms();
    let due_root = timed_roots.iter().find(|root| {
        !next_attempt_at
            .get(&root.id)
            .is_some_and(|next_attempt| now < *next_attempt)
            && scheduled_refresh_due(
                &root.refresh_policy,
                root.last_reconciled_unix_ms,
                now_unix_ms,
            )
    });
    let selected = due_root.or_else(|| {
        timed_roots.iter().find(|root| {
            !next_probe_at
                .get(&root.id)
                .is_some_and(|next_probe| now < *next_probe)
        })
    });
    let Some(root) = selected else {
        return;
    };
    if cancellation.is_cancelled() {
        return;
    }

    // Re-read immediately before dispatch. A manual reconcile or root settings mutation may have
    // changed the due decision after the first snapshot.
    let Some(current) = core
        .list_roots()
        .into_iter()
        .find(|candidate| candidate.id == root.id)
    else {
        return;
    };
    if !root_uses_timed_schedule(&current) {
        return;
    }
    let retry_allowed = !next_attempt_at
        .get(&current.id)
        .is_some_and(|next_attempt| Instant::now() < *next_attempt);
    let reconcile_if_present = current.availability == RootAvailability::Unavailable
        || (retry_allowed
            && scheduled_refresh_due(
                &current.refresh_policy,
                current.last_reconciled_unix_ms,
                unix_time_ms(),
            ));

    let root_id = current.id;
    let worker_core = Arc::clone(core);
    let worker_cancellation = cancellation.clone();
    let completion_sender = sender.clone();
    let runner = thread::spawn(move || {
        let outcome = if worker_cancellation.is_cancelled() {
            ScheduledRefreshOutcome::Cancelled
        } else if !current.path.exists() {
            if current.availability == RootAvailability::Unavailable {
                ScheduledRefreshOutcome::Unavailable
            } else {
                match worker_core
                    .set_root_unavailable(root_id, "scheduled root is currently unavailable".into())
                {
                    Ok(()) => ScheduledRefreshOutcome::Unavailable,
                    Err(error) => ScheduledRefreshOutcome::Failed(format!(
                        "scheduled root availability update failed: {error}"
                    )),
                }
            }
        } else if reconcile_if_present {
            match worker_core.reconcile_root(root_id, &worker_cancellation) {
                Ok(_) => ScheduledRefreshOutcome::Reconciled,
                Err(CoreError::Cancelled) => ScheduledRefreshOutcome::Cancelled,
                Err(CoreError::StaleCatalog { .. }) => ScheduledRefreshOutcome::StaleCatalog,
                Err(error) => ScheduledRefreshOutcome::Failed(format!(
                    "scheduled root reconcile failed: {error}"
                )),
            }
        } else {
            ScheduledRefreshOutcome::ProbeHealthy
        };
        let _ = completion_sender
            .send(RootWatcherMessage::ScheduledRefreshComplete { root_id, outcome });
    });
    *scheduled_task = Some(ScheduledRefreshTask { root_id, runner });
    if let Ok(mut watcher_status) = status.lock() {
        watcher_status.scheduled_in_flight = 1;
    }
}

fn probe_watched_roots(
    core: &Core,
    watcher: &mut RecommendedWatcher,
    desired: &mut HashMap<Uuid, RootRecord>,
    active: &mut HashMap<Uuid, PathBuf>,
    full_recovery: &mut HashSet<Uuid>,
    cancellation: &CancellationToken,
    status: &Arc<Mutex<RootWatcherStatus>>,
) {
    if cancellation.is_cancelled() {
        return;
    }
    let current_roots = core.list_roots();
    sync_watched_roots(
        watcher,
        desired,
        active,
        full_recovery,
        current_roots,
        false,
        status,
    );
    for root in desired.values() {
        if cancellation.is_cancelled() {
            return;
        }
        if root.path.exists() {
            continue;
        }
        if let Some(path) = active.remove(&root.id) {
            let _ = watcher.unwatch(&path);
        }
        if root.last_reconciled_unix_ms.is_some() {
            full_recovery.insert(root.id);
            if root.availability != RootAvailability::Unavailable {
                let _ = core.set_root_unavailable(
                    root.id,
                    "indexed root disappeared while live change monitoring was active".into(),
                );
            }
        }
    }
}

fn flush_watcher_work(
    core: &Core,
    desired: &mut HashMap<Uuid, RootRecord>,
    pending: &mut HashMap<Uuid, PendingRootChanges>,
    full_recovery: &mut HashSet<Uuid>,
    cancellation: &CancellationToken,
    status: &Arc<Mutex<RootWatcherStatus>>,
) {
    fn rebase_pending_generation(
        pending: &mut HashMap<Uuid, PendingRootChanges>,
        previous_generation: u64,
        current_generation: u64,
    ) {
        if previous_generation == current_generation {
            return;
        }
        for changes in pending.values_mut() {
            if changes.expected_catalog_generation == previous_generation {
                changes.expected_catalog_generation = current_generation;
            }
        }
    }

    let current = core
        .list_roots()
        .into_iter()
        .map(|root| (root.id, root))
        .collect::<HashMap<_, _>>();
    for (root_id, root) in &current {
        if desired.contains_key(root_id) {
            desired.insert(*root_id, root.clone());
        }
    }

    let recovery_ids = full_recovery.iter().copied().collect::<Vec<_>>();
    for root_id in recovery_ids {
        if cancellation.is_cancelled() {
            return;
        }
        let Some(root) = core
            .list_roots()
            .into_iter()
            .find(|root| root.id == root_id && root_is_automatically_watched(root))
        else {
            full_recovery.remove(&root_id);
            pending.remove(&root_id);
            continue;
        };
        desired.insert(root_id, root.clone());
        if !root.path.exists() {
            continue;
        }
        let recovery_started = Instant::now();
        match core.reconcile_root_with_generation_transition(root_id, cancellation) {
            Ok((_report, previous_generation, current_generation)) => {
                full_recovery.remove(&root_id);
                pending.remove(&root_id);
                rebase_pending_generation(pending, previous_generation, current_generation);
                if let Ok(mut status) = status.lock() {
                    status.full_reconciles = status.full_reconciles.saturating_add(1);
                    status.last_full_reconcile_micros =
                        Some(recovery_started.elapsed().as_micros().min(u64::MAX as u128) as u64);
                    status.last_error = None;
                }
            }
            Err(CoreError::Cancelled) => return,
            Err(error) => {
                record_watcher_error(
                    status,
                    format!("indexed-root recovery reconcile failed: {error}"),
                );
            }
        }
    }

    let pending_ids = pending.keys().copied().collect::<Vec<_>>();
    for root_id in pending_ids {
        if cancellation.is_cancelled() {
            return;
        }
        if full_recovery.contains(&root_id) {
            continue;
        }
        let Some(root) = core
            .list_roots()
            .into_iter()
            .find(|root| root.id == root_id && root_is_automatically_watched(root))
        else {
            pending.remove(&root_id);
            continue;
        };
        desired.insert(root_id, root.clone());
        if root.last_reconciled_unix_ms.is_none() {
            continue;
        }
        if !root.path.exists() {
            full_recovery.insert(root_id);
            continue;
        }
        let Some(changes) = pending.remove(&root_id) else {
            continue;
        };
        let path_count = changes.paths.len() as u64;
        let expected_generation = changes.expected_catalog_generation;
        match core.reconcile_paths_with_generation_transition(
            root_id,
            changes.paths.into_iter().collect(),
            Some(expected_generation),
            cancellation,
        ) {
            Ok((_report, previous_generation, current_generation)) => {
                rebase_pending_generation(pending, previous_generation, current_generation);
                if let Ok(mut status) = status.lock() {
                    status.reconcile_batches = status.reconcile_batches.saturating_add(1);
                    status.reconciled_paths = status.reconciled_paths.saturating_add(path_count);
                    status.last_error = None;
                }
            }
            Err(CoreError::Cancelled) => return,
            Err(CoreError::StaleCatalog { .. }) => {
                full_recovery.insert(root_id);
                if let Ok(mut status) = status.lock() {
                    status.stale_batches_rejected = status.stale_batches_rejected.saturating_add(1);
                }
            }
            Err(error) => {
                record_watcher_error(status, format!("dirty-path reconcile failed: {error}"));
                full_recovery.insert(root_id);
            }
        }
    }
}

fn run_root_watcher(
    core: Arc<Core>,
    mut watcher: RecommendedWatcher,
    receiver: mpsc::Receiver<RootWatcherMessage>,
    sender: mpsc::SyncSender<RootWatcherMessage>,
    status: Arc<Mutex<RootWatcherStatus>>,
    cancellation: CancellationToken,
    event_queue_overflowed: Arc<AtomicBool>,
) {
    let mut desired = HashMap::<Uuid, RootRecord>::new();
    let mut active = HashMap::<Uuid, PathBuf>::new();
    let mut pending = HashMap::<Uuid, PendingRootChanges>::new();
    let mut full_recovery = HashSet::<Uuid>::new();
    let mut last_event_at = None::<Instant>;
    let mut batch_started_at = None::<Instant>;
    let mut next_probe_at = Instant::now();
    let mut recovery_due = false;
    let mut next_recovery_at = Instant::now();
    let mut next_schedule_poll_at = Instant::now();
    let mut next_scheduled_attempt = HashMap::<Uuid, Instant>::new();
    let mut next_scheduled_probe = HashMap::<Uuid, Instant>::new();
    let mut scheduled_task = None::<ScheduledRefreshTask>;

    loop {
        if cancellation.is_cancelled() {
            break;
        }
        match receiver.recv_timeout(WATCH_LOOP_TICK) {
            Ok(RootWatcherMessage::Sync {
                roots,
                recover_untrusted,
            }) => {
                sync_watched_roots(
                    &mut watcher,
                    &mut desired,
                    &mut active,
                    &mut full_recovery,
                    roots,
                    recover_untrusted,
                    &status,
                );
                pending.retain(|root_id, _| desired.contains_key(root_id));
                recovery_due |= !full_recovery.is_empty();
            }
            Ok(RootWatcherMessage::Event(Ok(event))) => {
                let event_at = Instant::now();
                batch_started_at.get_or_insert(event_at);
                if let Ok(mut watcher_status) = status.lock() {
                    watcher_status.event_count = watcher_status.event_count.saturating_add(1);
                }
                if event.need_rescan() || event.paths.is_empty() {
                    full_recovery.extend(
                        desired
                            .values()
                            .filter(|root| root.last_reconciled_unix_ms.is_some())
                            .map(|root| root.id),
                    );
                    recovery_due = true;
                } else {
                    let event_generation = match core.catalog_generation() {
                        Ok(generation) => Some(generation),
                        Err(error) => {
                            record_watcher_error(
                                &status,
                                format!(
                                    "could not snapshot catalog generation for watcher batch: {error}"
                                ),
                            );
                            full_recovery.extend(
                                desired
                                    .values()
                                    .filter(|root| root.last_reconciled_unix_ms.is_some())
                                    .map(|root| root.id),
                            );
                            recovery_due = true;
                            None
                        }
                    };
                    if let Some(event_generation) = event_generation {
                        for path in event.paths {
                            if let Some(root_id) = root_for_changed_path(&desired, &path) {
                                if full_recovery.contains(&root_id) {
                                    continue;
                                }
                                if pending.get(&root_id).is_some_and(|changes| {
                                    changes.expected_catalog_generation != event_generation
                                }) {
                                    pending.remove(&root_id);
                                    full_recovery.insert(root_id);
                                    recovery_due = true;
                                    if let Ok(mut watcher_status) = status.lock() {
                                        watcher_status.stale_batches_rejected =
                                            watcher_status.stale_batches_rejected.saturating_add(1);
                                    }
                                    continue;
                                }
                                let changes =
                                    pending
                                        .entry(root_id)
                                        .or_insert_with(|| PendingRootChanges {
                                            expected_catalog_generation: event_generation,
                                            paths: HashSet::new(),
                                        });
                                if changes.paths.len() >= MAX_RECONCILE_PATHS {
                                    pending.remove(&root_id);
                                    full_recovery.insert(root_id);
                                    recovery_due = true;
                                } else {
                                    changes.paths.insert(path);
                                }
                            }
                        }
                    }
                }
                last_event_at = Some(event_at);
            }
            Ok(RootWatcherMessage::Event(Err(error))) => {
                let event_at = Instant::now();
                batch_started_at.get_or_insert(event_at);
                record_watcher_error(
                    &status,
                    format!("filesystem watcher reported an error: {error}"),
                );
                full_recovery.extend(
                    desired
                        .values()
                        .filter(|root| root.last_reconciled_unix_ms.is_some())
                        .map(|root| root.id),
                );
                recovery_due = true;
                last_event_at = Some(event_at);
            }
            Ok(RootWatcherMessage::ScheduledRefreshComplete { root_id, outcome }) => {
                if let Some(task) = scheduled_task.take() {
                    if task.root_id != root_id {
                        record_watcher_error(
                            &status,
                            "scheduled refresh completion did not match the active root",
                        );
                    }
                    if task.runner.join().is_err() {
                        record_watcher_error(&status, "scheduled refresh worker panicked");
                    }
                }
                if let Ok(mut watcher_status) = status.lock() {
                    watcher_status.scheduled_in_flight = 0;
                }
                next_scheduled_probe.insert(root_id, Instant::now() + SCHEDULE_AVAILABILITY_PROBE);
                match outcome {
                    ScheduledRefreshOutcome::Reconciled => {
                        next_scheduled_attempt.remove(&root_id);
                        if let Ok(mut watcher_status) = status.lock() {
                            watcher_status.scheduled_reconciles =
                                watcher_status.scheduled_reconciles.saturating_add(1);
                            watcher_status.last_error = None;
                        }
                    }
                    ScheduledRefreshOutcome::Unavailable => {
                        next_scheduled_attempt
                            .insert(root_id, Instant::now() + SCHEDULE_UNAVAILABLE_RETRY);
                    }
                    ScheduledRefreshOutcome::ProbeHealthy => {}
                    ScheduledRefreshOutcome::StaleCatalog => {
                        next_scheduled_attempt
                            .insert(root_id, Instant::now() + SCHEDULE_STALE_RETRY);
                    }
                    ScheduledRefreshOutcome::Cancelled => {}
                    ScheduledRefreshOutcome::Failed(error) => {
                        next_scheduled_attempt
                            .insert(root_id, Instant::now() + SCHEDULE_FAILURE_RETRY);
                        if let Ok(mut watcher_status) = status.lock() {
                            watcher_status.scheduled_failures =
                                watcher_status.scheduled_failures.saturating_add(1);
                            watcher_status.last_error = Some(error);
                        }
                    }
                }
            }
            Ok(RootWatcherMessage::Shutdown) => break,
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
            Err(mpsc::RecvTimeoutError::Timeout) => {}
        }

        if cancellation.is_cancelled() {
            break;
        }

        if event_queue_overflowed.swap(false, Ordering::AcqRel) {
            full_recovery.extend(
                desired
                    .values()
                    .filter(|root| root.last_reconciled_unix_ms.is_some())
                    .map(|root| root.id),
            );
            recovery_due = !full_recovery.is_empty();
            if let Ok(mut watcher_status) = status.lock() {
                watcher_status.queue_overflow_recoveries =
                    watcher_status.queue_overflow_recoveries.saturating_add(1);
            }
        }

        let now = Instant::now();
        if now >= next_probe_at {
            probe_watched_roots(
                &core,
                &mut watcher,
                &mut desired,
                &mut active,
                &mut full_recovery,
                &cancellation,
                &status,
            );
            next_probe_at = now + WATCH_ROOT_RETRY;
            recovery_due |= !full_recovery.is_empty();
        }
        let quiet = last_event_at.is_some_and(|last| now.duration_since(last) >= WATCH_DEBOUNCE);
        let max_age = batch_started_at
            .is_some_and(|started| now.duration_since(started) >= WATCH_MAX_COALESCE);
        let recovery_retry = recovery_due && last_event_at.is_none() && now >= next_recovery_at;
        if quiet || max_age || recovery_retry {
            flush_watcher_work(
                &core,
                &mut desired,
                &mut pending,
                &mut full_recovery,
                &cancellation,
                &status,
            );
            last_event_at = None;
            batch_started_at = None;
            recovery_due = false;
            next_recovery_at = now + WATCH_ROOT_RETRY;
        }
        if now >= next_schedule_poll_at {
            // Live notify work wins when both paths become due in the same tick. Timed policies
            // intentionally use full reconciliation, so do not make an already-coalesced live
            // change wait behind that crawl.
            run_due_scheduled_refreshes(
                &core,
                &cancellation,
                &status,
                &mut next_scheduled_attempt,
                &mut next_scheduled_probe,
                &mut scheduled_task,
                &sender,
            );
            next_schedule_poll_at = Instant::now() + SCHEDULE_POLL;
        }
        update_watcher_counts(&status, &active, &pending, &full_recovery);
    }

    cancellation.cancel();
    // Disconnect before joining the scheduled worker. A completion/control sender may be
    // blocked behind a saturated bounded queue; dropping the receiver makes that send fail
    // closed instead of creating a shutdown join cycle.
    drop(receiver);
    if let Some(task) = scheduled_task.take()
        && task.runner.join().is_err()
    {
        record_watcher_error(&status, "scheduled refresh worker panicked during shutdown");
    }
    if let Ok(mut watcher_status) = status.lock() {
        watcher_status.scheduled_in_flight = 0;
    }
    for path in active.into_values() {
        let _ = watcher.unwatch(&path);
    }
    update_watcher_counts(&status, &HashMap::new(), &HashMap::new(), &HashSet::new());
}

pub struct AgentServer {
    listener: TcpListener,
    shared: Arc<SharedState>,
    endpoint_path: PathBuf,
    root_watcher_thread: Mutex<Option<thread::JoinHandle<()>>>,
}

impl AgentServer {
    pub fn bind(
        config: CoreConfig,
        endpoint_path: Option<PathBuf>,
    ) -> Result<Self, AgentStartError> {
        Self::bind_with_workspace_catalog_and_http_api(config, endpoint_path, None, false)
    }

    pub fn bind_with_workspace_catalog(
        config: CoreConfig,
        endpoint_path: Option<PathBuf>,
        workspace_catalog: Option<PathBuf>,
    ) -> Result<Self, AgentStartError> {
        Self::bind_with_workspace_catalog_and_http_api(
            config,
            endpoint_path,
            workspace_catalog,
            false,
        )
    }

    pub fn bind_with_workspace_catalog_and_http_api(
        config: CoreConfig,
        endpoint_path: Option<PathBuf>,
        workspace_catalog: Option<PathBuf>,
        http_api_enabled: bool,
    ) -> Result<Self, AgentStartError> {
        let endpoint_path =
            endpoint_path.unwrap_or_else(|| config.state_dir.join(DEFAULT_ENDPOINT_FILE));
        let core = Arc::new(Core::open(config)?);
        let mut workspace_catalog_state = WorkspaceCatalogState::new(workspace_catalog);
        if let Some(path) = workspace_catalog_state.path.as_deref() {
            let token = CancellationToken::new();
            match replace_workspace_catalog_from_path(&core, path, &token) {
                Ok(refresh) => workspace_catalog_state.record_success(refresh),
                Err(error) => workspace_catalog_state.record_failure(&error),
            }
        }
        let listener = TcpListener::bind(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0))?;
        let listener_address = listener.local_addr()?;
        let (root_watcher, root_watcher_thread) = start_root_watcher(Arc::clone(&core))?;
        root_watcher.sync(core.list_roots(), true);
        let token = Uuid::new_v4().simple().to_string();
        let endpoint = AgentEndpoint {
            schema_version: 1,
            protocol_version: AGENT_PROTOCOL_VERSION,
            process_id: std::process::id(),
            address: listener_address,
            token: token.clone(),
        };
        if let Err(error) = write_endpoint(&endpoint_path, &endpoint) {
            root_watcher.stop();
            let _ = root_watcher_thread.join();
            return Err(error);
        }
        Ok(Self {
            listener,
            shared: Arc::new(SharedState {
                core,
                token,
                listener_address,
                http_api_enabled,
                requests: Mutex::new(HashMap::new()),
                active_connections: Mutex::new(0),
                connections_drained: Condvar::new(),
                workspace_catalog: Mutex::new(workspace_catalog_state),
                root_watcher,
                stopping: AtomicBool::new(false),
            }),
            endpoint_path,
            root_watcher_thread: Mutex::new(Some(root_watcher_thread)),
        })
    }

    pub fn endpoint(&self) -> Result<AgentEndpoint, AgentStartError> {
        let encoded = fs::read_to_string(&self.endpoint_path)?;
        Ok(serde_json::from_str(&encoded)?)
    }

    pub fn run(&self) -> Result<(), AgentStartError> {
        while !self.shared.stopping.load(Ordering::Acquire) {
            let (stream, _peer) = self.listener.accept()?;
            if self.shared.stopping.load(Ordering::Acquire) {
                drop(stream);
                break;
            }
            let shared = Arc::clone(&self.shared);
            shared.connection_started()?;
            thread::spawn(move || {
                let _ = handle_connection(stream, Arc::clone(&shared));
                shared.connection_finished();
            });
        }
        self.shared.root_watcher.stop();
        self.shared.cancel_active_requests();
        self.shared.wait_for_connections(IO_TIMEOUT)?;
        self.join_root_watcher()?;
        Ok(())
    }

    fn join_root_watcher(&self) -> Result<(), AgentStartError> {
        let mut guard = self
            .root_watcher_thread
            .lock()
            .map_err(|_| io::Error::other("root watcher thread lock poisoned"))?;
        if let Some(runner) = guard.take() {
            runner
                .join()
                .map_err(|_| io::Error::other("root watcher thread panicked"))?;
        }
        Ok(())
    }
}

impl Drop for AgentServer {
    fn drop(&mut self) {
        self.shared.root_watcher.stop();
        if let Ok(mut guard) = self.root_watcher_thread.lock()
            && let Some(runner) = guard.take()
        {
            let _ = runner.join();
        }
        if endpoint_belongs_to(&self.endpoint_path, &self.shared.token) {
            let _ = fs::remove_file(&self.endpoint_path);
        }
    }
}

#[derive(Debug)]
pub enum AgentStartError {
    Io(io::Error),
    Core(CoreError),
    Json(serde_json::Error),
}

impl std::fmt::Display for AgentStartError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(error) => write!(formatter, "{error}"),
            Self::Core(error) => write!(formatter, "{error}"),
            Self::Json(error) => write!(formatter, "{error}"),
        }
    }
}

impl std::error::Error for AgentStartError {}

impl From<io::Error> for AgentStartError {
    fn from(value: io::Error) -> Self {
        Self::Io(value)
    }
}

impl From<CoreError> for AgentStartError {
    fn from(value: CoreError) -> Self {
        Self::Core(value)
    }
}

impl From<serde_json::Error> for AgentStartError {
    fn from(value: serde_json::Error) -> Self {
        Self::Json(value)
    }
}

pub fn load_endpoint(path: &Path) -> Result<AgentEndpoint, AgentStartError> {
    let encoded = fs::read_to_string(path)?;
    let endpoint: AgentEndpoint = serde_json::from_str(&encoded)?;
    if endpoint.schema_version != 1 || endpoint.protocol_version != AGENT_PROTOCOL_VERSION {
        return Err(AgentStartError::Io(io::Error::new(
            io::ErrorKind::InvalidData,
            "unsupported Listary integration endpoint version",
        )));
    }
    if !endpoint.address.ip().is_loopback() || endpoint.token.is_empty() {
        return Err(AgentStartError::Io(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "Listary integration endpoint is not loopback authenticated",
        )));
    }
    Ok(endpoint)
}

pub fn send_request(
    endpoint: &AgentEndpoint,
    method: &str,
    request_id: Option<u64>,
    params: Value,
) -> Result<Value, AgentStartError> {
    let request = json!({
        "token": endpoint.token,
        "id": Uuid::new_v4().to_string(),
        "method": method,
        "request_id": request_id,
        "params": params,
    });
    let mut stream = TcpStream::connect_timeout(&endpoint.address, IO_TIMEOUT)?;
    stream.set_read_timeout(Some(IO_TIMEOUT))?;
    stream.set_write_timeout(Some(IO_TIMEOUT))?;
    let mut encoded = serde_json::to_vec(&request)?;
    encoded.push(b'\n');
    stream.write_all(&encoded)?;
    stream.flush()?;
    let response_bytes = read_bounded_line(&mut stream)?;
    let response: Value = serde_json::from_slice(&response_bytes)?;
    Ok(response)
}

fn handle_connection(mut stream: TcpStream, shared: Arc<SharedState>) -> io::Result<()> {
    stream.set_nonblocking(false)?;
    stream.set_nodelay(true)?;
    stream.set_read_timeout(Some(CONNECTION_READ_POLL))?;
    stream.set_write_timeout(Some(IO_TIMEOUT))?;
    loop {
        let Some(request_bytes) = read_bounded_line_until_stopping(&mut stream, &shared.stopping)?
        else {
            return Ok(());
        };
        if is_http_request_line(&request_bytes) {
            return handle_http_request(&mut stream, &shared, &request_bytes);
        }
        write_framed_response(
            &mut stream,
            &response_for_framed_request(&shared, &request_bytes),
        )?;
        if shared.stopping.load(Ordering::Acquire) {
            return Ok(());
        }
    }
}

fn response_for_framed_request(shared: &SharedState, request_bytes: &[u8]) -> AgentResponse {
    match serde_json::from_slice::<AgentRequest>(request_bytes) {
        Ok(request) => dispatch(shared, &request),
        Err(error) => failure(None, "invalid_request", &error.to_string()),
    }
}

fn write_framed_response(stream: &mut TcpStream, response: &AgentResponse) -> io::Result<()> {
    let mut encoded = serde_json::to_vec(response)
        .map_err(|error| io::Error::other(format!("response serialization failed: {error}")))?;
    encoded.push(b'\n');
    stream.write_all(&encoded)?;
    stream.flush()
}

fn is_http_request_line(request_line: &[u8]) -> bool {
    let Ok(line) = std::str::from_utf8(request_line) else {
        return false;
    };
    let line = line.trim_end_matches('\r');
    let mut parts = line.split_ascii_whitespace();
    let Some(method) = parts.next() else {
        return false;
    };
    let Some(_target) = parts.next() else {
        return false;
    };
    let Some(version) = parts.next() else {
        return false;
    };
    parts.next().is_none()
        && method.bytes().all(|byte| byte.is_ascii_uppercase())
        && version.starts_with("HTTP/")
}

fn handle_http_request(
    stream: &mut TcpStream,
    shared: &SharedState,
    request_line: &[u8],
) -> io::Result<()> {
    let request_line = match std::str::from_utf8(request_line) {
        Ok(line) => line.trim_end_matches('\r'),
        Err(_) => {
            return write_http_error(
                stream,
                400,
                "Bad Request",
                "invalid_request",
                "HTTP request line must be UTF-8 ASCII-compatible text",
            );
        }
    };
    let mut parts = request_line.split_ascii_whitespace();
    let method = parts.next().unwrap_or_default();
    let target = parts.next().unwrap_or_default();
    let version = parts.next().unwrap_or_default();
    if parts.next().is_some() || !matches!(version, "HTTP/1.0" | "HTTP/1.1") {
        return write_http_error(
            stream,
            400,
            "Bad Request",
            "invalid_request",
            "HTTP request line must use HTTP/1.0 or HTTP/1.1",
        );
    }
    if method != "POST" {
        return write_http_error(
            stream,
            405,
            "Method Not Allowed",
            "method_not_allowed",
            "local automation accepts POST only",
        );
    }
    if target != HTTP_RPC_PATH {
        return write_http_error(
            stream,
            404,
            "Not Found",
            "not_found",
            "unknown local automation path",
        );
    }

    let headers = match read_http_headers(stream, &shared.stopping) {
        Ok(headers) => headers,
        Err(error) if error.kind() == io::ErrorKind::InvalidData => {
            return write_http_error(
                stream,
                400,
                "Bad Request",
                "invalid_request",
                &error.to_string(),
            );
        }
        Err(error) => return Err(error),
    };
    if headers.transfer_encoding_present {
        return write_http_error(
            stream,
            400,
            "Bad Request",
            "invalid_request",
            "Transfer-Encoding is not supported; send one bounded Content-Length body",
        );
    }
    if headers.expect_present {
        return write_http_error(
            stream,
            417,
            "Expectation Failed",
            "invalid_request",
            "Expect is not supported by the local automation endpoint",
        );
    }
    let Some(content_length) = headers.content_length else {
        return write_http_error(
            stream,
            411,
            "Length Required",
            "length_required",
            "Content-Length is required",
        );
    };
    if content_length > MAX_REQUEST_BYTES as u64 {
        return write_http_error(
            stream,
            413,
            "Payload Too Large",
            "payload_too_large",
            "local automation request exceeds 8 MiB",
        );
    }
    // Consume the already-bounded declared body before emitting an application-level HTTP
    // rejection. On Windows, closing a socket with unread request bytes can turn a useful
    // 401/403 response into WSAECONNRESET for the caller. Unauthorized bodies are never parsed
    // or dispatched; this read only gives local automation clients deterministic HTTP errors.
    let body = read_exact_until_stopping(stream, content_length as usize, &shared.stopping)?;
    if !shared.http_api_enabled {
        return write_http_error(
            stream,
            404,
            "Not Found",
            "http_api_disabled",
            "local HTTP automation is disabled for this agent run",
        );
    }
    if headers.origin_present {
        return write_http_error(
            stream,
            403,
            "Forbidden",
            "browser_origin_rejected",
            "browser-origin requests are not accepted by the local automation endpoint",
        );
    }
    let Some(authorization) = headers.authorization.as_deref() else {
        return write_http_error(
            stream,
            401,
            "Unauthorized",
            "authentication_failed",
            "missing bearer token",
        );
    };
    let Some(token) = bearer_token(authorization) else {
        return write_http_error(
            stream,
            401,
            "Unauthorized",
            "authentication_failed",
            "invalid bearer token",
        );
    };
    if token != shared.token {
        return write_http_error(
            stream,
            401,
            "Unauthorized",
            "authentication_failed",
            "invalid bearer token",
        );
    }
    let Some(content_type) = headers.content_type.as_deref() else {
        return write_http_error(
            stream,
            415,
            "Unsupported Media Type",
            "unsupported_media_type",
            "Content-Type must be application/json",
        );
    };
    if !content_type
        .split(';')
        .next()
        .is_some_and(|value| value.trim().eq_ignore_ascii_case("application/json"))
    {
        return write_http_error(
            stream,
            415,
            "Unsupported Media Type",
            "unsupported_media_type",
            "Content-Type must be application/json",
        );
    }
    let parsed = match serde_json::from_slice::<HttpRpcRequest>(&body) {
        Ok(request) => request,
        Err(error) => {
            return write_http_error(
                stream,
                400,
                "Bad Request",
                "invalid_request",
                &error.to_string(),
            );
        }
    };
    if !http_method_allowed(&parsed.method) {
        let response = failure(
            parsed.id.as_ref(),
            "http_method_not_allowed",
            "HTTP automation exposes read/search/cancel methods only",
        );
        return write_http_response(stream, 403, "Forbidden", &response);
    }
    let request = AgentRequest {
        token: token.to_string(),
        id: parsed.id,
        method: parsed.method,
        request_id: parsed.request_id,
        params: parsed.params,
    };
    let response = dispatch(shared, &request);
    write_http_response(stream, 200, "OK", &response)
}

fn http_method_allowed(method: &str) -> bool {
    matches!(
        method,
        "status"
            | "workspace_catalog_status"
            | "search"
            | "file_search"
            | "facet_counts"
            | "get_item"
            | "get_favorites"
            | "list_filters"
            | "list_commands"
            | "list_roots"
            | "diagnostics"
            | "cancel"
    )
}

fn bearer_token(value: &str) -> Option<&str> {
    let (scheme, token) = value.trim().split_once(' ')?;
    let token = token.trim();
    if scheme.eq_ignore_ascii_case("bearer")
        && !token.is_empty()
        && !token.bytes().any(|byte| byte.is_ascii_whitespace())
    {
        Some(token)
    } else {
        None
    }
}

fn read_http_headers(reader: &mut impl Read, stopping: &AtomicBool) -> io::Result<HttpHeaders> {
    let mut headers = HttpHeaders::default();
    let mut total_bytes = 0usize;
    loop {
        let Some(line) = read_http_line_until_stopping(reader, stopping)? else {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "HTTP headers ended before the blank separator line",
            ));
        };
        total_bytes = total_bytes.saturating_add(line.len() + 1);
        if total_bytes > MAX_HTTP_HEADER_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "HTTP headers exceed 32 KiB",
            ));
        }
        let line = std::str::from_utf8(&line)
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "HTTP header is not UTF-8"))?
            .trim_end_matches('\r');
        if line.is_empty() {
            return Ok(headers);
        }
        let (name, value) = line.split_once(':').ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "HTTP header is missing ':'")
        })?;
        let name = name.trim();
        if name.is_empty()
            || !name
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "HTTP header name is invalid",
            ));
        }
        let value = value.trim();
        if name.eq_ignore_ascii_case("authorization") {
            set_unique_header(&mut headers.authorization, value, "Authorization")?;
        } else if name.eq_ignore_ascii_case("content-length") {
            if headers.content_length.is_some() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "duplicate Content-Length header",
                ));
            }
            headers.content_length = Some(value.parse::<u64>().map_err(|_| {
                io::Error::new(io::ErrorKind::InvalidData, "invalid Content-Length header")
            })?);
        } else if name.eq_ignore_ascii_case("content-type") {
            set_unique_header(&mut headers.content_type, value, "Content-Type")?;
        } else if name.eq_ignore_ascii_case("origin") {
            if headers.origin_present {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "duplicate Origin header",
                ));
            }
            headers.origin_present = true;
        } else if name.eq_ignore_ascii_case("transfer-encoding") {
            headers.transfer_encoding_present = true;
        } else if name.eq_ignore_ascii_case("expect") {
            headers.expect_present = true;
        }
    }
}

fn set_unique_header(
    destination: &mut Option<String>,
    value: &str,
    header_name: &str,
) -> io::Result<()> {
    if destination.is_some() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("duplicate {header_name} header"),
        ));
    }
    *destination = Some(value.to_string());
    Ok(())
}

fn read_http_line_until_stopping(
    reader: &mut impl Read,
    stopping: &AtomicBool,
) -> io::Result<Option<Vec<u8>>> {
    let mut bytes = Vec::with_capacity(256);
    let mut one = [0_u8; 1];
    while bytes.len() <= MAX_HTTP_LINE_BYTES {
        match reader.read(&mut one) {
            Ok(0) if bytes.is_empty() => return Ok(None),
            Ok(0) => return Ok(Some(bytes)),
            Ok(_) if one[0] == b'\n' => return Ok(Some(bytes)),
            Ok(_) => bytes.push(one[0]),
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock
                ) =>
            {
                if stopping.load(Ordering::Acquire) {
                    return Ok(None);
                }
            }
            Err(error) => return Err(error),
        }
    }
    Err(io::Error::new(
        io::ErrorKind::InvalidData,
        "HTTP header line exceeds 8 KiB",
    ))
}

fn read_exact_until_stopping(
    reader: &mut impl Read,
    length: usize,
    stopping: &AtomicBool,
) -> io::Result<Vec<u8>> {
    let mut body = vec![0_u8; length];
    let mut offset = 0usize;
    while offset < body.len() {
        match reader.read(&mut body[offset..]) {
            Ok(0) => {
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "HTTP body ended before Content-Length bytes were received",
                ));
            }
            Ok(read) => offset += read,
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock
                ) =>
            {
                if stopping.load(Ordering::Acquire) {
                    return Err(io::Error::new(
                        io::ErrorKind::Interrupted,
                        "agent is stopping",
                    ));
                }
            }
            Err(error) => return Err(error),
        }
    }
    Ok(body)
}

fn write_http_error(
    stream: &mut TcpStream,
    status: u16,
    reason: &'static str,
    code: &'static str,
    message: &str,
) -> io::Result<()> {
    write_http_response(stream, status, reason, &failure(None, code, message))
}

fn write_http_response(
    stream: &mut TcpStream,
    status: u16,
    reason: &'static str,
    response: &AgentResponse,
) -> io::Result<()> {
    let body = serde_json::to_vec(response)
        .map_err(|error| io::Error::other(format!("response serialization failed: {error}")))?;
    let mut header = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: {}\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nConnection: close\r\n",
        body.len()
    );
    if status == 401 {
        header.push_str("WWW-Authenticate: Bearer realm=\"Spiffy Listary\"\r\n");
    }
    if status == 405 {
        header.push_str("Allow: POST\r\n");
    }
    header.push_str("\r\n");
    stream.write_all(header.as_bytes())?;
    stream.write_all(&body)?;
    stream.flush()
}

fn dispatch(shared: &SharedState, request: &AgentRequest) -> AgentResponse {
    if request.token != shared.token {
        return failure(
            request.id.as_ref(),
            "authentication_failed",
            "invalid bearer token",
        );
    }
    let result = match request.method.as_str() {
        "status" => shared.core.diagnostics().map(|diagnostics| {
            json!({
                "protocol_version": AGENT_PROTOCOL_VERSION,
                "core_schema_version": diagnostics.schema_version,
                "diagnostics": diagnostics,
                "root_watcher": shared.root_watcher.status(),
                "http_api": {
                    "enabled": shared.http_api_enabled,
                    "path": HTTP_RPC_PATH,
                    "authorization": "bearer",
                    "same_listener": true,
                    "browser_origins": false
                }
            })
        }),
        "workspace_catalog_status" => require_empty_params(&request.params).and_then(|()| {
            let status = shared
                .workspace_catalog
                .lock()
                .map_err(|_| CoreError::Index("workspace catalog state lock poisoned".into()))?
                .status
                .clone();
            to_value(status)
        }),
        "workspace_catalog_refresh" => require_empty_params(&request.params).and_then(|()| {
            with_request(shared, request.request_id, |token| {
                refresh_configured_workspace_catalog(shared, token)
            })
            .and_then(to_value)
        }),
        "search" => decode_params::<SearchRequest>(&request.params)
            .and_then(|params| {
                with_request(shared, request.request_id, |token| {
                    shared.core.search(&params, token)
                })
            })
            .and_then(to_value),
        "file_search" => decode_params::<FileSearchRequest>(&request.params)
            .and_then(|params| {
                with_request(shared, request.request_id, |token| {
                    shared.core.file_search(&params, token)
                })
            })
            .and_then(to_value),
        "facet_counts" => decode_params::<FacetCountsRequest>(&request.params)
            .and_then(|params| {
                with_request(shared, request.request_id, |token| {
                    shared.core.facet_counts(&params, token)
                })
            })
            .and_then(to_value),
        "get_item" => decode_params::<ItemIdParams>(&request.params)
            .and_then(|params| to_value(shared.core.get_item(params.item_id))),
        "get_favorites" => require_empty_params(&request.params)
            .and_then(|()| to_value(shared.core.get_favorites())),
        "toggle_pin" => decode_params::<ItemIdParams>(&request.params).and_then(|params| {
            let pinned = shared.core.toggle_pin(params.item_id)?;
            Ok(json!({"pinned": pinned}))
        }),
        "list_filters" => require_empty_params(&request.params)
            .and_then(|()| to_value(shared.core.list_filters())),
        "put_filter" => decode_params::<FilterPreset>(&request.params)
            .and_then(|preset| shared.core.put_filter(preset))
            .and_then(to_value),
        "remove_filter" => decode_params::<FilterIdParams>(&request.params).and_then(|params| {
            shared.core.remove_filter(params.filter_id)?;
            Ok(json!({"removed": true}))
        }),
        "list_commands" => to_value(shared.core.list_commands()),
        "list_roots" => to_value(shared.core.list_roots()),
        "add_root" => decode_params::<AddRootParams>(&request.params).and_then(|params| {
            let root = shared.core.add_root(params.path, params.kind)?;
            shared.root_watcher.sync(shared.core.list_roots(), false);
            to_value(root)
        }),
        "put_root" => decode_params::<RootRecord>(&request.params).and_then(|root| {
            let root_id = root.id;
            shared.core.put_root(root)?;
            let canonical = shared
                .core
                .list_roots()
                .into_iter()
                .find(|candidate| candidate.id == root_id)
                .ok_or_else(|| CoreError::RootNotFound(root_id.to_string()))?;
            shared.root_watcher.sync(shared.core.list_roots(), false);
            to_value(canonical)
        }),
        "remove_root" => decode_params::<RootIdParams>(&request.params).and_then(|params| {
            shared.core.remove_root(params.root_id)?;
            shared.root_watcher.sync(shared.core.list_roots(), false);
            Ok(json!({"removed": true}))
        }),
        "reconcile_root" => decode_params::<RootIdParams>(&request.params).and_then(|params| {
            let result = with_request(shared, request.request_id, |token| {
                shared.core.reconcile_root(params.root_id, token)
            });
            shared.root_watcher.sync(shared.core.list_roots(), false);
            result.and_then(to_value)
        }),
        "diagnostics" => shared.core.diagnostics().and_then(to_value),
        "record_activation" => {
            decode_params::<RecordActivationParams>(&request.params).and_then(|params| {
                shared.core.record_activation_with_source(
                    params.item_id,
                    params.now_unix_ms,
                    params.source,
                )?;
                Ok(json!({"recorded": true}))
            })
        }
        "cancel" => {
            let request_id = request.request_id.ok_or_else(|| {
                CoreError::InvalidRequest("cancel requires a nonzero request_id".into())
            });
            request_id.and_then(|request_id| {
                if request_id == 0 {
                    return Err(CoreError::InvalidRequest(
                        "cancel requires a nonzero request_id".into(),
                    ));
                }
                let token = shared
                    .requests
                    .lock()
                    .map_err(|_| CoreError::Index("agent request map lock poisoned".into()))?
                    .get(&request_id)
                    .cloned();
                match token {
                    Some(token) => {
                        token.cancel();
                        Ok(json!({"cancelled": true}))
                    }
                    None => Ok(json!({"cancelled": false})),
                }
            })
        }
        "shutdown" => {
            if !shared.stopping.swap(true, Ordering::AcqRel) {
                shared.root_watcher.stop();
                shared.cancel_active_requests();
                let _ = TcpStream::connect_timeout(
                    &shared.listener_address,
                    Duration::from_millis(250),
                );
            }
            Ok(json!({"stopping": true}))
        }
        _ => Err(CoreError::InvalidRequest(format!(
            "unknown integration method: {}",
            request.method
        ))),
    };

    match result {
        Ok(data) => AgentResponse {
            id: request.id.clone(),
            ok: true,
            data: Some(data),
            error: None,
        },
        Err(error) => failure(
            request.id.as_ref(),
            core_error_code(&error),
            &error.to_string(),
        ),
    }
}

fn with_request<T>(
    shared: &SharedState,
    request_id: Option<u64>,
    operation: impl FnOnce(&CancellationToken) -> listary_core::Result<T>,
) -> listary_core::Result<T> {
    let request_id = request_id.ok_or_else(|| {
        CoreError::InvalidRequest("search methods require a nonzero request_id".into())
    })?;
    if request_id == 0 {
        return Err(CoreError::InvalidRequest(
            "search methods require a nonzero request_id".into(),
        ));
    }
    let token = CancellationToken::new();
    {
        let mut requests = shared
            .requests
            .lock()
            .map_err(|_| CoreError::Index("agent request map lock poisoned".into()))?;
        if requests.contains_key(&request_id) {
            return Err(CoreError::InvalidRequest(format!(
                "request_id is already active: {request_id}"
            )));
        }
        requests.insert(request_id, token.clone());
    }
    let result = operation(&token);
    if let Ok(mut requests) = shared.requests.lock() {
        requests.remove(&request_id);
    }
    result
}

fn decode_params<T: for<'de> Deserialize<'de>>(value: &Value) -> listary_core::Result<T> {
    serde_json::from_value(value.clone()).map_err(CoreError::from)
}

fn to_value<T: Serialize>(value: T) -> listary_core::Result<Value> {
    serde_json::to_value(value).map_err(CoreError::from)
}

fn require_empty_params(value: &Value) -> listary_core::Result<()> {
    match value {
        Value::Null => Ok(()),
        Value::Object(map) if map.is_empty() => Ok(()),
        _ => Err(CoreError::InvalidRequest(
            "workspace catalog methods do not accept caller-supplied parameters".into(),
        )),
    }
}

fn replace_workspace_catalog_from_path(
    core: &Core,
    path: &Path,
    token: &CancellationToken,
) -> listary_core::Result<WorkspaceCatalogRefresh> {
    let platform = WorkspaceCatalogPlatform::current()?;
    let snapshot = load_workspace_catalog(path, platform)?;
    core.replace_workspace_catalog_snapshot(snapshot, token)
}

fn refresh_configured_workspace_catalog(
    shared: &SharedState,
    token: &CancellationToken,
) -> listary_core::Result<WorkspaceCatalogRefresh> {
    let path = shared
        .workspace_catalog
        .lock()
        .map_err(|_| CoreError::Index("workspace catalog state lock poisoned".into()))?
        .path
        .clone()
        .ok_or_else(|| CoreError::InvalidRequest("workspace catalog is not configured".into()))?;

    let result = replace_workspace_catalog_from_path(&shared.core, &path, token);
    let mut state = shared
        .workspace_catalog
        .lock()
        .map_err(|_| CoreError::Index("workspace catalog state lock poisoned".into()))?;
    match &result {
        Ok(refresh) => state.record_success(refresh.clone()),
        Err(error) => state.record_failure(error),
    }
    result
}

fn failure(id: Option<&Value>, code: &'static str, message: &str) -> AgentResponse {
    AgentResponse {
        id: id.cloned(),
        ok: false,
        data: None,
        error: Some(AgentError {
            code,
            message: message.to_string(),
        }),
    }
}

fn core_error_code(error: &CoreError) -> &'static str {
    match error {
        CoreError::AlreadyOpen => "already_open",
        CoreError::Cancelled => "cancelled",
        CoreError::StaleCatalog { .. } => "stale_catalog",
        CoreError::RootNotFound(_) => "root_not_found",
        CoreError::FilterNotFound(_) => "filter_not_found",
        CoreError::CommandNotFound(_) => "command_not_found",
        CoreError::ActionNotFound(_) => "action_not_found",
        CoreError::InvalidRequest(_) => "invalid_request",
        CoreError::Index(_) => "index_error",
        CoreError::Serialization(_) => "serialization_error",
        CoreError::Filesystem { .. } => "filesystem_error",
    }
}

fn read_bounded_line(reader: &mut impl Read) -> io::Result<Vec<u8>> {
    let mut bytes = Vec::with_capacity(4096);
    let mut one = [0_u8; 1];
    while bytes.len() <= MAX_REQUEST_BYTES {
        match reader.read(&mut one) {
            Ok(0) => break,
            Ok(_) if one[0] == b'\n' => return Ok(bytes),
            Ok(_) => bytes.push(one[0]),
            Err(error) => return Err(error),
        }
    }
    if bytes.len() > MAX_REQUEST_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "integration request exceeds 8 MiB",
        ));
    }
    if bytes.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::UnexpectedEof,
            "integration request was empty",
        ));
    }
    Ok(bytes)
}

fn read_bounded_line_until_stopping(
    reader: &mut impl Read,
    stopping: &AtomicBool,
) -> io::Result<Option<Vec<u8>>> {
    let mut bytes = Vec::with_capacity(4096);
    let mut one = [0_u8; 1];
    while bytes.len() <= MAX_REQUEST_BYTES {
        match reader.read(&mut one) {
            Ok(0) if bytes.is_empty() => return Ok(None),
            Ok(0) => return Ok(Some(bytes)),
            Ok(_) if one[0] == b'\n' => return Ok(Some(bytes)),
            Ok(_) => bytes.push(one[0]),
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock
                ) =>
            {
                if stopping.load(Ordering::Acquire) {
                    return Ok(None);
                }
            }
            Err(error) => return Err(error),
        }
    }
    Err(io::Error::new(
        io::ErrorKind::InvalidData,
        "integration request exceeds 8 MiB",
    ))
}

fn write_endpoint(path: &Path, endpoint: &AgentEndpoint) -> Result<(), AgentStartError> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let encoded = serde_json::to_vec_pretty(endpoint)?;
    let temporary = path.with_extension(format!("{}.tmp", std::process::id()));
    fs::write(&temporary, encoded)?;
    restrict_owner_only(&temporary)?;
    if path.exists() {
        fs::remove_file(path)?;
    }
    fs::rename(&temporary, path)?;
    Ok(())
}

#[cfg(unix)]
fn restrict_owner_only(path: &Path) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let mut permissions = fs::metadata(path)?.permissions();
    permissions.set_mode(0o600);
    fs::set_permissions(path, permissions)
}

#[cfg(not(unix))]
fn restrict_owner_only(_path: &Path) -> io::Result<()> {
    Ok(())
}

fn endpoint_belongs_to(path: &Path, token: &str) -> bool {
    load_endpoint(path)
        .map(|endpoint| endpoint.token == token && endpoint.process_id == std::process::id())
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Instant, SystemTime, UNIX_EPOCH};

    fn now_ms() -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis() as i64
    }

    fn search_request(query: &str) -> Value {
        json!({
            "query": query,
            "limit": 10,
            "candidate_limit": 4096,
            "include_unavailable": true,
            "root_ids": [],
            "kinds": ["app"],
            "categories": [],
            "modified_time": null,
            "context": {"current_folder": null, "contextual": false},
            "now_unix_ms": now_ms(),
            "timezone_offset_minutes": 0
        })
    }

    fn file_name_search_request(query: &str) -> Value {
        json!({
            "query": query,
            "limit": 50,
            "candidate_limit": 4096,
            "include_unavailable": true,
            "root_ids": [],
            "kinds": ["file"],
            "categories": [],
            "modified_time": null,
            "context": {"current_folder": null, "contextual": false},
            "now_unix_ms": now_ms(),
            "timezone_offset_minutes": 0
        })
    }

    fn send_http_rpc(
        endpoint: &AgentEndpoint,
        bearer: &str,
        body: Value,
        origin: Option<&str>,
    ) -> (u16, String, Value) {
        let encoded = serde_json::to_vec(&body).unwrap();
        let mut stream = TcpStream::connect_timeout(&endpoint.address, IO_TIMEOUT).unwrap();
        stream.set_read_timeout(Some(IO_TIMEOUT)).unwrap();
        stream.set_write_timeout(Some(IO_TIMEOUT)).unwrap();
        let mut request = format!(
            "POST {HTTP_RPC_PATH} HTTP/1.1\r\nHost: {}\r\nAuthorization: Bearer {bearer}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n",
            endpoint.address,
            encoded.len()
        );
        if let Some(origin) = origin {
            request.push_str(&format!("Origin: {origin}\r\n"));
        }
        request.push_str("\r\n");
        stream.write_all(request.as_bytes()).unwrap();
        stream.write_all(&encoded).unwrap();
        stream.flush().unwrap();

        let mut response = Vec::new();
        stream.read_to_end(&mut response).unwrap();
        let separator = response
            .windows(4)
            .position(|window| window == b"\r\n\r\n")
            .expect("HTTP response must contain a header/body separator");
        let headers = String::from_utf8(response[..separator].to_vec()).unwrap();
        let status = headers
            .lines()
            .next()
            .unwrap()
            .split_ascii_whitespace()
            .nth(1)
            .unwrap()
            .parse::<u16>()
            .unwrap();
        let body: Value = serde_json::from_slice(&response[(separator + 4)..]).unwrap();
        (status, headers, body)
    }

    fn search_file_rows(endpoint: &AgentEndpoint, query: &str) -> Vec<Value> {
        let response = send_request(
            endpoint,
            "search",
            Some(70_000),
            file_name_search_request(query),
        )
        .unwrap();
        assert_eq!(response["ok"], true, "search failed: {response}");
        response["data"]["results"].as_array().unwrap().clone()
    }

    fn wait_for_file_search(
        endpoint: &AgentEndpoint,
        query: &str,
        predicate: impl Fn(&[Value]) -> bool,
    ) -> Vec<Value> {
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            let rows = search_file_rows(endpoint, query);
            if predicate(&rows) {
                return rows;
            }
            assert!(
                Instant::now() < deadline,
                "timed out waiting for live search state for {query}: {rows:?}"
            );
            thread::sleep(Duration::from_millis(25));
        }
    }

    fn set_test_root_refresh_policy(
        server: &AgentServer,
        root_id: Uuid,
        refresh_policy: RefreshPolicy,
    ) {
        let mut root = server
            .shared
            .core
            .list_roots()
            .into_iter()
            .find(|root| root.id == root_id)
            .unwrap();
        root.refresh_policy = refresh_policy;
        server.shared.core.put_root(root).unwrap();
        server
            .shared
            .root_watcher
            .sync(server.shared.core.list_roots(), false);
    }

    #[test]
    fn watcher_stop_does_not_block_on_a_saturated_queue() {
        let (sender, receiver) = mpsc::sync_channel(1);
        sender
            .send(RootWatcherMessage::Sync {
                roots: Vec::new(),
                recover_untrusted: false,
            })
            .unwrap();
        let handle = RootWatcherHandle {
            sender,
            cancellation: CancellationToken::new(),
            status: Arc::new(Mutex::new(RootWatcherStatus::default())),
            event_queue_overflowed: Arc::new(AtomicBool::new(false)),
        };
        let worker_handle = handle.clone();
        let (done_sender, done_receiver) = mpsc::channel();
        let stopper = thread::spawn(move || {
            worker_handle.stop();
            let _ = done_sender.send(());
        });

        assert!(
            done_receiver
                .recv_timeout(Duration::from_millis(100))
                .is_ok(),
            "watcher stop blocked behind a saturated bounded queue"
        );
        assert!(handle.cancellation.is_cancelled());
        drop(receiver);
        stopper.join().unwrap();
    }

    #[cfg(any(target_os = "windows", target_os = "macos"))]
    fn write_workspace_catalog(path: &Path, launcher: &Path, label: &str, alias: &str) {
        let relative_launcher = launcher
            .strip_prefix(path.parent().unwrap())
            .unwrap()
            .to_string_lossy()
            .replace('\\', "/");
        fs::write(
            path,
            serde_json::to_vec_pretty(&json!({
                "schema": "ss_scripts_workspace_catalog_v1",
                "version": 1,
                "workspace_id": "fixture_workspace",
                "systems": [{
                    "id": "fixture_system",
                    "platforms": ["windows", "macos"],
                    "launch_targets": [{
                        "id": "fixture_launcher",
                        "label": label,
                        "aliases": [alias],
                        "platform_paths": {
                            "windows": relative_launcher,
                            "macos": relative_launcher
                        }
                    }]
                }]
            }))
            .unwrap(),
        )
        .unwrap();
    }

    #[test]
    fn agent_serves_authenticated_search_and_removes_endpoint() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let fixture = temp.path().join("files");
        fs::create_dir_all(&fixture).unwrap();
        fs::write(fixture.join("SpiffyClipboard.exe"), b"fixture").unwrap();

        let endpoint_path = state.join(DEFAULT_ENDPOINT_FILE);
        let server =
            AgentServer::bind(CoreConfig::new(state.clone()), Some(endpoint_path.clone())).unwrap();
        let endpoint = server.endpoint().unwrap();
        let runner = thread::spawn(move || server.run().unwrap());

        let root = send_request(
            &endpoint,
            "add_root",
            None,
            json!({"path": fixture, "kind": "local"}),
        )
        .unwrap();
        assert_eq!(root["ok"], true);
        let root_id = root["data"]["id"].as_str().unwrap().to_string();
        let reconcile = send_request(
            &endpoint,
            "reconcile_root",
            Some(40),
            json!({"root_id": root_id}),
        )
        .unwrap();
        assert_eq!(reconcile["ok"], true);
        assert_eq!(reconcile["data"]["discovered"], 1);

        let status = send_request(&endpoint, "status", None, Value::Null).unwrap();
        assert_eq!(status["ok"], true);
        assert_eq!(status["data"]["diagnostics"]["item_count"], 1);

        let provider =
            send_request(&endpoint, "workspace_catalog_status", None, Value::Null).unwrap();
        assert_eq!(provider["ok"], true);
        assert_eq!(provider["data"]["configured"], false);
        assert_eq!(provider["data"]["loaded"], false);

        let search = send_request(
            &endpoint,
            "search",
            Some(41),
            json!({
                "query": "spiffy clipboard",
                "limit": 10,
                "candidate_limit": 4096,
                "include_unavailable": true,
                "root_ids": [],
                "kinds": ["app"],
                "categories": [],
                "modified_time": null,
                "context": {"current_folder": null, "contextual": false},
                "now_unix_ms": now_ms(),
                "timezone_offset_minutes": 0
            }),
        )
        .unwrap();
        assert_eq!(search["ok"], true);
        assert_eq!(
            search["data"]["results"][0]["item"]["name"],
            "SpiffyClipboard.exe"
        );
        let item_id = search["data"]["results"][0]["item"]["id"].as_u64().unwrap();
        let item = send_request(&endpoint, "get_item", None, json!({"item_id": item_id})).unwrap();
        assert_eq!(item["ok"], true);
        assert_eq!(item["data"]["name"], "SpiffyClipboard.exe");
        let missing =
            send_request(&endpoint, "get_item", None, json!({"item_id": u32::MAX})).unwrap();
        assert_eq!(missing["ok"], true);
        assert!(missing["data"].is_null());

        let favorites = send_request(&endpoint, "get_favorites", None, Value::Null).unwrap();
        assert_eq!(favorites["ok"], true);
        assert_eq!(favorites["data"]["pins"], json!([]));
        let pinned =
            send_request(&endpoint, "toggle_pin", None, json!({"item_id": item_id})).unwrap();
        assert_eq!(pinned["ok"], true);
        assert_eq!(pinned["data"]["pinned"], true);
        let favorites = send_request(&endpoint, "get_favorites", None, Value::Null).unwrap();
        assert_eq!(favorites["data"]["pins"].as_array().unwrap().len(), 1);
        assert_eq!(favorites["data"]["pins"][0]["path"], item["data"]["path"]);

        let zero_query = send_request(
            &endpoint,
            "search",
            Some(42),
            json!({
                "query": "",
                "limit": 10,
                "candidate_limit": 4096,
                "include_unavailable": true,
                "root_ids": [],
                "kinds": ["app"],
                "categories": [],
                "modified_time": null,
                "context": {"current_folder": null, "contextual": false},
                "now_unix_ms": now_ms(),
                "timezone_offset_minutes": 0
            }),
        )
        .unwrap();
        assert_eq!(zero_query["ok"], true);
        assert_eq!(zero_query["data"]["results"][0]["item"]["id"], item_id);

        let roots = send_request(&endpoint, "list_roots", None, Value::Null).unwrap();
        assert_eq!(roots["ok"], true);
        assert_eq!(roots["data"].as_array().unwrap().len(), 1);

        let filter_id = Uuid::new_v4();
        let saved_filter = send_request(
            &endpoint,
            "put_filter",
            None,
            json!({
                "id": filter_id,
                "keyword": "spiffyfixture",
                "name": "Spiffy fixture",
                "extra_query": "spiffy clipboard",
                "search_paths": [],
                "target": "any",
                "extensions": []
            }),
        )
        .unwrap();
        assert_eq!(saved_filter["ok"], true);
        assert_eq!(saved_filter["data"]["id"], filter_id.to_string());
        assert_eq!(saved_filter["data"]["keyword"], "spiffyfixture");
        let filters = send_request(&endpoint, "list_filters", None, Value::Null).unwrap();
        assert_eq!(filters["ok"], true);
        assert_eq!(filters["data"].as_array().unwrap().len(), 1);
        assert_eq!(filters["data"][0]["id"], filter_id.to_string());
        let filtered = send_request(
            &endpoint,
            "search",
            Some(44),
            search_request("spiffyfixture:"),
        )
        .unwrap();
        assert_eq!(filtered["ok"], true);
        assert_eq!(filtered["data"]["results"].as_array().unwrap().len(), 1);
        assert_eq!(
            filtered["data"]["results"][0]["item"]["name"],
            "SpiffyClipboard.exe"
        );

        let rejected = {
            let mut bad = endpoint.clone();
            bad.token = "wrong".into();
            send_request(&bad, "status", None, Value::Null).unwrap()
        };
        assert_eq!(rejected["ok"], false);
        assert_eq!(rejected["error"]["code"], "authentication_failed");

        let shutdown = send_request(&endpoint, "shutdown", None, Value::Null).unwrap();
        assert_eq!(shutdown["ok"], true);
        runner.join().unwrap();

        for _ in 0..50 {
            if !endpoint_path.exists() {
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
        assert!(!endpoint_path.exists());

        let reopened =
            AgentServer::bind(CoreConfig::new(state), Some(endpoint_path.clone())).unwrap();
        let reopened_endpoint = reopened.endpoint().unwrap();
        let reopened_runner = thread::spawn(move || reopened.run().unwrap());
        let favorites =
            send_request(&reopened_endpoint, "get_favorites", None, Value::Null).unwrap();
        assert_eq!(favorites["data"]["pins"].as_array().unwrap().len(), 1);
        let filters = send_request(&reopened_endpoint, "list_filters", None, Value::Null).unwrap();
        assert_eq!(filters["ok"], true);
        assert_eq!(filters["data"].as_array().unwrap().len(), 1);
        assert_eq!(filters["data"][0]["id"], filter_id.to_string());
        let invalid_filter = send_request(
            &reopened_endpoint,
            "put_filter",
            None,
            json!({
                "id": Uuid::new_v4(),
                "keyword": "file",
                "name": "Reserved keyword",
                "extra_query": "",
                "search_paths": [],
                "target": "any",
                "extensions": []
            }),
        )
        .unwrap();
        assert_eq!(invalid_filter["ok"], false);
        assert_eq!(invalid_filter["error"]["code"], "invalid_request");
        let missing_filter = send_request(
            &reopened_endpoint,
            "remove_filter",
            None,
            json!({"filter_id": Uuid::new_v4()}),
        )
        .unwrap();
        assert_eq!(missing_filter["ok"], false);
        assert_eq!(missing_filter["error"]["code"], "filter_not_found");
        let removed = send_request(
            &reopened_endpoint,
            "remove_filter",
            None,
            json!({"filter_id": filter_id}),
        )
        .unwrap();
        assert_eq!(removed["ok"], true);
        assert_eq!(removed["data"]["removed"], true);
        let filters = send_request(&reopened_endpoint, "list_filters", None, Value::Null).unwrap();
        assert_eq!(filters["data"], json!([]));
        let zero_query = send_request(
            &reopened_endpoint,
            "search",
            Some(43),
            json!({
                "query": "",
                "limit": 10,
                "candidate_limit": 4096,
                "include_unavailable": true,
                "root_ids": [],
                "kinds": ["app"],
                "categories": [],
                "modified_time": null,
                "context": {"current_folder": null, "contextual": false},
                "now_unix_ms": now_ms(),
                "timezone_offset_minutes": 0
            }),
        )
        .unwrap();
        assert_eq!(zero_query["data"]["results"][0]["item"]["id"], item_id);
        let unpinned = send_request(
            &reopened_endpoint,
            "toggle_pin",
            None,
            json!({"item_id": item_id}),
        )
        .unwrap();
        assert_eq!(unpinned["data"]["pinned"], false);
        assert_eq!(
            send_request(&reopened_endpoint, "shutdown", None, Value::Null).unwrap()["ok"],
            true
        );
        reopened_runner.join().unwrap();
    }

    #[test]
    fn agent_put_root_persists_canonical_refresh_policy_and_rejects_invalid_schedule() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let fixture = temp.path().join("files");
        fs::create_dir_all(&fixture).unwrap();
        fs::write(fixture.join("kept.txt"), b"fixture").unwrap();
        let endpoint_path = state.join(DEFAULT_ENDPOINT_FILE);
        let server =
            AgentServer::bind(CoreConfig::new(state.clone()), Some(endpoint_path.clone())).unwrap();
        let endpoint = server.endpoint().unwrap();
        let runner = thread::spawn(move || server.run().unwrap());

        let added = send_request(
            &endpoint,
            "add_root",
            None,
            json!({"path": fixture, "kind": "local"}),
        )
        .unwrap();
        assert_eq!(added["ok"], true);
        let mut root = added["data"].clone();
        let policies = [
            json!("never"),
            json!("automatic"),
            json!({"interval": {"seconds": 90}}),
            json!({"daily": {"hour": 6, "minute": 45}}),
            json!({"weekly": {"weekday_from_monday": 4, "hour": 18, "minute": 30}}),
        ];
        for policy in policies {
            root["refresh_policy"] = policy.clone();
            root["availability"] = json!("unavailable");
            root["last_error"] = json!("caller must not own health");
            root["last_reconciled_unix_ms"] = json!(123);
            let updated = send_request(&endpoint, "put_root", None, root.clone()).unwrap();
            assert_eq!(updated["ok"], true, "put_root failed: {updated}");
            assert_eq!(updated["data"]["refresh_policy"], policy);
            assert_eq!(updated["data"]["availability"], "available");
            assert_eq!(updated["data"]["last_error"], Value::Null);
            assert_eq!(updated["data"]["last_reconciled_unix_ms"], Value::Null);
            root = updated["data"].clone();
        }

        let mut invalid = root.clone();
        invalid["refresh_policy"] = json!({"interval": {"seconds": 0}});
        let rejected = send_request(&endpoint, "put_root", None, invalid).unwrap();
        assert_eq!(rejected["ok"], false);
        assert_eq!(rejected["error"]["code"], "invalid_request");
        let after_rejection = send_request(&endpoint, "list_roots", None, Value::Null).unwrap();
        assert_eq!(
            after_rejection["data"][0]["refresh_policy"],
            root["refresh_policy"]
        );

        root["enabled"] = json!(false);
        root["refresh_policy"] = json!("never");
        let disabled = send_request(&endpoint, "put_root", None, root).unwrap();
        assert_eq!(disabled["ok"], true);
        assert_eq!(disabled["data"]["enabled"], false);
        assert_eq!(disabled["data"]["refresh_policy"], "never");
        assert_eq!(
            send_request(&endpoint, "shutdown", None, Value::Null).unwrap()["ok"],
            true
        );
        runner.join().unwrap();

        let reopened =
            AgentServer::bind(CoreConfig::new(state), Some(endpoint_path.clone())).unwrap();
        let reopened_endpoint = reopened.endpoint().unwrap();
        let reopened_runner = thread::spawn(move || reopened.run().unwrap());
        let roots = send_request(&reopened_endpoint, "list_roots", None, Value::Null).unwrap();
        assert_eq!(roots["data"][0]["enabled"], false);
        assert_eq!(roots["data"][0]["refresh_policy"], "never");
        assert_eq!(
            send_request(&reopened_endpoint, "shutdown", None, Value::Null).unwrap()["ok"],
            true
        );
        reopened_runner.join().unwrap();
        assert!(!endpoint_path.exists());
    }

    #[test]
    fn http_rpc_is_disabled_by_default_and_framed_protocol_remains_available() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let endpoint_path = state.join(DEFAULT_ENDPOINT_FILE);
        let server =
            AgentServer::bind(CoreConfig::new(state), Some(endpoint_path.clone())).unwrap();
        let endpoint = server.endpoint().unwrap();
        let runner = thread::spawn(move || server.run().unwrap());

        let (status, _, rejected) = send_http_rpc(
            &endpoint,
            &endpoint.token,
            json!({
                "id": "http-disabled",
                "method": "status",
                "request_id": null,
                "params": null
            }),
            None,
        );
        assert_eq!(status, 404);
        assert_eq!(rejected["ok"], false);
        assert_eq!(rejected["error"]["code"], "http_api_disabled");

        let framed_status = send_request(&endpoint, "status", None, Value::Null).unwrap();
        assert_eq!(framed_status["ok"], true);
        assert_eq!(framed_status["data"]["http_api"]["enabled"], false);
        assert_eq!(
            send_request(&endpoint, "shutdown", None, Value::Null).unwrap()["ok"],
            true
        );
        runner.join().unwrap();
        assert!(!endpoint_path.exists());
    }

    #[test]
    fn http_rpc_reuses_authenticated_read_search_dispatch_and_framed_protocol() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let fixture = temp.path().join("files");
        fs::create_dir_all(&fixture).unwrap();
        fs::write(fixture.join("invoice.pdf"), b"fixture").unwrap();
        fs::write(fixture.join("invoice.txt"), b"fixture").unwrap();

        let endpoint_path = state.join(DEFAULT_ENDPOINT_FILE);
        let server = AgentServer::bind_with_workspace_catalog_and_http_api(
            CoreConfig::new(state),
            Some(endpoint_path.clone()),
            None,
            true,
        )
        .unwrap();
        let endpoint = server.endpoint().unwrap();
        let runner = thread::spawn(move || server.run().unwrap());

        let added = send_request(
            &endpoint,
            "add_root",
            None,
            json!({"path": fixture, "kind": "local"}),
        )
        .unwrap();
        assert_eq!(added["ok"], true);
        let root_id = added["data"]["id"].as_str().unwrap().to_string();

        let reconciled = send_request(
            &endpoint,
            "reconcile_root",
            Some(81),
            json!({"root_id": root_id}),
        )
        .unwrap();
        assert_eq!(reconciled["ok"], true);
        assert_eq!(reconciled["data"]["discovered"], 2);

        let (status, headers, search) = send_http_rpc(
            &endpoint,
            &endpoint.token,
            json!({
                "id": "http-search",
                "method": "search",
                "request_id": 82,
                "params": file_name_search_request("invoice.pdf")
            }),
            None,
        );
        assert_eq!(status, 200);
        assert!(headers.contains("Cache-Control: no-store"));
        assert!(
            !headers
                .to_ascii_lowercase()
                .contains("access-control-allow-origin")
        );
        assert_eq!(search["ok"], true);
        assert_eq!(search["data"]["results"].as_array().unwrap().len(), 1);
        assert_eq!(search["data"]["results"][0]["item"]["name"], "invoice.pdf");

        let (status, _, http_status) = send_http_rpc(
            &endpoint,
            &endpoint.token,
            json!({
                "id": "http-status",
                "method": "status",
                "request_id": null,
                "params": null
            }),
            None,
        );
        assert_eq!(status, 200);
        assert_eq!(http_status["data"]["http_api"]["enabled"], true);
        assert_eq!(http_status["data"]["http_api"]["path"], HTTP_RPC_PATH);
        assert_eq!(http_status["data"]["http_api"]["same_listener"], true);
        assert_eq!(http_status["data"]["http_api"]["browser_origins"], false);

        let (status, _, filters) = send_http_rpc(
            &endpoint,
            &endpoint.token,
            json!({
                "id": "http-list-filters",
                "method": "list_filters",
                "request_id": null,
                "params": null
            }),
            None,
        );
        assert_eq!(status, 200);
        assert_eq!(filters["ok"], true);
        assert_eq!(filters["data"], json!([]));

        let framed_status = send_request(&endpoint, "status", None, Value::Null).unwrap();
        assert_eq!(framed_status["ok"], true);
        assert_eq!(framed_status["data"]["http_api"]["enabled"], true);
        assert_eq!(framed_status["data"]["http_api"]["path"], HTTP_RPC_PATH);

        let shutdown = send_request(&endpoint, "shutdown", None, Value::Null).unwrap();
        assert_eq!(shutdown["ok"], true);
        runner.join().unwrap();
        assert!(!endpoint_path.exists());
    }

    #[test]
    fn http_rpc_rejects_bad_auth_browser_origin_and_mutating_methods() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let endpoint_path = state.join(DEFAULT_ENDPOINT_FILE);
        let server = AgentServer::bind_with_workspace_catalog_and_http_api(
            CoreConfig::new(state),
            Some(endpoint_path.clone()),
            None,
            true,
        )
        .unwrap();
        let endpoint = server.endpoint().unwrap();
        let runner = thread::spawn(move || server.run().unwrap());
        let shutdown_request = json!({
            "id": "blocked-shutdown",
            "method": "shutdown",
            "request_id": null,
            "params": null
        });

        let (status, headers, rejected) =
            send_http_rpc(&endpoint, "wrong-token", shutdown_request.clone(), None);
        assert_eq!(status, 401);
        assert!(headers.contains("WWW-Authenticate: Bearer"));
        assert_eq!(rejected["ok"], false);
        assert_eq!(rejected["error"]["code"], "authentication_failed");

        let (status, _, rejected) = send_http_rpc(
            &endpoint,
            &endpoint.token,
            shutdown_request,
            Some("https://example.invalid"),
        );
        assert_eq!(status, 403);
        assert_eq!(rejected["ok"], false);
        assert_eq!(rejected["error"]["code"], "browser_origin_rejected");

        let (status, _, rejected) = send_http_rpc(
            &endpoint,
            &endpoint.token,
            json!({
                "id": "blocked-shutdown-valid-auth",
                "method": "shutdown",
                "request_id": null,
                "params": null
            }),
            None,
        );
        assert_eq!(status, 403);
        assert_eq!(rejected["ok"], false);
        assert_eq!(rejected["id"], "blocked-shutdown-valid-auth");
        assert_eq!(rejected["error"]["code"], "http_method_not_allowed");

        let (status, _, rejected) = send_http_rpc(
            &endpoint,
            &endpoint.token,
            json!({
                "id": "blocked-put-filter",
                "method": "put_filter",
                "request_id": null,
                "params": {}
            }),
            None,
        );
        assert_eq!(status, 403);
        assert_eq!(rejected["ok"], false);
        assert_eq!(rejected["id"], "blocked-put-filter");
        assert_eq!(rejected["error"]["code"], "http_method_not_allowed");

        let still_running = send_request(&endpoint, "status", None, Value::Null).unwrap();
        assert_eq!(still_running["ok"], true);
        assert_eq!(
            send_request(&endpoint, "shutdown", None, Value::Null).unwrap()["ok"],
            true
        );
        runner.join().unwrap();
        assert!(!endpoint_path.exists());
    }

    #[test]
    fn accepted_connection_waits_for_a_delayed_framed_request() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let endpoint_path = state.join(DEFAULT_ENDPOINT_FILE);
        let server =
            AgentServer::bind(CoreConfig::new(state), Some(endpoint_path.clone())).unwrap();
        let endpoint = server.endpoint().unwrap();
        let runner = thread::spawn(move || server.run().unwrap());

        let mut stream = TcpStream::connect_timeout(&endpoint.address, IO_TIMEOUT).unwrap();
        stream.set_read_timeout(Some(IO_TIMEOUT)).unwrap();
        stream.set_write_timeout(Some(IO_TIMEOUT)).unwrap();
        thread::sleep(Duration::from_millis(75));
        let mut request = serde_json::to_vec(&json!({
            "token": endpoint.token,
            "id": "delayed-status",
            "method": "status",
            "request_id": null,
            "params": null,
        }))
        .unwrap();
        request.push(b'\n');
        stream.write_all(&request).unwrap();
        stream.flush().unwrap();
        let response: Value =
            serde_json::from_slice(&read_bounded_line(&mut stream).unwrap()).unwrap();
        assert_eq!(response["id"], "delayed-status");
        assert_eq!(response["ok"], true);
        drop(stream);

        let shutdown = send_request(&endpoint, "shutdown", None, Value::Null).unwrap();
        assert_eq!(shutdown["ok"], true);
        runner.join().unwrap();
        for _ in 0..50 {
            if !endpoint_path.exists() {
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
        assert!(!endpoint_path.exists());
    }

    #[test]
    fn persistent_connection_serves_multiple_frames_and_shutdown_drains_it() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let endpoint_path = state.join(DEFAULT_ENDPOINT_FILE);
        let server =
            AgentServer::bind(CoreConfig::new(state), Some(endpoint_path.clone())).unwrap();
        let endpoint = server.endpoint().unwrap();
        let runner = thread::spawn(move || server.run().unwrap());

        let mut stream = TcpStream::connect_timeout(&endpoint.address, IO_TIMEOUT).unwrap();
        stream.set_read_timeout(Some(IO_TIMEOUT)).unwrap();
        stream.set_write_timeout(Some(IO_TIMEOUT)).unwrap();
        stream
            .write_all(
                format!(
                    "{{\"token\":\"{}\",\"id\":\"first\",\"method\":\"status\",\"request_id\":null,\"params\":null}}\n",
                    endpoint.token
                )
                .as_bytes(),
            )
            .unwrap();
        let first: Value =
            serde_json::from_slice(&read_bounded_line(&mut stream).unwrap()).unwrap();
        assert_eq!(first["id"], "first");
        assert_eq!(first["ok"], true);

        stream.write_all(b"{malformed-json}\n").unwrap();
        let malformed: Value =
            serde_json::from_slice(&read_bounded_line(&mut stream).unwrap()).unwrap();
        assert_eq!(malformed["ok"], false);
        assert_eq!(malformed["error"]["code"], "invalid_request");

        stream
            .write_all(
                format!(
                    "{{\"token\":\"{}\",\"id\":\"second\",\"method\":\"list_roots\",\"request_id\":null,\"params\":null}}\n",
                    endpoint.token
                )
                .as_bytes(),
            )
            .unwrap();
        let second: Value =
            serde_json::from_slice(&read_bounded_line(&mut stream).unwrap()).unwrap();
        assert_eq!(second["id"], "second");
        assert_eq!(second["ok"], true);

        let shutdown_started = Instant::now();
        let shutdown = send_request(&endpoint, "shutdown", None, Value::Null).unwrap();
        assert_eq!(shutdown["ok"], true);
        runner.join().unwrap();
        assert!(shutdown_started.elapsed() < Duration::from_secs(3));
        assert!(!endpoint_path.exists());
    }

    #[test]
    fn automatic_root_watcher_keeps_live_catalog_fresh_and_coalesces_events() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let fixture = temp.path().join("files");
        fs::create_dir_all(&fixture).unwrap();
        fs::write(fixture.join("rename_old.txt"), b"rename").unwrap();
        fs::write(fixture.join("update_me.txt"), b"small").unwrap();
        fs::write(fixture.join("delete_me.txt"), b"delete").unwrap();

        let endpoint_path = state.join(DEFAULT_ENDPOINT_FILE);
        let server =
            AgentServer::bind(CoreConfig::new(state.clone()), Some(endpoint_path.clone())).unwrap();
        let endpoint = server.endpoint().unwrap();
        let runner = thread::spawn(move || server.run().unwrap());

        let root = send_request(
            &endpoint,
            "add_root",
            None,
            json!({"path": fixture, "kind": "local"}),
        )
        .unwrap();
        let root_id = root["data"]["id"].as_str().unwrap().to_string();
        assert_eq!(
            send_request(
                &endpoint,
                "reconcile_root",
                Some(70_001),
                json!({"root_id": root_id})
            )
            .unwrap()["ok"],
            true
        );

        fs::write(fixture.join("fresh_create.txt"), b"created").unwrap();
        wait_for_file_search(&endpoint, "fresh_create", |rows| rows.len() == 1);

        fs::rename(
            fixture.join("rename_old.txt"),
            fixture.join("rename_new.txt"),
        )
        .unwrap();
        wait_for_file_search(&endpoint, "rename_old", |rows| rows.is_empty());
        wait_for_file_search(&endpoint, "rename_new", |rows| rows.len() == 1);

        thread::sleep(Duration::from_millis(20));
        fs::write(fixture.join("update_me.txt"), vec![b'x'; 8192]).unwrap();
        let updated = wait_for_file_search(&endpoint, "update_me", |rows| {
            rows.first()
                .and_then(|row| row.get("item"))
                .and_then(|item| item.get("size"))
                .and_then(Value::as_u64)
                == Some(8192)
        });
        assert_eq!(updated[0]["item"]["size"], 8192);

        fs::remove_file(fixture.join("delete_me.txt")).unwrap();
        wait_for_file_search(&endpoint, "delete_me", |rows| rows.is_empty());

        for index in 0..30 {
            let path = fixture.join(format!("burst_{index:02}.txt"));
            fs::write(&path, format!("burst {index}")).unwrap();
            if index % 4 == 0 {
                fs::remove_file(path).unwrap();
            }
        }
        let expected = (0..30).filter(|index| index % 4 != 0).count();
        wait_for_file_search(&endpoint, "burst_", |rows| rows.len() == expected);

        let status = send_request(&endpoint, "status", None, Value::Null).unwrap();
        assert_eq!(status["ok"], true);
        assert_eq!(status["data"]["root_watcher"]["watched_root_count"], 1);
        assert!(
            status["data"]["root_watcher"]["event_count"]
                .as_u64()
                .unwrap()
                > 0
        );
        let batches = status["data"]["root_watcher"]["reconcile_batches"]
            .as_u64()
            .unwrap();
        let events = status["data"]["root_watcher"]["event_count"]
            .as_u64()
            .unwrap();
        assert!(batches > 0);
        assert!(batches < events, "event burst was not coalesced: {status}");

        assert_eq!(
            send_request(&endpoint, "shutdown", None, Value::Null).unwrap()["ok"],
            true
        );
        runner.join().unwrap();
        assert!(!endpoint_path.exists());
    }

    #[test]
    fn automatic_root_watcher_simulated_dropped_history_recovers_and_fences_stale_generation() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let fixture = temp.path().join("files with spaces");
        fs::create_dir_all(&fixture).unwrap();
        fs::write(fixture.join("Résumé βeta.txt"), b"rename").unwrap();
        fs::write(fixture.join("modify me.txt"), b"small").unwrap();
        fs::write(fixture.join("delete me.txt"), b"delete").unwrap();

        let endpoint_path = state.join(DEFAULT_ENDPOINT_FILE);
        let server =
            AgentServer::bind(CoreConfig::new(state), Some(endpoint_path.clone())).unwrap();
        let watcher = server.shared.root_watcher.clone();
        let endpoint = server.endpoint().unwrap();
        let runner = thread::spawn(move || server.run().unwrap());

        let root = send_request(
            &endpoint,
            "add_root",
            None,
            json!({"path": fixture, "kind": "local"}),
        )
        .unwrap();
        let root_id = root["data"]["id"].as_str().unwrap().to_string();
        assert_eq!(
            send_request(
                &endpoint,
                "reconcile_root",
                Some(70_051),
                json!({"root_id": root_id})
            )
            .unwrap()["ok"],
            true
        );

        let watch_deadline = Instant::now() + Duration::from_secs(2);
        loop {
            let status = send_request(&endpoint, "status", None, Value::Null).unwrap();
            if status["data"]["root_watcher"]["watched_root_count"] == 1 {
                break;
            }
            assert!(
                Instant::now() < watch_deadline,
                "root watch did not become active"
            );
            thread::sleep(Duration::from_millis(10));
        }
        let diagnostics = send_request(&endpoint, "diagnostics", None, Value::Null).unwrap();
        let stale_generation = diagnostics["data"]["catalog_generation"].as_u64().unwrap();
        let full_before = send_request(&endpoint, "status", None, Value::Null).unwrap()
            ["data"]["root_watcher"]["full_reconciles"]
            .as_u64()
            .unwrap();

        let renamed = fixture.join("Résumé γamma.txt");
        fs::rename(fixture.join("Résumé βeta.txt"), &renamed).unwrap();
        fs::write(fixture.join("modify me.txt"), vec![b'x'; 4096]).unwrap();
        fs::remove_file(fixture.join("delete me.txt")).unwrap();
        let created = fixture.join("new file Ω.txt");
        fs::write(&created, b"created").unwrap();

        let stale_event = NotifyEvent::new(notify::EventKind::Any)
            .add_path(created.clone())
            .add_path(fixture.join("Résumé βeta.txt"))
            .add_path(renamed.clone())
            .add_path(created.clone());
        watcher
            .sender
            .send(RootWatcherMessage::Event(Ok(stale_event.clone())))
            .unwrap();
        let dropped_history =
            NotifyEvent::new(notify::EventKind::Any).set_flag(notify::event::Flag::Rescan);
        watcher
            .sender
            .send(RootWatcherMessage::Event(Ok(dropped_history.clone())))
            .unwrap();
        watcher
            .sender
            .send(RootWatcherMessage::Event(Ok(dropped_history)))
            .unwrap();
        watcher
            .sender
            .send(RootWatcherMessage::Event(Ok(stale_event)))
            .unwrap();

        let recovery_deadline = Instant::now() + Duration::from_secs(5);
        loop {
            let status = send_request(&endpoint, "status", None, Value::Null).unwrap();
            if status["data"]["root_watcher"]["full_reconciles"]
                .as_u64()
                .unwrap()
                > full_before
            {
                break;
            }
            assert!(
                Instant::now() < recovery_deadline,
                "simulated dropped history did not trigger full recovery: {status}"
            );
            thread::sleep(Duration::from_millis(20));
        }
        let recovery_status = send_request(&endpoint, "status", None, Value::Null).unwrap();
        let recovery_micros = recovery_status["data"]["root_watcher"]["last_full_reconcile_micros"]
            .as_u64()
            .unwrap();
        eprintln!("simulated_dropped_history_recovery_micros={recovery_micros}");

        assert!(search_file_rows(&endpoint, "Résumé βeta").is_empty());
        assert_eq!(search_file_rows(&endpoint, "Résumé γamma").len(), 1);
        assert!(search_file_rows(&endpoint, "delete me").is_empty());
        assert_eq!(search_file_rows(&endpoint, "new file Ω").len(), 1);
        let modified = search_file_rows(&endpoint, "modify me");
        assert_eq!(modified[0]["item"]["size"].as_u64(), Some(4096));

        let stale_page = send_request(
            &endpoint,
            "file_search",
            Some(70_052),
            json!({
                "query": "new file Ω",
                "include_unavailable": false,
                "root_ids": [root_id],
                "categories": [],
                "modified_time": null,
                "now_unix_ms": now_ms(),
                "timezone_offset_minutes": 0,
                "offset": 0,
                "limit": 10,
                "sort_field": "relevance",
                "sort_direction": "descending",
                "expected_catalog_generation": stale_generation
            }),
        )
        .unwrap();
        assert_eq!(stale_page["ok"], false);
        assert_eq!(stale_page["error"]["code"], "stale_catalog");

        let current = send_request(&endpoint, "diagnostics", None, Value::Null).unwrap()
            ["data"]["catalog_generation"]
            .as_u64()
            .unwrap();
        assert!(current > stale_generation);
        let current_page = send_request(
            &endpoint,
            "file_search",
            Some(70_053),
            json!({
                "query": "new file Ω",
                "include_unavailable": false,
                "root_ids": [root_id],
                "categories": [],
                "modified_time": null,
                "now_unix_ms": now_ms(),
                "timezone_offset_minutes": 0,
                "offset": 0,
                "limit": 10,
                "sort_field": "relevance",
                "sort_direction": "descending",
                "expected_catalog_generation": current
            }),
        )
        .unwrap();
        assert_eq!(current_page["ok"], true);
        assert_eq!(current_page["data"]["results"].as_array().unwrap().len(), 1);

        assert_eq!(
            send_request(&endpoint, "shutdown", None, Value::Null).unwrap()["ok"],
            true
        );
        runner.join().unwrap();
        assert!(!endpoint_path.exists());
    }

    #[test]
    fn watcher_queue_overflow_signal_escalates_to_bounded_full_recovery() {
        let (bounded_sender, _bounded_receiver) = mpsc::sync_channel(1);
        bounded_sender
            .try_send(RootWatcherMessage::Event(Ok(NotifyEvent::new(
                notify::EventKind::Any,
            ))))
            .unwrap();
        let overflowed = AtomicBool::new(false);
        enqueue_watcher_event(
            &bounded_sender,
            &overflowed,
            Ok(NotifyEvent::new(notify::EventKind::Any)),
        );
        assert!(
            overflowed.load(Ordering::Acquire),
            "a full watcher channel must become one fail-closed overflow signal"
        );

        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let fixture = temp.path().join("files");
        fs::create_dir_all(&fixture).unwrap();
        fs::write(fixture.join("baseline.txt"), b"baseline").unwrap();

        let endpoint_path = state.join(DEFAULT_ENDPOINT_FILE);
        let server =
            AgentServer::bind(CoreConfig::new(state), Some(endpoint_path.clone())).unwrap();
        let watcher = server.shared.root_watcher.clone();
        let endpoint = server.endpoint().unwrap();
        let runner = thread::spawn(move || server.run().unwrap());
        let root = send_request(
            &endpoint,
            "add_root",
            None,
            json!({"path": fixture, "kind": "local"}),
        )
        .unwrap();
        let root_id = root["data"]["id"].as_str().unwrap().to_string();
        assert_eq!(
            send_request(
                &endpoint,
                "reconcile_root",
                Some(70_061),
                json!({"root_id": root_id})
            )
            .unwrap()["ok"],
            true
        );

        fs::write(fixture.join("overflow recovered.txt"), b"recovered").unwrap();
        watcher
            .event_queue_overflowed
            .store(true, Ordering::Release);
        wait_for_file_search(&endpoint, "overflow recovered", |rows| rows.len() == 1);
        let status = send_request(&endpoint, "status", None, Value::Null).unwrap();
        assert!(
            status["data"]["root_watcher"]["queue_overflow_recoveries"]
                .as_u64()
                .unwrap()
                >= 1,
            "overflow signal was not recorded: {status}"
        );
        assert!(
            status["data"]["root_watcher"]["full_reconciles"]
                .as_u64()
                .unwrap()
                >= 1,
            "overflow signal did not escalate to full recovery: {status}"
        );

        assert_eq!(
            send_request(&endpoint, "shutdown", None, Value::Null).unwrap()["ok"],
            true
        );
        runner.join().unwrap();
        assert!(!endpoint_path.exists());
    }

    #[test]
    fn cancelled_full_recovery_keeps_last_committed_snapshot_and_pending_recovery() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let fixture = temp.path().join("files");
        fs::create_dir_all(&fixture).unwrap();
        fs::write(fixture.join("committed.txt"), b"committed").unwrap();
        let core = Core::open(CoreConfig::new(state)).unwrap();
        let root = core.add_root(fixture.clone(), RootKind::Local).unwrap();
        core.reconcile_root(root.id, &CancellationToken::new())
            .unwrap();

        fs::remove_file(fixture.join("committed.txt")).unwrap();
        fs::write(fixture.join("uncommitted.txt"), b"uncommitted").unwrap();
        let mut desired = HashMap::from([(root.id, root.clone())]);
        let mut pending = HashMap::new();
        let mut full_recovery = HashSet::from([root.id]);
        let cancellation = CancellationToken::new();
        cancellation.cancel();
        let status = Arc::new(Mutex::new(RootWatcherStatus::default()));

        flush_watcher_work(
            &core,
            &mut desired,
            &mut pending,
            &mut full_recovery,
            &cancellation,
            &status,
        );

        assert!(full_recovery.contains(&root.id));
        let committed = core
            .search(
                &SearchRequest {
                    query: "committed".into(),
                    kinds: vec![listary_core::ItemKind::File],
                    ..SearchRequest::default()
                },
                &CancellationToken::new(),
            )
            .unwrap();
        let uncommitted = core
            .search(
                &SearchRequest {
                    query: "uncommitted".into(),
                    kinds: vec![listary_core::ItemKind::File],
                    ..SearchRequest::default()
                },
                &CancellationToken::new(),
            )
            .unwrap();
        assert_eq!(committed.results.len(), 1);
        assert!(uncommitted.results.is_empty());
    }

    #[test]
    fn legacy_global_generation_sequence_stales_independent_second_root() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("legacy-state");
        let root_a_path = temp.path().join("legacy root A");
        let root_b_path = temp.path().join("legacy root β");
        fs::create_dir_all(&root_a_path).unwrap();
        fs::create_dir_all(&root_b_path).unwrap();
        let file_a = root_a_path.join("alpha.txt");
        let file_b = root_b_path.join("beta.txt");
        fs::write(&file_a, b"a").unwrap();
        fs::write(&file_b, b"b").unwrap();

        let core = Core::open(CoreConfig::new(state)).unwrap();
        let root_a = core.add_root(root_a_path, RootKind::Local).unwrap();
        let root_b = core.add_root(root_b_path, RootKind::Local).unwrap();
        core.reconcile_root(root_a.id, &CancellationToken::new())
            .unwrap();
        core.reconcile_root(root_b.id, &CancellationToken::new())
            .unwrap();
        let generation = core.catalog_generation().unwrap();
        fs::write(&file_a, vec![b'a'; 4096]).unwrap();
        fs::write(&file_b, vec![b'b'; 8192]).unwrap();

        core.reconcile_paths_at_generation(
            root_a.id,
            vec![file_a],
            Some(generation),
            &CancellationToken::new(),
        )
        .unwrap();
        let second = core.reconcile_paths_at_generation(
            root_b.id,
            vec![file_b],
            Some(generation),
            &CancellationToken::new(),
        );
        assert!(matches!(second, Err(CoreError::StaleCatalog { .. })));
        eprintln!(
            "multi_root_work_count_legacy=incremental_successes:1,stale_rejections:1,full_recovery_queued:1"
        );
    }

    #[test]
    fn independent_root_batches_rebase_over_watcher_owned_generation_changes() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let root_a_path = temp.path().join("root A");
        let root_b_path = temp.path().join("root β");
        fs::create_dir_all(&root_a_path).unwrap();
        fs::create_dir_all(&root_b_path).unwrap();
        let file_a = root_a_path.join("alpha.txt");
        let file_b = root_b_path.join("beta.txt");
        fs::write(&file_a, b"a").unwrap();
        fs::write(&file_b, b"b").unwrap();

        let core = Core::open(CoreConfig::new(state)).unwrap();
        let root_a = core.add_root(root_a_path, RootKind::Local).unwrap();
        let root_b = core.add_root(root_b_path, RootKind::Local).unwrap();
        core.reconcile_root(root_a.id, &CancellationToken::new())
            .unwrap();
        core.reconcile_root(root_b.id, &CancellationToken::new())
            .unwrap();
        let initial_generation = core.catalog_generation().unwrap();

        fs::write(&file_a, vec![b'a'; 4096]).unwrap();
        fs::write(&file_b, vec![b'b'; 8192]).unwrap();
        let mut desired = core
            .list_roots()
            .into_iter()
            .map(|root| (root.id, root))
            .collect::<HashMap<_, _>>();
        let mut pending = HashMap::from([
            (
                root_a.id,
                PendingRootChanges {
                    expected_catalog_generation: initial_generation,
                    paths: HashSet::from([file_a.clone()]),
                },
            ),
            (
                root_b.id,
                PendingRootChanges {
                    expected_catalog_generation: initial_generation,
                    paths: HashSet::from([file_b.clone()]),
                },
            ),
        ]);
        let mut full_recovery = HashSet::new();
        let status = Arc::new(Mutex::new(RootWatcherStatus::default()));

        flush_watcher_work(
            &core,
            &mut desired,
            &mut pending,
            &mut full_recovery,
            &CancellationToken::new(),
            &status,
        );

        assert!(pending.is_empty());
        assert!(full_recovery.is_empty());
        assert_eq!(
            core.get_item_by_path(file_a.to_string_lossy().as_ref())
                .unwrap()
                .size,
            4096
        );
        assert_eq!(
            core.get_item_by_path(file_b.to_string_lossy().as_ref())
                .unwrap()
                .size,
            8192
        );
        let watcher_status = status.lock().unwrap().clone();
        assert_eq!(watcher_status.reconcile_batches, 2);
        assert_eq!(watcher_status.full_reconciles, 0);
        assert_eq!(watcher_status.stale_batches_rejected, 0);
        assert_eq!(core.catalog_generation().unwrap(), initial_generation + 2);
        eprintln!(
            "multi_root_work_count_hardened=incremental_successes:2,stale_rejections:0,full_recovery_queued:0"
        );
    }

    #[test]
    fn full_recovery_rebases_independent_pending_batch_without_stale_escalation() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let recovery_root_path = temp.path().join("recovery root Ω");
        let pending_root_path = temp.path().join("pending root β");
        fs::create_dir_all(&recovery_root_path).unwrap();
        fs::create_dir_all(&pending_root_path).unwrap();
        let recovery_file = recovery_root_path.join("recovered.txt");
        let pending_file = pending_root_path.join("pending.txt");
        fs::write(&recovery_file, b"old recovery").unwrap();
        fs::write(&pending_file, b"old pending").unwrap();

        let core = Core::open(CoreConfig::new(state)).unwrap();
        let recovery_root = core.add_root(recovery_root_path, RootKind::Local).unwrap();
        let pending_root = core.add_root(pending_root_path, RootKind::Local).unwrap();
        core.reconcile_root(recovery_root.id, &CancellationToken::new())
            .unwrap();
        core.reconcile_root(pending_root.id, &CancellationToken::new())
            .unwrap();
        let initial_generation = core.catalog_generation().unwrap();

        fs::write(&recovery_file, vec![b'r'; 4096]).unwrap();
        fs::write(&pending_file, vec![b'p'; 8192]).unwrap();
        let mut desired = core
            .list_roots()
            .into_iter()
            .map(|root| (root.id, root))
            .collect::<HashMap<_, _>>();
        let mut pending = HashMap::from([(
            pending_root.id,
            PendingRootChanges {
                expected_catalog_generation: initial_generation,
                paths: HashSet::from([pending_file.clone()]),
            },
        )]);
        let mut full_recovery = HashSet::from([recovery_root.id]);
        let status = Arc::new(Mutex::new(RootWatcherStatus::default()));

        flush_watcher_work(
            &core,
            &mut desired,
            &mut pending,
            &mut full_recovery,
            &CancellationToken::new(),
            &status,
        );

        assert!(pending.is_empty());
        assert!(full_recovery.is_empty());
        assert_eq!(
            core.get_item_by_path(recovery_file.to_string_lossy().as_ref())
                .unwrap()
                .size,
            4096
        );
        assert_eq!(
            core.get_item_by_path(pending_file.to_string_lossy().as_ref())
                .unwrap()
                .size,
            8192
        );
        let watcher_status = status.lock().unwrap().clone();
        assert_eq!(watcher_status.full_reconciles, 1);
        assert_eq!(watcher_status.reconcile_batches, 1);
        assert_eq!(watcher_status.stale_batches_rejected, 0);
        assert_eq!(core.catalog_generation().unwrap(), initial_generation + 2);
    }

    #[test]
    fn stale_pending_and_recovery_work_is_dropped_after_root_leaves_automatic_policy() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let fixture = temp.path().join("files");
        fs::create_dir_all(&fixture).unwrap();
        let existing = fixture.join("existing.txt");
        fs::write(&existing, b"old").unwrap();

        let core = Core::open(CoreConfig::new(state)).unwrap();
        let root = core.add_root(fixture.clone(), RootKind::Local).unwrap();
        core.reconcile_root(root.id, &CancellationToken::new())
            .unwrap();

        let mut disabled = core
            .list_roots()
            .into_iter()
            .find(|candidate| candidate.id == root.id)
            .unwrap();
        disabled.refresh_policy = RefreshPolicy::Never;
        core.put_root(disabled).unwrap();
        fs::write(&existing, vec![b'x'; 4096]).unwrap();
        fs::write(fixture.join("must_wait_for_future_owner.txt"), b"new").unwrap();

        let current_generation = core.catalog_generation().unwrap();
        let mut desired = HashMap::from([(root.id, root.clone())]);
        let mut pending = HashMap::from([(
            root.id,
            PendingRootChanges {
                expected_catalog_generation: current_generation,
                paths: HashSet::from([existing.clone()]),
            },
        )]);
        let mut full_recovery = HashSet::from([root.id]);
        let status = Arc::new(Mutex::new(RootWatcherStatus::default()));

        flush_watcher_work(
            &core,
            &mut desired,
            &mut pending,
            &mut full_recovery,
            &CancellationToken::new(),
            &status,
        );

        assert!(pending.is_empty());
        assert!(full_recovery.is_empty());
        assert_eq!(
            core.get_item_by_path(existing.to_string_lossy().as_ref())
                .unwrap()
                .size,
            3,
            "a stale watcher callback must not mutate a root after Automatic ownership is removed"
        );
        assert!(
            core.get_item_by_path(
                fixture
                    .join("must_wait_for_future_owner.txt")
                    .to_string_lossy()
                    .as_ref()
            )
            .is_none()
        );
        let watcher_status = status.lock().unwrap().clone();
        assert_eq!(watcher_status.reconcile_batches, 0);
        assert_eq!(watcher_status.full_reconciles, 0);
    }

    #[test]
    fn automatic_root_watcher_catches_up_changes_missed_while_policy_was_never() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let fixture = temp.path().join("files with spaces");
        fs::create_dir_all(&fixture).unwrap();
        fs::write(fixture.join("Résumé before.txt"), b"before").unwrap();
        let endpoint_path = state.join(DEFAULT_ENDPOINT_FILE);
        let server =
            AgentServer::bind(CoreConfig::new(state), Some(endpoint_path.clone())).unwrap();
        let endpoint = server.endpoint().unwrap();
        let runner = thread::spawn(move || server.run().unwrap());

        let added = send_request(
            &endpoint,
            "add_root",
            None,
            json!({"path": fixture, "kind": "local"}),
        )
        .unwrap();
        let root_id = added["data"]["id"].as_str().unwrap().to_string();
        assert_eq!(
            send_request(
                &endpoint,
                "reconcile_root",
                Some(70_071),
                json!({"root_id": root_id})
            )
            .unwrap()["ok"],
            true
        );

        let watch_deadline = Instant::now() + Duration::from_secs(2);
        loop {
            let status = send_request(&endpoint, "status", None, Value::Null).unwrap();
            if status["data"]["root_watcher"]["watched_root_count"] == 1 {
                break;
            }
            assert!(
                Instant::now() < watch_deadline,
                "root watch did not become active"
            );
            thread::sleep(Duration::from_millis(10));
        }

        let mut root =
            send_request(&endpoint, "list_roots", None, Value::Null).unwrap()["data"][0].clone();
        root["refresh_policy"] = json!("never");
        assert_eq!(
            send_request(&endpoint, "put_root", None, root.clone()).unwrap()["ok"],
            true
        );
        let unwatch_deadline = Instant::now() + Duration::from_secs(2);
        loop {
            let status = send_request(&endpoint, "status", None, Value::Null).unwrap();
            if status["data"]["root_watcher"]["watched_root_count"] == 0 {
                break;
            }
            assert!(
                Instant::now() < unwatch_deadline,
                "root watch did not stop after Never policy"
            );
            thread::sleep(Duration::from_millis(10));
        }

        fs::rename(
            fixture.join("Résumé before.txt"),
            fixture.join("Résumé after.txt"),
        )
        .unwrap();
        fs::write(fixture.join("missed Ω.txt"), b"missed while unwatched").unwrap();
        thread::sleep(Duration::from_millis(150));
        assert_eq!(search_file_rows(&endpoint, "Résumé before").len(), 1);
        assert!(search_file_rows(&endpoint, "Résumé after").is_empty());
        assert!(search_file_rows(&endpoint, "missed Ω").is_empty());

        root["refresh_policy"] = json!("automatic");
        let recovery_started = Instant::now();
        assert_eq!(
            send_request(&endpoint, "put_root", None, root).unwrap()["ok"],
            true
        );
        wait_for_file_search(&endpoint, "Résumé before", |rows| rows.is_empty());
        wait_for_file_search(&endpoint, "Résumé after", |rows| rows.len() == 1);
        wait_for_file_search(&endpoint, "missed Ω", |rows| rows.len() == 1);
        let recovery_elapsed = recovery_started.elapsed();
        let status = send_request(&endpoint, "status", None, Value::Null).unwrap();
        assert!(
            status["data"]["root_watcher"]["full_reconciles"]
                .as_u64()
                .unwrap()
                >= 1,
            "re-entering Automatic monitoring did not perform catch-up recovery: {status}"
        );
        eprintln!(
            "automatic_policy_reentry_recovery_micros={}",
            recovery_elapsed.as_micros()
        );

        assert_eq!(
            send_request(&endpoint, "shutdown", None, Value::Null).unwrap()["ok"],
            true
        );
        runner.join().unwrap();
        assert!(!endpoint_path.exists());
    }

    #[test]
    fn automatic_root_watcher_flushes_before_continuous_churn_goes_quiet() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let fixture = temp.path().join("files");
        fs::create_dir_all(&fixture).unwrap();
        let endpoint_path = state.join(DEFAULT_ENDPOINT_FILE);
        let server =
            AgentServer::bind(CoreConfig::new(state), Some(endpoint_path.clone())).unwrap();
        let endpoint = server.endpoint().unwrap();
        let runner = thread::spawn(move || server.run().unwrap());

        let root = send_request(
            &endpoint,
            "add_root",
            None,
            json!({"path": fixture, "kind": "local"}),
        )
        .unwrap();
        let root_id = root["data"]["id"].as_str().unwrap().to_string();
        assert_eq!(
            send_request(
                &endpoint,
                "reconcile_root",
                Some(70_101),
                json!({"root_id": root_id})
            )
            .unwrap()["ok"],
            true
        );
        let watch_deadline = Instant::now() + Duration::from_secs(2);
        loop {
            let status = send_request(&endpoint, "status", None, Value::Null).unwrap();
            if status["data"]["root_watcher"]["watched_root_count"] == 1 {
                break;
            }
            assert!(
                Instant::now() < watch_deadline,
                "root watch did not become active"
            );
            thread::sleep(Duration::from_millis(10));
        }

        let writer_done = Arc::new(AtomicBool::new(false));
        let writer_done_thread = Arc::clone(&writer_done);
        let writer_root = fixture.clone();
        let writer = thread::spawn(move || {
            fs::write(writer_root.join("continuous_target.txt"), b"target").unwrap();
            for index in 0..120 {
                fs::write(
                    writer_root.join(format!("continuous_churn_{}.txt", index % 4)),
                    format!("churn {index}"),
                )
                .unwrap();
                thread::sleep(Duration::from_millis(10));
            }
            writer_done_thread.store(true, Ordering::Release);
        });

        let found = wait_for_file_search(&endpoint, "continuous_target", |rows| rows.len() == 1);
        assert_eq!(found.len(), 1);
        assert!(
            !writer_done.load(Ordering::Acquire),
            "live reconciliation waited for the continuous event stream to go quiet"
        );
        writer.join().unwrap();

        assert_eq!(
            send_request(&endpoint, "shutdown", None, Value::Null).unwrap()["ok"],
            true
        );
        runner.join().unwrap();
        assert!(!endpoint_path.exists());
    }

    #[test]
    fn automatic_root_watcher_recovers_changes_missed_during_agent_restart() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let fixture = temp.path().join("files");
        fs::create_dir_all(&fixture).unwrap();
        fs::write(fixture.join("before_restart.txt"), b"before").unwrap();
        let endpoint_path = state.join(DEFAULT_ENDPOINT_FILE);

        let first =
            AgentServer::bind(CoreConfig::new(state.clone()), Some(endpoint_path.clone())).unwrap();
        let first_endpoint = first.endpoint().unwrap();
        let first_runner = thread::spawn(move || first.run().unwrap());
        let root = send_request(
            &first_endpoint,
            "add_root",
            None,
            json!({"path": fixture, "kind": "local"}),
        )
        .unwrap();
        let root_id = root["data"]["id"].as_str().unwrap().to_string();
        assert_eq!(
            send_request(
                &first_endpoint,
                "reconcile_root",
                Some(71_001),
                json!({"root_id": root_id})
            )
            .unwrap()["ok"],
            true
        );
        assert_eq!(
            send_request(&first_endpoint, "shutdown", None, Value::Null).unwrap()["ok"],
            true
        );
        first_runner.join().unwrap();

        fs::rename(
            fixture.join("before_restart.txt"),
            fixture.join("after_restart.txt"),
        )
        .unwrap();
        fs::write(fixture.join("created_while_down.txt"), b"down").unwrap();

        let second =
            AgentServer::bind(CoreConfig::new(state), Some(endpoint_path.clone())).unwrap();
        let second_endpoint = second.endpoint().unwrap();
        let second_runner = thread::spawn(move || second.run().unwrap());
        wait_for_file_search(&second_endpoint, "before_restart", |rows| rows.is_empty());
        wait_for_file_search(&second_endpoint, "after_restart", |rows| rows.len() == 1);
        wait_for_file_search(&second_endpoint, "created_while_down", |rows| {
            rows.len() == 1
        });
        let status = send_request(&second_endpoint, "status", None, Value::Null).unwrap();
        assert!(
            status["data"]["root_watcher"]["full_reconciles"]
                .as_u64()
                .unwrap()
                >= 1
        );

        assert_eq!(
            send_request(&second_endpoint, "shutdown", None, Value::Null).unwrap()["ok"],
            true
        );
        second_runner.join().unwrap();
        assert!(!endpoint_path.exists());
    }

    #[test]
    fn automatic_root_watcher_preserves_disappeared_root_and_recovers_when_it_returns() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let fixture = temp.path().join("mounted");
        let detached = temp.path().join("detached");
        fs::create_dir_all(&fixture).unwrap();
        fs::write(fixture.join("kept.txt"), b"kept").unwrap();
        let endpoint_path = state.join(DEFAULT_ENDPOINT_FILE);
        let server =
            AgentServer::bind(CoreConfig::new(state), Some(endpoint_path.clone())).unwrap();
        let endpoint = server.endpoint().unwrap();
        let runner = thread::spawn(move || server.run().unwrap());
        let root = send_request(
            &endpoint,
            "add_root",
            None,
            json!({"path": fixture, "kind": "local"}),
        )
        .unwrap();
        let root_id = root["data"]["id"].as_str().unwrap().to_string();
        assert_eq!(
            send_request(
                &endpoint,
                "reconcile_root",
                Some(72_001),
                json!({"root_id": root_id})
            )
            .unwrap()["ok"],
            true
        );

        fs::rename(&fixture, &detached).unwrap();
        let unavailable = wait_for_file_search(&endpoint, "kept", |rows| {
            rows.first()
                .and_then(|row| row.get("item"))
                .and_then(|item| item.get("available"))
                .and_then(Value::as_bool)
                == Some(false)
        });
        assert_eq!(unavailable.len(), 1);

        fs::rename(&detached, &fixture).unwrap();
        fs::write(fixture.join("returned.txt"), b"returned").unwrap();
        wait_for_file_search(&endpoint, "kept", |rows| {
            rows.first()
                .and_then(|row| row.get("item"))
                .and_then(|item| item.get("available"))
                .and_then(Value::as_bool)
                == Some(true)
        });
        wait_for_file_search(&endpoint, "returned", |rows| rows.len() == 1);

        assert_eq!(
            send_request(&endpoint, "shutdown", None, Value::Null).unwrap()["ok"],
            true
        );
        runner.join().unwrap();
        assert!(!endpoint_path.exists());
    }

    #[test]
    fn timed_interval_root_reconciles_without_a_notify_watch_or_duplicate_poll_run() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let fixture = temp.path().join("files");
        fs::create_dir_all(&fixture).unwrap();
        fs::write(fixture.join("initial.txt"), b"initial").unwrap();
        let endpoint_path = state.join(DEFAULT_ENDPOINT_FILE);
        let server =
            AgentServer::bind(CoreConfig::new(state), Some(endpoint_path.clone())).unwrap();
        let root = server
            .shared
            .core
            .add_root(fixture.clone(), RootKind::Local)
            .unwrap();
        set_test_root_refresh_policy(&server, root.id, RefreshPolicy::Interval { seconds: 1 });
        server
            .shared
            .core
            .reconcile_root(root.id, &CancellationToken::new())
            .unwrap();
        let endpoint = server.endpoint().unwrap();
        let runner = thread::spawn(move || server.run().unwrap());

        let status_deadline = Instant::now() + Duration::from_secs(2);
        loop {
            let status = send_request(&endpoint, "status", None, Value::Null).unwrap();
            if status["data"]["root_watcher"]["scheduled_root_count"] == 1 {
                assert_eq!(status["data"]["root_watcher"]["watched_root_count"], 0);
                break;
            }
            assert!(
                Instant::now() < status_deadline,
                "timed root did not become visible to the scheduler"
            );
            thread::sleep(Duration::from_millis(25));
        }

        fs::write(fixture.join("timed_fresh.txt"), b"fresh").unwrap();
        wait_for_file_search(&endpoint, "timed_fresh", |rows| rows.len() == 1);
        let status = send_request(&endpoint, "status", None, Value::Null).unwrap();
        let reconciles = status["data"]["root_watcher"]["scheduled_reconciles"]
            .as_u64()
            .unwrap();
        assert_eq!(
            reconciles, 1,
            "unexpected scheduled reconcile count: {status}"
        );
        thread::sleep(Duration::from_millis(300));
        let status = send_request(&endpoint, "status", None, Value::Null).unwrap();
        assert_eq!(
            status["data"]["root_watcher"]["scheduled_reconciles"]
                .as_u64()
                .unwrap(),
            reconciles,
            "polling retriggered the same interval before it became due again: {status}"
        );

        assert_eq!(
            send_request(&endpoint, "shutdown", None, Value::Null).unwrap()["ok"],
            true
        );
        runner.join().unwrap();
        assert!(!endpoint_path.exists());
    }

    #[test]
    fn timed_interval_root_catches_up_after_agent_restart() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let fixture = temp.path().join("files");
        fs::create_dir_all(&fixture).unwrap();
        fs::write(fixture.join("before_restart.txt"), b"before").unwrap();
        let endpoint_path = state.join(DEFAULT_ENDPOINT_FILE);

        let first =
            AgentServer::bind(CoreConfig::new(state.clone()), Some(endpoint_path.clone())).unwrap();
        let root = first
            .shared
            .core
            .add_root(fixture.clone(), RootKind::Local)
            .unwrap();
        set_test_root_refresh_policy(&first, root.id, RefreshPolicy::Interval { seconds: 1 });
        first
            .shared
            .core
            .reconcile_root(root.id, &CancellationToken::new())
            .unwrap();
        let first_endpoint = first.endpoint().unwrap();
        let first_runner = thread::spawn(move || first.run().unwrap());
        assert_eq!(
            send_request(&first_endpoint, "shutdown", None, Value::Null).unwrap()["ok"],
            true
        );
        first_runner.join().unwrap();

        fs::write(fixture.join("created_while_down.txt"), b"down").unwrap();
        thread::sleep(Duration::from_millis(1_100));

        let second =
            AgentServer::bind(CoreConfig::new(state), Some(endpoint_path.clone())).unwrap();
        let second_endpoint = second.endpoint().unwrap();
        let second_runner = thread::spawn(move || second.run().unwrap());
        wait_for_file_search(&second_endpoint, "created_while_down", |rows| {
            rows.len() == 1
        });
        let status = send_request(&second_endpoint, "status", None, Value::Null).unwrap();
        assert_eq!(status["data"]["root_watcher"]["watched_root_count"], 0);
        assert!(
            status["data"]["root_watcher"]["scheduled_reconciles"]
                .as_u64()
                .unwrap()
                >= 1,
            "restart did not perform the overdue scheduled reconcile: {status}"
        );

        assert_eq!(
            send_request(&second_endpoint, "shutdown", None, Value::Null).unwrap()["ok"],
            true
        );
        second_runner.join().unwrap();
        assert!(!endpoint_path.exists());
    }

    #[test]
    fn timed_long_cadence_root_stays_bounded_while_offline_and_recovers_when_it_returns() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let fixture = temp.path().join("mounted");
        let detached = temp.path().join("detached");
        fs::create_dir_all(&fixture).unwrap();
        fs::write(fixture.join("kept.txt"), b"kept").unwrap();
        let endpoint_path = state.join(DEFAULT_ENDPOINT_FILE);
        let server =
            AgentServer::bind(CoreConfig::new(state), Some(endpoint_path.clone())).unwrap();
        let root = server
            .shared
            .core
            .add_root(fixture.clone(), RootKind::Local)
            .unwrap();
        set_test_root_refresh_policy(
            &server,
            root.id,
            RefreshPolicy::Daily { hour: 0, minute: 0 },
        );
        server
            .shared
            .core
            .reconcile_root(root.id, &CancellationToken::new())
            .unwrap();
        let endpoint = server.endpoint().unwrap();
        let runner = thread::spawn(move || server.run().unwrap());

        fs::rename(&fixture, &detached).unwrap();
        let deadline = Instant::now() + Duration::from_secs(4);
        loop {
            let roots = send_request(&endpoint, "list_roots", None, Value::Null).unwrap();
            if roots["data"][0]["availability"] == "unavailable" {
                break;
            }
            assert!(
                Instant::now() < deadline,
                "timed root was not marked unavailable"
            );
            thread::sleep(Duration::from_millis(50));
        }
        let status_before = send_request(&endpoint, "status", None, Value::Null).unwrap();
        thread::sleep(Duration::from_millis(1_250));
        let status_after = send_request(&endpoint, "status", None, Value::Null).unwrap();
        assert_eq!(
            status_before["data"]["root_watcher"]["scheduled_failures"],
            status_after["data"]["root_watcher"]["scheduled_failures"],
            "an offline timed root generated repeated scheduler failures"
        );
        assert_eq!(
            status_before["data"]["root_watcher"]["scheduled_reconciles"],
            status_after["data"]["root_watcher"]["scheduled_reconciles"],
            "an offline timed root kept reconciling during the hold"
        );

        fs::rename(&detached, &fixture).unwrap();
        fs::write(fixture.join("returned.txt"), b"returned").unwrap();
        wait_for_file_search(&endpoint, "returned", |rows| rows.len() == 1);
        let available = wait_for_file_search(&endpoint, "kept", |rows| {
            rows.first()
                .and_then(|row| row.get("item"))
                .and_then(|item| item.get("available"))
                .and_then(Value::as_bool)
                == Some(true)
        });
        assert_eq!(available.len(), 1);

        assert_eq!(
            send_request(&endpoint, "shutdown", None, Value::Null).unwrap()["ok"],
            true
        );
        runner.join().unwrap();
        assert!(!endpoint_path.exists());
    }

    #[test]
    fn shutdown_cancels_registered_requests_before_server_exit() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let endpoint_path = state.join(DEFAULT_ENDPOINT_FILE);
        let server =
            AgentServer::bind(CoreConfig::new(state), Some(endpoint_path.clone())).unwrap();
        let endpoint = server.endpoint().unwrap();
        let token = CancellationToken::new();
        server
            .shared
            .requests
            .lock()
            .unwrap()
            .insert(991, token.clone());
        let runner = thread::spawn(move || server.run().unwrap());

        let shutdown = send_request(&endpoint, "shutdown", None, Value::Null).unwrap();
        assert_eq!(shutdown["ok"], true);
        assert_eq!(shutdown["data"]["stopping"], true);
        runner.join().unwrap();
        assert!(token.is_cancelled());
        assert!(!endpoint_path.exists());
    }

    #[cfg(any(target_os = "windows", target_os = "macos"))]
    #[test]
    fn configured_workspace_catalog_loads_before_publication_and_refreshes_only_configured_path() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let workspace = temp.path().join("workspace");
        fs::create_dir_all(&workspace).unwrap();
        let launcher = workspace.join("Launch Fixture");
        fs::write(&launcher, b"launcher").unwrap();
        let catalog = workspace.join("workspace_catalog.json");
        write_workspace_catalog(&catalog, &launcher, "Workspace Hero", "Hero Alias");

        let endpoint_path = state.join(DEFAULT_ENDPOINT_FILE);
        let server = AgentServer::bind_with_workspace_catalog(
            CoreConfig::new(state),
            Some(endpoint_path.clone()),
            Some(catalog.clone()),
        )
        .unwrap();
        assert!(endpoint_path.exists());
        let endpoint = server.endpoint().unwrap();
        let runner = thread::spawn(move || server.run().unwrap());

        let provider =
            send_request(&endpoint, "workspace_catalog_status", None, Value::Null).unwrap();
        assert_eq!(provider["ok"], true);
        assert_eq!(provider["data"]["configured"], true);
        assert_eq!(provider["data"]["loaded"], true);
        assert_eq!(provider["data"]["last_refresh"]["item_count"], 1);

        let initial =
            send_request(&endpoint, "search", Some(50), search_request("hero alias")).unwrap();
        assert_eq!(initial["ok"], true);
        assert_eq!(initial["data"]["results"].as_array().unwrap().len(), 1);

        write_workspace_catalog(&catalog, &launcher, "Workspace Updated", "Updated Alias");
        let refreshed = send_request(
            &endpoint,
            "workspace_catalog_refresh",
            Some(51),
            Value::Null,
        )
        .unwrap();
        assert_eq!(refreshed["ok"], true);
        assert_eq!(refreshed["data"]["item_count"], 1);

        let updated = send_request(
            &endpoint,
            "search",
            Some(52),
            search_request("updated alias"),
        )
        .unwrap();
        assert_eq!(updated["ok"], true);
        assert_eq!(updated["data"]["results"].as_array().unwrap().len(), 1);

        fs::write(&catalog, b"not-json").unwrap();
        let failed_refresh = send_request(
            &endpoint,
            "workspace_catalog_refresh",
            Some(53),
            Value::Null,
        )
        .unwrap();
        assert_eq!(failed_refresh["ok"], false);
        assert_eq!(failed_refresh["error"]["code"], "serialization_error");

        let provider_after_failure =
            send_request(&endpoint, "workspace_catalog_status", None, Value::Null).unwrap();
        assert_eq!(provider_after_failure["ok"], true);
        assert_eq!(provider_after_failure["data"]["loaded"], true);
        assert_eq!(
            provider_after_failure["data"]["last_refresh"]["item_count"],
            1
        );
        assert!(
            provider_after_failure["data"]["last_error"]
                .as_str()
                .is_some()
        );
        let still_loaded = send_request(
            &endpoint,
            "search",
            Some(54),
            search_request("updated alias"),
        )
        .unwrap();
        assert_eq!(still_loaded["ok"], true);
        assert_eq!(still_loaded["data"]["results"].as_array().unwrap().len(), 1);

        let other_catalog = workspace.join("other.json");
        fs::write(&other_catalog, b"{}").unwrap();
        let arbitrary = send_request(
            &endpoint,
            "workspace_catalog_refresh",
            Some(55),
            json!({"path": other_catalog}),
        )
        .unwrap();
        assert_eq!(arbitrary["ok"], false);
        assert_eq!(arbitrary["error"]["code"], "invalid_request");

        let shutdown = send_request(&endpoint, "shutdown", None, Value::Null).unwrap();
        assert_eq!(shutdown["ok"], true);
        runner.join().unwrap();
    }

    #[cfg(any(target_os = "windows", target_os = "macos"))]
    #[test]
    fn invalid_workspace_catalog_does_not_block_ordinary_agent_startup() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let catalog = temp.path().join("missing-workspace-catalog.json");
        let endpoint_path = state.join(DEFAULT_ENDPOINT_FILE);
        let server = AgentServer::bind_with_workspace_catalog(
            CoreConfig::new(state),
            Some(endpoint_path),
            Some(catalog),
        )
        .unwrap();
        let endpoint = server.endpoint().unwrap();
        let runner = thread::spawn(move || server.run().unwrap());

        let status = send_request(&endpoint, "status", None, Value::Null).unwrap();
        assert_eq!(status["ok"], true);

        let provider =
            send_request(&endpoint, "workspace_catalog_status", None, Value::Null).unwrap();
        assert_eq!(provider["ok"], true);
        assert_eq!(provider["data"]["configured"], true);
        assert_eq!(provider["data"]["loaded"], false);
        assert!(provider["data"]["last_error"].as_str().is_some());

        let refresh = send_request(
            &endpoint,
            "workspace_catalog_refresh",
            Some(60),
            Value::Null,
        )
        .unwrap();
        assert_eq!(refresh["ok"], false);
        assert_eq!(refresh["error"]["code"], "filesystem_error");

        let status_after = send_request(&endpoint, "status", None, Value::Null).unwrap();
        assert_eq!(status_after["ok"], true);

        let shutdown = send_request(&endpoint, "shutdown", None, Value::Null).unwrap();
        assert_eq!(shutdown["ok"], true);
        runner.join().unwrap();
    }
}
