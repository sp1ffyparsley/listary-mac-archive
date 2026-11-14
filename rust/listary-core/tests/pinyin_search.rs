use listary_core::{
    CancellationToken, Core, CoreConfig, DiscoveredItem, ItemKind, RootKind, SearchRequest,
    StartupCacheSource,
};
use std::{
    fs,
    path::{Path, PathBuf},
};
use tempfile::TempDir;

fn open_core(state: &Path) -> Core {
    Core::open(CoreConfig::new(state.to_path_buf())).expect("core opens")
}

fn search(core: &Core, query: &str) -> listary_core::SearchResponse {
    core.search(
        &SearchRequest {
            query: query.into(),
            limit: 50,
            ..SearchRequest::default()
        },
        &CancellationToken::new(),
    )
    .expect("search succeeds")
}

#[test]
fn quanpin_initials_mixed_and_fuzzy_pinyin_search_share_normal_filters() {
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from("X:/pinyin"), RootKind::Local)
        .unwrap();
    core.replace_root_snapshot(
        root.id,
        vec![
            DiscoveredItem::new(PathBuf::from("X:/pinyin/规划.txt"), ItemKind::File, 10, 100),
            DiscoveredItem::new(
                PathBuf::from("X:/pinyin/百度网盘.txt"),
                ItemKind::File,
                10,
                100,
            ),
            DiscoveredItem::new(
                PathBuf::from("X:/pinyin/公司手册.txt"),
                ItemKind::File,
                10,
                100,
            ),
            DiscoveredItem::new(
                PathBuf::from("X:/pinyin/项目 Report.mov"),
                ItemKind::File,
                10,
                100,
            ),
        ],
    )
    .unwrap();

    assert_eq!(search(&core, "guihua").results[0].item.name, "规划.txt");
    assert_eq!(search(&core, "gh").results[0].item.name, "规划.txt");
    assert_eq!(search(&core, "dupan").results[0].item.name, "百度网盘.txt");
    assert_eq!(
        search(&core, "gsshouce").results[0].item.name,
        "公司手册.txt"
    );
    assert_eq!(
        search(&core, "xiangmu report ext:mov").results[0].item.name,
        "项目 Report.mov"
    );
}

#[test]
fn literal_text_outranks_transliteration_and_exact_quotes_remain_literal() {
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from("X:/pinyin-ranking"), RootKind::Local)
        .unwrap();
    core.replace_root_snapshot(
        root.id,
        vec![
            DiscoveredItem::new(
                PathBuf::from("X:/pinyin-ranking/公司手册.txt"),
                ItemKind::File,
                10,
                100,
            ),
            DiscoveredItem::new(
                PathBuf::from("X:/pinyin-ranking/gsshouce"),
                ItemKind::File,
                10,
                100,
            ),
        ],
    )
    .unwrap();

    let unquoted = search(&core, "gsshouce");
    assert_eq!(unquoted.results[0].item.name, "gsshouce");
    assert!(
        unquoted
            .results
            .iter()
            .any(|result| result.item.name == "公司手册.txt")
    );
    assert!(
        unquoted.results[0].score.text_tier
            < unquoted
                .results
                .iter()
                .find(|result| result.item.name == "公司手册.txt")
                .unwrap()
                .score
                .text_tier
    );

    let quoted = search(&core, r#""gsshouce""#);
    assert_eq!(quoted.results.len(), 1);
    assert_eq!(quoted.results[0].item.name, "gsshouce");
}

#[test]
fn pinyin_path_search_and_postings_survive_mutation_and_snapshot_reopen() {
    let state = TempDir::new().unwrap();
    let root_id;
    {
        let core = open_core(state.path());
        let root = core
            .add_root(PathBuf::from("X:/中文项目"), RootKind::Local)
            .unwrap();
        root_id = root.id;
        core.replace_root_snapshot(
            root.id,
            vec![
                DiscoveredItem::new(
                    PathBuf::from("X:/中文项目/旧名字.txt"),
                    ItemKind::File,
                    10,
                    100,
                ),
                DiscoveredItem::new(
                    PathBuf::from("X:/中文项目/clip.mov"),
                    ItemKind::File,
                    10,
                    100,
                ),
            ],
        )
        .unwrap();
        assert_eq!(
            search(&core, "zhongwenxiangmu/ clip").results[0].item.name,
            "clip.mov"
        );
        assert_eq!(
            search(&core, "jiumingzi").results[0].item.name,
            "旧名字.txt"
        );

        core.replace_root_snapshot(
            root.id,
            vec![
                DiscoveredItem::new(
                    PathBuf::from("X:/中文项目/新名字.txt"),
                    ItemKind::File,
                    10,
                    200,
                ),
                DiscoveredItem::new(
                    PathBuf::from("X:/中文项目/clip.mov"),
                    ItemKind::File,
                    10,
                    100,
                ),
            ],
        )
        .unwrap();
        assert!(search(&core, "jiumingzi").results.is_empty());
        assert_eq!(
            search(&core, "xinmingzi").results[0].item.name,
            "新名字.txt"
        );
        core.flush().unwrap();
    }

    {
        let (reopened, metrics) =
            Core::open_profiled(CoreConfig::new(state.path().to_path_buf())).unwrap();
        assert_eq!(metrics.cache_source, Some(StartupCacheSource::Snapshot));
        assert!(reopened.list_roots().iter().any(|root| root.id == root_id));
        assert_eq!(
            search(&reopened, "zhongwenxiangmu/ xinmingzi").results[0]
                .item
                .name,
            "新名字.txt"
        );
        assert!(search(&reopened, "jiumingzi").results.is_empty());
    }

    let snapshot = state.path().join("cache/search-cache.bin");
    let mut old_schema = fs::read(&snapshot).unwrap();
    old_schema[8..12].copy_from_slice(&4u32.to_le_bytes());
    fs::write(&snapshot, old_schema).unwrap();
    let (rebuilt, metrics) =
        Core::open_profiled(CoreConfig::new(state.path().to_path_buf())).unwrap();
    assert_eq!(metrics.cache_source, Some(StartupCacheSource::Invalid));
    assert_eq!(
        search(&rebuilt, "zhongwenxiangmu/ xinmingzi").results[0]
            .item
            .name,
        "新名字.txt"
    );
}

#[test]
fn mixed_pinyin_target_is_not_lost_beyond_default_candidate_budget() {
    const DECOYS: usize = 5_000;
    let state = TempDir::new().unwrap();
    let core = open_core(state.path());
    let root = core
        .add_root(PathBuf::from("X:/pinyin-candidate-gate"), RootKind::Local)
        .unwrap();
    let mut items = (0..DECOYS)
        .map(|index| {
            DiscoveredItem::new(
                PathBuf::from(format!(
                    "X:/pinyin-candidate-gate/g_s_s_h_o_u_c_e_{index:05}.txt"
                )),
                ItemKind::File,
                10,
                100,
            )
        })
        .collect::<Vec<_>>();
    items.push(DiscoveredItem::new(
        PathBuf::from("X:/pinyin-candidate-gate/公司手册.txt"),
        ItemKind::File,
        10,
        100,
    ));
    core.replace_root_snapshot(root.id, items).unwrap();

    let response = core
        .search(
            &SearchRequest {
                query: "gsshouce".into(),
                limit: 10,
                candidate_limit: 32,
                ..SearchRequest::default()
            },
            &CancellationToken::new(),
        )
        .unwrap();
    assert_eq!(response.considered_candidates, 32);
    assert_eq!(response.results[0].item.name, "公司手册.txt");
    assert!(
        response
            .results
            .iter()
            .any(|result| result.item.name == "公司手册.txt")
    );
}
