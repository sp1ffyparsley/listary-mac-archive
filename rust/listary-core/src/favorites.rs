use crate::{
    error::{CoreError, Result},
    history::durable_json,
    model::ItemRecord,
    text::comparable_path,
};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashSet,
    fs,
    path::{Path, PathBuf},
};
use uuid::Uuid;

const FAVORITES_SCHEMA_VERSION: u32 = 2;
const FAVORITES_SCHEMA_VERSION_V1: u32 = 1;
const MAX_MENU_ITEMS: usize = 512;
const MAX_MENU_DEPTH: usize = 8;
const MAX_CHILDREN_PER_SUBMENU: usize = 128;
const MAX_TITLE_CHARS: usize = 160;
const MAX_PATH_CHARS: usize = 4_096;
pub(crate) const MAX_DYNAMIC_LIMIT: usize = 50;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FavoriteDynamicGroupKind {
    RecentFolders,
    OpenFolders,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FavoriteMenu {
    #[serde(default)]
    pub items: Vec<FavoriteMenuItem>,
    #[serde(default)]
    pub pins: Vec<FavoritePin>,
}

impl FavoriteMenu {
    pub fn empty() -> Self {
        Self {
            items: Vec::new(),
            pins: Vec::new(),
        }
    }

    pub fn item_count(&self) -> usize {
        count_items(&self.items).saturating_add(self.pins.len())
    }
}

impl Default for FavoriteMenu {
    fn default() -> Self {
        Self::empty()
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FavoritePin {
    pub id: Uuid,
    #[serde(default)]
    pub title: Option<String>,
    pub path: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum FavoriteMenuItem {
    Folder {
        id: Uuid,
        #[serde(default)]
        title: Option<String>,
        path: String,
    },
    Submenu {
        id: Uuid,
        title: String,
        #[serde(default)]
        items: Vec<FavoriteMenuItem>,
    },
    Separator {
        id: Uuid,
    },
    Command {
        id: Uuid,
        command_id: Uuid,
    },
    DynamicGroup {
        id: Uuid,
        #[serde(default)]
        title: Option<String>,
        kind: FavoriteDynamicGroupKind,
        #[serde(default = "default_dynamic_limit")]
        limit: usize,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OpenFolderCandidate {
    pub path: String,
    #[serde(default)]
    pub title: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RecentFolderCandidate {
    pub item: ItemRecord,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ResolvedFavoriteMenuItem {
    Folder {
        #[serde(default)]
        id: Option<Uuid>,
        title: String,
        path: String,
        source: FavoriteFolderSource,
    },
    Submenu {
        id: Uuid,
        title: String,
        items: Vec<ResolvedFavoriteMenuItem>,
    },
    Separator {
        id: Uuid,
    },
    Command {
        id: Uuid,
        title: String,
        command_id: Uuid,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FavoriteFolderSource {
    Favorite,
    Recent,
    OpenFolder,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ResolvedFavoriteMenu {
    pub items: Vec<ResolvedFavoriteMenuItem>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct FavoritesFile {
    schema_version: u32,
    menu: FavoriteMenu,
}

impl Default for FavoritesFile {
    fn default() -> Self {
        Self {
            schema_version: FAVORITES_SCHEMA_VERSION,
            menu: FavoriteMenu::default(),
        }
    }
}

#[derive(Debug)]
pub(crate) struct FavoritesStore {
    path: PathBuf,
    state: FavoritesFile,
}

impl FavoritesStore {
    pub(crate) fn open(state_dir: &Path) -> Result<Self> {
        let settings_dir = state_dir.join("settings");
        fs::create_dir_all(&settings_dir).map_err(|source| CoreError::Filesystem {
            path: settings_dir.clone(),
            source,
        })?;

        let path = settings_dir.join("favorites.json");
        let backup = settings_dir.join("favorites.json.bak");
        let temp = settings_dir.join("favorites.json.tmp");
        let state = durable_json::open_with_minimum(
            &path,
            &backup,
            &temp,
            FAVORITES_SCHEMA_VERSION_V1,
            FAVORITES_SCHEMA_VERSION,
            "favorites",
            validate_favorites_file,
        )?;

        Ok(Self { path, state })
    }

    pub(crate) fn get(&self) -> FavoriteMenu {
        self.state.menu.clone()
    }

    pub(crate) fn put(&mut self, menu: FavoriteMenu) -> Result<FavoriteMenu> {
        let menu = canonicalize_favorite_menu(menu)?;
        let next = FavoritesFile {
            schema_version: FAVORITES_SCHEMA_VERSION,
            menu: menu.clone(),
        };
        persist_favorites_file(&self.path, &next)?;
        self.state = next;
        Ok(menu)
    }

    pub(crate) fn references_command(&self, command_id: Uuid) -> bool {
        menu_references_command(&self.state.menu.items, command_id)
    }

    pub(crate) fn resolve<F>(
        &self,
        recent_folders: &[RecentFolderCandidate],
        open_folders: &[OpenFolderCandidate],
        mut command_title: F,
    ) -> Result<ResolvedFavoriteMenu>
    where
        F: FnMut(Uuid) -> Option<String>,
    {
        let mut items = Vec::new();
        resolve_items(
            &self.state.menu.items,
            recent_folders,
            open_folders,
            &mut command_title,
            &mut items,
        )?;
        Ok(ResolvedFavoriteMenu { items })
    }
}

pub(crate) fn canonicalize_favorite_menu(mut menu: FavoriteMenu) -> Result<FavoriteMenu> {
    let mut ids = HashSet::new();
    let mut pin_paths = HashSet::new();
    let mut count = 0usize;
    canonicalize_items(&mut menu.items, 1, &mut count, &mut ids)?;
    for pin in &mut menu.pins {
        count = count.saturating_add(1);
        if count > MAX_MENU_ITEMS {
            return Err(CoreError::InvalidRequest(format!(
                "favorites menu has more than {MAX_MENU_ITEMS} items"
            )));
        }
        if !ids.insert(pin.id) {
            return Err(CoreError::InvalidRequest(format!(
                "duplicate favorites item id: {}",
                pin.id
            )));
        }
        pin.title = canonical_optional_title(pin.title.take())?;
        pin.path = canonical_path(&pin.path)?;
        if !pin_paths.insert(path_key(&pin.path)) {
            return Err(CoreError::InvalidRequest(format!(
                "duplicate favorite pin path: {}",
                pin.path
            )));
        }
    }
    if count > MAX_MENU_ITEMS {
        return Err(CoreError::InvalidRequest(format!(
            "favorites menu has more than {MAX_MENU_ITEMS} items"
        )));
    }
    Ok(menu)
}

pub(crate) fn favorite_command_ids(menu: &FavoriteMenu) -> HashSet<Uuid> {
    let mut ids = HashSet::new();
    collect_command_ids(&menu.items, &mut ids);
    ids
}

pub(crate) fn favorite_pins(menu: &FavoriteMenu) -> &[FavoritePin] {
    &menu.pins
}

pub(crate) fn toggle_favorite_pin(
    menu: &mut FavoriteMenu,
    path: &str,
    title: Option<String>,
) -> Result<bool> {
    let canonical = canonical_path(path)?;
    let key = path_key(&canonical);
    if let Some(index) = menu.pins.iter().position(|pin| path_key(&pin.path) == key) {
        menu.pins.remove(index);
        return Ok(false);
    }
    menu.pins.push(FavoritePin {
        id: Uuid::new_v4(),
        title: canonical_optional_title(title)?,
        path: canonical,
    });
    *menu = canonicalize_favorite_menu(std::mem::take(menu))?;
    Ok(true)
}

pub(crate) fn map_favorite_paths<F>(menu: &mut FavoriteMenu, mut mapper: F) -> Result<()>
where
    F: FnMut(&str) -> Result<String>,
{
    map_item_paths(&mut menu.items, &mut mapper)?;
    for pin in &mut menu.pins {
        pin.path = mapper(&pin.path)?;
    }
    Ok(())
}

pub(crate) fn canonicalize_open_folders(
    candidates: Vec<OpenFolderCandidate>,
) -> Result<Vec<OpenFolderCandidate>> {
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for mut candidate in candidates {
        candidate.path = canonical_path(&candidate.path)?;
        candidate.title = canonical_optional_title(candidate.title)?;
        if seen.insert(path_key(&candidate.path)) {
            out.push(candidate);
        }
    }
    Ok(out)
}

fn canonicalize_items(
    items: &mut [FavoriteMenuItem],
    depth: usize,
    count: &mut usize,
    ids: &mut HashSet<Uuid>,
) -> Result<()> {
    if depth > MAX_MENU_DEPTH {
        return Err(CoreError::InvalidRequest(format!(
            "favorites menu exceeds maximum depth {MAX_MENU_DEPTH}"
        )));
    }
    if items.len() > MAX_CHILDREN_PER_SUBMENU {
        return Err(CoreError::InvalidRequest(format!(
            "favorites submenu has more than {MAX_CHILDREN_PER_SUBMENU} children"
        )));
    }
    for item in items {
        *count = count.saturating_add(1);
        if *count > MAX_MENU_ITEMS {
            return Err(CoreError::InvalidRequest(format!(
                "favorites menu has more than {MAX_MENU_ITEMS} items"
            )));
        }
        let id = item_id(item);
        if !ids.insert(id) {
            return Err(CoreError::InvalidRequest(format!(
                "duplicate favorites item id: {id}"
            )));
        }
        match item {
            FavoriteMenuItem::Folder { title, path, .. } => {
                *title = canonical_optional_title(title.take())?;
                *path = canonical_path(path)?;
            }
            FavoriteMenuItem::Submenu { title, items, .. } => {
                *title = canonical_title(title)?;
                if items.is_empty() {
                    return Err(CoreError::InvalidRequest(
                        "favorites submenu cannot be empty".into(),
                    ));
                }
                canonicalize_items(items, depth + 1, count, ids)?;
            }
            FavoriteMenuItem::Separator { .. } => {}
            FavoriteMenuItem::Command { .. } => {}
            FavoriteMenuItem::DynamicGroup { title, limit, .. } => {
                *title = canonical_optional_title(title.take())?;
                if *limit == 0 || *limit > MAX_DYNAMIC_LIMIT {
                    return Err(CoreError::InvalidRequest(format!(
                        "favorites dynamic group limit must be between 1 and {MAX_DYNAMIC_LIMIT}"
                    )));
                }
            }
        }
    }
    Ok(())
}

fn resolve_items<F>(
    items: &[FavoriteMenuItem],
    recent_folders: &[RecentFolderCandidate],
    open_folders: &[OpenFolderCandidate],
    command_title: &mut F,
    out: &mut Vec<ResolvedFavoriteMenuItem>,
) -> Result<()>
where
    F: FnMut(Uuid) -> Option<String>,
{
    for item in items {
        match item {
            FavoriteMenuItem::Folder { id, title, path } => {
                out.push(ResolvedFavoriteMenuItem::Folder {
                    id: Some(*id),
                    title: title.clone().unwrap_or_else(|| folder_title(path)),
                    path: path.clone(),
                    source: FavoriteFolderSource::Favorite,
                });
            }
            FavoriteMenuItem::Submenu { id, title, items } => {
                let mut resolved = Vec::new();
                resolve_items(
                    items,
                    recent_folders,
                    open_folders,
                    command_title,
                    &mut resolved,
                )?;
                out.push(ResolvedFavoriteMenuItem::Submenu {
                    id: *id,
                    title: title.clone(),
                    items: resolved,
                });
            }
            FavoriteMenuItem::Separator { id } => {
                out.push(ResolvedFavoriteMenuItem::Separator { id: *id });
            }
            FavoriteMenuItem::Command { id, command_id } => {
                let title = command_title(*command_id).ok_or_else(|| {
                    CoreError::InvalidRequest(format!(
                        "favorites menu references unknown command: {command_id}"
                    ))
                })?;
                out.push(ResolvedFavoriteMenuItem::Command {
                    id: *id,
                    title,
                    command_id: *command_id,
                });
            }
            FavoriteMenuItem::DynamicGroup {
                id,
                title,
                kind,
                limit,
            } => {
                let dynamic = dynamic_items(*kind, *limit, recent_folders, open_folders);
                if let Some(title) = title {
                    out.push(ResolvedFavoriteMenuItem::Submenu {
                        id: *id,
                        title: title.clone(),
                        items: dynamic,
                    });
                } else {
                    out.extend(dynamic);
                }
            }
        }
    }
    Ok(())
}

fn dynamic_items(
    kind: FavoriteDynamicGroupKind,
    limit: usize,
    recent_folders: &[RecentFolderCandidate],
    open_folders: &[OpenFolderCandidate],
) -> Vec<ResolvedFavoriteMenuItem> {
    let mut seen = HashSet::new();
    match kind {
        FavoriteDynamicGroupKind::RecentFolders => recent_folders
            .iter()
            .filter(|candidate| seen.insert(path_key(&candidate.item.path)))
            .take(limit)
            .map(|candidate| ResolvedFavoriteMenuItem::Folder {
                id: None,
                title: if candidate.item.name.is_empty() {
                    folder_title(&candidate.item.path)
                } else {
                    candidate.item.name.clone()
                },
                path: candidate.item.path.clone(),
                source: FavoriteFolderSource::Recent,
            })
            .collect(),
        FavoriteDynamicGroupKind::OpenFolders => open_folders
            .iter()
            .filter(|candidate| seen.insert(path_key(&candidate.path)))
            .take(limit)
            .map(|candidate| ResolvedFavoriteMenuItem::Folder {
                id: None,
                title: candidate
                    .title
                    .clone()
                    .filter(|value| !value.trim().is_empty())
                    .unwrap_or_else(|| folder_title(&candidate.path)),
                path: candidate.path.clone(),
                source: FavoriteFolderSource::OpenFolder,
            })
            .collect(),
    }
}

fn canonical_title(value: &str) -> Result<String> {
    let value = value.trim().to_string();
    if value.is_empty() {
        return Err(CoreError::InvalidRequest(
            "favorites title cannot be empty".into(),
        ));
    }
    if value.chars().count() > MAX_TITLE_CHARS {
        return Err(CoreError::InvalidRequest(format!(
            "favorites title is longer than {MAX_TITLE_CHARS} characters"
        )));
    }
    Ok(value)
}

fn canonical_optional_title(value: Option<String>) -> Result<Option<String>> {
    let Some(value) = value else {
        return Ok(None);
    };
    let value = value.trim().to_string();
    if value.is_empty() {
        return Ok(None);
    }
    if value.chars().count() > MAX_TITLE_CHARS {
        return Err(CoreError::InvalidRequest(format!(
            "favorites title is longer than {MAX_TITLE_CHARS} characters"
        )));
    }
    Ok(Some(value))
}

fn canonical_path(value: &str) -> Result<String> {
    let value = value.trim().to_string();
    if value.is_empty() {
        return Err(CoreError::InvalidRequest(
            "favorites folder path cannot be empty".into(),
        ));
    }
    if value.chars().count() > MAX_PATH_CHARS {
        return Err(CoreError::InvalidRequest(format!(
            "favorites folder path is longer than {MAX_PATH_CHARS} characters"
        )));
    }
    Ok(value)
}

fn item_id(item: &FavoriteMenuItem) -> Uuid {
    match item {
        FavoriteMenuItem::Folder { id, .. }
        | FavoriteMenuItem::Submenu { id, .. }
        | FavoriteMenuItem::Separator { id }
        | FavoriteMenuItem::Command { id, .. }
        | FavoriteMenuItem::DynamicGroup { id, .. } => *id,
    }
}

fn count_items(items: &[FavoriteMenuItem]) -> usize {
    items
        .iter()
        .map(|item| match item {
            FavoriteMenuItem::Submenu { items, .. } => 1 + count_items(items),
            _ => 1,
        })
        .sum()
}

fn menu_references_command(items: &[FavoriteMenuItem], command_id: Uuid) -> bool {
    items.iter().any(|item| match item {
        FavoriteMenuItem::Command {
            command_id: candidate,
            ..
        } => *candidate == command_id,
        FavoriteMenuItem::Submenu { items, .. } => menu_references_command(items, command_id),
        _ => false,
    })
}

fn collect_command_ids(items: &[FavoriteMenuItem], out: &mut HashSet<Uuid>) {
    for item in items {
        match item {
            FavoriteMenuItem::Command { command_id, .. } => {
                out.insert(*command_id);
            }
            FavoriteMenuItem::Submenu { items, .. } => collect_command_ids(items, out),
            _ => {}
        }
    }
}

fn map_item_paths<F>(items: &mut [FavoriteMenuItem], mapper: &mut F) -> Result<()>
where
    F: FnMut(&str) -> Result<String>,
{
    for item in items {
        match item {
            FavoriteMenuItem::Folder { path, .. } => *path = mapper(path)?,
            FavoriteMenuItem::Submenu { items, .. } => map_item_paths(items, mapper)?,
            _ => {}
        }
    }
    Ok(())
}

fn folder_title(path: &str) -> String {
    Path::new(path)
        .file_name()
        .map(|value| value.to_string_lossy().into_owned())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| path.to_string())
}

fn path_key(path: &str) -> String {
    comparable_path(path.trim_end_matches(['/', '\\']))
}

fn default_dynamic_limit() -> usize {
    10
}

fn validate_favorites_file(mut state: FavoritesFile) -> Option<FavoritesFile> {
    if !matches!(
        state.schema_version,
        FAVORITES_SCHEMA_VERSION_V1 | FAVORITES_SCHEMA_VERSION
    ) {
        return None;
    }
    state.menu = canonicalize_favorite_menu(state.menu).ok()?;
    state.schema_version = FAVORITES_SCHEMA_VERSION;
    Some(state)
}

fn persist_favorites_file(path: &Path, state: &FavoritesFile) -> Result<()> {
    let bytes = serde_json::to_vec_pretty(state)?;
    let temp = path.with_extension("json.tmp");
    let backup = path.with_extension("json.bak");
    fs::write(&temp, bytes).map_err(|source| CoreError::Filesystem {
        path: temp.clone(),
        source,
    })?;
    if path.exists() {
        fs::copy(path, &backup).map_err(|source| CoreError::Filesystem {
            path: backup.clone(),
            source,
        })?;
        fs::remove_file(path).map_err(|source| CoreError::Filesystem {
            path: path.to_path_buf(),
            source,
        })?;
    }
    fs::rename(&temp, path).map_err(|source| CoreError::Filesystem {
        path: path.to_path_buf(),
        source,
    })?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn folder(path: &str) -> FavoriteMenuItem {
        FavoriteMenuItem::Folder {
            id: Uuid::new_v4(),
            title: None,
            path: path.into(),
        }
    }

    #[test]
    fn validates_tree_and_recovers_from_backup() {
        let state = tempfile::TempDir::new().unwrap();
        let mut store = FavoritesStore::open(state.path()).unwrap();
        let command_id = Uuid::new_v4();
        let menu = FavoriteMenu {
            items: vec![
                folder("/Users/test/Scenes"),
                FavoriteMenuItem::Submenu {
                    id: Uuid::new_v4(),
                    title: "Tools".into(),
                    items: vec![FavoriteMenuItem::Command {
                        id: Uuid::new_v4(),
                        command_id,
                    }],
                },
            ],
            pins: Vec::new(),
        };
        store.put(menu.clone()).unwrap();
        let mut replacement = menu.clone();
        replacement
            .items
            .push(FavoriteMenuItem::Separator { id: Uuid::new_v4() });
        store.put(replacement).unwrap();

        let settings = state.path().join("settings");
        let path = settings.join("favorites.json");
        let backup = settings.join("favorites.json.bak");
        let backup_before = fs::read(&backup).unwrap();
        fs::write(&path, b"{broken").unwrap();
        let recovered = FavoritesStore::open(state.path()).unwrap();
        assert_eq!(recovered.get(), menu);
        assert!(recovered.references_command(command_id));
        assert_eq!(fs::read(&backup).unwrap(), backup_before);
        assert!(
            validate_favorites_file(serde_json::from_slice(&fs::read(&path).unwrap()).unwrap())
                .is_some()
        );

        let duplicate_id = Uuid::new_v4();
        let invalid = FavoriteMenu {
            items: vec![
                FavoriteMenuItem::Separator { id: duplicate_id },
                FavoriteMenuItem::Separator { id: duplicate_id },
            ],
            pins: Vec::new(),
        };
        assert!(canonicalize_favorite_menu(invalid).is_err());
    }

    #[test]
    fn favorites_file_future_schema_and_all_corrupt_fail_closed() {
        let future_state = tempfile::TempDir::new().unwrap();
        let settings = future_state.path().join("settings");
        fs::create_dir_all(&settings).unwrap();
        let primary = settings.join("favorites.json");
        let backup = settings.join("favorites.json.bak");
        let temp = settings.join("favorites.json.tmp");
        let valid = serde_json::to_vec_pretty(&FavoritesFile::default()).unwrap();
        let future = serde_json::to_vec_pretty(&FavoritesFile {
            schema_version: FAVORITES_SCHEMA_VERSION + 1,
            menu: FavoriteMenu::default(),
        })
        .unwrap();
        fs::write(&primary, &future).unwrap();
        fs::write(&backup, &valid).unwrap();
        fs::write(&temp, &valid).unwrap();
        let error = FavoritesStore::open(future_state.path()).unwrap_err();
        assert!(error.to_string().contains("upgrade the application"));
        assert_eq!(fs::read(&primary).unwrap(), future);
        assert_eq!(fs::read(&backup).unwrap(), valid);
        assert_eq!(fs::read(&temp).unwrap(), valid);

        let corrupt_state = tempfile::TempDir::new().unwrap();
        let settings = corrupt_state.path().join("settings");
        fs::create_dir_all(&settings).unwrap();
        let primary = settings.join("favorites.json");
        let backup = settings.join("favorites.json.bak");
        let temp = settings.join("favorites.json.tmp");
        fs::write(&primary, b"primary-corrupt").unwrap();
        fs::write(&backup, b"backup-corrupt").unwrap();
        fs::write(&temp, b"temp-corrupt").unwrap();
        assert!(FavoritesStore::open(corrupt_state.path()).is_err());
        assert_eq!(fs::read(&primary).unwrap(), b"primary-corrupt");
        assert_eq!(fs::read(&backup).unwrap(), b"backup-corrupt");
        assert_eq!(fs::read(&temp).unwrap(), b"temp-corrupt");
    }

    #[test]
    fn legacy_schema_one_loads_and_pin_write_advances_schema() {
        let state = tempfile::TempDir::new().unwrap();
        let settings = state.path().join("settings");
        fs::create_dir_all(&settings).unwrap();
        let primary = settings.join("favorites.json");
        fs::write(
            &primary,
            serde_json::to_vec_pretty(&serde_json::json!({
                "schema_version": 1,
                "menu": {
                    "items": [
                        {
                            "type": "folder",
                            "id": Uuid::new_v4(),
                            "title": "Legacy",
                            "path": "/Users/test/Legacy"
                        }
                    ]
                }
            }))
            .unwrap(),
        )
        .unwrap();

        let mut store = FavoritesStore::open(state.path()).unwrap();
        let mut menu = store.get();
        assert_eq!(menu.items.len(), 1);
        assert!(menu.pins.is_empty());
        assert!(
            toggle_favorite_pin(&mut menu, "/Users/test/Scene.mov", Some("Scene".into())).unwrap()
        );
        store.put(menu).unwrap();

        let written: serde_json::Value =
            serde_json::from_slice(&fs::read(primary).unwrap()).unwrap();
        assert_eq!(written["schema_version"], FAVORITES_SCHEMA_VERSION);
        assert_eq!(written["menu"]["pins"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn resolves_dynamic_groups_without_owning_history_or_finder_state() {
        let state = tempfile::TempDir::new().unwrap();
        let mut store = FavoritesStore::open(state.path()).unwrap();
        let command_id = Uuid::new_v4();
        store
            .put(FavoriteMenu {
                items: vec![
                    FavoriteMenuItem::DynamicGroup {
                        id: Uuid::new_v4(),
                        title: Some("Recent".into()),
                        kind: FavoriteDynamicGroupKind::RecentFolders,
                        limit: 2,
                    },
                    FavoriteMenuItem::DynamicGroup {
                        id: Uuid::new_v4(),
                        title: None,
                        kind: FavoriteDynamicGroupKind::OpenFolders,
                        limit: 2,
                    },
                    FavoriteMenuItem::Command {
                        id: Uuid::new_v4(),
                        command_id,
                    },
                ],
                pins: Vec::new(),
            })
            .unwrap();

        let recent = [
            RecentFolderCandidate {
                item: ItemRecord {
                    id: 1,
                    root_id: Uuid::new_v4(),
                    path: "/Users/test/A".into(),
                    parent_path: "/Users/test".into(),
                    name: "A".into(),
                    extension: String::new(),
                    kind: crate::model::ItemKind::Folder,
                    size: 0,
                    modified_unix_ms: 0,
                    available: true,
                    priority: crate::model::Priority::Normal,
                    stable_file_id: None,
                    preview_hint: None,
                },
            },
            RecentFolderCandidate {
                item: ItemRecord {
                    id: 2,
                    root_id: Uuid::new_v4(),
                    path: "/Users/test/B".into(),
                    parent_path: "/Users/test".into(),
                    name: "B".into(),
                    extension: String::new(),
                    kind: crate::model::ItemKind::Folder,
                    size: 0,
                    modified_unix_ms: 0,
                    available: true,
                    priority: crate::model::Priority::Normal,
                    stable_file_id: None,
                    preview_hint: None,
                },
            },
        ];
        let open = [
            OpenFolderCandidate {
                path: "/Users/test/Open".into(),
                title: Some("Open".into()),
            },
            OpenFolderCandidate {
                path: "/Users/test/Open".into(),
                title: None,
            },
        ];
        let resolved = store
            .resolve(&recent, &open, |id| {
                (id == command_id).then(|| "Run Tool".to_string())
            })
            .unwrap();
        assert_eq!(resolved.items.len(), 3);
        let ResolvedFavoriteMenuItem::Submenu { items, .. } = &resolved.items[0] else {
            panic!("recent group should resolve to submenu");
        };
        assert_eq!(items.len(), 2);
        assert!(matches!(
            resolved.items[1],
            ResolvedFavoriteMenuItem::Folder {
                source: FavoriteFolderSource::OpenFolder,
                ..
            }
        ));
        assert!(matches!(
            resolved.items[2],
            ResolvedFavoriteMenuItem::Command { ref title, .. } if title == "Run Tool"
        ));
    }
}
