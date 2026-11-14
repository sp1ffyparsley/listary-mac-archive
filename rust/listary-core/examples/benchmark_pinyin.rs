use listary_core::{
    CacheShapeStats, CancellationToken, Core, CoreConfig, DiscoveredItem, ItemKind, RootKind,
    SearchRequest, StartupMetrics,
};
use serde::Serialize;
use std::{
    env,
    path::PathBuf,
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

#[derive(Debug, Clone, Copy, Serialize)]
struct MemorySnapshot {
    working_set_bytes: u64,
    peak_working_set_bytes: u64,
    private_bytes: u64,
}

#[derive(Debug, Serialize)]
struct PinyinBenchmarkReport {
    items: usize,
    query_iterations: usize,
    fixture_generation_ms: f64,
    ingest_ms: f64,
    checkpoint_ms: f64,
    snapshot_bytes: u64,
    cache_shape: CacheShapeStats,
    memory_after_checkpoint: Option<MemorySnapshot>,
    snapshot_reopen_ms: f64,
    snapshot_reopen_startup: StartupMetrics,
    memory_after_reopen: Option<MemorySnapshot>,
    literal_query: Percentiles,
    quanpin_query: Percentiles,
    jianpin_query: Percentiles,
    mixed_query: Percentiles,
    targeted_mixed_query_us: u64,
    targeted_result_name: String,
    notes: Vec<&'static str>,
}

fn main() {
    let args: Vec<String> = env::args().collect();
    let items = parse_arg(&args, "--items").unwrap_or(100_000);
    let query_iterations = parse_arg(&args, "--queries").unwrap_or(50).max(1);
    let state = TempDir::new().expect("temp state");
    let core = Core::open(CoreConfig::new(state.path().to_path_buf())).expect("fresh core open");
    let root = core
        .add_root(PathBuf::from("X:/拼音性能"), RootKind::Local)
        .expect("benchmark root");

    let fixture_started = Instant::now();
    let fixture = (0..items).map(synthetic_item).collect::<Vec<_>>();
    let fixture_generation_ms = millis(fixture_started.elapsed());

    let ingest_started = Instant::now();
    core.replace_root_snapshot(root.id, fixture)
        .expect("snapshot ingest");
    let ingest_ms = millis(ingest_started.elapsed());
    let checkpoint_started = Instant::now();
    core.flush().expect("checkpoint");
    let checkpoint_ms = millis(checkpoint_started.elapsed());
    let snapshot = state.path().join("cache/search-cache.bin");
    let snapshot_bytes = std::fs::metadata(&snapshot)
        .expect("snapshot metadata")
        .len();
    let cache_shape = core.cache_shape_stats();
    let memory_after_checkpoint = process_memory();

    let literal_query = benchmark_query(&core, "scene", query_iterations);
    let quanpin_query = benchmark_query(&core, "gongsishouce", query_iterations);
    let jianpin_query = benchmark_query(&core, "gssc", query_iterations);
    let mixed_query = benchmark_query(&core, "gsshouce", query_iterations);
    let target = format!("gsshouce_scene_{:07}", items.saturating_sub(1));
    let target_started = Instant::now();
    let targeted = core
        .search(
            &SearchRequest {
                query: target,
                limit: 20,
                ..SearchRequest::default()
            },
            &CancellationToken::new(),
        )
        .expect("targeted mixed query");
    let targeted_mixed_query_us = micros(target_started.elapsed());
    let targeted_result_name = targeted
        .results
        .first()
        .map(|result| result.item.name.clone())
        .expect("targeted mixed query should find the last item");
    assert!(
        targeted_result_name.contains(&format!("{:07}", items.saturating_sub(1))),
        "targeted mixed Pinyin query did not rank the intended high-id item first"
    );

    drop(core);
    let reopen_started = Instant::now();
    let (reopened, snapshot_reopen_startup) =
        Core::open_profiled(CoreConfig::new(state.path().to_path_buf())).expect("snapshot reopen");
    let snapshot_reopen_ms = millis(reopen_started.elapsed());
    let memory_after_reopen = process_memory();
    assert!(
        !reopened
            .search(
                &SearchRequest {
                    query: "gsshouce".into(),
                    limit: 20,
                    ..SearchRequest::default()
                },
                &CancellationToken::new(),
            )
            .expect("reopen Pinyin query")
            .results
            .is_empty(),
        "Pinyin postings must survive derived-cache snapshot reopen"
    );

    let report = PinyinBenchmarkReport {
        items,
        query_iterations,
        fixture_generation_ms,
        ingest_ms,
        checkpoint_ms,
        snapshot_bytes,
        cache_shape,
        memory_after_checkpoint,
        snapshot_reopen_ms,
        snapshot_reopen_startup,
        memory_after_reopen,
        literal_query,
        quanpin_query,
        jianpin_query,
        mixed_query,
        targeted_mixed_query_us,
        targeted_result_name,
        notes: vec![
            "Synthetic paths contain repeated Hanzi parent/name segments plus a unique ASCII scene suffix, exercising derived Pinyin postings without creating physical files.",
            "Literal, Quanpin, Jianpin, and mixed Pinyin timings use the ordinary production search API with its default candidate bound.",
            "The benchmark is Windows/shared-core evidence only; it is not native AppKit or real-Mac latency proof.",
        ],
    };
    println!("{}", serde_json::to_string_pretty(&report).unwrap());
}

fn synthetic_item(index: usize) -> DiscoveredItem {
    DiscoveredItem::new(
        PathBuf::from(format!(
            "X:/拼音性能/项目_{:03}/公司手册_scene_{index:07}_最终版.txt",
            index % 500
        )),
        ItemKind::File,
        1_000_000 + (index % 10_000_000) as u64,
        1_700_000_000_000 + index as i64,
    )
}

fn benchmark_query(core: &Core, query: &str, iterations: usize) -> Percentiles {
    let mut values = Vec::with_capacity(iterations);
    for _ in 0..iterations {
        let started = Instant::now();
        let response = core
            .search(
                &SearchRequest {
                    query: query.into(),
                    limit: 50,
                    ..SearchRequest::default()
                },
                &CancellationToken::new(),
            )
            .unwrap_or_else(|error| panic!("query {query:?} failed: {error}"));
        assert!(
            !response.results.is_empty(),
            "query {query:?} should return benchmark results"
        );
        values.push(micros(started.elapsed()));
    }
    percentiles(values)
}

fn parse_arg(args: &[String], name: &str) -> Option<usize> {
    args.windows(2)
        .find(|window| window[0] == name)
        .and_then(|window| window[1].parse().ok())
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

fn millis(duration: Duration) -> f64 {
    duration.as_secs_f64() * 1_000.0
}

fn micros(duration: Duration) -> u64 {
    duration.as_micros().min(u64::MAX as u128) as u64
}
