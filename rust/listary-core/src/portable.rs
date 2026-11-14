use crate::{
    CancellationToken, Core, CoreConfig, CoreError, CustomAction, CustomCommand, FavoriteMenu,
    FilterPreset, ItemId, Priority, PriorityRule, PriorityRuleMatch, RefreshPolicy, Result,
    RootKind, RootRecord, WebSearchProvider,
    commands_actions::{canonicalize_action, canonicalize_command},
    core::validate_root,
    favorites::{canonicalize_favorite_menu, favorite_command_ids, map_favorite_paths},
    filters::canonicalize_preset,
    history::{HistoryEntry, normalize_path_prefix},
    priority_rules::canonicalize_priority_rules,
    store::Store,
    text::{comparable_path, normalize},
    web_search::{canonicalize_web_search_providers, map_web_search_paths},
};
use serde::{Deserialize, Serialize};
use std::{
    collections::{HashMap, HashSet},
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};

pub const PORTABLE_CONFIG_SCHEMA: &str = "listary_portable_config";
pub const PORTABLE_CONFIG_SCHEMA_VERSION: u32 = 6;
const PORTABLE_CONFIG_SCHEMA_VERSION_V1: u32 = 1;
const PORTABLE_CONFIG_SCHEMA_VERSION_V2: u32 = 2;
const PORTABLE_CONFIG_SCHEMA_VERSION_V3: u32 = 3;
const PORTABLE_CONFIG_SCHEMA_VERSION_V4: u32 = 4;
const PORTABLE_CONFIG_SCHEMA_VERSION_V5: u32 = 5;
const PORTABLE_CONFIG_MAX_BYTES: u64 = 8 * 1024 * 1024;
const IMPORT_MARKER_SCHEMA_VERSION: u32 = 1;
const RESTORE_POLICY: &str = "replace_empty_or_equivalent";

#[derive(Debug)]
struct ValidPortableCandidate {
    config: PortableConfig,
    bytes: Vec<u8>,
}

#[derive(Debug)]
enum PortableCandidateOutcome {
    Missing,
    Valid(Box<ValidPortableCandidate>),
    Corrupt(String),
    UnsupportedFuture { schema_version: u64 },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum LexicalPathStyle {
    Windows,
    Posix,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PortableConfig {
    pub schema: String,
    pub schema_version: u32,
    pub roots: Vec<PortableRoot>,
    pub filters: Vec<FilterPreset>,
    #[serde(default)]
    pub commands: Vec<CustomCommand>,
    #[serde(default)]
    pub actions: Vec<CustomAction>,
    #[serde(default)]
    pub favorites: FavoriteMenu,
    #[serde(default)]
    pub web_search_providers: Vec<WebSearchProvider>,
    #[serde(default)]
    pub priority_rules: Vec<PriorityRule>,
    pub history: PortableHistory,
}

impl Default for PortableConfig {
    fn default() -> Self {
        Self {
            schema: PORTABLE_CONFIG_SCHEMA.to_string(),
            schema_version: PORTABLE_CONFIG_SCHEMA_VERSION,
            roots: Vec::new(),
            filters: Vec::new(),
            commands: Vec::new(),
            actions: Vec::new(),
            favorites: FavoriteMenu::default(),
            web_search_providers: Vec::new(),
            priority_rules: Vec::new(),
            history: PortableHistory::default(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PortableRoot {
    pub path: String,
    pub kind: RootKind,
    pub refresh_policy: RefreshPolicy,
    pub priority: Priority,
    pub enabled: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct PortableItemIdentity {
    pub root_path: String,
    pub relative_path: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PortableHistoryEntry {
    pub item: PortableItemIdentity,
    pub activation_count: u32,
    pub last_activated_unix_ms: i64,
    pub last_source: crate::ActivationSource,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct PortableHistory {
    pub entries: Vec<PortableHistoryEntry>,
    pub hidden_items: Vec<PortableItemIdentity>,
    pub excluded_path_prefixes: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct PortableOmissions {
    pub history_entries_without_portable_identity: usize,
    pub hidden_items_without_portable_identity: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PortableExportReceipt {
    pub schema: String,
    pub schema_version: u32,
    pub root_count: usize,
    pub filter_count: usize,
    pub command_count: usize,
    pub action_count: usize,
    pub favorite_item_count: usize,
    pub web_search_provider_count: usize,
    pub priority_rule_count: usize,
    pub history_entry_count: usize,
    pub hidden_item_count: usize,
    pub excluded_path_prefix_count: usize,
    pub omissions: PortableOmissions,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PortablePathMapping {
    pub from: String,
    pub to: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum PortablePathStyle {
    #[default]
    Native,
    Windows,
    Posix,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PortableRestoreOptions {
    pub path_mappings: Vec<PortablePathMapping>,
    pub target_path_style: PortablePathStyle,
}

impl Default for PortableRestoreOptions {
    fn default() -> Self {
        Self {
            path_mappings: Vec::new(),
            target_path_style: PortablePathStyle::Native,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PortableRestoreReceipt {
    pub schema: String,
    pub schema_version: u32,
    pub policy: String,
    pub idempotent: bool,
    pub root_count: usize,
    pub filter_count: usize,
    pub command_count: usize,
    pub action_count: usize,
    pub favorite_item_count: usize,
    pub web_search_provider_count: usize,
    pub priority_rule_count: usize,
    pub history_entry_count: usize,
    pub hidden_item_count: usize,
    pub excluded_path_prefix_count: usize,
    pub unresolved_roots: Vec<String>,
    pub unresolved_history_items: Vec<PortableItemIdentity>,
    pub unresolved_hidden_items: Vec<PortableItemIdentity>,
    pub round_trip_equivalent: bool,
    pub rebuilt_local_catalog: bool,
    pub copied_source_catalog: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ImportMarker {
    schema_version: u32,
    target: PathBuf,
    stage: PathBuf,
    backup: PathBuf,
    target_existed: bool,
}

type StagedProfileResult = (
    PortableConfig,
    Vec<String>,
    Vec<PortableItemIdentity>,
    Vec<PortableItemIdentity>,
);

pub fn validate_portable_config(config: &PortableConfig) -> Result<()> {
    if config.schema != PORTABLE_CONFIG_SCHEMA {
        return Err(CoreError::InvalidRequest(format!(
            "unsupported portable config schema: {}",
            config.schema
        )));
    }
    if !matches!(
        config.schema_version,
        PORTABLE_CONFIG_SCHEMA_VERSION_V1
            | PORTABLE_CONFIG_SCHEMA_VERSION_V2
            | PORTABLE_CONFIG_SCHEMA_VERSION_V3
            | PORTABLE_CONFIG_SCHEMA_VERSION_V4
            | PORTABLE_CONFIG_SCHEMA_VERSION_V5
            | PORTABLE_CONFIG_SCHEMA_VERSION
    ) {
        return Err(CoreError::InvalidRequest(format!(
            "unsupported portable config schema version: {}",
            config.schema_version
        )));
    }

    let mut root_paths = HashSet::new();
    for root in &config.roots {
        if root.path.trim().is_empty() {
            return Err(CoreError::InvalidRequest(
                "portable root path is empty".into(),
            ));
        }
        validate_absolute_lexical_path(&root.path, "portable root")?;
        let key = comparable_portable_path(&root.path);
        if !root_paths.insert(key) {
            return Err(CoreError::InvalidRequest(format!(
                "duplicate portable root path: {}",
                root.path
            )));
        }
        let mut record = RootRecord::new(PathBuf::from(&root.path), root.kind);
        record.refresh_policy = root.refresh_policy.clone();
        record.priority = root.priority;
        record.enabled = root.enabled;
        validate_root(&record)?;
    }

    let mut filter_ids = HashSet::new();
    let mut filter_keywords = HashSet::new();
    for filter in &config.filters {
        let canonical = canonicalize_preset(filter.clone())?;
        if !filter_ids.insert(canonical.id) {
            return Err(CoreError::InvalidRequest(format!(
                "duplicate portable filter id: {}",
                canonical.id
            )));
        }
        if !filter_keywords.insert(canonical.keyword.clone()) {
            return Err(CoreError::InvalidRequest(format!(
                "duplicate portable filter keyword: {}",
                canonical.keyword
            )));
        }
    }

    let mut command_ids = HashSet::new();
    let mut command_keywords = HashSet::new();
    for command in &config.commands {
        let canonical = canonicalize_command(command.clone())?;
        if !command_ids.insert(canonical.id) {
            return Err(CoreError::InvalidRequest(format!(
                "duplicate portable command id: {}",
                canonical.id
            )));
        }
        if !command_keywords.insert(canonical.keyword.clone()) {
            return Err(CoreError::InvalidRequest(format!(
                "duplicate portable command keyword: {}",
                canonical.keyword
            )));
        }
    }

    let mut action_ids = HashSet::new();
    for action in &config.actions {
        let canonical = canonicalize_action(action.clone())?;
        if !action_ids.insert(canonical.id) {
            return Err(CoreError::InvalidRequest(format!(
                "duplicate portable action id: {}",
                canonical.id
            )));
        }
    }

    let favorites = canonicalize_favorite_menu(config.favorites.clone())?;
    let known_commands = config
        .commands
        .iter()
        .map(|command| command.id)
        .collect::<HashSet<_>>();
    for command_id in favorite_command_ids(&favorites) {
        if !known_commands.contains(&command_id) {
            return Err(CoreError::InvalidRequest(format!(
                "portable favorites reference unknown command: {command_id}"
            )));
        }
    }
    canonicalize_web_search_providers(config.web_search_providers.clone())?;
    canonicalize_priority_rules(config.priority_rules.clone())?;

    let known_roots = config
        .roots
        .iter()
        .map(|root| comparable_portable_path(&root.path))
        .collect::<HashSet<_>>();
    let mut history_items = HashSet::new();
    for entry in &config.history.entries {
        validate_item_identity(&entry.item, &known_roots)?;
        let key = item_identity_key(&entry.item);
        if !history_items.insert(key) {
            return Err(CoreError::InvalidRequest(format!(
                "duplicate portable history item: {}/{}",
                entry.item.root_path, entry.item.relative_path
            )));
        }
    }
    let mut hidden_items = HashSet::new();
    for item in &config.history.hidden_items {
        validate_item_identity(item, &known_roots)?;
        let key = item_identity_key(item);
        if !hidden_items.insert(key) {
            return Err(CoreError::InvalidRequest(format!(
                "duplicate portable hidden item: {}/{}",
                item.root_path, item.relative_path
            )));
        }
    }
    let mut excluded = HashSet::new();
    for prefix in &config.history.excluded_path_prefixes {
        if prefix.trim().is_empty() {
            return Err(CoreError::InvalidRequest(
                "portable excluded path prefix is empty".into(),
            ));
        }
        validate_absolute_lexical_path(prefix, "portable excluded path prefix")?;
        let key = comparable_portable_path(prefix);
        if !excluded.insert(key) {
            return Err(CoreError::InvalidRequest(format!(
                "duplicate portable excluded path prefix: {prefix}"
            )));
        }
    }
    Ok(())
}

pub fn export_portable_config(state_dir: &Path) -> Result<(PortableConfig, PortableExportReceipt)> {
    let core = Core::open(CoreConfig::new(state_dir.to_path_buf()))?;
    let (config, omissions) = export_from_core(&core);
    drop(core);
    let receipt = PortableExportReceipt {
        schema: PORTABLE_CONFIG_SCHEMA.to_string(),
        schema_version: PORTABLE_CONFIG_SCHEMA_VERSION,
        root_count: config.roots.len(),
        filter_count: config.filters.len(),
        command_count: config.commands.len(),
        action_count: config.actions.len(),
        favorite_item_count: config.favorites.item_count(),
        web_search_provider_count: config.web_search_providers.len(),
        priority_rule_count: config.priority_rules.len(),
        history_entry_count: config.history.entries.len(),
        hidden_item_count: config.history.hidden_items.len(),
        excluded_path_prefix_count: config.history.excluded_path_prefixes.len(),
        omissions,
    };
    Ok((config, receipt))
}

pub fn write_portable_config(path: &Path, config: &PortableConfig) -> Result<()> {
    let config = canonicalize_config(config.clone())?;
    let bytes = serde_json::to_vec_pretty(&config)?;
    atomic_replace_file(path, &bytes)
}

pub fn read_portable_config(path: &Path) -> Result<PortableConfig> {
    let backup = path.with_extension("json.bak");
    let staging = path.with_extension("json.tmp");
    let primary_outcome = inspect_portable_candidate(path);
    let backup_outcome = inspect_portable_candidate(&backup);
    let staging_outcome = inspect_portable_candidate(&staging);

    if let PortableCandidateOutcome::UnsupportedFuture { schema_version } = &primary_outcome {
        return Err(unsupported_future_generation_error(
            "primary",
            path,
            *schema_version,
        ));
    }

    if let PortableCandidateOutcome::Valid(primary) = &primary_outcome {
        if matches!(&staging_outcome, PortableCandidateOutcome::Valid(_)) {
            remove_recovery_generation(&staging)?;
        }
        return Ok(primary.config.clone());
    }

    // A future recovery generation is never interpreted as permission to
    // silently select an older generation. That would discard settings this
    // build does not understand.
    for (label, candidate_path, outcome) in [
        ("backup", &backup, &backup_outcome),
        ("staging", &staging, &staging_outcome),
    ] {
        if let PortableCandidateOutcome::UnsupportedFuture { schema_version } = outcome {
            return Err(unsupported_future_generation_error(
                label,
                candidate_path,
                *schema_version,
            ));
        }
    }

    let primary_was_corrupt = matches!(&primary_outcome, PortableCandidateOutcome::Corrupt(_));
    let (recovered, recovered_from_staging) =
        if let PortableCandidateOutcome::Valid(candidate) = &backup_outcome {
            (candidate, false)
        } else if let PortableCandidateOutcome::Valid(candidate) = &staging_outcome {
            (candidate, true)
        } else {
            if matches!(&primary_outcome, PortableCandidateOutcome::Missing)
                && matches!(&backup_outcome, PortableCandidateOutcome::Missing)
                && matches!(&staging_outcome, PortableCandidateOutcome::Missing)
            {
                return Err(CoreError::Filesystem {
                    path: path.to_path_buf(),
                    source: std::io::Error::new(
                        std::io::ErrorKind::NotFound,
                        "portable config and recovery generations are missing",
                    ),
                });
            }
            return Err(no_readable_generation_error(
                path,
                &primary_outcome,
                &backup_outcome,
                &staging_outcome,
            ));
        };

    repair_portable_primary(path, &recovered.bytes, primary_was_corrupt)?;
    if recovered_from_staging || matches!(&staging_outcome, PortableCandidateOutcome::Valid(_)) {
        remove_recovery_generation(&staging)?;
    }
    Ok(recovered.config.clone())
}

fn inspect_portable_candidate(path: &Path) -> PortableCandidateOutcome {
    let metadata = match fs::metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return PortableCandidateOutcome::Missing;
        }
        Err(error) => {
            return PortableCandidateOutcome::Corrupt(format!(
                "metadata could not be read: {error}"
            ));
        }
    };
    if !metadata.is_file() || metadata.len() > PORTABLE_CONFIG_MAX_BYTES {
        return PortableCandidateOutcome::Corrupt(format!(
            "not a regular file within the {PORTABLE_CONFIG_MAX_BYTES} byte limit"
        ));
    }
    let bytes = match fs::read(path) {
        Ok(bytes) => bytes,
        Err(error) => {
            return PortableCandidateOutcome::Corrupt(format!("could not be read: {error}"));
        }
    };
    let value: serde_json::Value = match serde_json::from_slice(&bytes) {
        Ok(value) => value,
        Err(error) => {
            return PortableCandidateOutcome::Corrupt(format!("malformed JSON: {error}"));
        }
    };
    if value.get("schema").and_then(serde_json::Value::as_str) == Some(PORTABLE_CONFIG_SCHEMA)
        && let Some(schema_version) = value
            .get("schema_version")
            .and_then(serde_json::Value::as_u64)
        && schema_version > u64::from(PORTABLE_CONFIG_SCHEMA_VERSION)
    {
        return PortableCandidateOutcome::UnsupportedFuture { schema_version };
    }
    let config: PortableConfig = match serde_json::from_value(value) {
        Ok(config) => config,
        Err(error) => {
            return PortableCandidateOutcome::Corrupt(format!(
                "portable config shape is invalid: {error}"
            ));
        }
    };
    match canonicalize_config(config) {
        Ok(config) => {
            PortableCandidateOutcome::Valid(Box::new(ValidPortableCandidate { config, bytes }))
        }
        Err(error) => PortableCandidateOutcome::Corrupt(error.to_string()),
    }
}

fn unsupported_future_generation_error(label: &str, path: &Path, schema_version: u64) -> CoreError {
    CoreError::InvalidRequest(format!(
        "portable config {label} generation uses unsupported future schema version \
         {schema_version} at {}; refusing to downgrade to an older generation",
        path.display()
    ))
}

fn no_readable_generation_error(
    path: &Path,
    primary: &PortableCandidateOutcome,
    backup: &PortableCandidateOutcome,
    staging: &PortableCandidateOutcome,
) -> CoreError {
    fn describe(outcome: &PortableCandidateOutcome) -> String {
        match outcome {
            PortableCandidateOutcome::Missing => "missing".into(),
            PortableCandidateOutcome::Valid(_) => "valid".into(),
            PortableCandidateOutcome::Corrupt(reason) => format!("corrupt ({reason})"),
            PortableCandidateOutcome::UnsupportedFuture { schema_version } => {
                format!("unsupported future schema version {schema_version}")
            }
        }
    }

    CoreError::InvalidRequest(format!(
        "portable config recovery failed for {}; primary: {}; backup: {}; staging: {}; all \
         candidate files were preserved for diagnosis",
        path.display(),
        describe(primary),
        describe(backup),
        describe(staging)
    ))
}

fn repair_portable_primary(path: &Path, bytes: &[u8], preserve_primary: bool) -> Result<()> {
    path.parent().ok_or_else(|| {
        CoreError::InvalidRequest("portable config input path has no parent".into())
    })?;
    let repair = path.with_extension("json.repair.tmp");
    let mut file = OpenOptions::new()
        .create(true)
        .truncate(true)
        .write(true)
        .open(&repair)
        .map_err(|source| CoreError::Filesystem {
            path: repair.clone(),
            source,
        })?;
    file.write_all(bytes)
        .and_then(|_| file.sync_all())
        .map_err(|source| CoreError::Filesystem {
            path: repair.clone(),
            source,
        })?;
    drop(file);

    let evidence = if preserve_primary && path.exists() {
        let evidence = next_corrupt_evidence_path(path);
        fs::rename(path, &evidence).map_err(|source| CoreError::Filesystem {
            path: path.to_path_buf(),
            source,
        })?;
        Some(evidence)
    } else {
        None
    };

    if let Err(source) = fs::rename(&repair, path) {
        if let Some(evidence) = &evidence
            && !path.exists()
        {
            let _ = fs::rename(evidence, path);
        }
        return Err(CoreError::Filesystem {
            path: path.to_path_buf(),
            source,
        });
    }
    Ok(())
}

fn next_corrupt_evidence_path(path: &Path) -> PathBuf {
    let initial = path.with_extension("json.corrupt");
    if !initial.exists() {
        return initial;
    }
    for suffix in 1u32.. {
        let candidate = path.with_extension(format!("json.corrupt.{suffix}"));
        if !candidate.exists() {
            return candidate;
        }
    }
    unreachable!("u32 evidence suffix space is exhausted")
}

fn remove_recovery_generation(path: &Path) -> Result<()> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(source) => Err(CoreError::Filesystem {
            path: path.to_path_buf(),
            source,
        }),
    }
}

pub fn restore_portable_config(
    destination_state_dir: &Path,
    config: &PortableConfig,
    options: &PortableRestoreOptions,
) -> Result<PortableRestoreReceipt> {
    let parent = destination_state_dir.parent().ok_or_else(|| {
        CoreError::InvalidRequest("destination state directory has no parent".into())
    })?;
    fs::create_dir_all(parent).map_err(|source| CoreError::Filesystem {
        path: parent.to_path_buf(),
        source,
    })?;
    let _profile_lease = Store::acquire_exclusive_profile_lease(destination_state_dir)?;
    recover_interrupted_import(destination_state_dir)?;
    let resolved = resolve_config(config, options)?;

    if destination_state_dir.exists() && !destination_state_dir.is_dir() {
        return Err(CoreError::InvalidRequest(format!(
            "destination state path is not a directory: {}",
            destination_state_dir.display()
        )));
    }

    if destination_state_dir.is_dir() {
        let current_core = Core::open_under_exclusive_profile_lease(CoreConfig::new(
            destination_state_dir.to_path_buf(),
        ))?;
        let (current, _) = export_from_core(&current_core);
        let (expected, unresolved_history, unresolved_hidden) =
            effective_config_for_core(&current_core, &resolved);
        let is_empty = portable_config_is_empty(&current);
        let is_equivalent = current == expected;
        let unresolved_roots = missing_roots_from_core(&current_core, &resolved);
        drop(current_core);
        if is_equivalent {
            return Ok(receipt_for(
                &expected,
                true,
                unresolved_roots,
                unresolved_history,
                unresolved_hidden,
            ));
        }
        if !is_empty {
            return Err(CoreError::InvalidRequest(format!(
                "destination portable state conflicts with import; policy is {RESTORE_POLICY}"
            )));
        }
    }

    let (marker_path, stage_path, backup_path) = import_paths(destination_state_dir)?;
    if stage_path.exists() || backup_path.exists() || marker_path.exists() {
        return Err(CoreError::InvalidRequest(format!(
            "portable import staging artifacts already exist near {}",
            destination_state_dir.display()
        )));
    }

    let target_existed = destination_state_dir.exists();
    let marker = ImportMarker {
        schema_version: IMPORT_MARKER_SCHEMA_VERSION,
        target: destination_state_dir.to_path_buf(),
        stage: stage_path.clone(),
        backup: backup_path.clone(),
        target_existed,
    };
    write_import_marker_new(&marker_path, &marker)?;
    crate::test_fault::hit("portable_after_marker_publish");

    if let Err(source) = fs::create_dir(&stage_path) {
        let _ = fs::remove_file(&marker_path);
        return Err(CoreError::Filesystem {
            path: stage_path,
            source,
        });
    }

    let staged = build_staged_profile(&stage_path, &resolved);
    let (expected, unresolved_roots, unresolved_history, unresolved_hidden) = match staged {
        Ok(value) => {
            remove_stage_profile_lease(&stage_path)?;
            value
        }
        Err(error) => {
            remove_owned_import_dir(destination_state_dir, &stage_path)?;
            let _ = remove_stage_profile_lease(&stage_path);
            let _ = fs::remove_file(&marker_path);
            return Err(error);
        }
    };
    crate::test_fault::hit("portable_after_stage_ready");

    if target_existed {
        fs::rename(destination_state_dir, &backup_path).map_err(|source| {
            CoreError::Filesystem {
                path: destination_state_dir.to_path_buf(),
                source,
            }
        })?;
    }
    crate::test_fault::hit("portable_after_backup_rename");

    if let Err(source) = fs::rename(&stage_path, destination_state_dir) {
        if target_existed && backup_path.exists() && !destination_state_dir.exists() {
            if fs::rename(&backup_path, destination_state_dir).is_ok()
                && remove_owned_import_dir(destination_state_dir, &stage_path).is_ok()
            {
                let _ = fs::remove_file(&marker_path);
            }
        } else if !target_existed
            && remove_owned_import_dir(destination_state_dir, &stage_path).is_ok()
        {
            let _ = fs::remove_file(&marker_path);
        }
        return Err(CoreError::Filesystem {
            path: destination_state_dir.to_path_buf(),
            source,
        });
    }
    crate::test_fault::hit("portable_after_target_promote");

    if backup_path.exists() {
        remove_owned_import_dir(destination_state_dir, &backup_path)?;
    }
    fs::remove_file(&marker_path).map_err(|source| CoreError::Filesystem {
        path: marker_path,
        source,
    })?;

    Ok(receipt_for(
        &expected,
        false,
        unresolved_roots,
        unresolved_history,
        unresolved_hidden,
    ))
}

fn export_from_core(core: &Core) -> (PortableConfig, PortableOmissions) {
    let roots = core.list_roots();
    let root_by_id = roots
        .iter()
        .cloned()
        .map(|root| (root.id, root))
        .collect::<HashMap<_, _>>();
    let mut portable_roots = roots
        .into_iter()
        .map(|root| PortableRoot {
            path: root.path.to_string_lossy().into_owned(),
            kind: root.kind,
            refresh_policy: root.refresh_policy,
            priority: root.priority,
            enabled: root.enabled,
        })
        .collect::<Vec<_>>();
    portable_roots.sort_by(|a, b| {
        comparable_portable_path(&a.path)
            .cmp(&comparable_portable_path(&b.path))
            .then_with(|| a.path.cmp(&b.path))
    });

    let mut filters = core.list_filters();
    filters.sort_by(|a, b| {
        a.keyword
            .cmp(&b.keyword)
            .then_with(|| a.name.cmp(&b.name))
            .then_with(|| a.id.cmp(&b.id))
    });
    let mut commands = core.list_commands();
    commands.sort_by(|a, b| {
        a.keyword
            .cmp(&b.keyword)
            .then_with(|| a.title.cmp(&b.title))
            .then_with(|| a.id.cmp(&b.id))
    });
    let mut actions = core.list_actions();
    actions.sort_by(|a, b| a.title.cmp(&b.title).then_with(|| a.id.cmp(&b.id)));
    let favorites = core.get_favorites();
    let web_search_providers = core.list_web_search_providers();
    let priority_rules = core.list_priority_rules();

    let (history_rows, hidden_ids, excluded_path_prefixes) = core.portable_history_snapshot();
    let mut omissions = PortableOmissions::default();
    let mut history_entries = Vec::new();
    for (item_id, entry) in history_rows {
        let Some(item) = core.get_item(item_id) else {
            omissions.history_entries_without_portable_identity += 1;
            continue;
        };
        let Some(root) = root_by_id.get(&item.root_id) else {
            if item
                .preview_hint
                .as_deref()
                .is_some_and(crate::workspace_catalog::is_workspace_provider_hint)
            {
                continue;
            }
            omissions.history_entries_without_portable_identity += 1;
            continue;
        };
        let Some(identity) = portable_item_identity(&root.path, &item.path) else {
            omissions.history_entries_without_portable_identity += 1;
            continue;
        };
        history_entries.push(PortableHistoryEntry {
            item: identity,
            activation_count: entry.activation_count,
            last_activated_unix_ms: entry.last_activated_unix_ms,
            last_source: entry.last_source,
        });
    }
    history_entries.sort_by_key(|entry| item_identity_key(&entry.item));

    let mut hidden_items = Vec::new();
    for item_id in hidden_ids {
        let Some(item) = core.get_item(item_id) else {
            omissions.hidden_items_without_portable_identity += 1;
            continue;
        };
        let Some(root) = root_by_id.get(&item.root_id) else {
            if item
                .preview_hint
                .as_deref()
                .is_some_and(crate::workspace_catalog::is_workspace_provider_hint)
            {
                continue;
            }
            omissions.hidden_items_without_portable_identity += 1;
            continue;
        };
        let Some(identity) = portable_item_identity(&root.path, &item.path) else {
            omissions.hidden_items_without_portable_identity += 1;
            continue;
        };
        hidden_items.push(identity);
    }
    hidden_items.sort_by_key(item_identity_key);

    let mut excluded_path_prefixes = excluded_path_prefixes;
    excluded_path_prefixes.sort_by(|a, b| {
        comparable_portable_path(a)
            .cmp(&comparable_portable_path(b))
            .then_with(|| a.cmp(b))
    });

    (
        PortableConfig {
            schema: PORTABLE_CONFIG_SCHEMA.to_string(),
            schema_version: PORTABLE_CONFIG_SCHEMA_VERSION,
            roots: portable_roots,
            filters,
            commands,
            actions,
            favorites,
            web_search_providers,
            priority_rules,
            history: PortableHistory {
                entries: history_entries,
                hidden_items,
                excluded_path_prefixes,
            },
        },
        omissions,
    )
}

fn resolve_config(
    config: &PortableConfig,
    options: &PortableRestoreOptions,
) -> Result<PortableConfig> {
    validate_portable_config(config)?;
    let target_style = resolved_path_style(options.target_path_style);
    validate_mappings(&options.path_mappings, target_style)?;
    let mut resolved = config.clone();
    for root in &mut resolved.roots {
        root.path = map_portable_path(&root.path, &options.path_mappings, target_style)?;
    }
    for filter in &mut resolved.filters {
        for path in &mut filter.search_paths {
            *path = map_portable_path(path, &options.path_mappings, target_style)?;
        }
    }
    for command in &mut resolved.commands {
        command.executable =
            map_portable_executable(&command.executable, &options.path_mappings, target_style)?;
        if let Some(directory) = command.working_directory.as_mut()
            && looks_absolute_path(directory)
        {
            *directory = map_portable_path(directory, &options.path_mappings, target_style)?;
        }
    }
    for action in &mut resolved.actions {
        action.executable =
            map_portable_executable(&action.executable, &options.path_mappings, target_style)?;
        if let Some(directory) = action.working_directory.as_mut()
            && looks_absolute_path(directory)
        {
            *directory = map_portable_path(directory, &options.path_mappings, target_style)?;
        }
    }
    map_favorite_paths(&mut resolved.favorites, |path| {
        map_portable_path(path, &options.path_mappings, target_style)
    })?;
    map_web_search_paths(&mut resolved.web_search_providers, |path| {
        map_portable_path(path, &options.path_mappings, target_style)
    })?;
    for rule in &mut resolved.priority_rules {
        if let PriorityRuleMatch::Path { path, .. } = &mut rule.matcher {
            *path = map_portable_path(path, &options.path_mappings, target_style)?;
        }
    }
    for entry in &mut resolved.history.entries {
        entry.item.root_path =
            map_portable_path(&entry.item.root_path, &options.path_mappings, target_style)?;
        entry.item.relative_path = normalize_relative_path(&entry.item.relative_path)?;
    }
    for item in &mut resolved.history.hidden_items {
        item.root_path = map_portable_path(&item.root_path, &options.path_mappings, target_style)?;
        item.relative_path = normalize_relative_path(&item.relative_path)?;
    }
    for prefix in &mut resolved.history.excluded_path_prefixes {
        *prefix = map_portable_path(prefix, &options.path_mappings, target_style)?;
    }
    canonicalize_config(resolved)
}

fn canonicalize_config(mut config: PortableConfig) -> Result<PortableConfig> {
    if matches!(
        config.schema_version,
        PORTABLE_CONFIG_SCHEMA_VERSION_V1
            | PORTABLE_CONFIG_SCHEMA_VERSION_V2
            | PORTABLE_CONFIG_SCHEMA_VERSION_V3
            | PORTABLE_CONFIG_SCHEMA_VERSION_V4
            | PORTABLE_CONFIG_SCHEMA_VERSION_V5
    ) {
        config.schema_version = PORTABLE_CONFIG_SCHEMA_VERSION;
    }
    validate_portable_config(&config)?;
    for filter in &mut config.filters {
        *filter = canonicalize_preset(filter.clone())?;
    }
    for command in &mut config.commands {
        *command = canonicalize_command(command.clone())?;
    }
    for action in &mut config.actions {
        *action = canonicalize_action(action.clone())?;
    }
    config.favorites = canonicalize_favorite_menu(config.favorites)?;
    config.web_search_providers = canonicalize_web_search_providers(config.web_search_providers)?;
    config.priority_rules = canonicalize_priority_rules(config.priority_rules)?;
    config.roots.sort_by(|a, b| {
        comparable_portable_path(&a.path)
            .cmp(&comparable_portable_path(&b.path))
            .then_with(|| a.path.cmp(&b.path))
    });
    config.filters.sort_by(|a, b| {
        a.keyword
            .cmp(&b.keyword)
            .then_with(|| a.name.cmp(&b.name))
            .then_with(|| a.id.cmp(&b.id))
    });
    config.commands.sort_by(|a, b| {
        a.keyword
            .cmp(&b.keyword)
            .then_with(|| a.title.cmp(&b.title))
            .then_with(|| a.id.cmp(&b.id))
    });
    config
        .actions
        .sort_by(|a, b| a.title.cmp(&b.title).then_with(|| a.id.cmp(&b.id)));
    config
        .history
        .entries
        .sort_by_key(|entry| item_identity_key(&entry.item));
    config.history.hidden_items.sort_by_key(item_identity_key);
    config.history.excluded_path_prefixes.sort_by(|a, b| {
        comparable_portable_path(a)
            .cmp(&comparable_portable_path(b))
            .then_with(|| a.cmp(b))
    });
    for prefix in &mut config.history.excluded_path_prefixes {
        *prefix = normalize_path_prefix(prefix);
    }
    config.history.excluded_path_prefixes.sort();
    config.history.excluded_path_prefixes.dedup();
    Ok(config)
}

fn build_staged_profile(stage_path: &Path, config: &PortableConfig) -> Result<StagedProfileResult> {
    let core = Core::open(CoreConfig::new(stage_path.to_path_buf()))?;
    let mut imported_roots = HashMap::new();
    for portable in &config.roots {
        let mut root = core.add_root(PathBuf::from(&portable.path), portable.kind)?;
        root.refresh_policy = portable.refresh_policy.clone();
        root.priority = portable.priority;
        root.enabled = portable.enabled;
        core.put_root(root.clone())?;
        imported_roots.insert(comparable_portable_path(&portable.path), root.id);
    }
    for filter in &config.filters {
        core.put_filter(filter.clone())?;
    }
    for command in &config.commands {
        core.put_command(command.clone())?;
    }
    for action in &config.actions {
        core.put_action(action.clone())?;
    }
    core.put_favorites(config.favorites.clone())?;
    core.replace_web_search_providers(config.web_search_providers.clone())?;
    core.replace_priority_rules(config.priority_rules.clone())?;

    let mut unresolved_roots = Vec::new();
    for portable in &config.roots {
        if !portable.enabled {
            continue;
        }
        let root_id = imported_roots
            .get(&comparable_portable_path(&portable.path))
            .copied()
            .ok_or_else(|| CoreError::InvalidRequest("portable root mapping disappeared".into()))?;
        if core
            .reconcile_root(root_id, &CancellationToken::new())
            .is_err()
        {
            unresolved_roots.push(portable.path.clone());
        }
    }
    unresolved_roots.sort_by_key(|path| comparable_portable_path(path));

    let (unresolved_history, unresolved_hidden) = apply_portable_history(&core, &config.history)?;
    core.flush()?;
    let (actual, _) = export_from_core(&core);
    let expected = config_without_unresolved(config, &unresolved_history, &unresolved_hidden);
    if actual != expected {
        return Err(CoreError::InvalidRequest(
            "portable config staging round-trip mismatch".into(),
        ));
    }
    drop(core);
    Ok((
        expected,
        unresolved_roots,
        unresolved_history,
        unresolved_hidden,
    ))
}

fn apply_portable_history(
    core: &Core,
    history: &PortableHistory,
) -> Result<(Vec<PortableItemIdentity>, Vec<PortableItemIdentity>)> {
    let mut entries = HashMap::<ItemId, HistoryEntry>::new();
    let mut unresolved_history = Vec::new();
    for entry in &history.entries {
        let path = item_identity_absolute_path(&entry.item);
        let Some(item) = core.get_item_by_path(&path) else {
            unresolved_history.push(entry.item.clone());
            continue;
        };
        entries.insert(
            item.id,
            HistoryEntry {
                activation_count: entry.activation_count,
                last_activated_unix_ms: entry.last_activated_unix_ms,
                last_source: entry.last_source,
            },
        );
    }

    let mut hidden_ids = HashSet::new();
    let mut unresolved_hidden = Vec::new();
    for identity in &history.hidden_items {
        let path = item_identity_absolute_path(identity);
        let Some(item) = core.get_item_by_path(&path) else {
            unresolved_hidden.push(identity.clone());
            continue;
        };
        hidden_ids.insert(item.id);
    }
    core.replace_portable_history(entries, hidden_ids, history.excluded_path_prefixes.clone())?;
    unresolved_history.sort_by_key(item_identity_key);
    unresolved_hidden.sort_by_key(item_identity_key);
    Ok((unresolved_history, unresolved_hidden))
}

fn effective_config_for_core(
    core: &Core,
    config: &PortableConfig,
) -> (
    PortableConfig,
    Vec<PortableItemIdentity>,
    Vec<PortableItemIdentity>,
) {
    let mut unresolved_history = Vec::new();
    let mut unresolved_hidden = Vec::new();
    let mut effective = config.clone();
    effective.history.entries.retain(|entry| {
        let present = core
            .get_item_by_path(&item_identity_absolute_path(&entry.item))
            .is_some();
        if !present {
            unresolved_history.push(entry.item.clone());
        }
        present
    });
    effective.history.hidden_items.retain(|item| {
        let present = core
            .get_item_by_path(&item_identity_absolute_path(item))
            .is_some();
        if !present {
            unresolved_hidden.push(item.clone());
        }
        present
    });
    unresolved_history.sort_by_key(item_identity_key);
    unresolved_hidden.sort_by_key(item_identity_key);
    (effective, unresolved_history, unresolved_hidden)
}

fn config_without_unresolved(
    config: &PortableConfig,
    unresolved_history: &[PortableItemIdentity],
    unresolved_hidden: &[PortableItemIdentity],
) -> PortableConfig {
    let unresolved_history = unresolved_history
        .iter()
        .map(item_identity_key)
        .collect::<HashSet<_>>();
    let unresolved_hidden = unresolved_hidden
        .iter()
        .map(item_identity_key)
        .collect::<HashSet<_>>();
    let mut expected = config.clone();
    expected
        .history
        .entries
        .retain(|entry| !unresolved_history.contains(&item_identity_key(&entry.item)));
    expected
        .history
        .hidden_items
        .retain(|item| !unresolved_hidden.contains(&item_identity_key(item)));
    expected
}

fn missing_roots_from_core(core: &Core, config: &PortableConfig) -> Vec<String> {
    let roots = core.list_roots();
    let unavailable = roots
        .into_iter()
        .filter(|root| root.availability != crate::RootAvailability::Available)
        .map(|root| comparable_portable_path(&root.path.to_string_lossy()))
        .collect::<HashSet<_>>();
    let mut paths = config
        .roots
        .iter()
        .filter(|root| unavailable.contains(&comparable_portable_path(&root.path)))
        .map(|root| root.path.clone())
        .collect::<Vec<_>>();
    paths.sort_by_key(|path| comparable_portable_path(path));
    paths
}

fn receipt_for(
    config: &PortableConfig,
    idempotent: bool,
    unresolved_roots: Vec<String>,
    unresolved_history_items: Vec<PortableItemIdentity>,
    unresolved_hidden_items: Vec<PortableItemIdentity>,
) -> PortableRestoreReceipt {
    PortableRestoreReceipt {
        schema: PORTABLE_CONFIG_SCHEMA.to_string(),
        schema_version: PORTABLE_CONFIG_SCHEMA_VERSION,
        policy: RESTORE_POLICY.to_string(),
        idempotent,
        root_count: config.roots.len(),
        filter_count: config.filters.len(),
        command_count: config.commands.len(),
        action_count: config.actions.len(),
        favorite_item_count: config.favorites.item_count(),
        web_search_provider_count: config.web_search_providers.len(),
        priority_rule_count: config.priority_rules.len(),
        history_entry_count: config.history.entries.len(),
        hidden_item_count: config.history.hidden_items.len(),
        excluded_path_prefix_count: config.history.excluded_path_prefixes.len(),
        unresolved_roots,
        unresolved_history_items,
        unresolved_hidden_items,
        round_trip_equivalent: true,
        rebuilt_local_catalog: true,
        copied_source_catalog: false,
    }
}

fn portable_config_is_empty(config: &PortableConfig) -> bool {
    config.roots.is_empty()
        && config.filters.is_empty()
        && config.commands.is_empty()
        && config.actions.is_empty()
        && config.favorites.items.is_empty()
        && config.web_search_providers.is_empty()
        && config.priority_rules.is_empty()
        && config.history.entries.is_empty()
        && config.history.hidden_items.is_empty()
        && config.history.excluded_path_prefixes.is_empty()
}

fn validate_item_identity(
    item: &PortableItemIdentity,
    known_roots: &HashSet<String>,
) -> Result<()> {
    if !known_roots.contains(&comparable_portable_path(&item.root_path)) {
        return Err(CoreError::InvalidRequest(format!(
            "portable item references unknown root path: {}",
            item.root_path
        )));
    }
    normalize_relative_path(&item.relative_path)?;
    Ok(())
}

fn portable_item_identity(root: &Path, item_path: &str) -> Option<PortableItemIdentity> {
    let item = Path::new(item_path);
    let relative = item.strip_prefix(root).ok()?;
    let relative_path = relative
        .components()
        .map(|component| component.as_os_str().to_string_lossy())
        .collect::<Vec<_>>()
        .join("/");
    if relative_path.is_empty() {
        return None;
    }
    Some(PortableItemIdentity {
        root_path: root.to_string_lossy().into_owned(),
        relative_path,
    })
}

fn item_identity_absolute_path(identity: &PortableItemIdentity) -> String {
    join_portable_path(&identity.root_path, &identity.relative_path)
}

fn item_identity_key(item: &PortableItemIdentity) -> String {
    format!(
        "{}|{}",
        comparable_portable_path(&item.root_path),
        normalize(&item.relative_path.replace('\\', "/"))
    )
}

fn normalize_relative_path(path: &str) -> Result<String> {
    let normalized = path.replace('\\', "/");
    let security_normalized = normalize(&normalized).replace('\\', "/");
    if normalized.is_empty() {
        return Err(CoreError::InvalidRequest(
            "portable item relative path is empty".into(),
        ));
    }
    if security_normalized.starts_with('/')
        || security_normalized.ends_with('/')
        || looks_windows_drive_designator(&security_normalized)
        || normalized.contains('\0')
        || security_normalized.split('/').any(component_is_unsafe)
    {
        return Err(CoreError::InvalidRequest(format!(
            "portable item relative path is unsafe: {path}"
        )));
    }
    Ok(normalized)
}

fn join_portable_path(root: &str, relative: &str) -> String {
    let separator = preferred_separator(root);
    let root = root.trim_end_matches(['/', '\\']);
    let relative = relative.replace(['/', '\\'], &separator.to_string());
    format!("{root}{separator}{relative}")
}

fn validate_mappings(
    mappings: &[PortablePathMapping],
    target_style: PortablePathStyle,
) -> Result<()> {
    let mut seen = HashSet::new();
    for mapping in mappings {
        if mapping.from.trim().is_empty() || mapping.to.trim().is_empty() {
            return Err(CoreError::InvalidRequest(
                "portable path mappings require non-empty from/to paths".into(),
            ));
        }
        validate_absolute_lexical_path(&mapping.from, "portable path mapping source")?;
        ensure_path_style(&mapping.to, target_style)?;
        let key = comparable_portable_path(&mapping.from);
        if !seen.insert(key) {
            return Err(CoreError::InvalidRequest(format!(
                "duplicate portable path mapping source: {}",
                mapping.from
            )));
        }
    }
    Ok(())
}

fn map_portable_path(
    path: &str,
    mappings: &[PortablePathMapping],
    target_style: PortablePathStyle,
) -> Result<String> {
    validate_absolute_lexical_path(path, "portable path")?;
    let mut ordered = mappings.iter().collect::<Vec<_>>();
    ordered.sort_by_key(|mapping| std::cmp::Reverse(mapping_component_count(&mapping.from)));
    for mapping in ordered {
        let Some(remainder) = mapping_remainder(path, &mapping.from) else {
            continue;
        };
        let mapped = if remainder.is_empty() {
            mapping.to.clone()
        } else {
            validate_mapping_remainder(&remainder, target_style)?;
            join_portable_path(&mapping.to, &remainder)
        };
        ensure_path_style(&mapped, target_style)?;
        return Ok(mapped);
    }
    ensure_path_style(path, target_style)?;
    Ok(path.to_string())
}

fn map_portable_executable(
    executable: &str,
    mappings: &[PortablePathMapping],
    target_style: PortablePathStyle,
) -> Result<String> {
    if looks_absolute_path(executable) {
        map_portable_path(executable, mappings, target_style)
    } else {
        Ok(executable.to_string())
    }
}

fn looks_absolute_path(path: &str) -> bool {
    looks_windows_absolute(path) || looks_posix_absolute(path)
}

fn mapping_component_count(path: &str) -> usize {
    let value = path.replace('\\', "/");
    let trimmed = value.trim_end_matches('/');
    if trimmed.is_empty() && value.starts_with('/') {
        0
    } else {
        trimmed.split('/').count()
    }
}

fn mapping_remainder(path: &str, from: &str) -> Option<String> {
    let path = path.replace('\\', "/");
    let from = from.replace('\\', "/");
    if comparable_path(&path) == comparable_path(&from) {
        return Some(String::new());
    }

    if comparable_path(&from) == "/" {
        return path
            .starts_with('/')
            .then(|| path.trim_start_matches('/').to_string());
    }

    let path_trimmed = path.trim_end_matches('/');
    let from_trimmed = from.trim_end_matches('/');
    let path_parts = path_trimmed.split('/').collect::<Vec<_>>();
    let from_parts = from_trimmed.split('/').collect::<Vec<_>>();
    if from_parts.is_empty() || path_parts.len() <= from_parts.len() {
        return None;
    }
    if !path_parts
        .iter()
        .zip(&from_parts)
        .all(|(candidate, expected)| normalize(candidate) == normalize(expected))
    {
        return None;
    }
    Some(path_parts[from_parts.len()..].join("/"))
}

fn ensure_path_style(path: &str, style: PortablePathStyle) -> Result<()> {
    let lexical_style = validate_absolute_lexical_path(path, "portable mapped path")?;
    match (resolved_path_style(style), lexical_style) {
        (PortablePathStyle::Windows, LexicalPathStyle::Posix) => Err(CoreError::InvalidRequest(
            format!("portable path requires an explicit Windows mapping: {path}"),
        )),
        (PortablePathStyle::Posix, LexicalPathStyle::Windows) => Err(CoreError::InvalidRequest(
            format!("portable path requires an explicit POSIX mapping: {path}"),
        )),
        _ => Ok(()),
    }
}

fn validate_absolute_lexical_path(path: &str, label: &str) -> Result<LexicalPathStyle> {
    if path.is_empty() || path.contains('\0') {
        return Err(CoreError::InvalidRequest(format!(
            "{label} must be a non-empty absolute path"
        )));
    }

    let normalized_separators = path.replace('\\', "/");
    let (style, remainder, minimum_components) = if looks_windows_drive_absolute(path) {
        (
            LexicalPathStyle::Windows,
            &normalized_separators[3..],
            0usize,
        )
    } else if looks_windows_unc(path) {
        (
            LexicalPathStyle::Windows,
            &normalized_separators[2..],
            2usize,
        )
    } else if looks_posix_absolute(path) {
        (LexicalPathStyle::Posix, &normalized_separators[1..], 0usize)
    } else {
        return Err(CoreError::InvalidRequest(format!(
            "{label} must be absolute: {path}"
        )));
    };

    let components = if remainder.is_empty() {
        Vec::new()
    } else {
        remainder.split('/').collect::<Vec<_>>()
    };
    if components.len() < minimum_components
        || components.iter().copied().any(component_is_unsafe)
        || (style == LexicalPathStyle::Windows
            && components
                .iter()
                .any(|component| windows_component_is_unsafe(component)))
    {
        return Err(CoreError::InvalidRequest(format!(
            "{label} is not lexically normalized or contains traversal: {path}"
        )));
    }
    Ok(style)
}

fn validate_mapping_remainder(remainder: &str, target_style: PortablePathStyle) -> Result<()> {
    let security_normalized = normalize(remainder).replace('\\', "/");
    if security_normalized.starts_with('/')
        || security_normalized.split('/').any(component_is_unsafe)
        || (resolved_path_style(target_style) == PortablePathStyle::Windows
            && (looks_windows_drive_designator(&security_normalized)
                || security_normalized
                    .split('/')
                    .any(windows_component_is_unsafe)))
    {
        return Err(CoreError::InvalidRequest(format!(
            "portable path mapping remainder can escape its destination: {remainder}"
        )));
    }
    Ok(())
}

fn component_is_unsafe(component: &str) -> bool {
    let comparable = normalize(component);
    component.is_empty() || comparable == "." || comparable == ".."
}

fn windows_component_is_unsafe(component: &str) -> bool {
    let comparable = normalize(component);
    looks_windows_drive_designator(&comparable) || comparable.contains(':')
}

fn resolved_path_style(style: PortablePathStyle) -> PortablePathStyle {
    match style {
        PortablePathStyle::Native => {
            if cfg!(windows) {
                PortablePathStyle::Windows
            } else {
                PortablePathStyle::Posix
            }
        }
        other => other,
    }
}

fn looks_windows_absolute(path: &str) -> bool {
    looks_windows_drive_absolute(path) || looks_windows_unc(path)
}

fn looks_windows_drive_absolute(path: &str) -> bool {
    let bytes = path.as_bytes();
    bytes.len() >= 3
        && bytes[0].is_ascii_alphabetic()
        && bytes[1] == b':'
        && matches!(bytes[2], b'/' | b'\\')
}

fn looks_windows_drive_designator(path: &str) -> bool {
    let bytes = path.as_bytes();
    bytes.len() >= 2 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':'
}

fn looks_windows_unc(path: &str) -> bool {
    path.starts_with("\\\\")
}

fn looks_posix_absolute(path: &str) -> bool {
    path.starts_with('/')
}

fn preferred_separator(path: &str) -> char {
    if path.contains('\\') && !path.contains('/') {
        '\\'
    } else {
        '/'
    }
}

fn comparable_portable_path(path: &str) -> String {
    comparable_path(path)
}

fn atomic_replace_file(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path.parent().ok_or_else(|| {
        CoreError::InvalidRequest("portable config output path has no parent".into())
    })?;
    fs::create_dir_all(parent).map_err(|source| CoreError::Filesystem {
        path: parent.to_path_buf(),
        source,
    })?;
    let temp = path.with_extension("json.tmp");
    let backup = path.with_extension("json.bak");
    fs::write(&temp, bytes).map_err(|source| CoreError::Filesystem {
        path: temp.clone(),
        source,
    })?;
    crate::test_fault::hit("portable_file_after_temp_write");
    if path.exists() {
        fs::copy(path, &backup).map_err(|source| CoreError::Filesystem {
            path: backup.clone(),
            source,
        })?;
        crate::test_fault::hit("portable_file_after_backup_copy");
        fs::remove_file(path).map_err(|source| CoreError::Filesystem {
            path: path.to_path_buf(),
            source,
        })?;
        crate::test_fault::hit("portable_file_after_target_remove");
    }
    fs::rename(&temp, path).map_err(|source| CoreError::Filesystem {
        path: path.to_path_buf(),
        source,
    })?;
    crate::test_fault::hit("portable_file_after_target_promote");
    Ok(())
}

fn import_paths(destination: &Path) -> Result<(PathBuf, PathBuf, PathBuf)> {
    let parent = destination.parent().ok_or_else(|| {
        CoreError::InvalidRequest("destination state directory has no parent".into())
    })?;
    let name = destination
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| {
            CoreError::InvalidRequest("destination state directory has no name".into())
        })?;
    Ok((
        parent.join(format!(".{name}.portable-import.json")),
        parent.join(format!(".{name}.portable-stage")),
        parent.join(format!(".{name}.portable-backup")),
    ))
}

fn write_import_marker_new(path: &Path, marker: &ImportMarker) -> Result<()> {
    let bytes = serde_json::to_vec_pretty(marker)?;
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|source| CoreError::Filesystem {
            path: path.to_path_buf(),
            source,
        })?;
    file.write_all(&bytes)
        .and_then(|_| file.sync_all())
        .map_err(|source| CoreError::Filesystem {
            path: path.to_path_buf(),
            source,
        })?;
    Ok(())
}

fn recover_interrupted_import(destination: &Path) -> Result<()> {
    let (marker_path, _, _) = import_paths(destination)?;
    if !marker_path.exists() {
        return Ok(());
    }
    let bytes = fs::read(&marker_path).map_err(|source| CoreError::Filesystem {
        path: marker_path.clone(),
        source,
    })?;
    let marker: ImportMarker = serde_json::from_slice(&bytes)?;
    if marker.schema_version != IMPORT_MARKER_SCHEMA_VERSION || marker.target != destination {
        return Err(CoreError::InvalidRequest(format!(
            "portable import recovery marker does not match destination: {}",
            marker_path.display()
        )));
    }
    assert_owned_import_path(destination, &marker.stage)?;
    assert_owned_import_path(destination, &marker.backup)?;
    remove_stage_profile_lease(&marker.stage)?;

    let target_exists = destination.exists();
    let stage_exists = marker.stage.exists();
    let backup_exists = marker.backup.exists();

    if target_exists && stage_exists && !backup_exists && marker.target_existed {
        // The marker was published, but the original target has not been moved yet.
        remove_owned_import_dir(destination, &marker.stage)?;
    } else if !target_exists && stage_exists && backup_exists && marker.target_existed {
        // Original target moved aside, staged replacement not yet promoted.
        fs::rename(&marker.backup, destination).map_err(|source| CoreError::Filesystem {
            path: destination.to_path_buf(),
            source,
        })?;
        remove_owned_import_dir(destination, &marker.stage)?;
    } else if !target_exists && stage_exists && !backup_exists && !marker.target_existed {
        // Fresh target interrupted before the staged profile was promoted.
        remove_owned_import_dir(destination, &marker.stage)?;
    } else if target_exists && !stage_exists && backup_exists {
        // Staged profile was promoted. Keep it and discard the old target.
        remove_owned_import_dir(destination, &marker.backup)?;
    } else if target_exists && !stage_exists && !backup_exists {
        // Promotion and cleanup completed; only the marker survived.
    } else if !target_exists && !stage_exists && !backup_exists && !marker.target_existed {
        // Marker was published before a fresh staging directory was created.
    } else if !target_exists && !stage_exists && backup_exists && marker.target_existed {
        // Original target was moved aside but staging disappeared before promotion.
        fs::rename(&marker.backup, destination).map_err(|source| CoreError::Filesystem {
            path: destination.to_path_buf(),
            source,
        })?;
    } else {
        return Err(CoreError::InvalidRequest(format!(
            "portable import recovery state is ambiguous: target={target_exists} stage={stage_exists} backup={backup_exists}"
        )));
    }
    fs::remove_file(&marker_path).map_err(|source| CoreError::Filesystem {
        path: marker_path,
        source,
    })?;
    Ok(())
}

fn remove_stage_profile_lease(stage: &Path) -> Result<()> {
    let lease = Store::profile_lease_path(stage);
    if lease.exists() {
        fs::remove_file(&lease).map_err(|source| CoreError::Filesystem {
            path: lease,
            source,
        })?;
    }
    Ok(())
}

fn remove_owned_import_dir(destination: &Path, path: &Path) -> Result<()> {
    assert_owned_import_path(destination, path)?;
    if path.exists() {
        fs::remove_dir_all(path).map_err(|source| CoreError::Filesystem {
            path: path.to_path_buf(),
            source,
        })?;
    }
    Ok(())
}

fn assert_owned_import_path(destination: &Path, path: &Path) -> Result<()> {
    let parent = destination.parent().ok_or_else(|| {
        CoreError::InvalidRequest("destination state directory has no parent".into())
    })?;
    if path.parent() != Some(parent) {
        return Err(CoreError::InvalidRequest(format!(
            "portable import artifact is outside destination parent: {}",
            path.display()
        )));
    }
    let destination_name = destination
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| {
            CoreError::InvalidRequest("destination state directory has no name".into())
        })?;
    let name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or_default();
    if !name.starts_with(&format!(".{destination_name}.portable-")) {
        return Err(CoreError::InvalidRequest(format!(
            "portable import artifact is not owned by this destination: {}",
            path.display()
        )));
    }
    Ok(())
}
