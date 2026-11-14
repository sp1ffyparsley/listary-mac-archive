use listary_core::{
    CancellationToken, Core, CoreConfig, CoreError, DiscoveredItem, FacetCountsRequest,
    FileFacetCategory, FileSearchRequest, FileSearchResponse, FileSearchSortField, FilterPreset,
    FilterTarget, ItemKind, MAX_FILE_SEARCH_PAGE_LIMIT, ModifiedTimeFacet, Priority, PriorityRule,
    RootKind, SortDirection,
};
use std::{collections::HashSet, path::PathBuf};
use tempfile::TempDir;

const NOW: i64 = 1_800_000_000_000;

fn open_core(state: &TempDir) -> Core {
    Core::open(CoreConfig::new(state.path().to_path_buf())).expect("core opens")
}

fn exact(core: &Core, request: FileSearchRequest) -> FileSearchResponse {
    core.file_search(&request, &CancellationToken::new())
        .expect("exact file search succeeds")
}

fn discovered(
    path: impl Into<PathBuf>,
    kind: ItemKind,
    size: u64,
    modified_unix_ms: i64,
    priority: Priority,
) -> DiscoveredItem {
    let mut item = DiscoveredItem::new(path.into(), kind, size, modified_unix_ms);
    item.priority = priority;
    item
}

#[test]
fn exact_search_pages_past_interactive_budget_and_rejects_stale_generation() {
    const ITEM_COUNT: usize = 5_003;
    let state = TempDir::new().unwrap();
    let core = open_core(&state);
    let root = core
        .add_root(PathBuf::from("X:/exact-scale"), RootKind::Local)
        .unwrap();
    core.replace_root_snapshot(
        root.id,
        (0..ITEM_COUNT)
            .map(|index| {
                discovered(
                    format!("X:/exact-scale/asset_{index:05}.txt"),
                    ItemKind::File,
                    index as u64,
                    index as i64,
                    Priority::Normal,
                )
            })
            .collect(),
    )
    .unwrap();

    let base = FileSearchRequest {
        query: "asset".into(),
        root_ids: vec![root.id],
        limit: 113,
        sort_field: FileSearchSortField::Name,
        sort_direction: SortDirection::Ascending,
        now_unix_ms: NOW,
        ..FileSearchRequest::default()
    };
    let first = exact(&core, base.clone());
    assert_eq!(first.total_matches, ITEM_COUNT as u64);
    assert_eq!(first.considered_candidates, ITEM_COUNT as u64);
    assert_eq!(first.results.len(), 113);
    assert!(first.has_more);
    assert_eq!(first.results[0].item.name, "asset_00000.txt");

    let repeated = exact(
        &core,
        FileSearchRequest {
            expected_catalog_generation: Some(first.catalog_generation),
            ..base.clone()
        },
    );
    assert_eq!(
        first
            .results
            .iter()
            .map(|result| result.item.id)
            .collect::<Vec<_>>(),
        repeated
            .results
            .iter()
            .map(|result| result.item.id)
            .collect::<Vec<_>>()
    );

    let second = exact(
        &core,
        FileSearchRequest {
            offset: first.limit as u64,
            expected_catalog_generation: Some(first.catalog_generation),
            ..base.clone()
        },
    );
    let first_ids: HashSet<_> = first.results.iter().map(|result| result.item.id).collect();
    let second_ids: HashSet<_> = second.results.iter().map(|result| result.item.id).collect();
    assert!(first_ids.is_disjoint(&second_ids));
    assert_eq!(second.results[0].item.name, "asset_00113.txt");

    let deep = exact(
        &core,
        FileSearchRequest {
            offset: 4_096,
            limit: MAX_FILE_SEARCH_PAGE_LIMIT,
            expected_catalog_generation: Some(first.catalog_generation),
            ..base.clone()
        },
    );
    assert_eq!(deep.results.len(), ITEM_COUNT - 4_096);
    assert_eq!(deep.results[0].item.name, "asset_04096.txt");
    assert!(!deep.has_more);

    core.upsert_items(
        root.id,
        vec![discovered(
            "X:/exact-scale/asset_99999.txt",
            ItemKind::File,
            1,
            1,
            Priority::Normal,
        )],
    )
    .unwrap();
    let stale = core
        .file_search(
            &FileSearchRequest {
                expected_catalog_generation: Some(first.catalog_generation),
                ..base
            },
            &CancellationToken::new(),
        )
        .unwrap_err();
    assert!(matches!(
        stale,
        CoreError::StaleCatalog {
            expected,
            actual
        } if expected == first.catalog_generation && actual > expected
    ));
}

#[test]
fn every_sort_field_supports_both_directions_with_deterministic_pages() {
    let state = TempDir::new().unwrap();
    let core = open_core(&state);
    let root = core
        .add_root(PathBuf::from("X:/sort"), RootKind::Local)
        .unwrap();
    core.replace_root_snapshot(
        root.id,
        vec![
            discovered(
                "X:/sort/z/Alpha.txt",
                ItemKind::File,
                90,
                600,
                Priority::High,
            ),
            discovered("X:/sort/a/Zulu", ItemKind::Folder, 0, 100, Priority::Low),
            discovered(
                "X:/sort/y/Bravo.app",
                ItemKind::App,
                70,
                500,
                Priority::Normal,
            ),
            discovered(
                "X:/sort/b/Yankee.pkg",
                ItemKind::Package,
                20,
                200,
                Priority::Normal,
            ),
            discovered(
                "X:/sort/x/Charlie.mov",
                ItemKind::File,
                60,
                400,
                Priority::Low,
            ),
            discovered(
                "X:/sort/c/Xray.pdf",
                ItemKind::File,
                30,
                300,
                Priority::High,
            ),
        ],
    )
    .unwrap();

    for field in [
        FileSearchSortField::Relevance,
        FileSearchSortField::Name,
        FileSearchSortField::Path,
        FileSearchSortField::Kind,
        FileSearchSortField::Modified,
        FileSearchSortField::Size,
    ] {
        for direction in [SortDirection::Ascending, SortDirection::Descending] {
            let request = FileSearchRequest {
                root_ids: vec![root.id],
                limit: 6,
                sort_field: field,
                sort_direction: direction,
                now_unix_ms: NOW,
                ..FileSearchRequest::default()
            };
            let first = exact(&core, request.clone());
            let repeated = exact(
                &core,
                FileSearchRequest {
                    expected_catalog_generation: Some(first.catalog_generation),
                    ..request
                },
            );
            let ids: Vec<_> = first.results.iter().map(|result| result.item.id).collect();
            assert_eq!(ids.len(), 6, "field={field:?} direction={direction:?}");
            assert_eq!(
                ids,
                repeated
                    .results
                    .iter()
                    .map(|result| result.item.id)
                    .collect::<Vec<_>>(),
                "unstable field={field:?} direction={direction:?}"
            );

            for pair in first.results.windows(2) {
                let ordering = match field {
                    FileSearchSortField::Relevance => {
                        pair[0].score.combined.cmp(&pair[1].score.combined)
                    }
                    FileSearchSortField::Name => pair[0]
                        .item
                        .name
                        .to_lowercase()
                        .cmp(&pair[1].item.name.to_lowercase()),
                    FileSearchSortField::Path => pair[0]
                        .item
                        .path
                        .to_lowercase()
                        .cmp(&pair[1].item.path.to_lowercase()),
                    FileSearchSortField::Kind => {
                        kind_key(pair[0].item.kind).cmp(&kind_key(pair[1].item.kind))
                    }
                    FileSearchSortField::Modified => pair[0]
                        .item
                        .modified_unix_ms
                        .cmp(&pair[1].item.modified_unix_ms),
                    FileSearchSortField::Size => pair[0].item.size.cmp(&pair[1].item.size),
                };
                match direction {
                    SortDirection::Ascending => assert!(
                        ordering.is_le(),
                        "not ascending: field={field:?} pair={pair:?}"
                    ),
                    SortDirection::Descending => assert!(
                        ordering.is_ge(),
                        "not descending: field={field:?} pair={pair:?}"
                    ),
                }
            }

            for offset in [0u64, 2, 4] {
                let page = exact(
                    &core,
                    FileSearchRequest {
                        root_ids: vec![root.id],
                        offset,
                        limit: 2,
                        sort_field: field,
                        sort_direction: direction,
                        now_unix_ms: NOW,
                        expected_catalog_generation: Some(first.catalog_generation),
                        ..FileSearchRequest::default()
                    },
                );
                let start = offset as usize;
                let expected_ids = first.results[start..(start + 2).min(first.results.len())]
                    .iter()
                    .map(|result| result.item.id)
                    .collect::<Vec<_>>();
                assert_eq!(
                    page.results
                        .iter()
                        .map(|result| result.item.id)
                        .collect::<Vec<_>>(),
                    expected_ids,
                    "partial page mismatch field={field:?} direction={direction:?} offset={offset}"
                );
                assert_eq!(page.total_matches, first.total_matches);
            }
        }
    }
}

#[test]
fn partial_pages_match_full_order_for_tie_heavy_large_catalog() {
    const ITEM_COUNT: usize = 513;
    let state = TempDir::new().unwrap();
    let core = open_core(&state);
    let root = core
        .add_root(PathBuf::from("X:/tie-heavy"), RootKind::Local)
        .unwrap();
    core.replace_root_snapshot(
        root.id,
        (0..ITEM_COUNT)
            .map(|index| {
                discovered(
                    format!(
                        "X:/tie-heavy/group_{:03}/shared_{:02}.dat",
                        index,
                        index % 7
                    ),
                    match index % 4 {
                        0 => ItemKind::File,
                        1 => ItemKind::Folder,
                        2 => ItemKind::App,
                        _ => ItemKind::Package,
                    },
                    (index % 11) as u64,
                    (index % 13) as i64,
                    match index % 3 {
                        0 => Priority::High,
                        1 => Priority::Normal,
                        _ => Priority::Low,
                    },
                )
            })
            .collect(),
    )
    .unwrap();

    for field in [
        FileSearchSortField::Relevance,
        FileSearchSortField::Name,
        FileSearchSortField::Path,
        FileSearchSortField::Kind,
        FileSearchSortField::Modified,
        FileSearchSortField::Size,
    ] {
        for direction in [SortDirection::Ascending, SortDirection::Descending] {
            let full = exact(
                &core,
                FileSearchRequest {
                    query: "shared".into(),
                    root_ids: vec![root.id],
                    limit: ITEM_COUNT as u32,
                    sort_field: field,
                    sort_direction: direction,
                    now_unix_ms: NOW,
                    ..FileSearchRequest::default()
                },
            );
            assert_eq!(full.total_matches, ITEM_COUNT as u64);
            let full_ids = full
                .results
                .iter()
                .map(|result| result.item.id)
                .collect::<Vec<_>>();

            for offset in [0u64, 1, 17, 128, 500, ITEM_COUNT as u64, 700] {
                let page = exact(
                    &core,
                    FileSearchRequest {
                        query: "shared".into(),
                        root_ids: vec![root.id],
                        offset,
                        limit: 13,
                        sort_field: field,
                        sort_direction: direction,
                        now_unix_ms: NOW,
                        expected_catalog_generation: Some(full.catalog_generation),
                        ..FileSearchRequest::default()
                    },
                );
                let start = (offset as usize).min(ITEM_COUNT);
                let end = start.saturating_add(13).min(ITEM_COUNT);
                assert_eq!(
                    page.results
                        .iter()
                        .map(|result| result.item.id)
                        .collect::<Vec<_>>(),
                    full_ids[start..end],
                    "tie-heavy page mismatch field={field:?} direction={direction:?} offset={offset}"
                );
                assert_eq!(page.total_matches, full.total_matches);
                assert_eq!(page.has_more, end < ITEM_COUNT);
            }
        }
    }
}

fn kind_key(kind: ItemKind) -> u8 {
    match kind {
        ItemKind::File => 0,
        ItemKind::Folder => 1,
        ItemKind::App => 2,
        ItemKind::Package => 3,
    }
}

#[test]
fn exact_search_honors_saved_filters_query_facets_exclusions_priority_and_availability() {
    let state = TempDir::new().unwrap();
    let core = open_core(&state);
    let root = core
        .add_root(PathBuf::from("X:/semantics"), RootKind::Local)
        .unwrap();
    core.replace_root_snapshot(
        root.id,
        vec![
            discovered(
                "X:/semantics/kept/recent-report.pdf",
                ItemKind::File,
                100,
                NOW - 86_400_000,
                Priority::High,
            ),
            discovered(
                "X:/semantics/kept/old-report.pdf",
                ItemKind::File,
                100,
                NOW - 400 * 86_400_000,
                Priority::Normal,
            ),
            discovered(
                "X:/semantics/kept/clip.mov",
                ItemKind::File,
                100,
                NOW - 86_400_000,
                Priority::Normal,
            ),
            discovered(
                "X:/semantics/kept/excluded/secret.pdf",
                ItemKind::File,
                100,
                NOW - 86_400_000,
                Priority::Normal,
            ),
            discovered(
                "X:/semantics/kept/ignored.pdf",
                ItemKind::File,
                100,
                NOW - 86_400_000,
                Priority::Normal,
            ),
            discovered(
                "X:/semantics/outside/outside.pdf",
                ItemKind::File,
                100,
                NOW - 86_400_000,
                Priority::Normal,
            ),
        ],
    )
    .unwrap();

    let mut docs = FilterPreset::new("docs", "Documents");
    docs.target = FilterTarget::Files;
    docs.search_paths = vec!["X:/semantics/kept".into()];
    docs.extensions = vec!["pdf".into()];
    core.put_filter(docs).unwrap();
    core.exclude_recommendation_path("X:/semantics/kept/excluded".into())
        .unwrap();
    core.replace_priority_rules(vec![PriorityRule::path(
        "X:/semantics/kept/ignored.pdf",
        false,
        Priority::Ignored,
    )])
    .unwrap();

    let filtered = exact(
        &core,
        FileSearchRequest {
            query: "docs: size:>10b".into(),
            root_ids: vec![root.id],
            categories: vec![FileFacetCategory::Document],
            modified_time: Some(ModifiedTimeFacet::Last7Days),
            now_unix_ms: NOW,
            limit: 20,
            ..FileSearchRequest::default()
        },
    );
    assert_eq!(filtered.total_matches, 1);
    assert_eq!(filtered.results[0].item.name, "recent-report.pdf");
    assert_eq!(filtered.results[0].item.priority, Priority::High);
    assert!(!filtered.parsed.size_bytes.is_empty());

    let unavailable_root = core
        .add_root(PathBuf::from("X:/offline"), RootKind::Network)
        .unwrap();
    core.replace_root_snapshot(
        unavailable_root.id,
        vec![discovered(
            "X:/offline/offline-result.txt",
            ItemKind::File,
            1,
            NOW,
            Priority::Normal,
        )],
    )
    .unwrap();
    core.set_root_unavailable(unavailable_root.id, "network disconnected".into())
        .unwrap();

    let hidden_offline = exact(
        &core,
        FileSearchRequest {
            query: "offline-result".into(),
            include_unavailable: false,
            root_ids: vec![unavailable_root.id],
            limit: 10,
            now_unix_ms: NOW,
            ..FileSearchRequest::default()
        },
    );
    assert_eq!(hidden_offline.total_matches, 0);
    let shown_offline = exact(
        &core,
        FileSearchRequest {
            include_unavailable: true,
            ..FileSearchRequest {
                query: "offline-result".into(),
                include_unavailable: false,
                root_ids: vec![unavailable_root.id],
                limit: 10,
                now_unix_ms: NOW,
                ..FileSearchRequest::default()
            }
        },
    );
    assert_eq!(shown_offline.total_matches, 1);
    assert!(!shown_offline.results[0].item.available);
}

#[test]
fn exact_search_projects_live_facets_from_its_single_candidate_scan() {
    const DAY: i64 = 86_400_000;
    let state = TempDir::new().unwrap();
    let core = open_core(&state);
    let root = core
        .add_root(PathBuf::from("X:/live-facets"), RootKind::Local)
        .unwrap();
    core.replace_root_snapshot(
        root.id,
        vec![
            discovered(
                "X:/live-facets/needle folder",
                ItemKind::Folder,
                0,
                NOW,
                Priority::Normal,
            ),
            discovered(
                "X:/live-facets/needle today.pdf",
                ItemKind::File,
                20,
                NOW,
                Priority::Normal,
            ),
            discovered(
                "X:/live-facets/needle picture.jpg",
                ItemKind::File,
                30,
                NOW - 3 * DAY,
                Priority::Normal,
            ),
            discovered(
                "X:/live-facets/needle large.txt",
                ItemKind::File,
                101 * 1024 * 1024,
                NOW,
                Priority::Normal,
            ),
            discovered(
                "X:/live-facets/needle archive.7z",
                ItemKind::File,
                40,
                NOW - 2 * DAY,
                Priority::Normal,
            ),
            discovered(
                "X:/live-facets/needle old.pdf",
                ItemKind::File,
                50,
                NOW - 400 * DAY,
                Priority::Normal,
            ),
            discovered(
                "X:/live-facets/needle app.exe",
                ItemKind::App,
                60,
                NOW,
                Priority::Normal,
            ),
        ],
    )
    .unwrap();

    let request = FileSearchRequest {
        query: "needle".into(),
        root_ids: vec![root.id],
        categories: vec![FileFacetCategory::Document, FileFacetCategory::Picture],
        modified_time: Some(ModifiedTimeFacet::Last7Days),
        now_unix_ms: NOW,
        limit: 20,
        include_facets: true,
        ..FileSearchRequest::default()
    };
    let projected = exact(&core, request.clone());
    let baseline = exact(
        &core,
        FileSearchRequest {
            include_facets: false,
            ..request.clone()
        },
    );
    assert_eq!(projected.total_matches, 3);
    assert_eq!(
        projected.considered_candidates,
        baseline.considered_candidates
    );
    assert_eq!(
        projected
            .results
            .iter()
            .map(|result| result.item.id)
            .collect::<Vec<_>>(),
        baseline
            .results
            .iter()
            .map(|result| result.item.id)
            .collect::<Vec<_>>()
    );

    let facets = projected
        .facets
        .as_ref()
        .expect("facet projection requested");
    assert_eq!(facets.matched_total, projected.total_matches);
    assert_eq!(facets.category_scope_total, 6);
    assert_eq!(facets.modified_scope_total, 4);
    assert_eq!(facets.quick_filters.folder, 0);
    assert_eq!(facets.quick_filters.file, 3);
    assert_eq!(facets.quick_filters.pdf, 1);
    assert_eq!(facets.quick_filters.today, 2);
    assert_eq!(facets.quick_filters.week, 3);
    assert_eq!(facets.quick_filters.large, 1);

    let standalone = core
        .facet_counts(
            &FacetCountsRequest {
                query: request.query.clone(),
                include_unavailable: request.include_unavailable,
                root_ids: request.root_ids.clone(),
                categories: request.categories.clone(),
                modified_time: request.modified_time,
                now_unix_ms: request.now_unix_ms,
                timezone_offset_minutes: request.timezone_offset_minutes,
            },
            &CancellationToken::new(),
        )
        .unwrap();
    assert_eq!(facets.matched_total, standalone.matched_total);
    assert_eq!(facets.categories, standalone.categories);
    assert_eq!(facets.modified_time, standalone.modified_time);
    assert_eq!(
        projected.considered_candidates,
        standalone.considered_candidates
    );
}

#[test]
fn exact_search_validates_page_limits_and_honors_pre_cancelled_tokens() {
    let state = TempDir::new().unwrap();
    let core = open_core(&state);
    for limit in [0, MAX_FILE_SEARCH_PAGE_LIMIT + 1] {
        let error = core
            .file_search(
                &FileSearchRequest {
                    limit,
                    ..FileSearchRequest::default()
                },
                &CancellationToken::new(),
            )
            .unwrap_err();
        assert!(matches!(error, CoreError::InvalidRequest(_)));
    }

    let token = CancellationToken::new();
    token.cancel();
    let error = core
        .file_search(&FileSearchRequest::default(), &token)
        .unwrap_err();
    assert!(matches!(error, CoreError::Cancelled));
}
