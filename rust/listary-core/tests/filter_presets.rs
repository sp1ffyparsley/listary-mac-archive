use listary_core::{
    CancellationToken, Core, CoreConfig, FilterPreset, FilterTarget, ItemKind, RootKind,
    SearchRequest,
};
use std::fs;
use tempfile::TempDir;

fn search(core: &Core, query: &str) -> listary_core::SearchResponse {
    core.search(
        &SearchRequest {
            query: query.into(),
            limit: 50,
            candidate_limit: 256,
            now_unix_ms: 1_800_000_000_000,
            ..SearchRequest::default()
        },
        &CancellationToken::new(),
    )
    .unwrap()
}

#[test]
fn saved_filters_scope_real_search_and_survive_reopen_and_rebuild() {
    let state = TempDir::new().unwrap();
    let files = TempDir::new().unwrap();
    let project_a = files.path().join("Project A");
    let project_b = files.path().join("Project B");
    fs::create_dir_all(project_a.join("Nested Folder")).unwrap();
    fs::create_dir_all(&project_b).unwrap();
    fs::write(project_a.join("invoice.xlsx"), b"a").unwrap();
    fs::write(project_a.join("archive.tar.gz"), b"archive").unwrap();
    fs::write(project_a.join("clip.mov"), b"clip").unwrap();
    fs::write(project_b.join("invoice.xlsx"), b"b").unwrap();

    let core = Core::open(CoreConfig::new(state.path().to_path_buf())).unwrap();
    let root = core
        .add_root(files.path().to_path_buf(), RootKind::Local)
        .unwrap();
    core.reconcile_root(root.id, &CancellationToken::new())
        .unwrap();

    let mut invoices = FilterPreset::new("invoices", "Project A invoices");
    invoices.extra_query = "invoice".into();
    invoices.search_paths = vec![project_a.to_string_lossy().into_owned()];
    invoices.target = FilterTarget::Files;
    invoices.extensions = vec![".XLSX".into()];
    let invoices = core.put_filter(invoices).unwrap();
    assert_eq!(invoices.extensions, vec!["xlsx"]);

    let scoped = search(&core, "INVOICES:");
    assert_eq!(scoped.results.len(), 1);
    assert_eq!(scoped.results[0].item.name, "invoice.xlsx");
    assert!(scoped.results[0].item.path.contains("Project A"));
    assert_eq!(scoped.parsed.positive_terms[0].text, "invoice");

    let mut archives = FilterPreset::new("archives", "Compound archives");
    archives.search_paths = vec![project_a.to_string_lossy().into_owned()];
    archives.target = FilterTarget::Files;
    archives.extensions = vec!["tar.gz".into()];
    core.put_filter(archives).unwrap();
    let compound = search(&core, "archives:");
    assert_eq!(compound.results.len(), 1);
    assert_eq!(compound.results[0].item.name, "archive.tar.gz");

    let folders = search(&core, "folder: Project");
    assert!(!folders.results.is_empty());
    assert!(
        folders
            .results
            .iter()
            .all(|result| result.item.kind == ItemKind::Folder)
    );
    let files_only = search(&core, "file: invoice");
    assert_eq!(files_only.results.len(), 2);
    assert!(
        files_only
            .results
            .iter()
            .all(|result| result.item.kind == ItemKind::File)
    );

    drop(core);
    let reopened = Core::open(CoreConfig::new(state.path().to_path_buf())).unwrap();
    assert_eq!(reopened.list_filters().len(), 2);
    assert_eq!(search(&reopened, "invoices:").results.len(), 1);

    reopened.rebuild_all(&CancellationToken::new()).unwrap();
    assert_eq!(reopened.list_filters().len(), 2);
    assert_eq!(search(&reopened, "invoices:").results.len(), 1);

    reopened.remove_filter(invoices.id).unwrap();
    assert_eq!(reopened.list_filters().len(), 1);
    let literal_after_remove = search(&reopened, "invoices:");
    assert!(literal_after_remove.results.is_empty());
}

#[test]
fn saved_filter_validation_rejects_keyword_collisions_and_invalid_folder_extensions() {
    let state = TempDir::new().unwrap();
    let core = Core::open(CoreConfig::new(state.path().to_path_buf())).unwrap();

    core.put_filter(FilterPreset::new("work", "Work")).unwrap();
    let duplicate = core.put_filter(FilterPreset::new("WORK", "Duplicate"));
    assert!(duplicate.is_err());

    let mut invalid = FilterPreset::new("folders", "Folders");
    invalid.target = FilterTarget::Folders;
    invalid.extensions = vec!["pdf".into()];
    assert!(core.put_filter(invalid).is_err());
}
