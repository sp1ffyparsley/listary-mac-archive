use crate::error::{CoreError, Result};
use crate::history::durable_json;
use crate::text::normalize;
use serde::{Deserialize, Serialize};
use std::{
    collections::HashSet,
    fs,
    path::{Path, PathBuf},
};
use uuid::Uuid;

const SETTINGS_SCHEMA_VERSION: u32 = 1;
const MAX_COMMANDS: usize = 256;
const MAX_ACTIONS: usize = 256;
const MAX_KEYWORD_CHARS: usize = 64;
const MAX_TITLE_CHARS: usize = 160;
const MAX_EXECUTABLE_CHARS: usize = 4_096;
const MAX_ARGUMENTS: usize = 64;
const MAX_ARGUMENT_CHARS: usize = 4_096;
const MAX_DIRECTORY_CHARS: usize = 4_096;
const MAX_HOTKEY_CHARS: usize = 128;

const COMMAND_PLACEHOLDERS: &[&str] = &["query", "current_folder"];
const ACTION_PLACEHOLDERS: &[&str] = &["action_path", "action_path:parent"];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum HotkeyScope {
    #[default]
    Launcher,
    FileSearch,
    Global,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum ActionSelectionMode {
    #[default]
    Single,
    EachSelection,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CustomCommand {
    pub id: Uuid,
    pub keyword: String,
    pub title: String,
    pub executable: String,
    #[serde(default)]
    pub arguments: Vec<String>,
    #[serde(default)]
    pub working_directory: Option<String>,
    #[serde(default)]
    pub hotkey: Option<String>,
    #[serde(default)]
    pub hotkey_scope: HotkeyScope,
    #[serde(default)]
    pub run_in_background: bool,
}

impl CustomCommand {
    pub fn new(
        keyword: impl Into<String>,
        title: impl Into<String>,
        executable: impl Into<String>,
    ) -> Self {
        Self {
            id: Uuid::new_v4(),
            keyword: keyword.into(),
            title: title.into(),
            executable: executable.into(),
            arguments: Vec::new(),
            working_directory: None,
            hotkey: None,
            hotkey_scope: HotkeyScope::Launcher,
            run_in_background: false,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CustomAction {
    pub id: Uuid,
    pub title: String,
    pub executable: String,
    #[serde(default)]
    pub arguments: Vec<String>,
    #[serde(default)]
    pub working_directory: Option<String>,
    #[serde(default)]
    pub selection_mode: ActionSelectionMode,
    #[serde(default)]
    pub run_in_background: bool,
}

impl CustomAction {
    pub fn new(title: impl Into<String>, executable: impl Into<String>) -> Self {
        Self {
            id: Uuid::new_v4(),
            title: title.into(),
            executable: executable.into(),
            arguments: Vec::new(),
            working_directory: None,
            selection_mode: ActionSelectionMode::Single,
            run_in_background: false,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct CommandContext {
    #[serde(default)]
    pub query: String,
    #[serde(default)]
    pub current_folder: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ActionContext {
    pub action_path: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExpandedInvocation {
    pub executable: String,
    pub arguments: Vec<String>,
    pub working_directory: Option<String>,
    pub run_in_background: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct CustomizationFile {
    schema_version: u32,
    #[serde(default)]
    commands: Vec<CustomCommand>,
    #[serde(default)]
    actions: Vec<CustomAction>,
}

impl Default for CustomizationFile {
    fn default() -> Self {
        Self {
            schema_version: SETTINGS_SCHEMA_VERSION,
            commands: Vec::new(),
            actions: Vec::new(),
        }
    }
}

#[derive(Debug)]
pub(crate) struct CustomizationStore {
    path: PathBuf,
    state: CustomizationFile,
}

impl CustomizationStore {
    pub(crate) fn open(state_dir: &Path) -> Result<Self> {
        let settings_dir = state_dir.join("settings");
        fs::create_dir_all(&settings_dir).map_err(|source| CoreError::Filesystem {
            path: settings_dir.clone(),
            source,
        })?;

        let path = settings_dir.join("commands-actions.json");
        let backup = settings_dir.join("commands-actions.json.bak");
        let temp = settings_dir.join("commands-actions.json.tmp");
        let state = durable_json::open(
            &path,
            &backup,
            &temp,
            SETTINGS_SCHEMA_VERSION,
            "commands and actions",
            validate_customization_file,
        )?;

        Ok(Self { path, state })
    }

    pub(crate) fn list_commands(&self) -> Vec<CustomCommand> {
        let mut commands = self.state.commands.clone();
        commands.sort_by(|a, b| {
            a.keyword
                .cmp(&b.keyword)
                .then_with(|| a.title.cmp(&b.title))
                .then_with(|| a.id.cmp(&b.id))
        });
        commands
    }

    pub(crate) fn list_actions(&self) -> Vec<CustomAction> {
        let mut actions = self.state.actions.clone();
        actions.sort_by(|a, b| a.title.cmp(&b.title).then_with(|| a.id.cmp(&b.id)));
        actions
    }

    pub(crate) fn put_command(&mut self, command: CustomCommand) -> Result<CustomCommand> {
        let command = canonicalize_command(command)?;
        if self
            .state
            .commands
            .iter()
            .any(|other| other.id != command.id && other.keyword == command.keyword)
        {
            return Err(CoreError::InvalidRequest(format!(
                "command keyword already exists: {}",
                command.keyword
            )));
        }
        let mut next = self.state.clone();
        if let Some(existing) = next.commands.iter_mut().find(|item| item.id == command.id) {
            *existing = command.clone();
        } else {
            if next.commands.len() >= MAX_COMMANDS {
                return Err(CoreError::InvalidRequest(format!(
                    "too many custom commands; maximum is {MAX_COMMANDS}"
                )));
            }
            next.commands.push(command.clone());
        }
        persist_customization_file(&self.path, &next)?;
        self.state = next;
        Ok(command)
    }

    pub(crate) fn remove_command(&mut self, command_id: Uuid) -> Result<()> {
        let mut next = self.state.clone();
        let before = next.commands.len();
        next.commands.retain(|command| command.id != command_id);
        if next.commands.len() == before {
            return Err(CoreError::CommandNotFound(command_id.to_string()));
        }
        persist_customization_file(&self.path, &next)?;
        self.state = next;
        Ok(())
    }

    pub(crate) fn put_action(&mut self, action: CustomAction) -> Result<CustomAction> {
        let action = canonicalize_action(action)?;
        let mut next = self.state.clone();
        if let Some(existing) = next.actions.iter_mut().find(|item| item.id == action.id) {
            *existing = action.clone();
        } else {
            if next.actions.len() >= MAX_ACTIONS {
                return Err(CoreError::InvalidRequest(format!(
                    "too many custom actions; maximum is {MAX_ACTIONS}"
                )));
            }
            next.actions.push(action.clone());
        }
        persist_customization_file(&self.path, &next)?;
        self.state = next;
        Ok(action)
    }

    pub(crate) fn remove_action(&mut self, action_id: Uuid) -> Result<()> {
        let mut next = self.state.clone();
        let before = next.actions.len();
        next.actions.retain(|action| action.id != action_id);
        if next.actions.len() == before {
            return Err(CoreError::ActionNotFound(action_id.to_string()));
        }
        persist_customization_file(&self.path, &next)?;
        self.state = next;
        Ok(())
    }

    pub(crate) fn expand_command(
        &self,
        command_id: Uuid,
        context: &CommandContext,
    ) -> Result<ExpandedInvocation> {
        let command = self
            .state
            .commands
            .iter()
            .find(|command| command.id == command_id)
            .ok_or_else(|| CoreError::CommandNotFound(command_id.to_string()))?;
        let current_folder = context.current_folder.as_deref().unwrap_or_default();
        Ok(ExpandedInvocation {
            executable: command.executable.clone(),
            arguments: command
                .arguments
                .iter()
                .map(|value| {
                    expand_template(
                        value,
                        &[
                            ("query", context.query.as_str()),
                            ("current_folder", current_folder),
                        ],
                    )
                })
                .collect(),
            working_directory: command.working_directory.as_deref().map(|value| {
                expand_template(
                    value,
                    &[
                        ("query", context.query.as_str()),
                        ("current_folder", current_folder),
                    ],
                )
            }),
            run_in_background: command.run_in_background,
        })
    }

    pub(crate) fn expand_action(
        &self,
        action_id: Uuid,
        context: &ActionContext,
    ) -> Result<ExpandedInvocation> {
        let action = self
            .state
            .actions
            .iter()
            .find(|action| action.id == action_id)
            .ok_or_else(|| CoreError::ActionNotFound(action_id.to_string()))?;
        let parent = Path::new(&context.action_path)
            .parent()
            .map(|value| value.to_string_lossy().into_owned())
            .unwrap_or_default();
        Ok(ExpandedInvocation {
            executable: action.executable.clone(),
            arguments: action
                .arguments
                .iter()
                .map(|value| {
                    expand_template(
                        value,
                        &[
                            ("action_path", context.action_path.as_str()),
                            ("action_path:parent", parent.as_str()),
                        ],
                    )
                })
                .collect(),
            working_directory: action.working_directory.as_deref().map(|value| {
                expand_template(
                    value,
                    &[
                        ("action_path", context.action_path.as_str()),
                        ("action_path:parent", parent.as_str()),
                    ],
                )
            }),
            run_in_background: action.run_in_background,
        })
    }
}

pub(crate) fn canonicalize_command(mut command: CustomCommand) -> Result<CustomCommand> {
    command.keyword = canonical_keyword(&command.keyword, "command")?;
    command.title = canonical_text(&command.title, "command title", MAX_TITLE_CHARS)?;
    command.executable = canonical_text(
        &command.executable,
        "command executable",
        MAX_EXECUTABLE_CHARS,
    )?;
    command.arguments = canonical_arguments(command.arguments, "command", COMMAND_PLACEHOLDERS)?;
    command.working_directory = canonical_optional_template(
        command.working_directory,
        "command working directory",
        MAX_DIRECTORY_CHARS,
        COMMAND_PLACEHOLDERS,
    )?;
    command.hotkey = canonical_optional_text(command.hotkey, "command hotkey", MAX_HOTKEY_CHARS)?;
    Ok(command)
}

pub(crate) fn canonicalize_action(mut action: CustomAction) -> Result<CustomAction> {
    action.title = canonical_text(&action.title, "action title", MAX_TITLE_CHARS)?;
    action.executable = canonical_text(
        &action.executable,
        "action executable",
        MAX_EXECUTABLE_CHARS,
    )?;
    action.arguments = canonical_arguments(action.arguments, "action", ACTION_PLACEHOLDERS)?;
    action.working_directory = canonical_optional_template(
        action.working_directory,
        "action working directory",
        MAX_DIRECTORY_CHARS,
        ACTION_PLACEHOLDERS,
    )?;
    Ok(action)
}

fn canonical_keyword(value: &str, kind: &str) -> Result<String> {
    let raw = value.trim();
    if raw.is_empty() {
        return Err(CoreError::InvalidRequest(format!(
            "{kind} keyword is empty"
        )));
    }
    if raw.chars().count() > MAX_KEYWORD_CHARS {
        return Err(CoreError::InvalidRequest(format!(
            "{kind} keyword is longer than {MAX_KEYWORD_CHARS} characters"
        )));
    }
    if raw
        .chars()
        .any(|ch| ch.is_whitespace() || matches!(ch, ':' | '：' | '"' | '\''))
    {
        return Err(CoreError::InvalidRequest(format!(
            "{kind} keyword cannot contain whitespace, quotes, or a colon"
        )));
    }
    let keyword = normalize(raw);
    if matches!(
        keyword.as_str(),
        "ext" | "date" | "size" | "folder" | "file" | "doc" | "pic" | "video" | "audio"
    ) {
        return Err(CoreError::InvalidRequest(format!(
            "{kind} keyword is reserved: {keyword}"
        )));
    }
    Ok(keyword)
}

fn canonical_text(value: &str, label: &str, max_chars: usize) -> Result<String> {
    let value = value.trim().to_string();
    if value.is_empty() {
        return Err(CoreError::InvalidRequest(format!("{label} is empty")));
    }
    if value.chars().count() > max_chars {
        return Err(CoreError::InvalidRequest(format!(
            "{label} is longer than {max_chars} characters"
        )));
    }
    Ok(value)
}

fn canonical_optional_text(
    value: Option<String>,
    label: &str,
    max_chars: usize,
) -> Result<Option<String>> {
    let Some(value) = value else {
        return Ok(None);
    };
    let value = value.trim().to_string();
    if value.is_empty() {
        return Ok(None);
    }
    if value.chars().count() > max_chars {
        return Err(CoreError::InvalidRequest(format!(
            "{label} is longer than {max_chars} characters"
        )));
    }
    Ok(Some(value))
}

fn canonical_arguments(
    arguments: Vec<String>,
    kind: &str,
    placeholders: &[&str],
) -> Result<Vec<String>> {
    if arguments.len() > MAX_ARGUMENTS {
        return Err(CoreError::InvalidRequest(format!(
            "{kind} has more than {MAX_ARGUMENTS} arguments"
        )));
    }
    for argument in &arguments {
        if argument.chars().count() > MAX_ARGUMENT_CHARS {
            return Err(CoreError::InvalidRequest(format!(
                "{kind} argument is longer than {MAX_ARGUMENT_CHARS} characters"
            )));
        }
        validate_placeholders(argument, placeholders)?;
    }
    Ok(arguments)
}

fn canonical_optional_template(
    value: Option<String>,
    label: &str,
    max_chars: usize,
    placeholders: &[&str],
) -> Result<Option<String>> {
    let value = canonical_optional_text(value, label, max_chars)?;
    if let Some(value) = value.as_deref() {
        validate_placeholders(value, placeholders)?;
    }
    Ok(value)
}

fn validate_placeholders(value: &str, allowed: &[&str]) -> Result<()> {
    let mut offset = 0;
    while offset < value.len() {
        let next_open = value[offset..].find('{');
        let next_close = value[offset..].find('}');
        if let Some(close_rel) = next_close
            && next_open.is_none_or(|open_rel| close_rel < open_rel)
        {
            return Err(CoreError::InvalidRequest(format!(
                "unmatched closing brace in template: {value}"
            )));
        }
        let Some(open_rel) = next_open else {
            break;
        };
        let open = offset + open_rel;
        let Some(close_rel) = value[open + 1..].find('}') else {
            return Err(CoreError::InvalidRequest(format!(
                "unterminated placeholder in template: {value}"
            )));
        };
        let close = open + 1 + close_rel;
        let name = &value[open + 1..close];
        if name.contains('{') || !allowed.contains(&name) {
            return Err(CoreError::InvalidRequest(format!(
                "unsupported placeholder {{{name}}}"
            )));
        }
        offset = close + 1;
    }
    Ok(())
}

fn expand_template(value: &str, replacements: &[(&str, &str)]) -> String {
    let mut expanded = value.to_string();
    for (name, replacement) in replacements {
        expanded = expanded.replace(&format!("{{{name}}}"), replacement);
    }
    expanded
}

fn validate_customization_file(mut state: CustomizationFile) -> Option<CustomizationFile> {
    if state.schema_version != SETTINGS_SCHEMA_VERSION
        || state.commands.len() > MAX_COMMANDS
        || state.actions.len() > MAX_ACTIONS
    {
        return None;
    }
    let mut command_ids = HashSet::new();
    let mut command_keywords = HashSet::new();
    for command in &mut state.commands {
        let canonical = canonicalize_command(command.clone()).ok()?;
        if !command_ids.insert(canonical.id) || !command_keywords.insert(canonical.keyword.clone())
        {
            return None;
        }
        *command = canonical;
    }
    let mut action_ids = HashSet::new();
    for action in &mut state.actions {
        let canonical = canonicalize_action(action.clone()).ok()?;
        if !action_ids.insert(canonical.id) {
            return None;
        }
        *action = canonical;
    }
    Some(state)
}

fn persist_customization_file(path: &Path, state: &CustomizationFile) -> Result<()> {
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
    fn command_and_action_placeholders_expand_without_shell_parsing() {
        let state = TempDir::new().unwrap();
        let mut store = CustomizationStore::open(state.path()).unwrap();
        let mut command = CustomCommand::new("find", "Find here", "/usr/bin/tool");
        command.arguments = vec!["--query={query}".into(), "{current_folder}".into()];
        command.working_directory = Some("{current_folder}".into());
        let command = store.put_command(command).unwrap();
        let expanded = store
            .expand_command(
                command.id,
                &CommandContext {
                    query: "snow fall".into(),
                    current_folder: Some("/Users/test/Scenes".into()),
                },
            )
            .unwrap();
        assert_eq!(
            expanded.arguments,
            vec!["--query=snow fall", "/Users/test/Scenes"]
        );
        assert_eq!(
            expanded.working_directory.as_deref(),
            Some("/Users/test/Scenes")
        );

        let mut action = CustomAction::new("Process", "/usr/bin/tool");
        action.arguments = vec!["{action_path}".into(), "{action_path:parent}".into()];
        let action = store.put_action(action).unwrap();
        let expanded = store
            .expand_action(
                action.id,
                &ActionContext {
                    action_path: "/Users/test/Scenes/clip.mov".into(),
                },
            )
            .unwrap();
        assert_eq!(
            expanded.arguments,
            vec!["/Users/test/Scenes/clip.mov", "/Users/test/Scenes"]
        );
    }

    #[test]
    fn customization_file_recovers_from_backup_and_rejects_unknown_placeholders() {
        let state = TempDir::new().unwrap();
        let command_id;
        {
            let mut store = CustomizationStore::open(state.path()).unwrap();
            let command = store
                .put_command(CustomCommand::new("open", "Open", "/usr/bin/open"))
                .unwrap();
            command_id = command.id;
            let mut replacement = command.clone();
            replacement.title = "Open latest".into();
            store.put_command(replacement).unwrap();
        }
        let settings = state.path().join("settings");
        let path = settings.join("commands-actions.json");
        let backup = settings.join("commands-actions.json.bak");
        let backup_before = fs::read(&backup).unwrap();
        fs::write(&path, b"{broken").unwrap();
        let store = CustomizationStore::open(state.path()).unwrap();
        assert_eq!(store.list_commands()[0].id, command_id);
        assert_eq!(fs::read(&backup).unwrap(), backup_before);
        assert!(
            validate_customization_file(serde_json::from_slice(&fs::read(&path).unwrap()).unwrap())
                .is_some()
        );

        let mut invalid = CustomCommand::new("bad", "Bad", "/usr/bin/tool");
        invalid.arguments = vec!["{selected_path}".into()];
        assert!(canonicalize_command(invalid).is_err());

        let mut unmatched = CustomCommand::new("brace", "Brace", "/usr/bin/tool");
        unmatched.arguments = vec!["prefix} {query}".into()];
        assert!(canonicalize_command(unmatched).is_err());
    }

    #[test]
    fn customization_file_future_schema_and_all_corrupt_fail_closed() {
        let future_state = TempDir::new().unwrap();
        let settings = future_state.path().join("settings");
        fs::create_dir_all(&settings).unwrap();
        let primary = settings.join("commands-actions.json");
        let backup = settings.join("commands-actions.json.bak");
        let temp = settings.join("commands-actions.json.tmp");
        let valid = serde_json::to_vec_pretty(&CustomizationFile::default()).unwrap();
        let future = serde_json::to_vec_pretty(&CustomizationFile {
            schema_version: SETTINGS_SCHEMA_VERSION + 1,
            commands: Vec::new(),
            actions: Vec::new(),
        })
        .unwrap();
        fs::write(&primary, &future).unwrap();
        fs::write(&backup, &valid).unwrap();
        fs::write(&temp, &valid).unwrap();
        let error = CustomizationStore::open(future_state.path()).unwrap_err();
        assert!(error.to_string().contains("upgrade the application"));
        assert_eq!(fs::read(&primary).unwrap(), future);
        assert_eq!(fs::read(&backup).unwrap(), valid);
        assert_eq!(fs::read(&temp).unwrap(), valid);

        let corrupt_state = TempDir::new().unwrap();
        let settings = corrupt_state.path().join("settings");
        fs::create_dir_all(&settings).unwrap();
        let primary = settings.join("commands-actions.json");
        let backup = settings.join("commands-actions.json.bak");
        let temp = settings.join("commands-actions.json.tmp");
        fs::write(&primary, b"primary-corrupt").unwrap();
        fs::write(&backup, b"backup-corrupt").unwrap();
        fs::write(&temp, b"temp-corrupt").unwrap();
        assert!(CustomizationStore::open(corrupt_state.path()).is_err());
        assert_eq!(fs::read(&primary).unwrap(), b"primary-corrupt");
        assert_eq!(fs::read(&backup).unwrap(), b"backup-corrupt");
        assert_eq!(fs::read(&temp).unwrap(), b"temp-corrupt");
    }
}
