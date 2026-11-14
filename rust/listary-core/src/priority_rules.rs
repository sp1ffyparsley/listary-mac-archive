use crate::{
    cache::{CachedItem, CatalogCache},
    error::{CoreError, Result},
    history::durable_json,
    model::{ItemKind, ItemRecord, Priority},
    text::comparable_path,
};
use regex::{Regex, RegexBuilder};
use roaring::RoaringBitmap;
use serde::{Deserialize, Serialize};
use std::{
    collections::HashSet,
    fs,
    path::{Path, PathBuf},
};
use uuid::Uuid;

const PRIORITY_RULE_SCHEMA_VERSION: u32 = 1;
const MAX_PRIORITY_RULES: usize = 512;
const MAX_PATH_CHARS: usize = 4_096;
const MAX_REGEX_CHARS: usize = 2_048;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum PriorityRuleMatch {
    Path {
        path: String,
        #[serde(default)]
        descendants: bool,
    },
    Regex {
        pattern: String,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum PriorityRuleTarget {
    #[default]
    Any,
    Files,
    Folders,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PriorityRule {
    pub id: Uuid,
    pub matcher: PriorityRuleMatch,
    #[serde(default)]
    pub target: PriorityRuleTarget,
    pub priority: Priority,
    #[serde(default = "default_true")]
    pub enabled: bool,
}

impl PriorityRule {
    pub fn path(path: impl Into<String>, descendants: bool, priority: Priority) -> Self {
        Self {
            id: Uuid::new_v4(),
            matcher: PriorityRuleMatch::Path {
                path: path.into(),
                descendants,
            },
            target: PriorityRuleTarget::Any,
            priority,
            enabled: true,
        }
    }

    pub fn regex(pattern: impl Into<String>, priority: Priority) -> Self {
        Self {
            id: Uuid::new_v4(),
            matcher: PriorityRuleMatch::Regex {
                pattern: pattern.into(),
            },
            target: PriorityRuleTarget::Any,
            priority,
            enabled: true,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct PriorityRuleFile {
    schema_version: u32,
    rules: Vec<PriorityRule>,
}

impl Default for PriorityRuleFile {
    fn default() -> Self {
        Self {
            schema_version: PRIORITY_RULE_SCHEMA_VERSION,
            rules: Vec::new(),
        }
    }
}

#[derive(Debug, Clone)]
enum CompiledMatcher {
    Path {
        comparable: String,
        descendants: bool,
        depth: usize,
    },
    Regex(Regex),
}

#[derive(Debug, Clone)]
struct CompiledRule {
    rule: PriorityRule,
    matcher: CompiledMatcher,
    order: usize,
}

#[derive(Debug)]
pub(crate) struct PriorityRuleStore {
    path: PathBuf,
    state: PriorityRuleFile,
    compiled: Vec<CompiledRule>,
    overrides: PriorityOverrideIndex,
}

#[derive(Debug, Default)]
struct PriorityOverrideIndex {
    ignored: RoaringBitmap,
    low: RoaringBitmap,
    normal: RoaringBitmap,
    high: RoaringBitmap,
}

impl PriorityOverrideIndex {
    fn clear_id(&mut self, id: u32) {
        self.ignored.remove(id);
        self.low.remove(id);
        self.normal.remove(id);
        self.high.remove(id);
    }

    fn insert(&mut self, id: u32, priority: Priority) {
        self.clear_id(id);
        match priority {
            Priority::Ignored => self.ignored.insert(id),
            Priority::Low => self.low.insert(id),
            Priority::Normal => self.normal.insert(id),
            Priority::High => self.high.insert(id),
        };
    }

    fn get(&self, id: u32) -> Option<Priority> {
        if self.ignored.contains(id) {
            Some(Priority::Ignored)
        } else if self.low.contains(id) {
            Some(Priority::Low)
        } else if self.normal.contains(id) {
            Some(Priority::Normal)
        } else if self.high.contains(id) {
            Some(Priority::High)
        } else {
            None
        }
    }
}

impl PriorityRuleStore {
    pub(crate) fn open(state_dir: &Path) -> Result<Self> {
        let settings_dir = state_dir.join("settings");
        fs::create_dir_all(&settings_dir).map_err(|source| CoreError::Filesystem {
            path: settings_dir.clone(),
            source,
        })?;
        let path = settings_dir.join("priority-rules.json");
        let backup = settings_dir.join("priority-rules.json.bak");
        let temp = settings_dir.join("priority-rules.json.tmp");
        let state = durable_json::open(
            &path,
            &backup,
            &temp,
            PRIORITY_RULE_SCHEMA_VERSION,
            "priority rules",
            validate_rule_file,
        )?;
        let (_, compiled) = canonicalize_rules(state.rules.clone())?;
        Ok(Self {
            path,
            state,
            compiled,
            overrides: PriorityOverrideIndex::default(),
        })
    }

    pub(crate) fn list(&self) -> Vec<PriorityRule> {
        self.state.rules.clone()
    }

    pub(crate) fn replace(&mut self, rules: Vec<PriorityRule>) -> Result<Vec<PriorityRule>> {
        let (rules, compiled) = canonicalize_rules(rules)?;
        let next = PriorityRuleFile {
            schema_version: PRIORITY_RULE_SCHEMA_VERSION,
            rules: rules.clone(),
        };
        persist_rule_file(&self.path, &next)?;
        self.state = next;
        self.compiled = compiled;
        self.overrides = PriorityOverrideIndex::default();
        Ok(rules)
    }

    pub(crate) fn effective_priority_cached(&self, item: &CachedItem) -> Priority {
        self.overrides.get(item.id).unwrap_or(item.priority)
    }

    pub(crate) fn effective_priority_record(&self, item: &ItemRecord) -> Priority {
        self.resolve(&item.path, item.kind, item.priority)
    }

    pub(crate) fn high_candidate_ids(&self, candidate_ids: &RoaringBitmap) -> RoaringBitmap {
        &self.overrides.high & candidate_ids
    }

    pub(crate) fn ignored_candidate_ids(&self, candidate_ids: &RoaringBitmap) -> RoaringBitmap {
        &self.overrides.ignored & candidate_ids
    }

    pub(crate) fn rebuild_overrides(&mut self, cache: &CatalogCache) {
        self.overrides = PriorityOverrideIndex::default();
        if self.compiled.is_empty() {
            return;
        }
        for id in cache.all_ids().iter() {
            let Some(item) = cache.get_cached(id) else {
                continue;
            };
            if let Some(priority) = self.matched_priority(&item.path, item.kind) {
                self.overrides.insert(id, priority);
            }
        }
    }

    pub(crate) fn refresh_record(&mut self, item: &ItemRecord) {
        self.overrides.clear_id(item.id);
        if let Some(priority) = self.matched_priority(&item.path, item.kind) {
            self.overrides.insert(item.id, priority);
        }
    }

    pub(crate) fn remove_id(&mut self, id: u32) {
        self.overrides.clear_id(id);
    }

    fn resolve(&self, path: &str, kind: ItemKind, fallback: Priority) -> Priority {
        self.matched_priority(path, kind).unwrap_or(fallback)
    }

    fn matched_priority(&self, path: &str, kind: ItemKind) -> Option<Priority> {
        if self.compiled.is_empty() {
            return None;
        }
        let candidate = comparable_path(path);
        let mut best: Option<((u8, usize, usize), usize, Priority)> = None;
        for compiled in &self.compiled {
            if !compiled.rule.enabled || !target_matches(compiled.rule.target, kind) {
                continue;
            }
            let specificity = match &compiled.matcher {
                CompiledMatcher::Path {
                    comparable,
                    descendants,
                    depth,
                } => {
                    if *descendants {
                        if !same_or_descendant(&candidate, comparable) {
                            continue;
                        }
                        (2, *depth, comparable.len())
                    } else {
                        if candidate != *comparable {
                            continue;
                        }
                        (3, *depth, comparable.len())
                    }
                }
                CompiledMatcher::Regex(regex) => {
                    if !regex.is_match(&candidate) {
                        continue;
                    }
                    (1, 0, 0)
                }
            };
            match best {
                None => best = Some((specificity, compiled.order, compiled.rule.priority)),
                Some((best_specificity, best_order, _))
                    if specificity > best_specificity
                        || (specificity == best_specificity && compiled.order < best_order) =>
                {
                    best = Some((specificity, compiled.order, compiled.rule.priority));
                }
                _ => {}
            }
        }
        best.map(|(_, _, priority)| priority)
    }
}

pub(crate) fn canonicalize_priority_rules(rules: Vec<PriorityRule>) -> Result<Vec<PriorityRule>> {
    canonicalize_rules(rules).map(|(rules, _)| rules)
}

fn canonicalize_rules(rules: Vec<PriorityRule>) -> Result<(Vec<PriorityRule>, Vec<CompiledRule>)> {
    if rules.len() > MAX_PRIORITY_RULES {
        return Err(CoreError::InvalidRequest(format!(
            "too many priority rules; maximum is {MAX_PRIORITY_RULES}"
        )));
    }
    let mut ids = HashSet::new();
    let mut canonical = Vec::with_capacity(rules.len());
    let mut compiled = Vec::with_capacity(rules.len());
    for (order, mut rule) in rules.into_iter().enumerate() {
        if !ids.insert(rule.id) {
            return Err(CoreError::InvalidRequest(format!(
                "duplicate priority rule id: {}",
                rule.id
            )));
        }
        let matcher = match &mut rule.matcher {
            PriorityRuleMatch::Path { path, descendants } => {
                *path = path.trim().to_string();
                if path.is_empty() {
                    return Err(CoreError::InvalidRequest(
                        "priority rule path is empty".into(),
                    ));
                }
                if path.chars().count() > MAX_PATH_CHARS {
                    return Err(CoreError::InvalidRequest(format!(
                        "priority rule path exceeds {MAX_PATH_CHARS} characters"
                    )));
                }
                let comparable = comparable_path(path);
                let depth = comparable
                    .split('/')
                    .filter(|component| !component.is_empty())
                    .count();
                CompiledMatcher::Path {
                    comparable,
                    descendants: *descendants,
                    depth,
                }
            }
            PriorityRuleMatch::Regex { pattern } => {
                *pattern = pattern.trim().to_string();
                if pattern.is_empty() {
                    return Err(CoreError::InvalidRequest(
                        "priority rule regex is empty".into(),
                    ));
                }
                if pattern.chars().count() > MAX_REGEX_CHARS {
                    return Err(CoreError::InvalidRequest(format!(
                        "priority rule regex exceeds {MAX_REGEX_CHARS} characters"
                    )));
                }
                let regex = RegexBuilder::new(pattern)
                    .case_insensitive(true)
                    .unicode(true)
                    .size_limit(2 * 1024 * 1024)
                    .dfa_size_limit(4 * 1024 * 1024)
                    .build()
                    .map_err(|error| {
                        CoreError::InvalidRequest(format!(
                            "invalid priority rule regex `{pattern}`: {error}"
                        ))
                    })?;
                CompiledMatcher::Regex(regex)
            }
        };
        canonical.push(rule.clone());
        compiled.push(CompiledRule {
            rule,
            matcher,
            order,
        });
    }
    Ok((canonical, compiled))
}

fn target_matches(target: PriorityRuleTarget, kind: ItemKind) -> bool {
    match target {
        PriorityRuleTarget::Any => true,
        PriorityRuleTarget::Files => kind == ItemKind::File,
        PriorityRuleTarget::Folders => kind == ItemKind::Folder,
    }
}

fn same_or_descendant(candidate: &str, prefix: &str) -> bool {
    candidate == prefix
        || candidate
            .strip_prefix(prefix)
            .is_some_and(|rest| rest.starts_with('/'))
}

fn default_true() -> bool {
    true
}

fn validate_rule_file(mut state: PriorityRuleFile) -> Option<PriorityRuleFile> {
    if state.schema_version != PRIORITY_RULE_SCHEMA_VERSION {
        return None;
    }
    let (rules, _) = canonicalize_rules(state.rules).ok()?;
    state.rules = rules;
    Some(state)
}

fn persist_rule_file(path: &Path, state: &PriorityRuleFile) -> Result<()> {
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

    fn item(path: &str, kind: ItemKind, priority: Priority) -> ItemRecord {
        ItemRecord::from_path_metadata(1, Uuid::nil(), Path::new(path), kind, 0, 0, priority)
    }

    #[test]
    fn exact_beats_nested_and_nested_beats_regex() {
        let state = TempDir::new().unwrap();
        let mut store = PriorityRuleStore::open(state.path()).unwrap();
        store
            .replace(vec![
                PriorityRule::regex(r"/project/.+\.tmp$", Priority::Low),
                PriorityRule::path("X:/project", true, Priority::High),
                PriorityRule::path("X:/project/archive", true, Priority::Ignored),
                PriorityRule::path("X:/project/archive/keep.tmp", false, Priority::Normal),
            ])
            .unwrap();

        assert_eq!(
            store.effective_priority_record(&item(
                "X:/project/work/file.tmp",
                ItemKind::File,
                Priority::Normal
            )),
            Priority::High
        );
        assert_eq!(
            store.effective_priority_record(&item(
                "X:/project/archive/drop.tmp",
                ItemKind::File,
                Priority::Normal
            )),
            Priority::Ignored
        );
        assert_eq!(
            store.effective_priority_record(&item(
                "X:/project/archive/keep.tmp",
                ItemKind::File,
                Priority::Ignored
            )),
            Priority::Normal
        );
    }

    #[test]
    fn regex_order_is_deterministic_and_targets_are_respected() {
        let state = TempDir::new().unwrap();
        let mut store = PriorityRuleStore::open(state.path()).unwrap();
        let mut first = PriorityRule::regex(r"/build/", Priority::Low);
        first.target = PriorityRuleTarget::Files;
        let second = PriorityRule::regex(r"/build/", Priority::High);
        store.replace(vec![first, second]).unwrap();

        assert_eq!(
            store.effective_priority_record(&item(
                "X:/repo/build/output.bin",
                ItemKind::File,
                Priority::Normal
            )),
            Priority::Low
        );
        assert_eq!(
            store.effective_priority_record(&item(
                "X:/repo/build/folder",
                ItemKind::Folder,
                Priority::Normal
            )),
            Priority::High
        );
    }

    #[test]
    fn persistence_recovers_from_backup_and_rejects_invalid_regex() {
        let state = TempDir::new().unwrap();
        let keep_id;
        {
            let mut store = PriorityRuleStore::open(state.path()).unwrap();
            let first = PriorityRule::path("X:/keep", true, Priority::High);
            keep_id = first.id;
            store.replace(vec![first]).unwrap();
            store
                .replace(vec![PriorityRule::path("X:/second", true, Priority::Low)])
                .unwrap();
            assert!(
                store
                    .replace(vec![PriorityRule::regex("(", Priority::High)])
                    .is_err()
            );
        }
        let settings = state.path().join("settings");
        let path = settings.join("priority-rules.json");
        let backup = settings.join("priority-rules.json.bak");
        let backup_before = fs::read(&backup).unwrap();
        fs::write(&path, b"{broken").unwrap();
        let recovered = PriorityRuleStore::open(state.path()).unwrap().list();
        assert_eq!(recovered.len(), 1);
        assert_eq!(recovered[0].id, keep_id);
        assert_eq!(fs::read(&backup).unwrap(), backup_before);
        assert!(
            validate_rule_file(serde_json::from_slice(&fs::read(&path).unwrap()).unwrap())
                .is_some()
        );
    }

    #[test]
    fn priority_rule_file_future_schema_and_all_corrupt_fail_closed() {
        let future_state = TempDir::new().unwrap();
        let settings = future_state.path().join("settings");
        fs::create_dir_all(&settings).unwrap();
        let primary = settings.join("priority-rules.json");
        let backup = settings.join("priority-rules.json.bak");
        let temp = settings.join("priority-rules.json.tmp");
        let valid = serde_json::to_vec_pretty(&PriorityRuleFile::default()).unwrap();
        let future = serde_json::to_vec_pretty(&PriorityRuleFile {
            schema_version: PRIORITY_RULE_SCHEMA_VERSION + 1,
            rules: Vec::new(),
        })
        .unwrap();
        fs::write(&primary, &future).unwrap();
        fs::write(&backup, &valid).unwrap();
        fs::write(&temp, &valid).unwrap();
        let error = PriorityRuleStore::open(future_state.path()).unwrap_err();
        assert!(error.to_string().contains("upgrade the application"));
        assert_eq!(fs::read(&primary).unwrap(), future);
        assert_eq!(fs::read(&backup).unwrap(), valid);
        assert_eq!(fs::read(&temp).unwrap(), valid);

        let corrupt_state = TempDir::new().unwrap();
        let settings = corrupt_state.path().join("settings");
        fs::create_dir_all(&settings).unwrap();
        let primary = settings.join("priority-rules.json");
        let backup = settings.join("priority-rules.json.bak");
        let temp = settings.join("priority-rules.json.tmp");
        fs::write(&primary, b"primary-corrupt").unwrap();
        fs::write(&backup, b"backup-corrupt").unwrap();
        fs::write(&temp, b"temp-corrupt").unwrap();
        assert!(PriorityRuleStore::open(corrupt_state.path()).is_err());
        assert_eq!(fs::read(&primary).unwrap(), b"primary-corrupt");
        assert_eq!(fs::read(&backup).unwrap(), b"backup-corrupt");
        assert_eq!(fs::read(&temp).unwrap(), b"temp-corrupt");
    }
}
