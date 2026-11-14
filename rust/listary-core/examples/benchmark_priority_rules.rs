use listary_core::{
    CancellationToken, Core, CoreConfig, DiscoveredItem, ItemKind, Priority, PriorityRule,
    RootKind, SearchRequest,
};
use serde::Serialize;
use std::{env, path::PathBuf, time::Instant};
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
struct Report {
    items: usize,
    iterations: usize,
    baseline: Percentiles,
    nested_rule_update_ms: f64,
    nested_path_rules: Percentiles,
    regex_rule_update_ms: f64,
    regex_high_rule: Percentiles,
    regex_reopen_ms: f64,
    regex_reopen_priority_rules_load_us: u64,
    regex_reopen_query: Percentiles,
    notes: Vec<&'static str>,
}

fn main() {
    let args: Vec<String> = env::args().collect();
    let items = parse_arg(&args, "--items").unwrap_or(100_000);
    let iterations = parse_arg(&args, "--queries").unwrap_or(30).max(1);
    let state = TempDir::new().expect("temp state");
    let core = Core::open(CoreConfig::new(state.path().to_path_buf())).expect("core open");
    let root = core
        .add_root(PathBuf::from(r"X:\priority-benchmark"), RootKind::Local)
        .expect("root");
    let fixture = (0..items)
        .map(|index| {
            DiscoveredItem::new(
                PathBuf::from(format!(
                    r"X:\priority-benchmark\project_{:03}\scene_{index:07}.txt",
                    index % 500
                )),
                ItemKind::File,
                1024,
                1_700_000_000_000 + index as i64,
            )
        })
        .collect();
    core.replace_root_snapshot(root.id, fixture)
        .expect("ingest");

    let baseline = benchmark(&core, iterations);

    let nested_update_started = Instant::now();
    core.replace_priority_rules(vec![
        PriorityRule::path("X:/priority-benchmark/project_499", true, Priority::High),
        PriorityRule::path(
            "X:/priority-benchmark/project_499/archive",
            true,
            Priority::Ignored,
        ),
    ])
    .expect("path rules");
    let nested_rule_update_ms = millis(nested_update_started.elapsed());
    let nested_path_rules = benchmark(&core, iterations);

    let regex_update_started = Instant::now();
    core.replace_priority_rules(vec![PriorityRule::regex(
        r"/project_499/scene_.*[57]\.txt$",
        Priority::High,
    )])
    .expect("regex rule");
    let regex_rule_update_ms = millis(regex_update_started.elapsed());
    let regex_high_rule = benchmark(&core, iterations);
    core.flush().expect("flush");
    drop(core);

    let reopen_started = Instant::now();
    let (reopened, startup) = Core::open_profiled(CoreConfig::new(state.path().to_path_buf()))
        .expect("reopen with persisted rules");
    let regex_reopen_ms = millis(reopen_started.elapsed());
    let regex_reopen_priority_rules_load_us = startup.priority_rules_load_micros;
    let regex_reopen_query = benchmark(&reopened, iterations.min(10));

    println!(
        "{}",
        serde_json::to_string_pretty(&Report {
            items,
            iterations,
            baseline,
            nested_rule_update_ms,
            nested_path_rules,
            regex_rule_update_ms,
            regex_high_rule,
            regex_reopen_ms,
            regex_reopen_priority_rules_load_us,
            regex_reopen_query,
            notes: vec![
                "Synthetic Windows-style paths exercise the production shared-core query/ranking path without physical files.",
                "The same broad prefix query is repeated for baseline, nested path rules, and a High regex rule.",
                "Priority rule updates rebuild one derived in-memory priority bitmap; regex rules are not rescanned across the full catalog per query.",
                "This is Windows/shared-core evidence only; it is not native AppKit or real-Mac latency proof.",
            ],
        })
        .unwrap()
    );
}

fn millis(duration: std::time::Duration) -> f64 {
    duration.as_secs_f64() * 1_000.0
}

fn benchmark(core: &Core, iterations: usize) -> Percentiles {
    let mut values = Vec::with_capacity(iterations);
    for _ in 0..iterations {
        let started = Instant::now();
        let response = core
            .search(
                &SearchRequest {
                    query: "scene".into(),
                    limit: 50,
                    ..SearchRequest::default()
                },
                &CancellationToken::new(),
            )
            .expect("search");
        assert!(!response.results.is_empty());
        values.push(started.elapsed().as_micros().min(u64::MAX as u128) as u64);
    }
    values.sort_unstable();
    let at = |p: f64| -> u64 {
        let index = (((values.len() - 1) as f64 * p).round() as usize).min(values.len() - 1);
        values[index]
    };
    Percentiles {
        min_us: values[0],
        p50_us: at(0.50),
        p95_us: at(0.95),
        p99_us: at(0.99),
        max_us: *values.last().unwrap(),
    }
}

fn parse_arg(args: &[String], name: &str) -> Option<usize> {
    args.windows(2)
        .find(|window| window[0] == name)
        .and_then(|window| window[1].parse().ok())
}
