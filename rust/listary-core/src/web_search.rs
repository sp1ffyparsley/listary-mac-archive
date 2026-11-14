use crate::{CoreError, Result, history::durable_json, text::normalize};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashSet,
    fs,
    path::{Path, PathBuf},
};
use uuid::Uuid;

const WEB_SEARCH_SCHEMA_VERSION: u32 = 1;
const MAX_PROVIDERS: usize = 256;
const MAX_KEYWORD_CHARS: usize = 64;
const MAX_TITLE_CHARS: usize = 160;
const MAX_URL_CHARS: usize = 4096;
const MAX_HOTKEY_CHARS: usize = 128;
const MAX_ICON_PATH_CHARS: usize = 4096;
const QUERY_PLACEHOLDER: &str = "query";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WebSearchProvider {
    pub id: Uuid,
    #[serde(default = "default_enabled")]
    pub enabled: bool,
    pub keyword: String,
    #[serde(default)]
    pub hotkey: Option<String>,
    pub title: String,
    pub url_template: String,
    #[serde(default)]
    pub suggestion_provider: WebSuggestionProvider,
    #[serde(default)]
    pub icon_path: Option<String>,
}

impl WebSearchProvider {
    pub fn new(
        keyword: impl Into<String>,
        title: impl Into<String>,
        url_template: impl Into<String>,
    ) -> Self {
        Self {
            id: Uuid::new_v4(),
            enabled: true,
            keyword: keyword.into(),
            hotkey: None,
            title: title.into(),
            url_template: url_template.into(),
            suggestion_provider: WebSuggestionProvider::None,
            icon_path: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum WebSuggestionProvider {
    #[default]
    None,
    Google,
    Baidu,
    Bing,
    Custom {
        url_template: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "mode", rename_all = "snake_case")]
pub enum WebSearchRequest {
    Input { input: String },
    Provider { provider_id: Uuid, query: String },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct WebSearchResolution {
    pub query: String,
    pub matches: Vec<ResolvedWebSearch>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ResolvedWebSearch {
    pub provider_id: Uuid,
    pub keyword: String,
    pub title: String,
    pub url: String,
    #[serde(default)]
    pub hotkey: Option<String>,
    #[serde(default)]
    pub icon_path: Option<String>,
    #[serde(default)]
    pub suggestion: Option<ResolvedWebSuggestion>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ResolvedWebSuggestion {
    Google,
    Baidu,
    Bing,
    Custom { url: String },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct WebSearchFile {
    schema_version: u32,
    providers: Vec<WebSearchProvider>,
}

impl Default for WebSearchFile {
    fn default() -> Self {
        Self {
            schema_version: WEB_SEARCH_SCHEMA_VERSION,
            providers: Vec::new(),
        }
    }
}

#[derive(Debug)]
pub(crate) struct WebSearchStore {
    path: PathBuf,
    state: WebSearchFile,
}

impl WebSearchStore {
    pub(crate) fn open(state_dir: &Path) -> Result<Self> {
        let settings_dir = state_dir.join("settings");
        fs::create_dir_all(&settings_dir).map_err(|source| CoreError::Filesystem {
            path: settings_dir.clone(),
            source,
        })?;
        let path = settings_dir.join("web-search.json");
        let backup = settings_dir.join("web-search.json.bak");
        let temp = settings_dir.join("web-search.json.tmp");
        let state = durable_json::open(
            &path,
            &backup,
            &temp,
            WEB_SEARCH_SCHEMA_VERSION,
            "web search",
            validate_web_search_file,
        )?;
        Ok(Self { path, state })
    }

    pub(crate) fn list(&self) -> Vec<WebSearchProvider> {
        self.state.providers.clone()
    }

    pub(crate) fn replace(
        &mut self,
        providers: Vec<WebSearchProvider>,
    ) -> Result<Vec<WebSearchProvider>> {
        let providers = canonicalize_web_search_providers(providers)?;
        let state = WebSearchFile {
            schema_version: WEB_SEARCH_SCHEMA_VERSION,
            providers,
        };
        persist_web_search_file(&self.path, &state)?;
        self.state = state;
        Ok(self.state.providers.clone())
    }

    pub(crate) fn resolve(&self, request: &WebSearchRequest) -> Result<WebSearchResolution> {
        match request {
            WebSearchRequest::Input { input } => self.resolve_input(input),
            WebSearchRequest::Provider { provider_id, query } => {
                let provider = self
                    .state
                    .providers
                    .iter()
                    .find(|provider| provider.id == *provider_id)
                    .ok_or_else(|| {
                        CoreError::InvalidRequest(format!(
                            "web search provider not found: {provider_id}"
                        ))
                    })?;
                if !provider.enabled {
                    return Err(CoreError::InvalidRequest(format!(
                        "web search provider is disabled: {provider_id}"
                    )));
                }
                let query = query.trim().to_string();
                Ok(WebSearchResolution {
                    query: query.clone(),
                    matches: vec![resolve_provider(provider, &query)],
                })
            }
        }
    }

    fn resolve_input(&self, input: &str) -> Result<WebSearchResolution> {
        let input = input.trim_start();
        let Some(boundary) = input.find(char::is_whitespace) else {
            return Ok(WebSearchResolution::default());
        };
        let keyword = normalize(&input[..boundary]);
        if keyword.is_empty() {
            return Ok(WebSearchResolution::default());
        }
        let query = input[boundary..].trim().to_string();
        let matches = self
            .state
            .providers
            .iter()
            .filter(|provider| provider.enabled && provider.keyword == keyword)
            .map(|provider| resolve_provider(provider, &query))
            .collect();
        Ok(WebSearchResolution { query, matches })
    }
}

pub(crate) fn canonicalize_web_search_providers(
    providers: Vec<WebSearchProvider>,
) -> Result<Vec<WebSearchProvider>> {
    if providers.len() > MAX_PROVIDERS {
        return Err(CoreError::InvalidRequest(format!(
            "web search has more than {MAX_PROVIDERS} providers"
        )));
    }
    let mut ids = HashSet::new();
    providers
        .into_iter()
        .map(|provider| {
            if !ids.insert(provider.id) {
                return Err(CoreError::InvalidRequest(format!(
                    "duplicate web search provider id: {}",
                    provider.id
                )));
            }
            canonicalize_web_search_provider(provider)
        })
        .collect()
}

pub(crate) fn map_web_search_paths<F>(
    providers: &mut [WebSearchProvider],
    mut map_path: F,
) -> Result<()>
where
    F: FnMut(&str) -> Result<String>,
{
    for provider in providers {
        if let Some(icon_path) = provider.icon_path.as_mut()
            && looks_absolute_path(icon_path)
        {
            *icon_path = map_path(icon_path)?;
        }
    }
    Ok(())
}

fn canonicalize_web_search_provider(mut provider: WebSearchProvider) -> Result<WebSearchProvider> {
    provider.keyword = canonical_keyword(&provider.keyword)?;
    provider.title = canonical_text(&provider.title, "web search title", MAX_TITLE_CHARS)?;
    provider.url_template = canonical_url_template(&provider.url_template, "web search URL")?;
    provider.hotkey =
        canonical_optional_text(provider.hotkey, "web search hotkey", MAX_HOTKEY_CHARS)?;
    provider.icon_path = canonical_icon_path(provider.icon_path)?;
    provider.suggestion_provider = match provider.suggestion_provider {
        WebSuggestionProvider::Custom { url_template } => WebSuggestionProvider::Custom {
            url_template: canonical_url_template(
                &url_template,
                "web search custom suggestion URL",
            )?,
        },
        other => other,
    };
    Ok(provider)
}

fn canonical_keyword(value: &str) -> Result<String> {
    let value = value.trim();
    if value.is_empty() {
        return Err(CoreError::InvalidRequest(
            "web search keyword is empty".into(),
        ));
    }
    if value.chars().count() > MAX_KEYWORD_CHARS {
        return Err(CoreError::InvalidRequest(format!(
            "web search keyword is longer than {MAX_KEYWORD_CHARS} characters"
        )));
    }
    if value.chars().any(char::is_whitespace) {
        return Err(CoreError::InvalidRequest(
            "web search keyword cannot contain whitespace".into(),
        ));
    }
    Ok(normalize(value))
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
    value
        .map(|value| canonical_text(&value, label, max_chars))
        .transpose()
}

fn canonical_icon_path(value: Option<String>) -> Result<Option<String>> {
    let Some(path) = canonical_optional_text(value, "web search icon path", MAX_ICON_PATH_CHARS)?
    else {
        return Ok(None);
    };
    let extension = Path::new(&path)
        .extension()
        .and_then(|value| value.to_str())
        .map(|value| value.to_ascii_lowercase());
    if !matches!(
        extension.as_deref(),
        Some("png" | "bmp" | "jpg" | "jpeg" | "ico")
    ) {
        return Err(CoreError::InvalidRequest(
            "web search icon must use png, bmp, jpg, jpeg, or ico".into(),
        ));
    }
    Ok(Some(path))
}

fn canonical_url_template(value: &str, label: &str) -> Result<String> {
    let value = canonical_text(value, label, MAX_URL_CHARS)?;
    let lower = value.to_ascii_lowercase();
    if !(lower.starts_with("https://") || lower.starts_with("http://")) {
        return Err(CoreError::InvalidRequest(format!(
            "{label} must use http or https"
        )));
    }
    validate_template_placeholders(&value, label)?;
    if !value.contains("{query}") {
        return Err(CoreError::InvalidRequest(format!(
            "{label} must contain {{query}}"
        )));
    }
    Ok(value)
}

fn validate_template_placeholders(value: &str, label: &str) -> Result<()> {
    let bytes = value.as_bytes();
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'{' => {
                let start = index + 1;
                let Some(relative_end) = value[start..].find('}') else {
                    return Err(CoreError::InvalidRequest(format!(
                        "{label} has an unterminated placeholder"
                    )));
                };
                let end = start + relative_end;
                let placeholder = &value[start..end];
                if placeholder != QUERY_PLACEHOLDER || placeholder.contains('{') {
                    return Err(CoreError::InvalidRequest(format!(
                        "{label} contains unsupported placeholder: {{{placeholder}}}"
                    )));
                }
                index = end + 1;
            }
            b'}' => {
                return Err(CoreError::InvalidRequest(format!(
                    "{label} contains an unmatched closing brace"
                )));
            }
            _ => index += 1,
        }
    }
    Ok(())
}

fn resolve_provider(provider: &WebSearchProvider, query: &str) -> ResolvedWebSearch {
    let encoded_query = percent_encode_query(query);
    let suggestion = if query.is_empty() {
        None
    } else {
        match &provider.suggestion_provider {
            WebSuggestionProvider::None => None,
            WebSuggestionProvider::Google => Some(ResolvedWebSuggestion::Google),
            WebSuggestionProvider::Baidu => Some(ResolvedWebSuggestion::Baidu),
            WebSuggestionProvider::Bing => Some(ResolvedWebSuggestion::Bing),
            WebSuggestionProvider::Custom { url_template } => Some(ResolvedWebSuggestion::Custom {
                url: expand_query_template(url_template, &encoded_query),
            }),
        }
    };
    ResolvedWebSearch {
        provider_id: provider.id,
        keyword: provider.keyword.clone(),
        title: provider.title.clone(),
        url: expand_query_template(&provider.url_template, &encoded_query),
        hotkey: provider.hotkey.clone(),
        icon_path: provider.icon_path.clone(),
        suggestion,
    }
}

fn expand_query_template(template: &str, encoded_query: &str) -> String {
    template.replace("{query}", encoded_query)
}

fn percent_encode_query(query: &str) -> String {
    let mut encoded = String::with_capacity(query.len());
    for byte in query.as_bytes() {
        if byte.is_ascii_alphanumeric() || matches!(*byte, b'-' | b'.' | b'_' | b'~') {
            encoded.push(char::from(*byte));
        } else {
            use std::fmt::Write as _;
            let _ = write!(&mut encoded, "%{byte:02X}");
        }
    }
    encoded
}

fn default_enabled() -> bool {
    true
}

fn looks_absolute_path(path: &str) -> bool {
    path.starts_with('/')
        || path.starts_with("\\\\")
        || (path.len() >= 3
            && path.as_bytes()[0].is_ascii_alphabetic()
            && path.as_bytes()[1] == b':'
            && matches!(path.as_bytes()[2], b'/' | b'\\'))
}

fn validate_web_search_file(mut state: WebSearchFile) -> Option<WebSearchFile> {
    if state.schema_version != WEB_SEARCH_SCHEMA_VERSION {
        return None;
    }
    state.providers = canonicalize_web_search_providers(state.providers).ok()?;
    Some(state)
}

fn persist_web_search_file(path: &Path, state: &WebSearchFile) -> Result<()> {
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

    fn provider(keyword: &str, title: &str, id: Uuid) -> WebSearchProvider {
        WebSearchProvider {
            id,
            enabled: true,
            keyword: keyword.into(),
            hotkey: None,
            title: title.into(),
            url_template: "https://example.test/search?q={query}".into(),
            suggestion_provider: WebSuggestionProvider::None,
            icon_path: None,
        }
    }

    #[test]
    fn preserves_order_duplicate_keywords_and_recovers_from_backup() {
        let state = tempfile::TempDir::new().unwrap();
        let mut store = WebSearchStore::open(state.path()).unwrap();
        let first = provider("Store", "First", Uuid::new_v4());
        let mut second = provider("store", "Second", Uuid::new_v4());
        second.hotkey = Some("Ctrl+2".into());
        let canonical = store.replace(vec![first.clone(), second.clone()]).unwrap();
        assert_eq!(canonical[0].keyword, "store");
        assert_eq!(canonical[1].keyword, "store");
        assert_eq!(canonical[0].title, "First");
        assert_eq!(canonical[1].title, "Second");

        let replacement = provider("other", "Other", Uuid::new_v4());
        store.replace(vec![replacement]).unwrap();
        let settings = state.path().join("settings");
        let path = settings.join("web-search.json");
        let backup = settings.join("web-search.json.bak");
        let backup_before = fs::read(&backup).unwrap();
        fs::write(&path, b"{broken").unwrap();
        let recovered = WebSearchStore::open(state.path()).unwrap();
        assert_eq!(recovered.list(), canonical);
        assert_eq!(fs::read(&backup).unwrap(), backup_before);
        assert!(
            validate_web_search_file(serde_json::from_slice(&fs::read(&path).unwrap()).unwrap())
                .is_some()
        );

        let duplicate_id = Uuid::new_v4();
        assert!(
            canonicalize_web_search_providers(vec![
                provider("a", "A", duplicate_id),
                provider("b", "B", duplicate_id),
            ])
            .is_err()
        );
    }

    #[test]
    fn web_search_file_future_schema_and_all_corrupt_fail_closed() {
        let future_state = tempfile::TempDir::new().unwrap();
        let settings = future_state.path().join("settings");
        fs::create_dir_all(&settings).unwrap();
        let primary = settings.join("web-search.json");
        let backup = settings.join("web-search.json.bak");
        let temp = settings.join("web-search.json.tmp");
        let valid = serde_json::to_vec_pretty(&WebSearchFile::default()).unwrap();
        let future = serde_json::to_vec_pretty(&WebSearchFile {
            schema_version: WEB_SEARCH_SCHEMA_VERSION + 1,
            providers: Vec::new(),
        })
        .unwrap();
        fs::write(&primary, &future).unwrap();
        fs::write(&backup, &valid).unwrap();
        fs::write(&temp, &valid).unwrap();
        let error = WebSearchStore::open(future_state.path()).unwrap_err();
        assert!(error.to_string().contains("upgrade the application"));
        assert_eq!(fs::read(&primary).unwrap(), future);
        assert_eq!(fs::read(&backup).unwrap(), valid);
        assert_eq!(fs::read(&temp).unwrap(), valid);

        let corrupt_state = tempfile::TempDir::new().unwrap();
        let settings = corrupt_state.path().join("settings");
        fs::create_dir_all(&settings).unwrap();
        let primary = settings.join("web-search.json");
        let backup = settings.join("web-search.json.bak");
        let temp = settings.join("web-search.json.tmp");
        fs::write(&primary, b"primary-corrupt").unwrap();
        fs::write(&backup, b"backup-corrupt").unwrap();
        fs::write(&temp, b"temp-corrupt").unwrap();
        assert!(WebSearchStore::open(corrupt_state.path()).is_err());
        assert_eq!(fs::read(&primary).unwrap(), b"primary-corrupt");
        assert_eq!(fs::read(&backup).unwrap(), b"backup-corrupt");
        assert_eq!(fs::read(&temp).unwrap(), b"temp-corrupt");
    }

    #[test]
    fn resolves_keyword_and_provider_modes_without_network_io() {
        let state = tempfile::TempDir::new().unwrap();
        let mut store = WebSearchStore::open(state.path()).unwrap();
        let first_id = Uuid::new_v4();
        let second_id = Uuid::new_v4();
        let disabled_id = Uuid::new_v4();
        let mut first = provider("store", "First", first_id);
        first.suggestion_provider = WebSuggestionProvider::Google;
        let mut second = provider("store", "Second", second_id);
        second.url_template = "https://second.test/?term={query}".into();
        second.suggestion_provider = WebSuggestionProvider::Custom {
            url_template: "https://suggest.test/?q={query}".into(),
        };
        let mut disabled = provider("store", "Disabled", disabled_id);
        disabled.enabled = false;
        store.replace(vec![first, second, disabled]).unwrap();

        let resolved = store
            .resolve(&WebSearchRequest::Input {
                input: "STORE café au lait".into(),
            })
            .unwrap();
        assert_eq!(resolved.query, "café au lait");
        assert_eq!(resolved.matches.len(), 2);
        assert_eq!(resolved.matches[0].provider_id, first_id);
        assert_eq!(
            resolved.matches[0].url,
            "https://example.test/search?q=caf%C3%A9%20au%20lait"
        );
        assert!(matches!(
            resolved.matches[0].suggestion,
            Some(ResolvedWebSuggestion::Google)
        ));
        assert!(matches!(
            &resolved.matches[1].suggestion,
            Some(ResolvedWebSuggestion::Custom { url })
                if url == "https://suggest.test/?q=caf%C3%A9%20au%20lait"
        ));

        let direct = store
            .resolve(&WebSearchRequest::Provider {
                provider_id: second_id,
                query: "rust ffi".into(),
            })
            .unwrap();
        assert_eq!(direct.matches.len(), 1);
        assert_eq!(
            direct.matches[0].url,
            "https://second.test/?term=rust%20ffi"
        );
        assert!(
            store
                .resolve(&WebSearchRequest::Provider {
                    provider_id: disabled_id,
                    query: "x".into(),
                })
                .is_err()
        );
        assert!(
            store
                .resolve(&WebSearchRequest::Input {
                    input: "store".into()
                })
                .unwrap()
                .matches
                .is_empty()
        );
    }

    #[test]
    fn rejects_invalid_templates_icons_and_unknown_placeholders() {
        let id = Uuid::new_v4();
        let mut candidate = provider("g", "Google", id);
        candidate.url_template = "ftp://example.test/?q={query}".into();
        assert!(canonicalize_web_search_providers(vec![candidate]).is_err());

        let mut candidate = provider("g", "Google", id);
        candidate.url_template = "https://example.test/?q={other}".into();
        assert!(canonicalize_web_search_providers(vec![candidate]).is_err());

        let mut candidate = provider("g", "Google", id);
        candidate.url_template = "https://example.test/search".into();
        assert!(canonicalize_web_search_providers(vec![candidate]).is_err());

        let mut candidate = provider("g", "Google", id);
        candidate.icon_path = Some("/tmp/icon.svg".into());
        assert!(canonicalize_web_search_providers(vec![candidate]).is_err());
    }
}
