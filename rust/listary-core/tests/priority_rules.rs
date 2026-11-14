use listary_core::{
    CancellationToken, Core, CoreConfig, DiscoveredItem, ItemKind, Priority, PriorityRule,
    PriorityRuleTarget, RootKind, SearchRequest,
};
use std::path::{Path, PathBuf};
use tempfile::TempDir;

fn open_core(state: &Path) -> Core {
    Core::open(CoreConfig::new(state.to_path_buf())).expect("core opens")
}

fn search(core: &Core, query: &str, limit: usize) -> listary_core::SearchResponse {
    core.search(
        &SearchRequest {
            query: query.to_string(),
            limit,
            ..SearchRequest::default()
        },
        &CancellationToken::new(),
    )
    .expect("search succeeds")
}

#[test]
fn nested_path_rules_use_most_specific_match_and_exact_path_can_rescue_ignored_item() {
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from("X:/rules"), RootKind::Local)
        .unwrap();
    core.replace_root_snapshot(
        root.id,
        vec![
            DiscoveredItem::new(
                PathBuf::from("X:/rules/project/active/scene.txt"),
                ItemKind::File,
                1,
                1,
            ),
            DiscoveredItem::new(
                PathBuf::from("X:/rules/project/archive/drop.txt"),
                ItemKind::File,
                1,
                1,
            ),
            DiscoveredItem::new(
                PathBuf::from("X:/rules/project/archive/keep.txt"),
                ItemKind::File,
                1,
                1,
            ),
        ],
    )
    .unwrap();

    core.replace_priority_rules(vec![
        PriorityRule::regex(r"/project/.+\.txt$", Priority::Low),
        PriorityRule::path("X:/rules/project", true, Priority::High),
        PriorityRule::path("X:/rules/project/archive", true, Priority::Ignored),
        PriorityRule::path("X:/rules/project/archive/keep.txt", false, Priority::Normal),
    ])
    .unwrap();

    let active = search(&core, "scene", 10);
    assert_eq!(active.results.len(), 1);
    assert_eq!(active.results[0].item.priority, Priority::High);

    assert!(search(&core, "drop", 10).results.is_empty());
    let rescued = search(&core, "keep", 10);
    assert_eq!(rescued.results.len(), 1);
    assert_eq!(rescued.results[0].item.priority, Priority::Normal);
}

#[test]
fn regex_rules_are_ordered_deterministically_and_respect_file_folder_target() {
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from("X:/regex"), RootKind::Local)
        .unwrap();
    core.replace_root_snapshot(
        root.id,
        vec![
            DiscoveredItem::new(
                PathBuf::from("X:/regex/build/output.tmp"),
                ItemKind::File,
                1,
                1,
            ),
            DiscoveredItem::new(
                PathBuf::from("X:/regex/build/folder.tmp"),
                ItemKind::Folder,
                0,
                1,
            ),
        ],
    )
    .unwrap();

    let mut first = PriorityRule::regex(r"/build/", Priority::Low);
    first.target = PriorityRuleTarget::Files;
    let second = PriorityRule::regex(r"/build/", Priority::High);
    core.replace_priority_rules(vec![first, second]).unwrap();

    let file = search(&core, "output", 10);
    assert_eq!(file.results[0].item.priority, Priority::Low);
    let folder = search(&core, "folder", 10);
    assert_eq!(folder.results[0].item.priority, Priority::High);
}

#[test]
fn persisted_rules_survive_reopen_without_reindexing_catalog() {
    let state = TempDir::new().unwrap();
    let rule_id;
    {
        let core = open_core(state.path());
        let root = core
            .add_root(PathBuf::from("X:/persist"), RootKind::Local)
            .unwrap();
        core.replace_root_snapshot(
            root.id,
            vec![DiscoveredItem::new(
                PathBuf::from("X:/persist/nested/result.txt"),
                ItemKind::File,
                1,
                1,
            )],
        )
        .unwrap();
        let rule = PriorityRule::path("X:/persist/nested", true, Priority::High);
        rule_id = rule.id;
        core.replace_priority_rules(vec![rule]).unwrap();
        core.flush().unwrap();
    }

    let reopened = open_core(state.path());
    assert_eq!(reopened.list_priority_rules()[0].id, rule_id);
    let result = search(&reopened, "result", 10);
    assert_eq!(result.results[0].item.priority, Priority::High);
}

#[test]
fn high_priority_rule_admits_late_match_before_candidate_budget_cutoff() {
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from(r"X:\budget"), RootKind::Local)
        .unwrap();
    let mut items = Vec::with_capacity(20_000);
    for i in 0..20_000 {
        let folder = if i == 19_999 { "z-high" } else { "a-normal" };
        items.push(DiscoveredItem::new(
            PathBuf::from(format!(r"X:\budget\{folder}\scene_{i:05}.txt")),
            ItemKind::File,
            1,
            1,
        ));
    }
    core.replace_root_snapshot(root.id, items).unwrap();
    core.replace_priority_rules(vec![PriorityRule::path(
        "X:/budget/z-high",
        true,
        Priority::High,
    )])
    .unwrap();

    let response = core
        .search(
            &SearchRequest {
                query: "scene".into(),
                limit: 1,
                candidate_limit: 64,
                ..SearchRequest::default()
            },
            &CancellationToken::new(),
        )
        .unwrap();
    assert_eq!(response.results[0].item.name, "scene_19999.txt");
    assert_eq!(response.results[0].item.priority, Priority::High);
    assert!(response.considered_candidates <= 64);
}

#[test]
fn derived_priority_bitmap_tracks_ingest_upsert_remove_and_reopen() {
    let state = TempDir::new().unwrap();
    let root_id;
    {
        let core = open_core(state.path());
        let root = core
            .add_root(PathBuf::from(r"X:\mutations"), RootKind::Local)
            .unwrap();
        root_id = root.id;
        core.replace_priority_rules(vec![PriorityRule::regex(r"/hot/.+\.txt$", Priority::High)])
            .unwrap();

        core.replace_root_snapshot(
            root.id,
            vec![DiscoveredItem::new(
                PathBuf::from(r"X:\mutations\hot\first.txt"),
                ItemKind::File,
                1,
                1,
            )],
        )
        .unwrap();
        assert_eq!(
            search(&core, "first", 10).results[0].item.priority,
            Priority::High
        );

        core.upsert_items(
            root.id,
            vec![DiscoveredItem::new(
                PathBuf::from(r"X:\mutations\hot\second.txt"),
                ItemKind::File,
                1,
                2,
            )],
        )
        .unwrap();
        assert_eq!(
            search(&core, "second", 10).results[0].item.priority,
            Priority::High
        );

        core.replace_root_snapshot(
            root.id,
            vec![DiscoveredItem::new(
                PathBuf::from(r"X:\mutations\cold\replacement.txt"),
                ItemKind::File,
                1,
                3,
            )],
        )
        .unwrap();
        assert!(search(&core, "first", 10).results.is_empty());
        assert!(search(&core, "second", 10).results.is_empty());
        assert_eq!(
            search(&core, "replacement", 10).results[0].item.priority,
            Priority::Normal
        );
        core.flush().unwrap();
    }

    let reopened = open_core(state.path());
    assert_eq!(reopened.list_roots()[0].id, root_id);
    assert_eq!(
        search(&reopened, "replacement", 10).results[0]
            .item
            .priority,
        Priority::Normal
    );
    reopened
        .upsert_items(
            root_id,
            vec![DiscoveredItem::new(
                PathBuf::from(r"X:\mutations\hot\after-reopen.txt"),
                ItemKind::File,
                1,
                4,
            )],
        )
        .unwrap();
    assert_eq!(
        search(&reopened, "after-reopen", 10).results[0]
            .item
            .priority,
        Priority::High
    );
}

#[test]
fn ignored_rule_candidates_do_not_starve_allowed_match_at_tiny_budget() {
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from(r"X:\ignored-budget"), RootKind::Local)
        .unwrap();
    let mut items = Vec::with_capacity(20_000);
    for i in 0..20_000 {
        let folder = if i == 19_999 {
            "z-allowed"
        } else {
            "a-ignored"
        };
        items.push(DiscoveredItem::new(
            PathBuf::from(format!(r"X:\ignored-budget\{folder}\target_{i:05}.txt")),
            ItemKind::File,
            1,
            1,
        ));
    }
    core.replace_root_snapshot(root.id, items).unwrap();
    core.replace_priority_rules(vec![PriorityRule::path(
        "X:/ignored-budget/a-ignored",
        true,
        Priority::Ignored,
    )])
    .unwrap();

    let response = core
        .search(
            &SearchRequest {
                query: "target".into(),
                limit: 1,
                candidate_limit: 64,
                ..SearchRequest::default()
            },
            &CancellationToken::new(),
        )
        .unwrap();
    assert_eq!(response.results.len(), 1);
    assert_eq!(response.results[0].item.name, "target_19999.txt");
    assert_eq!(response.results[0].item.priority, Priority::Normal);
    assert!(response.considered_candidates <= 64);
}

#[test]
fn broad_high_rule_preserves_space_for_stronger_normal_text_match() {
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from(r"X:\high-lane"), RootKind::Local)
        .unwrap();
    let mut items = Vec::with_capacity(257);
    for i in 0..256 {
        items.push(DiscoveredItem::new(
            PathBuf::from(format!(r"X:\high-lane\hot\a_noise_{i:03}_b_middle_c.txt")),
            ItemKind::File,
            1,
            1,
        ));
    }
    items.push(DiscoveredItem::new(
        PathBuf::from(r"X:\high-lane\normal\abc_best_match.txt"),
        ItemKind::File,
        1,
        1,
    ));
    core.replace_root_snapshot(root.id, items).unwrap();
    core.replace_priority_rules(vec![PriorityRule::path(
        "X:/high-lane/hot",
        true,
        Priority::High,
    )])
    .unwrap();

    let response = core
        .search(
            &SearchRequest {
                query: "abc".into(),
                limit: 1,
                candidate_limit: 8,
                ..SearchRequest::default()
            },
            &CancellationToken::new(),
        )
        .unwrap();
    assert_eq!(response.considered_candidates, 8);
    assert_eq!(response.results[0].item.name, "abc_best_match.txt");
    assert_eq!(response.results[0].item.priority, Priority::Normal);
}

#[test]
fn base_high_priority_uses_the_bounded_late_admission_lane() {
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from(r"X:\base-high"), RootKind::Local)
        .unwrap();
    let mut items = (0..256)
        .map(|i| {
            DiscoveredItem::new(
                PathBuf::from(format!(r"X:\base-high\scene_{i:03}.txt")),
                ItemKind::File,
                1,
                1,
            )
        })
        .collect::<Vec<_>>();
    let mut late_high = DiscoveredItem::new(
        PathBuf::from(r"X:\base-high\scene_999.txt"),
        ItemKind::File,
        1,
        1,
    );
    late_high.priority = Priority::High;
    items.push(late_high);
    core.replace_root_snapshot(root.id, items).unwrap();

    let response = core
        .search(
            &SearchRequest {
                query: "scene".into(),
                limit: 1,
                candidate_limit: 8,
                ..SearchRequest::default()
            },
            &CancellationToken::new(),
        )
        .unwrap();
    assert_eq!(response.considered_candidates, 8);
    assert_eq!(response.results[0].item.name, "scene_999.txt");
    assert_eq!(response.results[0].item.priority, Priority::High);
}

#[test]
fn base_ignored_candidates_are_removed_before_the_budget_is_spent() {
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from(r"X:\base-ignored"), RootKind::Local)
        .unwrap();
    let mut items = Vec::with_capacity(257);
    for i in 0..256 {
        let mut ignored = DiscoveredItem::new(
            PathBuf::from(format!(r"X:\base-ignored\target_{i:03}.txt")),
            ItemKind::File,
            1,
            1,
        );
        ignored.priority = Priority::Ignored;
        items.push(ignored);
    }
    items.push(DiscoveredItem::new(
        PathBuf::from(r"X:\base-ignored\target_allowed.txt"),
        ItemKind::File,
        1,
        1,
    ));
    core.replace_root_snapshot(root.id, items).unwrap();

    let response = core
        .search(
            &SearchRequest {
                query: "target".into(),
                limit: 1,
                candidate_limit: 8,
                ..SearchRequest::default()
            },
            &CancellationToken::new(),
        )
        .unwrap();
    assert_eq!(response.considered_candidates, 1);
    assert_eq!(response.results[0].item.name, "target_allowed.txt");
    assert_eq!(response.results[0].item.priority, Priority::Normal);
}

#[test]
fn short_true_prefix_is_strong_even_when_late_fuzzy_matches_fill_the_catalog() {
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from(r"X:\short-prefix"), RootKind::Local)
        .unwrap();
    let mut items = (0..256)
        .map(|i| {
            DiscoveredItem::new(
                PathBuf::from(format!(r"X:\short-prefix\a_noise_{i:03}_b.txt")),
                ItemKind::File,
                1,
                1,
            )
        })
        .collect::<Vec<_>>();
    items.push(DiscoveredItem::new(
        PathBuf::from(r"X:\short-prefix\absolutely_the_best.txt"),
        ItemKind::File,
        1,
        1,
    ));
    core.replace_root_snapshot(root.id, items).unwrap();

    let response = core
        .search(
            &SearchRequest {
                query: "ab".into(),
                limit: 1,
                candidate_limit: 8,
                ..SearchRequest::default()
            },
            &CancellationToken::new(),
        )
        .unwrap();
    assert_eq!(response.considered_candidates, 8);
    assert_eq!(response.results[0].item.name, "absolutely_the_best.txt");
}
