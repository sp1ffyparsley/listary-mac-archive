use crate::{
    CoreError, Result,
    model::{DiscoveredItem, ItemKind, Priority},
};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashSet,
    fs,
    path::{Component, Path, PathBuf},
};
use uuid::Uuid;

const WORKSPACE_CATALOG_SCHEMA: &str = "ss_scripts_workspace_catalog_v1";
const WORKSPACE_CATALOG_VERSION: u32 = 1;
const MAX_CATALOG_BYTES: u64 = 4 * 1024 * 1024;
const MAX_SYSTEMS: usize = 512;
const MAX_LAUNCH_TARGETS: usize = 2048;
const MAX_ALIASES: usize = 64;
const MAX_TEXT_CHARS: usize = 512;
const WORKSPACE_PROVIDER_NAMESPACE: Uuid = Uuid::from_u128(0x71cb03ca_80a5_5a5d_8c9a_c8f37e888958);

pub const WORKSPACE_PROVIDER_HINT_PREFIX: &str = "workspace_application_v1:";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WorkspaceCatalogPlatform {
    Windows,
    Macos,
}

impl WorkspaceCatalogPlatform {
    pub fn current() -> Result<Self> {
        #[cfg(target_os = "windows")]
        {
            return Ok(Self::Windows);
        }
        #[cfg(target_os = "macos")]
        {
            return Ok(Self::Macos);
        }
        #[allow(unreachable_code)]
        Err(CoreError::InvalidRequest(
            "workspace catalog launch targets are supported only on Windows and macOS".into(),
        ))
    }

    fn key(self) -> &'static str {
        match self {
            Self::Windows => "windows",
            Self::Macos => "macos",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorkspaceCatalogTarget {
    pub system_id: String,
    pub launch_target_id: String,
    pub label: String,
    pub aliases: Vec<String>,
    pub launch_path: PathBuf,
    pub stable_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorkspaceCatalogSnapshot {
    pub workspace_id: String,
    pub source_provider: String,
    pub root_id: Uuid,
    pub targets: Vec<WorkspaceCatalogTarget>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorkspaceCatalogRefresh {
    pub workspace_id: String,
    pub source_provider: String,
    pub root_id: Uuid,
    pub item_count: usize,
    pub removed_count: usize,
    pub catalog_generation: u64,
}

impl WorkspaceCatalogSnapshot {
    pub fn provider_root_path(&self, state_dir: &Path) -> PathBuf {
        state_dir
            .join("providers")
            .join("workspace_catalog")
            .join(&self.workspace_id)
    }

    pub fn discovered_items(&self, state_dir: &Path) -> Result<Vec<DiscoveredItem>> {
        let root = self.provider_root_path(state_dir);
        self.targets
            .iter()
            .map(|target| {
                let search_name = std::iter::once(target.label.as_str())
                    .chain(target.aliases.iter().map(String::as_str))
                    .collect::<Vec<_>>()
                    .join(" ");
                let launch_path = target.launch_path.to_string_lossy();
                let metadata = WorkspaceProviderMetadata {
                    schema: "workspace_catalog_launch_target_v1",
                    source_provider: &self.source_provider,
                    workspace_id: &self.workspace_id,
                    system_id: &target.system_id,
                    launch_target_id: &target.launch_target_id,
                    label: &target.label,
                    aliases: &target.aliases,
                    launch_path: launch_path.as_ref(),
                };
                let encoded = serde_json::to_string(&metadata)?;
                Ok(DiscoveredItem {
                    path: root
                        .join(&target.system_id)
                        .join(format!("{}.workspaceapp", target.launch_target_id)),
                    kind: ItemKind::App,
                    size: 0,
                    modified_unix_ms: 0,
                    priority: Priority::High,
                    stable_file_id: Some(target.stable_id.clone()),
                    preview_hint: Some(format!("{WORKSPACE_PROVIDER_HINT_PREFIX}{encoded}")),
                    search_name: Some(search_name),
                })
            })
            .collect()
    }
}

#[derive(Serialize)]
struct WorkspaceProviderMetadata<'a> {
    schema: &'static str,
    source_provider: &'a str,
    workspace_id: &'a str,
    system_id: &'a str,
    launch_target_id: &'a str,
    label: &'a str,
    aliases: &'a [String],
    launch_path: &'a str,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WorkspaceProviderMetadataOwned {
    schema: String,
    source_provider: String,
    workspace_id: String,
    system_id: String,
    launch_target_id: String,
    label: String,
    aliases: Vec<String>,
    launch_path: String,
}

pub(crate) fn workspace_provider_launch_path(hint: &str) -> Option<String> {
    let encoded = hint.strip_prefix(WORKSPACE_PROVIDER_HINT_PREFIX)?;
    let metadata: WorkspaceProviderMetadataOwned = serde_json::from_str(encoded).ok()?;
    if metadata.schema != "workspace_catalog_launch_target_v1"
        || metadata.workspace_id.trim().is_empty()
        || metadata.system_id.trim().is_empty()
        || metadata.launch_target_id.trim().is_empty()
        || metadata.label.trim().is_empty()
        || metadata.aliases.iter().any(|alias| alias.trim().is_empty())
        || metadata.source_provider != format!("workspace_catalog:{}", metadata.workspace_id)
    {
        return None;
    }
    let launch_path = Path::new(&metadata.launch_path);
    launch_path.is_absolute().then_some(metadata.launch_path)
}

pub(crate) fn is_workspace_provider_hint(hint: &str) -> bool {
    workspace_provider_launch_path(hint).is_some()
}

#[derive(Deserialize)]
struct RawCatalog {
    schema: String,
    version: u32,
    workspace_id: String,
    systems: Vec<RawSystem>,
    #[serde(flatten)]
    _ignored: serde_json::Map<String, serde_json::Value>,
}

#[derive(Deserialize)]
struct RawSystem {
    id: String,
    #[serde(default)]
    platforms: Vec<String>,
    #[serde(default)]
    launch_targets: Vec<RawLaunchTarget>,
    #[serde(flatten)]
    _ignored: serde_json::Map<String, serde_json::Value>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawLaunchTarget {
    id: String,
    label: String,
    aliases: Vec<String>,
    platform_paths: serde_json::Map<String, serde_json::Value>,
}

pub fn load_workspace_catalog(
    catalog_path: &Path,
    platform: WorkspaceCatalogPlatform,
) -> Result<WorkspaceCatalogSnapshot> {
    let metadata = fs::metadata(catalog_path).map_err(|source| CoreError::Filesystem {
        path: catalog_path.to_path_buf(),
        source,
    })?;
    if !metadata.is_file() {
        return Err(CoreError::InvalidRequest(format!(
            "workspace catalog is not a file: {}",
            catalog_path.display()
        )));
    }
    if metadata.len() > MAX_CATALOG_BYTES {
        return Err(CoreError::InvalidRequest(format!(
            "workspace catalog exceeds {MAX_CATALOG_BYTES} bytes"
        )));
    }
    let bytes = fs::read(catalog_path).map_err(|source| CoreError::Filesystem {
        path: catalog_path.to_path_buf(),
        source,
    })?;
    let raw: RawCatalog = serde_json::from_slice(&bytes)?;
    if raw.schema != WORKSPACE_CATALOG_SCHEMA || raw.version != WORKSPACE_CATALOG_VERSION {
        return Err(CoreError::InvalidRequest(format!(
            "unsupported workspace catalog schema/version: {}/{}",
            raw.schema, raw.version
        )));
    }
    let workspace_id = canonical_id(&raw.workspace_id, "workspace id")?;
    if raw.systems.len() > MAX_SYSTEMS {
        return Err(CoreError::InvalidRequest(format!(
            "workspace catalog has more than {MAX_SYSTEMS} systems"
        )));
    }
    let workspace_root = catalog_path
        .parent()
        .ok_or_else(|| {
            CoreError::InvalidRequest("workspace catalog has no parent directory".into())
        })?
        .canonicalize()
        .map_err(|source| CoreError::Filesystem {
            path: catalog_path.to_path_buf(),
            source,
        })?;
    let platform_key = platform.key();
    let mut seen_system_ids = HashSet::new();
    let mut seen_target_ids = HashSet::new();
    let mut seen_launch_paths = HashSet::new();
    let mut targets = Vec::new();

    for system in raw.systems {
        let system_id = canonical_id(&system.id, "system id")?;
        if !seen_system_ids.insert(system_id.clone()) {
            return Err(CoreError::InvalidRequest(format!(
                "duplicate workspace catalog system id: {system_id}"
            )));
        }
        let declared_platforms = system
            .platforms
            .iter()
            .map(|value| value.trim())
            .collect::<HashSet<_>>();
        for target in system.launch_targets {
            if targets.len() >= MAX_LAUNCH_TARGETS {
                return Err(CoreError::InvalidRequest(format!(
                    "workspace catalog has more than {MAX_LAUNCH_TARGETS} launch targets"
                )));
            }
            let target_id = canonical_id(&target.id, "launch target id")?;
            if !seen_target_ids.insert(target_id.clone()) {
                return Err(CoreError::InvalidRequest(format!(
                    "duplicate workspace catalog launch target id: {target_id}"
                )));
            }
            let label = canonical_text(&target.label, "launch target label")?;
            if target.aliases.len() > MAX_ALIASES {
                return Err(CoreError::InvalidRequest(format!(
                    "launch target {target_id} has more than {MAX_ALIASES} aliases"
                )));
            }
            let mut aliases = Vec::with_capacity(target.aliases.len());
            let mut seen_aliases = HashSet::new();
            for alias in target.aliases {
                let alias = canonical_text(&alias, "launch target alias")?;
                if seen_aliases.insert(alias.to_lowercase()) {
                    aliases.push(alias);
                }
            }
            for (key, value) in &target.platform_paths {
                if !declared_platforms.contains(key.as_str()) {
                    return Err(CoreError::InvalidRequest(format!(
                        "launch target {target_id} uses undeclared platform: {key}"
                    )));
                }
                let raw_path = value.as_str().ok_or_else(|| {
                    CoreError::InvalidRequest(format!(
                        "launch target {target_id} path for {key} must be a string"
                    ))
                })?;
                validate_relative_launch_path(raw_path, &target_id, key)?;
            }
            if !declared_platforms.contains(platform_key) {
                continue;
            }
            let Some(raw_path) = target.platform_paths.get(platform_key) else {
                continue;
            };
            let raw_path = raw_path.as_str().expect("platform path validated above");
            let launch_path =
                resolve_launch_path(&workspace_root, raw_path, &target_id, platform_key)?;
            let dedupe_path = normalized_path_identity(&launch_path);
            if !seen_launch_paths.insert(dedupe_path) {
                return Err(CoreError::InvalidRequest(format!(
                    "duplicate workspace catalog launch path for target {target_id}: {}",
                    launch_path.display()
                )));
            }
            let stable_id = format!("workspace_catalog:{workspace_id}:{system_id}:{target_id}");
            targets.push(WorkspaceCatalogTarget {
                system_id: system_id.clone(),
                launch_target_id: target_id,
                label,
                aliases,
                launch_path,
                stable_id,
            });
        }
    }
    targets.sort_by(|a, b| a.stable_id.cmp(&b.stable_id));
    Ok(WorkspaceCatalogSnapshot {
        root_id: Uuid::new_v5(
            &WORKSPACE_PROVIDER_NAMESPACE,
            format!("workspace_catalog:{workspace_id}").as_bytes(),
        ),
        source_provider: format!("workspace_catalog:{workspace_id}"),
        workspace_id,
        targets,
    })
}

fn canonical_id(value: &str, field: &str) -> Result<String> {
    let value = canonical_text(value, field)?;
    if !value
        .chars()
        .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-' | '.'))
    {
        return Err(CoreError::InvalidRequest(format!(
            "{field} contains unsupported characters: {value}"
        )));
    }
    Ok(value)
}

fn canonical_text(value: &str, field: &str) -> Result<String> {
    let value = value.trim();
    if value.is_empty()
        || value.chars().count() > MAX_TEXT_CHARS
        || value.chars().any(char::is_control)
    {
        return Err(CoreError::InvalidRequest(format!("invalid {field}")));
    }
    Ok(value.to_string())
}

fn validate_relative_launch_path(raw: &str, target_id: &str, platform: &str) -> Result<()> {
    let raw = raw.trim();
    let path = Path::new(raw);
    if raw.is_empty()
        || path.is_absolute()
        || looks_like_cross_platform_absolute_path(raw)
        || has_cross_platform_parent_component(raw)
        || path.components().any(|component| {
            matches!(
                component,
                Component::ParentDir | Component::RootDir | Component::Prefix(_)
            )
        })
    {
        return Err(CoreError::InvalidRequest(format!(
            "launch target {target_id} has unsafe {platform} path: {raw}"
        )));
    }
    Ok(())
}

fn looks_like_cross_platform_absolute_path(raw: &str) -> bool {
    let bytes = raw.as_bytes();
    raw.starts_with('/')
        || raw.starts_with('\\')
        || raw.starts_with("//")
        || (bytes.len() >= 2 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':')
}

fn has_cross_platform_parent_component(raw: &str) -> bool {
    raw.split(['/', '\\']).any(|component| component == "..")
}

fn resolve_launch_path(
    workspace_root: &Path,
    raw: &str,
    target_id: &str,
    platform: &str,
) -> Result<PathBuf> {
    let joined = workspace_root.join(raw.trim());
    let canonical = joined
        .canonicalize()
        .map_err(|source| CoreError::Filesystem {
            path: joined.clone(),
            source,
        })?;
    if !canonical.starts_with(workspace_root) {
        return Err(CoreError::InvalidRequest(format!(
            "launch target {target_id} escapes workspace root through its {platform} path"
        )));
    }
    if !canonical.is_file() {
        return Err(CoreError::InvalidRequest(format!(
            "launch target {target_id} is not a file: {}",
            canonical.display()
        )));
    }
    Ok(platform_canonical_path(canonical))
}

#[cfg(target_os = "windows")]
fn platform_canonical_path(path: PathBuf) -> PathBuf {
    let value = path.to_string_lossy();
    if let Some(rest) = value.strip_prefix(r"\\?\UNC\") {
        return PathBuf::from(format!(r"\\{rest}"));
    }
    if let Some(rest) = value.strip_prefix(r"\\?\") {
        return PathBuf::from(rest);
    }
    path
}

#[cfg(not(target_os = "windows"))]
fn platform_canonical_path(path: PathBuf) -> PathBuf {
    path
}

fn normalized_path_identity(path: &Path) -> String {
    let value = path.to_string_lossy().replace('\\', "/");
    #[cfg(target_os = "windows")]
    {
        value.to_lowercase()
    }
    #[cfg(not(target_os = "windows"))]
    {
        value.into_owned()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn write_catalog(root: &Path, body: serde_json::Value) -> PathBuf {
        let path = root.join("workspace_catalog.json");
        fs::write(&path, serde_json::to_vec_pretty(&body).unwrap()).unwrap();
        path
    }

    #[test]
    fn parses_current_platform_targets_aliases_and_deterministic_identity() {
        let temp = tempdir().unwrap();
        let windows = temp.path().join("Launch Tool.bat");
        let macos = temp.path().join("Launch Tool.command");
        fs::write(&windows, b"@echo off\n").unwrap();
        fs::write(&macos, b"#!/bin/zsh\n").unwrap();
        let catalog = write_catalog(
            temp.path(),
            serde_json::json!({
                "schema": WORKSPACE_CATALOG_SCHEMA,
                "version": 1,
                "workspace_id": "ss-scripts",
                "description": "ignored root metadata",
                "policy": {"integration": "canonical"},
                "relations": [],
                "external_references": [],
                "systems": [{
                    "id": "spiffy_workspace",
                    "platforms": ["windows", "macos"],
                    "launch_targets": [{
                        "id": "tool",
                        "label": "Spiffy Tool",
                        "aliases": ["Tool", "Editor"],
                        "platform_paths": {
                            "windows": "Launch Tool.bat",
                            "macos": "Launch Tool.command"
                        }
                    }]
                }]
            }),
        );
        let first = load_workspace_catalog(&catalog, WorkspaceCatalogPlatform::Windows).unwrap();
        let second = load_workspace_catalog(&catalog, WorkspaceCatalogPlatform::Windows).unwrap();
        assert_eq!(first.root_id, second.root_id);
        assert_eq!(first.targets[0].label, "Spiffy Tool");
        assert_eq!(first.targets[0].aliases, ["Tool", "Editor"]);
        assert_eq!(
            first.targets[0].launch_path,
            platform_canonical_path(windows.canonicalize().unwrap())
        );
        assert_eq!(
            first.targets[0].stable_id,
            "workspace_catalog:ss-scripts:spiffy_workspace:tool"
        );
    }

    #[test]
    fn rejects_cross_platform_absolute_paths_before_platform_selection() {
        let temp = tempdir().unwrap();
        let launcher = temp.path().join("Launch Tool.bat");
        fs::write(&launcher, b"@echo off\n").unwrap();
        for unsafe_path in [
            r"C:\\Spiffy\\Launch Tool.command",
            r"\\\\server\\share\\tool.command",
            r"\Windows\System32\cmd.exe",
        ] {
            let catalog = write_catalog(
                temp.path(),
                serde_json::json!({
                    "schema": WORKSPACE_CATALOG_SCHEMA,
                    "version": 1,
                    "workspace_id": "ss-scripts",
                    "systems": [{
                        "id": "spiffy_workspace",
                        "platforms": ["windows", "macos"],
                        "launch_targets": [{
                            "id": "tool",
                            "label": "Spiffy Tool",
                            "aliases": [],
                            "platform_paths": {
                                "windows": "Launch Tool.bat",
                                "macos": unsafe_path
                            }
                        }]
                    }]
                }),
            );
            assert!(load_workspace_catalog(&catalog, WorkspaceCatalogPlatform::Windows).is_err());
        }
    }

    #[test]
    fn rejects_windows_parent_escape_on_non_windows_hosts() {
        let temp = tempdir().unwrap();
        let launcher = temp.path().join("Launch Tool.command");
        fs::write(&launcher, b"#!/bin/zsh\n").unwrap();
        let catalog = write_catalog(
            temp.path(),
            serde_json::json!({
                "schema": WORKSPACE_CATALOG_SCHEMA,
                "version": 1,
                "workspace_id": "ss-scripts",
                "systems": [{
                    "id": "spiffy_workspace",
                    "platforms": ["windows", "macos"],
                    "launch_targets": [{
                        "id": "tool",
                        "label": "Spiffy Tool",
                        "aliases": [],
                        "platform_paths": {
                            "windows": "..\\outside.bat",
                            "macos": "Launch Tool.command"
                        }
                    }]
                }]
            }),
        );
        assert!(load_workspace_catalog(&catalog, WorkspaceCatalogPlatform::Macos).is_err());
    }

    #[test]
    fn rejects_directory_launch_target() {
        let temp = tempdir().unwrap();
        fs::create_dir(temp.path().join("launcher-dir")).unwrap();
        let catalog = write_catalog(
            temp.path(),
            serde_json::json!({
                "schema": WORKSPACE_CATALOG_SCHEMA,
                "version": 1,
                "workspace_id": "ss-scripts",
                "systems": [{
                    "id": "spiffy_workspace",
                    "platforms": ["windows"],
                    "launch_targets": [{
                        "id":"dir","label":"Directory","aliases":[],
                        "platform_paths":{"windows":"launcher-dir"}
                    }]
                }]
            }),
        );
        assert!(load_workspace_catalog(&catalog, WorkspaceCatalogPlatform::Windows).is_err());
    }

    #[test]
    fn rejects_duplicate_ids_missing_targets_and_parent_escape() {
        let temp = tempdir().unwrap();
        let catalog = write_catalog(
            temp.path(),
            serde_json::json!({
                "schema": WORKSPACE_CATALOG_SCHEMA,
                "version": 1,
                "workspace_id": "ss-scripts",
                "systems": [{
                    "id": "spiffy_workspace",
                    "platforms": ["windows"],
                    "launch_targets": [
                        {"id":"dup","label":"A","aliases":[],"platform_paths":{"windows":"missing.bat"}},
                        {"id":"dup","label":"B","aliases":[],"platform_paths":{"windows":"../escape.bat"}}
                    ]
                }]
            }),
        );
        assert!(load_workspace_catalog(&catalog, WorkspaceCatalogPlatform::Windows).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn rejects_symlink_that_resolves_outside_workspace() {
        use std::os::unix::fs::symlink;
        let temp = tempdir().unwrap();
        let outside = tempdir().unwrap();
        let target = outside.path().join("outside.command");
        fs::write(&target, b"#!/bin/zsh\n").unwrap();
        symlink(&target, temp.path().join("escaped.command")).unwrap();
        let catalog = write_catalog(
            temp.path(),
            serde_json::json!({
                "schema": WORKSPACE_CATALOG_SCHEMA,
                "version": 1,
                "workspace_id": "ss-scripts",
                "systems": [{
                    "id": "spiffy_workspace",
                    "platforms": ["macos"],
                    "launch_targets": [{
                        "id":"escape","label":"Escape","aliases":[],
                        "platform_paths":{"macos":"escaped.command"}
                    }]
                }]
            }),
        );
        assert!(load_workspace_catalog(&catalog, WorkspaceCatalogPlatform::Macos).is_err());
    }
}
