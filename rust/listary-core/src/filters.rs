use crate::{
    error::{CoreError, Result},
    history::durable_json,
    model::ItemKind,
    text::normalize,
};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashSet,
    fs,
    path::{Path, PathBuf},
};
use uuid::Uuid;

const FILTER_SCHEMA_VERSION: u32 = 1;
const MAX_FILTERS: usize = 256;
const MAX_KEYWORD_CHARS: usize = 64;
const MAX_NAME_CHARS: usize = 160;
const MAX_EXTRA_QUERY_CHARS: usize = 2_048;
const MAX_SEARCH_PATHS: usize = 64;
const MAX_EXTENSIONS: usize = 64;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum FilterTarget {
    #[default]
    Any,
    Files,
    Folders,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FilterPreset {
    pub id: Uuid,
    pub keyword: String,
    pub name: String,
    #[serde(default)]
    pub extra_query: String,
    #[serde(default)]
    pub search_paths: Vec<String>,
    #[serde(default)]
    pub target: FilterTarget,
    #[serde(default)]
    pub extensions: Vec<String>,
}

impl FilterPreset {
    pub fn new(keyword: impl Into<String>, name: impl Into<String>) -> Self {
        Self {
            id: Uuid::new_v4(),
            keyword: keyword.into(),
            name: name.into(),
            extra_query: String::new(),
            search_paths: Vec::new(),
            target: FilterTarget::Any,
            extensions: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct FilterFile {
    schema_version: u32,
    presets: Vec<FilterPreset>,
}

impl Default for FilterFile {
    fn default() -> Self {
        Self {
            schema_version: FILTER_SCHEMA_VERSION,
            presets: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, Default)]
pub(crate) struct FilterResolution {
    pub(crate) expanded_query: String,
    pub(crate) path_groups: Vec<Vec<String>>,
    pub(crate) extension_groups: Vec<Vec<String>>,
    pub(crate) allowed_kinds: Option<Vec<ItemKind>>,
}

#[derive(Debug)]
pub(crate) struct FilterStore {
    path: PathBuf,
    state: FilterFile,
}

impl FilterStore {
    pub(crate) fn open(state_dir: &Path) -> Result<Self> {
        let settings_dir = state_dir.join("settings");
        fs::create_dir_all(&settings_dir).map_err(|source| CoreError::Filesystem {
            path: settings_dir.clone(),
            source,
        })?;

        let path = settings_dir.join("filters.json");
        let backup = settings_dir.join("filters.json.bak");
        let temp = settings_dir.join("filters.json.tmp");
        let state = durable_json::open(
            &path,
            &backup,
            &temp,
            FILTER_SCHEMA_VERSION,
            "saved filters",
            validate_filter_file,
        )?;

        Ok(Self { path, state })
    }

    pub(crate) fn list(&self) -> Vec<FilterPreset> {
        let mut presets = self.state.presets.clone();
        presets.sort_by(|a, b| {
            a.keyword
                .cmp(&b.keyword)
                .then_with(|| a.name.cmp(&b.name))
                .then_with(|| a.id.cmp(&b.id))
        });
        presets
    }

    pub(crate) fn put(&mut self, preset: FilterPreset) -> Result<FilterPreset> {
        let preset = canonicalize_preset(preset)?;
        if self
            .state
            .presets
            .iter()
            .any(|other| other.id != preset.id && other.keyword == preset.keyword)
        {
            return Err(CoreError::InvalidRequest(format!(
                "filter keyword already exists: {}",
                preset.keyword
            )));
        }

        let mut next = self.state.clone();
        if let Some(existing) = next.presets.iter_mut().find(|item| item.id == preset.id) {
            *existing = preset.clone();
        } else {
            if next.presets.len() >= MAX_FILTERS {
                return Err(CoreError::InvalidRequest(format!(
                    "too many saved filters; maximum is {MAX_FILTERS}"
                )));
            }
            next.presets.push(preset.clone());
        }
        persist_filter_file(&self.path, &next)?;
        self.state = next;
        Ok(preset)
    }

    pub(crate) fn remove(&mut self, filter_id: Uuid) -> Result<()> {
        let mut next = self.state.clone();
        let before = next.presets.len();
        next.presets.retain(|preset| preset.id != filter_id);
        if next.presets.len() == before {
            return Err(CoreError::FilterNotFound(filter_id.to_string()));
        }
        persist_filter_file(&self.path, &next)?;
        self.state = next;
        Ok(())
    }

    pub(crate) fn resolve_query(&self, query: &str) -> FilterResolution {
        let mut resolution = FilterResolution::default();
        let mut remaining = Vec::new();
        let mut extra_queries = Vec::new();

        for token in query_tokens(query) {
            let Some(keyword) = activation_keyword(token.raw, token.contains_quote) else {
                remaining.push(token.raw);
                continue;
            };

            if keyword == "folder" {
                apply_kind_constraint(&mut resolution.allowed_kinds, &[ItemKind::Folder]);
                continue;
            }
            if keyword == "file" {
                apply_kind_constraint(&mut resolution.allowed_kinds, &[ItemKind::File]);
                continue;
            }

            let Some(preset) = self
                .state
                .presets
                .iter()
                .find(|preset| preset.keyword == keyword)
            else {
                remaining.push(token.raw);
                continue;
            };

            if !preset.extra_query.is_empty() {
                extra_queries.push(preset.extra_query.as_str());
            }
            if !preset.search_paths.is_empty() {
                resolution.path_groups.push(preset.search_paths.clone());
            }
            if !preset.extensions.is_empty() {
                resolution.extension_groups.push(preset.extensions.clone());
            }
            match preset.target {
                FilterTarget::Any => {}
                FilterTarget::Files => {
                    apply_kind_constraint(&mut resolution.allowed_kinds, &[ItemKind::File]);
                }
                FilterTarget::Folders => {
                    apply_kind_constraint(&mut resolution.allowed_kinds, &[ItemKind::Folder]);
                }
            }
        }

        remaining.extend(extra_queries);
        resolution.expanded_query = remaining.join(" ");
        resolution
    }
}

#[derive(Debug, Clone, Copy)]
struct RawToken<'a> {
    raw: &'a str,
    contains_quote: bool,
}

fn query_tokens(input: &str) -> Vec<RawToken<'_>> {
    let mut tokens = Vec::new();
    let mut start = None;
    let mut in_quote = false;
    let mut escaped = false;
    let mut contains_quote = false;

    for (index, ch) in input.char_indices() {
        if start.is_none() {
            if ch.is_whitespace() {
                continue;
            }
            start = Some(index);
            contains_quote = false;
        }

        if in_quote {
            if escaped {
                escaped = false;
            } else if ch == '\\' {
                escaped = true;
            } else if ch == '"' {
                contains_quote = true;
                in_quote = false;
            }
            continue;
        }

        if ch == '"' {
            contains_quote = true;
            in_quote = true;
            continue;
        }
        if ch.is_whitespace() {
            let token_start = start.take().expect("token start exists");
            tokens.push(RawToken {
                raw: &input[token_start..index],
                contains_quote,
            });
        }
    }

    if let Some(token_start) = start {
        tokens.push(RawToken {
            raw: &input[token_start..],
            contains_quote,
        });
    }
    tokens
}

fn activation_keyword(raw: &str, contains_quote: bool) -> Option<String> {
    if contains_quote {
        return None;
    }
    let keyword = raw.strip_suffix(':').or_else(|| raw.strip_suffix('：'))?;
    if keyword.is_empty() || keyword.contains([':', '：']) {
        return None;
    }
    Some(normalize(keyword))
}

fn apply_kind_constraint(current: &mut Option<Vec<ItemKind>>, allowed: &[ItemKind]) {
    match current {
        Some(existing) => existing.retain(|kind| allowed.contains(kind)),
        None => *current = Some(allowed.to_vec()),
    }
}

pub(crate) fn canonicalize_preset(mut preset: FilterPreset) -> Result<FilterPreset> {
    let keyword_raw = preset.keyword.trim();
    if keyword_raw.is_empty() {
        return Err(CoreError::InvalidRequest("filter keyword is empty".into()));
    }
    if keyword_raw.chars().count() > MAX_KEYWORD_CHARS {
        return Err(CoreError::InvalidRequest(format!(
            "filter keyword is longer than {MAX_KEYWORD_CHARS} characters"
        )));
    }
    if keyword_raw
        .chars()
        .any(|ch| ch.is_whitespace() || matches!(ch, ':' | '：' | '"' | '\''))
    {
        return Err(CoreError::InvalidRequest(
            "filter keyword cannot contain whitespace, quotes, or a colon".into(),
        ));
    }
    let keyword = normalize(keyword_raw);
    if matches!(
        keyword.as_str(),
        "ext" | "date" | "size" | "folder" | "file" | "doc" | "pic" | "video" | "audio"
    ) {
        return Err(CoreError::InvalidRequest(format!(
            "filter keyword is reserved: {keyword}"
        )));
    }
    preset.keyword = keyword;

    preset.name = preset.name.trim().to_string();
    if preset.name.is_empty() {
        return Err(CoreError::InvalidRequest("filter name is empty".into()));
    }
    if preset.name.chars().count() > MAX_NAME_CHARS {
        return Err(CoreError::InvalidRequest(format!(
            "filter name is longer than {MAX_NAME_CHARS} characters"
        )));
    }

    preset.extra_query = preset.extra_query.trim().to_string();
    if preset.extra_query.chars().count() > MAX_EXTRA_QUERY_CHARS {
        return Err(CoreError::InvalidRequest(format!(
            "filter extra query is longer than {MAX_EXTRA_QUERY_CHARS} characters"
        )));
    }

    if preset.search_paths.len() > MAX_SEARCH_PATHS {
        return Err(CoreError::InvalidRequest(format!(
            "filter has more than {MAX_SEARCH_PATHS} search paths"
        )));
    }
    let mut path_keys = HashSet::new();
    let mut search_paths = Vec::new();
    for path in preset.search_paths {
        let path = path.trim().to_string();
        if path.is_empty() {
            continue;
        }
        let key = normalize(path.trim_end_matches(['/', '\\']));
        if path_keys.insert(key) {
            search_paths.push(path);
        }
    }
    preset.search_paths = search_paths;

    let mut extensions = Vec::new();
    let mut extension_keys = HashSet::new();
    for raw in preset.extensions {
        for value in raw.split([';', ',']) {
            let value = value
                .trim()
                .trim_start_matches("*.")
                .trim_start_matches('.');
            if value.is_empty() {
                continue;
            }
            if value
                .chars()
                .any(|ch| ch.is_whitespace() || matches!(ch, '/' | '\\' | ':' | '：' | ';' | ','))
            {
                return Err(CoreError::InvalidRequest(format!(
                    "invalid filter extension: {value}"
                )));
            }
            let value = normalize(value);
            if extension_keys.insert(value.clone()) {
                extensions.push(value);
            }
        }
    }
    if extensions.len() > MAX_EXTENSIONS {
        return Err(CoreError::InvalidRequest(format!(
            "filter has more than {MAX_EXTENSIONS} extensions"
        )));
    }
    if preset.target == FilterTarget::Folders && !extensions.is_empty() {
        return Err(CoreError::InvalidRequest(
            "folder-only filters cannot also restrict file extensions".into(),
        ));
    }
    preset.extensions = extensions;
    Ok(preset)
}

fn validate_filter_file(mut state: FilterFile) -> Option<FilterFile> {
    if state.schema_version != FILTER_SCHEMA_VERSION || state.presets.len() > MAX_FILTERS {
        return None;
    }

    let mut ids = HashSet::new();
    let mut keywords = HashSet::new();
    for preset in &mut state.presets {
        let canonical = canonicalize_preset(preset.clone()).ok()?;
        if !ids.insert(canonical.id) || !keywords.insert(canonical.keyword.clone()) {
            return None;
        }
        *preset = canonical;
    }
    Some(state)
}

fn persist_filter_file(path: &Path, state: &FilterFile) -> Result<()> {
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
    use tempfile::TempDir;

    #[test]
    fn resolves_filter_tokens_without_consuming_unknown_or_quoted_tokens() {
        let state = TempDir::new().unwrap();
        let mut store = FilterStore::open(state.path()).unwrap();
        let mut preset = FilterPreset::new("work", "Work files");
        preset.extra_query = "Project A".into();
        preset.search_paths = vec!["/Users/example/Work".into()];
        preset.target = FilterTarget::Files;
        preset.extensions = vec!["PDF;docx".into()];
        store.put(preset).unwrap();

        let resolved = store.resolve_query(r#"before work: unknown: \"work:\" after"#);
        assert_eq!(
            resolved.expanded_query,
            r#"before unknown: \"work:\" after Project A"#
        );
        assert_eq!(resolved.path_groups.len(), 1);
        assert_eq!(resolved.extension_groups[0], vec!["pdf", "docx"]);
        assert_eq!(resolved.allowed_kinds, Some(vec![ItemKind::File]));
    }

    #[test]
    fn built_in_file_and_folder_filters_are_kind_constraints() {
        let state = TempDir::new().unwrap();
        let store = FilterStore::open(state.path()).unwrap();

        let folders = store.resolve_query("folder: project");
        assert_eq!(folders.expanded_query, "project");
        assert_eq!(folders.allowed_kinds, Some(vec![ItemKind::Folder]));

        let impossible = store.resolve_query("folder: file: project");
        assert_eq!(impossible.allowed_kinds, Some(Vec::new()));
    }

    #[test]
    fn filter_file_recovers_from_backup_if_primary_is_corrupt() {
        let state = TempDir::new().unwrap();
        let filter_id;
        {
            let mut store = FilterStore::open(state.path()).unwrap();
            let first = store.put(FilterPreset::new("first", "First")).unwrap();
            filter_id = first.id;
            let mut updated = first;
            updated.name = "Updated".into();
            store.put(updated).unwrap();
        }

        let settings = state.path().join("settings");
        let path = settings.join("filters.json");
        let backup = settings.join("filters.json.bak");
        let backup_before = fs::read(&backup).unwrap();
        fs::write(&path, b"{broken").unwrap();
        let recovered = FilterStore::open(state.path()).unwrap().list();
        assert_eq!(recovered.len(), 1);
        assert_eq!(recovered[0].id, filter_id);
        assert_eq!(recovered[0].name, "First");
        assert_eq!(fs::read(&backup).unwrap(), backup_before);
        assert!(
            validate_filter_file(serde_json::from_slice(&fs::read(&path).unwrap()).unwrap())
                .is_some()
        );
    }

    #[test]
    fn filter_file_future_schema_and_all_corrupt_fail_closed() {
        let future_state = TempDir::new().unwrap();
        let settings = future_state.path().join("settings");
        fs::create_dir_all(&settings).unwrap();
        let primary = settings.join("filters.json");
        let backup = settings.join("filters.json.bak");
        let temp = settings.join("filters.json.tmp");
        let valid = serde_json::to_vec_pretty(&FilterFile::default()).unwrap();
        let future = serde_json::to_vec_pretty(&FilterFile {
            schema_version: FILTER_SCHEMA_VERSION + 1,
            presets: Vec::new(),
        })
        .unwrap();
        fs::write(&primary, &future).unwrap();
        fs::write(&backup, &valid).unwrap();
        fs::write(&temp, &valid).unwrap();
        let error = FilterStore::open(future_state.path()).unwrap_err();
        assert!(error.to_string().contains("upgrade the application"));
        assert_eq!(fs::read(&primary).unwrap(), future);
        assert_eq!(fs::read(&backup).unwrap(), valid);
        assert_eq!(fs::read(&temp).unwrap(), valid);

        let corrupt_state = TempDir::new().unwrap();
        let settings = corrupt_state.path().join("settings");
        fs::create_dir_all(&settings).unwrap();
        let primary = settings.join("filters.json");
        let backup = settings.join("filters.json.bak");
        let temp = settings.join("filters.json.tmp");
        fs::write(&primary, b"primary-corrupt").unwrap();
        fs::write(&backup, b"backup-corrupt").unwrap();
        fs::write(&temp, b"temp-corrupt").unwrap();
        assert!(FilterStore::open(corrupt_state.path()).is_err());
        assert_eq!(fs::read(&primary).unwrap(), b"primary-corrupt");
        assert_eq!(fs::read(&backup).unwrap(), b"backup-corrupt");
        assert_eq!(fs::read(&temp).unwrap(), b"temp-corrupt");
    }
}
