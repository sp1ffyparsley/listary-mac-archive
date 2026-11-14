use crate::{
    cancel::CancellationToken,
    error::{CoreError, Result},
    model::{DiscoveredItem, ItemKind, RootRecord},
};
use chrono::Utc;
use std::{
    fs,
    path::{Path, PathBuf},
};
use walkdir::{DirEntry, WalkDir};

#[derive(Debug, Clone)]
pub struct CrawlOptions {
    pub follow_links: bool,
    pub max_depth: Option<usize>,
    pub exclude_hidden_metadata_dirs: bool,
}

impl Default for CrawlOptions {
    fn default() -> Self {
        Self {
            follow_links: false,
            max_depth: None,
            exclude_hidden_metadata_dirs: true,
        }
    }
}

#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
pub struct CrawlReport {
    pub discovered: usize,
    pub skipped: usize,
    pub errors: usize,
    #[serde(skip)]
    failed_paths: Vec<PathBuf>,
}

impl CrawlReport {
    pub(crate) fn failed_paths(&self) -> &[PathBuf] {
        &self.failed_paths
    }

    pub(crate) fn merge(&mut self, other: Self) {
        self.discovered = self.discovered.saturating_add(other.discovered);
        self.skipped = self.skipped.saturating_add(other.skipped);
        self.errors = self.errors.saturating_add(other.errors);
        for path in other.failed_paths {
            self.record_preservation_prefix(path);
        }
    }

    fn record_localized_error(&mut self, path: PathBuf) {
        self.errors = self.errors.saturating_add(1);
        self.record_preservation_prefix(path);
    }

    fn record_preservation_prefix(&mut self, path: PathBuf) {
        if self
            .failed_paths
            .iter()
            .any(|existing| path.starts_with(existing))
        {
            return;
        }
        self.failed_paths
            .retain(|existing| !existing.starts_with(&path));
        self.failed_paths.push(path);
    }
}

pub fn crawl_root(
    root: &RootRecord,
    token: &CancellationToken,
    options: &CrawlOptions,
) -> Result<(Vec<DiscoveredItem>, CrawlReport)> {
    fs::metadata(&root.path).map_err(|source| CoreError::Filesystem {
        path: root.path.clone(),
        source,
    })?;
    let mut builder = WalkDir::new(&root.path)
        .follow_links(options.follow_links)
        .follow_root_links(true);
    if let Some(depth) = options.max_depth {
        builder = builder.max_depth(depth);
    }

    let mut report = CrawlReport::default();
    let mut items = Vec::new();
    let iter = builder
        .into_iter()
        .filter_entry(|entry| should_descend(entry, options));

    for entry in iter {
        token.check()?;
        let entry = match entry {
            Ok(v) => v,
            Err(error) => {
                let Some(path) = error.path().map(Path::to_path_buf) else {
                    return Err(walk_error(error, &root.path));
                };
                report.record_localized_error(path);
                continue;
            }
        };
        if entry.path() == root.path
            && !(entry.file_type().is_dir() && has_app_extension(entry.path()))
        {
            continue;
        }
        let metadata = match entry.metadata() {
            Ok(value) => value,
            Err(_) => {
                report.record_localized_error(entry.path().to_path_buf());
                continue;
            }
        };
        let kind = classify_path(entry.path(), &metadata);
        let modified_unix_ms = metadata
            .modified()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_millis().min(i64::MAX as u128) as i64)
            .unwrap_or_else(|| Utc::now().timestamp_millis());
        let size = if metadata.is_file() {
            metadata.len()
        } else {
            0
        };
        let mut item =
            DiscoveredItem::new(entry.path().to_path_buf(), kind, size, modified_unix_ms);
        item.priority = root.priority;
        item.stable_file_id = stable_file_id(entry.path(), &metadata);
        item.preview_hint = preview_hint(entry.path(), kind);
        items.push(item);
        report.discovered += 1;
    }
    Ok((items, report))
}

pub fn crawl_changed_path(
    root: &RootRecord,
    path: &Path,
    token: &CancellationToken,
    options: &CrawlOptions,
) -> Result<(Vec<DiscoveredItem>, CrawlReport)> {
    token.check()?;
    if !path.starts_with(&root.path) {
        return Err(CoreError::InvalidRequest(format!(
            "changed path is outside root: {}",
            path.display()
        )));
    }
    // The core also promotes the removal scope before calling us, so a dirty
    // private file repairs previously indexed bundle descendants atomically.
    // Keep direct crawler callers on the same outermost, root-local boundary.
    let path = outermost_app_bundle(&root.path, path).unwrap_or(path);
    let metadata_result = if path == root.path {
        // A configured root may itself be a symlink. Full-root crawling
        // intentionally follows that root boundary, so keep dirty-root scans
        // aligned with the same contract.
        fs::metadata(path)
    } else {
        // Nested links are indexed as occurrences. Do not follow a dirty
        // symlink/junction into its target or make a broken target look deleted.
        fs::symlink_metadata(path)
    };
    let metadata = match metadata_result {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok((Vec::new(), CrawlReport::default()));
        }
        Err(source) => {
            return Err(CoreError::Filesystem {
                path: path.to_path_buf(),
                source,
            });
        }
    };
    if metadata.is_file() || metadata.file_type().is_symlink() {
        let modified_unix_ms = metadata
            .modified()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_millis().min(i64::MAX as u128) as i64)
            .unwrap_or_else(|| Utc::now().timestamp_millis());
        let mut item = DiscoveredItem::new(
            path.to_path_buf(),
            classify_path(path, &metadata),
            metadata.len(),
            modified_unix_ms,
        );
        item.priority = root.priority;
        item.stable_file_id = stable_file_id(path, &metadata);
        item.preview_hint = preview_hint(path, item.kind);
        return Ok((
            vec![item],
            CrawlReport {
                discovered: 1,
                ..Default::default()
            },
        ));
    }

    let mut report = CrawlReport::default();
    let mut items = Vec::new();
    let mut builder = WalkDir::new(path)
        .follow_links(options.follow_links)
        .follow_root_links(true);
    if let Some(depth) = options.max_depth {
        builder = builder.max_depth(depth);
    }
    for entry in builder
        .into_iter()
        .filter_entry(|entry| should_descend(entry, options))
    {
        token.check()?;
        let entry = match entry {
            Ok(value) => value,
            Err(error) => {
                let Some(error_path) = error.path().map(Path::to_path_buf) else {
                    return Err(walk_error(error, path));
                };
                report.record_localized_error(error_path);
                continue;
            }
        };
        let metadata = match entry.metadata() {
            Ok(value) => value,
            Err(_) => {
                report.record_localized_error(entry.path().to_path_buf());
                continue;
            }
        };
        let kind = classify_path(entry.path(), &metadata);
        let modified_unix_ms = metadata
            .modified()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_millis().min(i64::MAX as u128) as i64)
            .unwrap_or_else(|| Utc::now().timestamp_millis());
        let size = if metadata.is_file() {
            metadata.len()
        } else {
            0
        };
        let mut item =
            DiscoveredItem::new(entry.path().to_path_buf(), kind, size, modified_unix_ms);
        item.priority = root.priority;
        item.stable_file_id = stable_file_id(entry.path(), &metadata);
        item.preview_hint = preview_hint(entry.path(), kind);
        items.push(item);
        report.discovered += 1;
    }
    Ok((items, report))
}

pub(crate) fn outermost_app_bundle<'a>(root: &Path, path: &'a Path) -> Option<&'a Path> {
    // Do not stat here: deleted/renamed bundle events still have to remove the
    // old bundle row and any leaked descendants. A bundle above an explicitly
    // configured inner root must not widen that root's reconciliation scope.
    path.ancestors()
        .take_while(|ancestor| ancestor.starts_with(root))
        .filter(|ancestor| has_app_extension(ancestor))
        .last()
}

fn has_app_extension(path: &Path) -> bool {
    path.extension()
        .is_some_and(|extension| extension.eq_ignore_ascii_case("app"))
}

fn should_descend(entry: &DirEntry, options: &CrawlOptions) -> bool {
    // filter_entry controls both inclusion and descent. Keep the app itself,
    // but exclude *every* direct child (including files and symlinks), which
    // also prevents traversal to nested bundles. Depth zero is the caller's
    // explicit scan scope, not an ancestor outside its configured root.
    if entry.depth() > 0 && entry.path().parent().is_some_and(has_app_extension) {
        return false;
    }
    if !entry.file_type().is_dir() {
        return true;
    }
    if options.exclude_hidden_metadata_dirs {
        let name = entry.file_name().to_string_lossy();
        if matches!(
            name.as_ref(),
            ".Spotlight-V100"
                | ".fseventsd"
                | ".Trashes"
                | ".Trash"
                | "$RECYCLE.BIN"
                | "System Volume Information"
        ) {
            return false;
        }
    }
    true
}

fn walk_error(error: walkdir::Error, fallback_path: &Path) -> CoreError {
    let path = error
        .path()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| fallback_path.to_path_buf());
    let source = error
        .io_error()
        .map(io_error_copy)
        .unwrap_or_else(|| std::io::Error::other(error.to_string()));
    CoreError::Filesystem { path, source }
}

fn io_error_copy(error: &std::io::Error) -> std::io::Error {
    std::io::Error::new(error.kind(), error.to_string())
}

#[cfg(unix)]
fn stable_file_id(_path: &Path, metadata: &fs::Metadata) -> Option<String> {
    use std::os::unix::fs::MetadataExt;

    Some(format!("unix:{:x}:{:x}", metadata.dev(), metadata.ino()))
}

#[cfg(windows)]
fn stable_file_id(path: &Path, _metadata: &fs::Metadata) -> Option<String> {
    use std::os::windows::{fs::OpenOptionsExt, io::AsRawHandle};
    use windows_sys::Win32::Storage::FileSystem::{
        BY_HANDLE_FILE_INFORMATION, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT,
        FILE_READ_ATTRIBUTES, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
        GetFileInformationByHandle,
    };

    let file = fs::OpenOptions::new()
        .access_mode(FILE_READ_ATTRIBUTES)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
        .ok()?;
    let mut information = BY_HANDLE_FILE_INFORMATION::default();
    // SAFETY: the file owns a live handle for this call and `information` is a writable,
    // correctly sized output buffer. The handle remains owned by `file` and is closed on drop.
    let succeeded =
        unsafe { GetFileInformationByHandle(file.as_raw_handle() as _, &mut information) };
    if succeeded == 0 {
        return None;
    }
    let file_index =
        (u64::from(information.nFileIndexHigh) << 32) | u64::from(information.nFileIndexLow);

    Some(format!(
        "windows:{:08x}:{:016x}",
        information.dwVolumeSerialNumber, file_index
    ))
}

#[cfg(not(any(unix, windows)))]
fn stable_file_id(_path: &Path, _metadata: &fs::Metadata) -> Option<String> {
    None
}

fn classify_path(path: &Path, metadata: &fs::Metadata) -> ItemKind {
    // On Windows, symlink_metadata reports an NTFS directory junction as a
    // reparse-point link. Its directory attribute remains on the link object
    // even though Metadata::is_dir() is false. Read the link's own attribute;
    // do not resolve its target or descend through it.
    let is_directory = metadata.is_dir() || windows_directory_link(metadata);
    if is_directory
        && path
            .extension()
            .is_some_and(|ext| ext.eq_ignore_ascii_case("app"))
    {
        return ItemKind::App;
    }
    if is_directory {
        return ItemKind::Folder;
    }
    #[cfg(target_os = "windows")]
    if path
        .extension()
        .is_some_and(|ext| ext.eq_ignore_ascii_case("exe"))
    {
        return ItemKind::App;
    }
    if path
        .extension()
        .is_some_and(|ext| ext.eq_ignore_ascii_case("app"))
    {
        ItemKind::Package
    } else {
        ItemKind::File
    }
}

#[cfg(windows)]
fn windows_directory_link(metadata: &fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;
    use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_DIRECTORY;

    metadata.file_type().is_symlink() && metadata.file_attributes() & FILE_ATTRIBUTE_DIRECTORY != 0
}

#[cfg(not(windows))]
fn windows_directory_link(_metadata: &fs::Metadata) -> bool {
    false
}

fn preview_hint(path: &Path, kind: ItemKind) -> Option<String> {
    if matches!(kind, ItemKind::Folder | ItemKind::App | ItemKind::Package) {
        return None;
    }
    let ext = path.extension()?.to_string_lossy().to_ascii_lowercase();
    let hint = match ext.as_str() {
        "png" | "jpg" | "jpeg" | "gif" | "webp" | "heic" | "tif" | "tiff" | "bmp" => "image",
        "pdf" => "pdf",
        "txt" | "md" | "json" | "toml" | "yaml" | "yml" | "rs" | "swift" | "py" | "js" | "ts" => {
            "text"
        }
        "mp3" | "wav" | "m4a" | "flac" | "aac" => "audio",
        "mp4" | "mov" | "mkv" | "avi" | "webm" => "video",
        _ => return None,
    };
    Some(hint.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::RootKind;
    use std::fs;
    use tempfile::TempDir;

    #[test]
    fn preview_hint_includes_bmp_supported_by_shared_thumbnail_owner() {
        assert_eq!(
            preview_hint(Path::new("poster.bmp"), ItemKind::File).as_deref(),
            Some("image")
        );
        assert_eq!(
            preview_hint(Path::new("folder.bmp"), ItemKind::Folder),
            None
        );
    }

    #[test]
    fn crawl_indexes_app_bundle_once_without_descending_into_contents() {
        let sandbox = TempDir::new().unwrap();
        let root_path = sandbox.path().join("Applications");
        let bundle = root_path.join("Example.app");
        let executable = bundle.join("Contents").join("MacOS").join("Example");
        fs::create_dir_all(executable.parent().unwrap()).unwrap();
        fs::write(&executable, b"fixture executable").unwrap();
        fs::write(bundle.join("private-direct.txt"), b"bundle private file").unwrap();
        let nested_app = bundle.join("Contents").join("Helper.APP");
        fs::create_dir_all(&nested_app).unwrap();
        fs::write(nested_app.join("private-helper.txt"), b"helper").unwrap();
        fs::write(root_path.join("ordinary.txt"), b"ordinary file").unwrap();

        let root = RootRecord::new(root_path, RootKind::Local);
        let (items, report) =
            crawl_root(&root, &CancellationToken::new(), &CrawlOptions::default()).unwrap();

        let bundle_rows = items
            .iter()
            .filter(|item| item.path == bundle)
            .collect::<Vec<_>>();
        assert_eq!(bundle_rows.len(), 1);
        assert_eq!(bundle_rows[0].kind, ItemKind::App);
        assert!(
            items
                .iter()
                .all(|item| !item.path.starts_with(bundle.join("Contents")))
        );
        assert_eq!(report.discovered, 2);
        assert_eq!(report.errors, 0);
    }

    #[test]
    fn changed_app_bundle_paths_scan_only_the_outermost_bundle() {
        let sandbox = TempDir::new().unwrap();
        let root_path = sandbox.path().join("Applications");
        let bundle = root_path.join("Example.APP");
        let nested = bundle.join("Contents").join("Helper.app");
        fs::create_dir_all(&nested).unwrap();
        let direct_file = bundle.join("private.txt");
        let nested_file = nested.join("helper.txt");
        fs::write(&direct_file, b"private").unwrap();
        fs::write(&nested_file, b"helper").unwrap();
        let root = RootRecord::new(root_path, RootKind::Local);
        for path in [
            direct_file,
            nested_file,
            nested,
            bundle.join("Contents"),
            bundle.join("deleted.txt"),
            bundle.clone(),
        ] {
            let (items, report) = crawl_changed_path(
                &root,
                &path,
                &CancellationToken::new(),
                &CrawlOptions::default(),
            )
            .unwrap();
            assert_eq!(items.len(), 1, "dirty event {path:?}");
            assert_eq!(items[0].path, bundle);
            assert_eq!(items[0].kind, ItemKind::App);
            assert_eq!(report.discovered, 1);
            assert_eq!(report.errors, 0);
        }
    }

    #[test]
    fn app_bundle_root_is_one_item_but_explicit_inner_root_remains_a_folder_scope() {
        let sandbox = TempDir::new().unwrap();
        let bundle = sandbox.path().join("Example.app");
        let inner_root = bundle.join("Contents");
        fs::create_dir_all(&inner_root).unwrap();
        let ordinary = inner_root.join("ordinary.txt");
        fs::write(&ordinary, b"ordinary").unwrap();

        let (items, _) = crawl_root(
            &RootRecord::new(bundle.clone(), RootKind::Local),
            &CancellationToken::new(),
            &CrawlOptions::default(),
        )
        .unwrap();
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].path, bundle);
        assert_eq!(items[0].kind, ItemKind::App);

        let root = RootRecord::new(inner_root, RootKind::Local);
        let (items, _) =
            crawl_root(&root, &CancellationToken::new(), &CrawlOptions::default()).unwrap();
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].path, ordinary);
        let (items, _) = crawl_changed_path(
            &root,
            &ordinary,
            &CancellationToken::new(),
            &CrawlOptions::default(),
        )
        .unwrap();
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].path, ordinary);
        assert_eq!(items[0].kind, ItemKind::File);
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn windows_executables_are_indexed_as_apps() {
        let sandbox = TempDir::new().unwrap();
        let root_path = sandbox.path().join("Programs");
        fs::create_dir_all(&root_path).unwrap();
        let executable = root_path.join("SpiffyClipboard.exe");
        fs::write(&executable, b"fixture executable").unwrap();

        let root = RootRecord::new(root_path, RootKind::Local);
        let (items, report) =
            crawl_root(&root, &CancellationToken::new(), &CrawlOptions::default()).unwrap();

        assert_eq!(items.len(), 1);
        assert_eq!(items[0].path, executable);
        assert_eq!(items[0].kind, ItemKind::App);
        assert_eq!(report.discovered, 1);
        assert_eq!(report.errors, 0);
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn windows_junction_is_a_folder_occurrence_without_following_nested_target() {
        // A real pre-created task-owned NTFS junction is required; creation
        // privileges vary across Windows hosts. Never modify the target here.
        let Ok(alias) = std::env::var("CHAT15_WINDOWS_JUNCTION_FIXTURE") else {
            return;
        };
        let alias = PathBuf::from(alias);
        let root_path = alias.parent().unwrap().to_path_buf();
        let metadata = fs::symlink_metadata(&alias).unwrap();
        assert!(metadata.file_type().is_symlink());
        assert_eq!(classify_path(&alias, &metadata), ItemKind::Folder);
        let root = RootRecord::new(root_path.clone(), RootKind::Local);
        let (items, report) =
            crawl_root(&root, &CancellationToken::new(), &CrawlOptions::default()).unwrap();
        let junction = items.iter().find(|item| item.path == alias).unwrap();
        assert_eq!(junction.kind, ItemKind::Folder);
        assert!(
            junction
                .stable_file_id
                .as_deref()
                .is_some_and(|id| id.starts_with("windows:"))
        );
        assert!(
            items
                .iter()
                .all(|item| !item.path.starts_with(alias.join("payload.txt")))
        );
        assert_eq!(report.errors, 0);
        let (dirty, _) = crawl_changed_path(
            &root,
            &alias,
            &CancellationToken::new(),
            &CrawlOptions::default(),
        )
        .unwrap();
        assert_eq!(dirty.len(), 1);
        assert_eq!(dirty[0].kind, ItemKind::Folder);
        assert_eq!(dirty[0].stable_file_id, junction.stable_file_id);
    }

    #[test]
    fn localized_crawl_errors_keep_minimal_private_preservation_prefixes() {
        let mut report = CrawlReport::default();
        report.record_localized_error(PathBuf::from("X:/root/locked/child.txt"));
        report.record_localized_error(PathBuf::from("X:/root/locked"));
        report.record_localized_error(PathBuf::from("X:/root/other/file.txt"));
        report.record_localized_error(PathBuf::from("X:/root/locked/another.txt"));

        assert_eq!(report.errors, 4);
        assert_eq!(
            report.failed_paths(),
            &[
                PathBuf::from("X:/root/locked"),
                PathBuf::from("X:/root/other/file.txt")
            ]
        );
        let serialized = serde_json::to_value(&report).unwrap();
        assert_eq!(serialized["errors"], 4);
        assert!(serialized.get("failed_paths").is_none());
    }
}
