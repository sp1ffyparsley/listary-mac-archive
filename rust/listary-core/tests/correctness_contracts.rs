use listary_core::{
    CancellationToken, Core, CoreConfig, DiscoveredItem, ItemKind, RootKind, SearchRequest,
};
use std::{
    panic::catch_unwind,
    path::{Path, PathBuf},
};
use tempfile::TempDir;
use unicode_normalization::UnicodeNormalization;

fn open_core(state: &Path) -> Core {
    Core::open(CoreConfig::new(state.to_path_buf())).expect("core opens")
}

fn normalized(input: &str) -> String {
    input
        .nfkc()
        .flat_map(char::to_lowercase)
        .collect::<String>()
}

fn next(seed: &mut u64) -> usize {
    *seed = seed
        .wrapping_mul(6_364_136_223_846_793_005)
        .wrapping_add(1_442_695_040_888_963_407);
    (*seed >> 32) as usize
}

fn assert_sorted_unique(values: &[String]) {
    assert!(
        values.windows(2).all(|pair| pair[0] < pair[1]),
        "expected sorted unique values, got {values:?}"
    );
}

#[test]
fn query_parser_unicode_and_syntax_invariants_hold_for_generated_corpus() {
    const ATOMS: &[&str] = &[
        "snowfall",
        "ＣＡＦÉ",
        "cafe\u{301}",
        "场景",
        "🎬",
        "İSTANBUL",
        "ext:MP4;mov",
        "ext：.tar.gz,ＰＤＦ",
        "-ext:TMP",
        "*.PNG",
        "-*.tmp",
        "date:>=2026-01",
        "date:2026-01..2026-12",
        "date:not-a-date",
        "size:>10mb",
        "size:0.5gb..2gb",
        "size:not-a-size",
        "\"quoted value\"",
        "-\"bad take\"",
        "\"unterminated",
        "\"escaped\\\"quote\"",
        "projects/",
        "folder\\",
        "--draft",
        "-",
        "a:b",
        "\\",
        "\u{200d}",
        "\0",
    ];
    const SEPARATORS: &[&str] = &[" ", "\t", "\n", "\r\n", "\u{2003}"];
    const NOW: i64 = 1_800_000_000_000;

    for case in 0..768u64 {
        let mut seed = 0x9e37_79b9_7f4a_7c15u64 ^ case.wrapping_mul(0x1000_0000_01b3);
        let count = 1 + next(&mut seed) % 10;
        let mut input = String::new();
        for index in 0..count {
            if index != 0 {
                input.push_str(SEPARATORS[next(&mut seed) % SEPARATORS.len()]);
            }
            input.push_str(ATOMS[next(&mut seed) % ATOMS.len()]);
        }

        let timezone_offset_minutes = match case % 5 {
            0 => -1_439,
            1 => -240,
            2 => 0,
            3 => 330,
            _ => 1_439,
        };
        let parsed =
            catch_unwind(|| listary_core::query::parse(&input, NOW, timezone_offset_minutes))
                .unwrap_or_else(|_| panic!("parser panicked for generated case {case}: {input:?}"));
        let repeated = listary_core::query::parse(&input, NOW, timezone_offset_minutes);
        assert_eq!(
            parsed, repeated,
            "parser was nondeterministic for {input:?}"
        );

        for term in parsed
            .positive_terms
            .iter()
            .chain(parsed.negative_terms.iter())
        {
            assert!(
                !term.text.is_empty(),
                "empty normalized term from {input:?}"
            );
            assert_eq!(
                term.text,
                normalized(&term.text),
                "term normalization was not idempotent for {input:?}"
            );
        }
        for extensions in [&parsed.include_extensions, &parsed.exclude_extensions] {
            assert_sorted_unique(extensions);
            for extension in extensions {
                assert!(!extension.is_empty());
                assert!(!extension.starts_with('.'));
                assert_eq!(extension, &normalized(extension));
            }
        }

        let json = serde_json::to_vec(&parsed).expect("parsed query serializes");
        let roundtrip = serde_json::from_slice(&json).expect("parsed query deserializes");
        assert_eq!(
            parsed, roundtrip,
            "ParsedQuery JSON round-trip drifted for {input:?}"
        );
    }
}

type RankingSignature = Vec<(String, i64, u8, i32)>;

fn ranking_signature(core: &Core) -> RankingSignature {
    let request = SearchRequest {
        query: "scene".into(),
        limit: 64,
        candidate_limit: 1_024,
        now_unix_ms: 1_800_000_000_000,
        ..SearchRequest::default()
    };
    core.search(&request, &CancellationToken::new())
        .expect("determinism search succeeds")
        .results
        .into_iter()
        .map(|result| {
            (
                result.item.path,
                result.score.combined,
                result.score.text_tier,
                result.score.text_quality,
            )
        })
        .collect()
}

fn build_catalog_and_reopen(
    state: &TempDir,
    reverse: bool,
) -> (RankingSignature, RankingSignature) {
    let before_restart = {
        let core = open_core(state.path());
        let root = core
            .add_root(PathBuf::from("X:/deterministic-ranking"), RootKind::Local)
            .expect("root added");
        let mut items: Vec<_> = (0..128)
            .map(|i| {
                DiscoveredItem::new(
                    PathBuf::from(format!(
                        "X:/deterministic-ranking/folder_{:03}/scene_take_{i:03}.mov",
                        i % 8
                    )),
                    ItemKind::File,
                    1_024,
                    1_700_000_000_000,
                )
            })
            .collect();
        if reverse {
            items.reverse();
        }
        core.replace_root_snapshot(root.id, items)
            .expect("snapshot replaced");
        let signature = ranking_signature(&core);
        core.flush().expect("catalog flushed");
        signature
    };

    let (reopened, metrics) = Core::open_profiled(CoreConfig::new(state.path().to_path_buf()))
        .expect("snapshot reopen succeeds");
    assert_eq!(
        metrics.cache_source,
        Some(listary_core::StartupCacheSource::Snapshot),
        "determinism fixture should exercise the persisted compact snapshot"
    );
    let after_restart = ranking_signature(&reopened);
    (before_restart, after_restart)
}

#[test]
fn ranking_is_deterministic_across_insertion_order_and_restart() {
    let forward_state = TempDir::new().unwrap();
    let reverse_state = TempDir::new().unwrap();

    let (forward, forward_reopened) = build_catalog_and_reopen(&forward_state, false);
    let (reverse, reverse_reopened) = build_catalog_and_reopen(&reverse_state, true);

    assert_eq!(forward.len(), 64);
    assert_eq!(forward, forward_reopened, "restart changed ranking order");
    assert_eq!(
        reverse, reverse_reopened,
        "restart changed reverse-built ranking"
    );
    assert_eq!(
        forward, reverse,
        "equivalent catalogs ranked differently when insertion order changed"
    );
}

#[test]
fn broad_queries_obey_candidate_and_result_bounds_at_adversarial_limits() {
    const ITEM_COUNT: usize = 4_096;
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from("X:/broad-bounds"), RootKind::Local)
        .unwrap();
    let items = (0..ITEM_COUNT)
        .map(|i| {
            DiscoveredItem::new(
                PathBuf::from(format!(
                    "X:/broad-bounds/folder_{:02}/scene_{i:05}_common.mov",
                    i % 32
                )),
                ItemKind::File,
                10_000 + i as u64,
                1_700_000_000_000 + i as i64,
            )
        })
        .collect();
    core.replace_root_snapshot(root.id, items).unwrap();

    let cases = [
        ("scene", 0usize, 0usize),
        ("scene", 7, 3),
        ("ext:mov", 5, 13),
        ("-does-not-exist", 17, 17),
        ("size:>=0b", 64, 1),
    ];
    for (query, limit, candidate_limit) in cases {
        let request = SearchRequest {
            query: query.into(),
            limit,
            candidate_limit,
            now_unix_ms: 1_800_000_000_000,
            ..SearchRequest::default()
        };
        let response = core
            .search(&request, &CancellationToken::new())
            .unwrap_or_else(|error| panic!("broad query {query:?} failed: {error}"));
        let effective_candidate_limit = candidate_limit.max(limit).clamp(1, ITEM_COUNT);

        assert_eq!(
            response.considered_candidates, effective_candidate_limit,
            "candidate work escaped its bound for {query:?}"
        );
        assert!(
            response.results.len() <= limit,
            "result count {} exceeded limit {limit} for {query:?}",
            response.results.len()
        );
    }
}
