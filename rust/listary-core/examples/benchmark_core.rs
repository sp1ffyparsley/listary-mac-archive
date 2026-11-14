use listary_core::{
    CacheShapeStats, CancellationToken, Core, CoreConfig, DiscoveredItem, FacetCountsRequest,
    ItemKind, RootKind, SearchRequest, StartupMetrics,
};
use serde::{Deserialize, Serialize};
use std::{
    env,
    path::{Path, PathBuf},
    process::Command,
    time::{Duration, Instant},
};
use tempfile::TempDir;

#[derive(Debug, Serialize)]
struct Percentiles {
    min_us: u64,
    p50_us: u64,
    p95_us: u64,
    p99_us: u64,
    max_us: u64,
}

#[derive(Debug, Serialize)]
struct BenchmarkReport {
    items: usize,
    updates: usize,
    query_iterations: usize,
    fresh_database_open_ms: f64,
    synthetic_fixture_generation_ms: f64,
    initial_catalog_ingest_ms: f64,
    initial_cache_checkpoint_ms: f64,
    initial_ingest_plus_checkpoint_ms: f64,
    existing_catalog_cache_miss_reopen_ms: f64,
    existing_catalog_cache_miss_startup: Option<StartupMetrics>,
    existing_catalog_cache_miss_memory: Option<MemorySnapshot>,
    warm_reopen_existing_ms: f64,
    separate_process_reopen_ms: Option<f64>,
    separate_process_total_ms: Option<f64>,
    separate_process_startup: Option<StartupMetrics>,
    separate_process_memory: Option<MemorySnapshot>,
    cache_shape: CacheShapeStats,
    first_query_us: u64,
    incremental_query: Percentiles,
    exact_empty_facet_counts_us: u64,
    exact_broad_facet_counts_us: u64,
    deep_50k_candidate_query_us: u64,
    update_catalog_ingest_ms: f64,
    update_cache_checkpoint_ms: f64,
    update_ingest_plus_checkpoint_ms: f64,
    update_query_us: u64,
    index_bytes: u64,
    notes: Vec<&'static str>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
struct MemorySnapshot {
    working_set_bytes: u64,
    peak_working_set_bytes: u64,
    private_bytes: u64,
}

#[derive(Debug, Serialize, Deserialize)]
struct ChildOpenReport {
    core_open_ms: f64,
    item_count: usize,
    startup: StartupMetrics,
    memory: Option<MemorySnapshot>,
}

struct ChildMeasurement {
    report: Option<ChildOpenReport>,
    total_ms: f64,
}

#[derive(Debug, Serialize)]
struct PreparedStateOpenReport {
    first_open: ChildOpenReport,
    warm_reopen: ChildOpenReport,
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.get(1).map(String::as_str) == Some("--child-open") {
        child_open(Path::new(args.get(2).expect("state path required")));
        return;
    }
    if args.get(1).map(String::as_str) == Some("--prepare-state") {
        let state = Path::new(args.get(2).expect("state path required"));
        let items = parse_arg(&args, "--items").unwrap_or(100_000);
        prepare_state(state, items);
        return;
    }
    if args.get(1).map(String::as_str) == Some("--measure-state") {
        measure_prepared_state(Path::new(args.get(2).expect("state path required")));
        return;
    }

    let items = parse_arg(&args, "--items").unwrap_or(100_000);
    let updates = parse_arg(&args, "--updates").unwrap_or(5_000).min(items);
    let query_iterations = parse_arg(&args, "--queries").unwrap_or(200);
    let state = TempDir::new().expect("temp state");

    let open_started = Instant::now();
    let core = Core::open(CoreConfig::new(state.path().to_path_buf())).expect("fresh core open");
    let fresh_database_open_ms = millis(open_started.elapsed());
    let root = core
        .add_root(PathBuf::from("X:/listary-benchmark"), RootKind::Local)
        .expect("benchmark root");

    let fixture_started = Instant::now();
    let fixture = synthetic_items(items);
    let synthetic_fixture_generation_ms = millis(fixture_started.elapsed());

    let ingest_started = Instant::now();
    core.replace_root_snapshot(root.id, fixture)
        .expect("snapshot ingest");
    let initial_catalog_ingest_ms = millis(ingest_started.elapsed());
    let checkpoint_started = Instant::now();
    core.flush().expect("flush");
    let initial_cache_checkpoint_ms = millis(checkpoint_started.elapsed());
    let initial_ingest_plus_checkpoint_ms = millis(ingest_started.elapsed());
    drop(core);

    let snapshot_child = measure_child_open(state.path());

    let derived_cache = state.path().join("cache").join("search-cache.bin");
    std::fs::remove_file(&derived_cache).expect("remove derived cache for cache-miss startup");
    let cache_miss_child = measure_child_open(state.path());
    let existing_catalog_cache_miss_reopen_ms = cache_miss_child
        .report
        .as_ref()
        .map(|report| report.core_open_ms)
        .unwrap_or_default();

    let warm_started = Instant::now();
    let core = Core::open(CoreConfig::new(state.path().to_path_buf())).expect("warm reopen");
    let warm_reopen_existing_ms = millis(warm_started.elapsed());

    let target = format!("scene_{:07}", items.saturating_sub(1));
    let first_started = Instant::now();
    let response = core
        .search(
            &SearchRequest {
                query: target.clone(),
                limit: 20,
                ..SearchRequest::default()
            },
            &CancellationToken::new(),
        )
        .expect("first query");
    let first_query_us = micros(first_started.elapsed());
    assert!(
        response
            .results
            .first()
            .is_some_and(|r| r.item.name.contains(&target)),
        "target should rank first"
    );

    let mut latencies = Vec::with_capacity(query_iterations);
    let query_shapes = [
        "scene",
        "scene_0",
        "scene_00",
        "final",
        "snowfall",
        "project_042",
        "scnfl",
        "ext:mov scene",
    ];
    for i in 0..query_iterations {
        let query = query_shapes[i % query_shapes.len()];
        let started = Instant::now();
        let _ = core
            .search(
                &SearchRequest {
                    query: query.to_string(),
                    limit: 50,
                    ..SearchRequest::default()
                },
                &CancellationToken::new(),
            )
            .expect("incremental query");
        latencies.push(micros(started.elapsed()));
    }

    let empty_facets_started = Instant::now();
    let empty_facets = core
        .facet_counts(
            &FacetCountsRequest {
                root_ids: vec![root.id],
                ..FacetCountsRequest::default()
            },
            &CancellationToken::new(),
        )
        .expect("empty facet counts");
    let exact_empty_facet_counts_us = micros(empty_facets_started.elapsed());
    assert_eq!(empty_facets.matched_total, items as u64);

    let broad_facets_started = Instant::now();
    let broad_facets = core
        .facet_counts(
            &FacetCountsRequest {
                query: "snowfall".into(),
                root_ids: vec![root.id],
                ..FacetCountsRequest::default()
            },
            &CancellationToken::new(),
        )
        .expect("broad facet counts");
    let exact_broad_facet_counts_us = micros(broad_facets_started.elapsed());
    assert_eq!(broad_facets.matched_total, items as u64);

    let update_fixture: Vec<_> = (0..updates)
        .map(|i| {
            let index = items.saturating_sub(1).saturating_sub(i);
            DiscoveredItem::new(
                synthetic_path(index),
                ItemKind::File,
                50_000_000 + i as u64,
                2_000_000_000_000 + i as i64,
            )
        })
        .collect();
    let update_started = Instant::now();
    core.upsert_items(root.id, update_fixture)
        .expect("update ingest");
    let update_catalog_ingest_ms = millis(update_started.elapsed());
    let update_checkpoint_started = Instant::now();
    core.flush().expect("update flush");
    let update_cache_checkpoint_ms = millis(update_checkpoint_started.elapsed());
    let update_ingest_plus_checkpoint_ms = millis(update_started.elapsed());

    let update_query_started = Instant::now();
    let _ = core
        .search(
            &SearchRequest {
                query: target,
                limit: 20,
                ..SearchRequest::default()
            },
            &CancellationToken::new(),
        )
        .expect("update query");
    let update_query_us = micros(update_query_started.elapsed());

    let deep_started = Instant::now();
    let _ = core
        .search(
            &SearchRequest {
                query: "snowfall".into(),
                limit: 500,
                candidate_limit: 50_000,
                ..SearchRequest::default()
            },
            &CancellationToken::new(),
        )
        .expect("deep query");
    let deep_50k_candidate_query_us = micros(deep_started.elapsed());
    let diagnostics = core.diagnostics().expect("diagnostics");
    let cache_shape = core.cache_shape_stats();

    let report = BenchmarkReport {
        items,
        updates,
        query_iterations,
        fresh_database_open_ms,
        synthetic_fixture_generation_ms,
        initial_catalog_ingest_ms,
        initial_cache_checkpoint_ms,
        initial_ingest_plus_checkpoint_ms,
        existing_catalog_cache_miss_reopen_ms,
        existing_catalog_cache_miss_startup: cache_miss_child
            .report
            .as_ref()
            .map(|report| report.startup.clone()),
        existing_catalog_cache_miss_memory: cache_miss_child
            .report
            .as_ref()
            .and_then(|report| report.memory),
        warm_reopen_existing_ms,
        separate_process_reopen_ms: snapshot_child
            .report
            .as_ref()
            .map(|report| report.core_open_ms),
        separate_process_total_ms: Some(snapshot_child.total_ms),
        separate_process_startup: snapshot_child
            .report
            .as_ref()
            .map(|report| report.startup.clone()),
        separate_process_memory: snapshot_child
            .report
            .as_ref()
            .and_then(|report| report.memory),
        cache_shape,
        first_query_us,
        incremental_query: percentiles(latencies),
        exact_empty_facet_counts_us,
        exact_broad_facet_counts_us,
        deep_50k_candidate_query_us,
        update_catalog_ingest_ms,
        update_cache_checkpoint_ms,
        update_ingest_plus_checkpoint_ms,
        update_query_us,
        index_bytes: diagnostics.index_bytes,
        notes: vec![
            "Scale fixture uses real RocksDB persistence and the production in-memory candidate indexes, but synthetic metadata instead of creating hundreds of thousands of physical files.",
            "separate_process_reopen_ms is a fresh process with the OS page cache left intact; it is not a forced cold-disk measurement.",
            "Filesystem crawl/mutation behavior is covered separately by disposable on-disk integration tests.",
            "Facet count timings are exact full candidate-set scans on the shared cache and are measured separately from candidate-bounded interactive search.",
        ],
    };
    println!("{}", serde_json::to_string_pretty(&report).unwrap());
}

fn child_open(state: &Path) {
    let report = open_report(state, "child open");
    println!("{}", serde_json::to_string(&report).unwrap());
}

fn open_report(state: &Path, context: &str) -> ChildOpenReport {
    let started = Instant::now();
    let (core, startup) =
        Core::open_profiled(CoreConfig::new(state.to_path_buf())).unwrap_or_else(|error| {
            panic!("{context}: {error}");
        });
    let elapsed = millis(started.elapsed());
    let memory = process_memory();
    let diagnostics = core.diagnostics().unwrap_or_else(|error| {
        panic!("{context} diagnostics: {error}");
    });
    ChildOpenReport {
        core_open_ms: elapsed,
        item_count: diagnostics.item_count,
        startup,
        memory,
    }
}

fn measure_prepared_state(state: &Path) {
    let first_open = open_report(state, "prepared-state first open");
    let warm_reopen = open_report(state, "prepared-state warm reopen");
    let report = PreparedStateOpenReport {
        first_open,
        warm_reopen,
    };
    println!("{}", serde_json::to_string_pretty(&report).unwrap());
}

fn prepare_state(state: &Path, items: usize) {
    if state.exists() {
        let mut entries = std::fs::read_dir(state).expect("read prepared-state directory");
        assert!(
            entries.next().is_none(),
            "prepared-state directory must be empty"
        );
    } else {
        std::fs::create_dir_all(state).expect("create prepared-state directory");
    }
    let core = Core::open(CoreConfig::new(state.to_path_buf())).expect("prepare core open");
    let root = core
        .add_root(PathBuf::from("X:/listary-benchmark"), RootKind::Local)
        .expect("prepare benchmark root");
    core.replace_root_snapshot(root.id, synthetic_items(items))
        .expect("prepare snapshot ingest");
    core.flush().expect("prepare snapshot flush");
    let diagnostics = core.diagnostics().expect("prepare diagnostics");
    let snapshot = state.join("cache").join("search-cache.bin");
    let snapshot_bytes = std::fs::metadata(snapshot)
        .expect("prepared snapshot metadata")
        .len();
    println!(
        "{}",
        serde_json::json!({
            "items": diagnostics.item_count,
            "catalog_generation": diagnostics.catalog_generation,
            "snapshot_bytes": snapshot_bytes,
        })
    );
}

fn measure_child_open(state: &Path) -> ChildMeasurement {
    let Some(exe) = env::current_exe().ok() else {
        return ChildMeasurement {
            report: None,
            total_ms: 0.0,
        };
    };
    let total_started = Instant::now();
    let output = Command::new(exe)
        .arg("--child-open")
        .arg(state)
        .output()
        .ok();
    let total_ms = millis(total_started.elapsed());
    let Some(output) = output else {
        return ChildMeasurement {
            report: None,
            total_ms,
        };
    };
    if !output.status.success() {
        return ChildMeasurement {
            report: None,
            total_ms,
        };
    }
    let report = String::from_utf8(output.stdout)
        .ok()
        .and_then(|stdout| serde_json::from_str(stdout.trim()).ok());
    ChildMeasurement { report, total_ms }
}

#[cfg(windows)]
fn process_memory() -> Option<MemorySnapshot> {
    use windows_sys::Win32::System::{
        ProcessStatus::{GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS},
        Threading::GetCurrentProcess,
    };

    let mut counters = PROCESS_MEMORY_COUNTERS {
        cb: std::mem::size_of::<PROCESS_MEMORY_COUNTERS>() as u32,
        ..Default::default()
    };
    let ok = unsafe {
        GetProcessMemoryInfo(
            GetCurrentProcess(),
            &mut counters,
            std::mem::size_of::<PROCESS_MEMORY_COUNTERS>() as u32,
        )
    };
    (ok != 0).then_some(MemorySnapshot {
        working_set_bytes: counters.WorkingSetSize as u64,
        peak_working_set_bytes: counters.PeakWorkingSetSize as u64,
        private_bytes: counters.PagefileUsage as u64,
    })
}

#[cfg(not(windows))]
fn process_memory() -> Option<MemorySnapshot> {
    None
}

fn parse_arg(args: &[String], name: &str) -> Option<usize> {
    args.windows(2)
        .find(|window| window[0] == name)
        .and_then(|window| window[1].parse().ok())
}

fn synthetic_items(count: usize) -> Vec<DiscoveredItem> {
    (0..count)
        .map(|i| {
            DiscoveredItem::new(
                synthetic_path(i),
                ItemKind::File,
                1_000_000 + (i % 50_000_000) as u64,
                1_700_000_000_000 + i as i64,
            )
        })
        .collect()
}

fn synthetic_path(i: usize) -> PathBuf {
    PathBuf::from(format!(
        "X:/listary-benchmark/project_{:03}/sequence_{:03}/scene_{i:07}_snowfall_final.mov",
        i % 500,
        (i / 17) % 250
    ))
}

fn percentiles(mut values: Vec<u64>) -> Percentiles {
    values.sort_unstable();
    let index = |p: f64| -> usize {
        (((values.len().saturating_sub(1)) as f64 * p).round() as usize)
            .min(values.len().saturating_sub(1))
    };
    Percentiles {
        min_us: *values.first().unwrap_or(&0),
        p50_us: values.get(index(0.50)).copied().unwrap_or(0),
        p95_us: values.get(index(0.95)).copied().unwrap_or(0),
        p99_us: values.get(index(0.99)).copied().unwrap_or(0),
        max_us: *values.last().unwrap_or(&0),
    }
}

fn millis(duration: Duration) -> f64 {
    duration.as_secs_f64() * 1_000.0
}

fn micros(duration: Duration) -> u64 {
    duration.as_micros().min(u64::MAX as u128) as u64
}
