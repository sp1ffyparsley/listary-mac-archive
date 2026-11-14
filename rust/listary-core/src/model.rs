use chrono::{DateTime, Datelike, Duration, Local, LocalResult, NaiveDate, TimeZone, Utc};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use uuid::Uuid;

pub type ItemId = u32;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum ActivationSource {
    #[default]
    Launcher,
    FileSearch,
    FileManager,
    QuickSwitch,
    Api,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ItemKind {
    File,
    Folder,
    App,
    Package,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RootKind {
    Local,
    Network,
    Removable,
    Cloud,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RootAvailability {
    Available,
    Unavailable,
    Stale,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum Priority {
    Ignored,
    Low,
    #[default]
    Normal,
    High,
}

impl Priority {
    pub fn boost(self) -> i32 {
        match self {
            Self::Ignored => -4_000,
            Self::Low => -250,
            Self::Normal => 0,
            Self::High => 250,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum RefreshPolicy {
    Never,
    #[default]
    Automatic,
    Interval {
        seconds: u64,
    },
    Daily {
        hour: u8,
        minute: u8,
    },
    Weekly {
        weekday_from_monday: u8,
        hour: u8,
        minute: u8,
    },
}

impl RefreshPolicy {
    pub fn is_timed(&self) -> bool {
        matches!(
            self,
            Self::Interval { .. } | Self::Daily { .. } | Self::Weekly { .. }
        )
    }
}

pub fn scheduled_refresh_due(
    policy: &RefreshPolicy,
    last_reconciled_unix_ms: Option<i64>,
    now_unix_ms: i64,
) -> bool {
    let Some(now_utc) = Utc.timestamp_millis_opt(now_unix_ms).single() else {
        return false;
    };
    scheduled_refresh_due_at(
        policy,
        last_reconciled_unix_ms,
        now_utc.with_timezone(&Local),
    )
}

fn scheduled_refresh_due_at<Tz>(
    policy: &RefreshPolicy,
    last_reconciled_unix_ms: Option<i64>,
    now: DateTime<Tz>,
) -> bool
where
    Tz: TimeZone,
    Tz::Offset: Copy,
{
    let Some(last_reconciled_unix_ms) = last_reconciled_unix_ms else {
        return policy.is_timed();
    };
    let now_unix_ms = now.timestamp_millis();
    if last_reconciled_unix_ms > now_unix_ms {
        // A wall-clock rollback or a persisted future timestamp must not suppress refreshes until
        // the clock catches up. One successful reconcile rewrites the runtime-owned timestamp to
        // the current clock and returns normal cadence semantics.
        return policy.is_timed();
    }
    match policy {
        RefreshPolicy::Never | RefreshPolicy::Automatic => false,
        RefreshPolicy::Interval { seconds } => {
            if *seconds == 0 {
                return false;
            }
            let interval_ms = seconds.saturating_mul(1_000).min(i64::MAX as u64) as i64;
            now_unix_ms.saturating_sub(last_reconciled_unix_ms) >= interval_ms
        }
        RefreshPolicy::Daily { hour, minute } => most_recent_daily_occurrence(&now, *hour, *minute)
            .is_some_and(|scheduled| last_reconciled_unix_ms < scheduled.timestamp_millis()),
        RefreshPolicy::Weekly {
            weekday_from_monday,
            hour,
            minute,
        } => most_recent_weekly_occurrence(&now, *weekday_from_monday, *hour, *minute)
            .is_some_and(|scheduled| last_reconciled_unix_ms < scheduled.timestamp_millis()),
    }
}

fn most_recent_daily_occurrence<Tz>(
    now: &DateTime<Tz>,
    hour: u8,
    minute: u8,
) -> Option<DateTime<Tz>>
where
    Tz: TimeZone,
    Tz::Offset: Copy,
{
    let timezone = now.timezone();
    let today = now.date_naive();
    let today_occurrence = resolve_wall_clock(&timezone, today, hour, minute)?;
    if today_occurrence.timestamp_millis() <= now.timestamp_millis() {
        return Some(today_occurrence);
    }
    resolve_wall_clock(
        &timezone,
        today.checked_sub_signed(Duration::days(1))?,
        hour,
        minute,
    )
}

fn most_recent_weekly_occurrence<Tz>(
    now: &DateTime<Tz>,
    weekday_from_monday: u8,
    hour: u8,
    minute: u8,
) -> Option<DateTime<Tz>>
where
    Tz: TimeZone,
    Tz::Offset: Copy,
{
    if weekday_from_monday >= 7 {
        return None;
    }
    let timezone = now.timezone();
    let days_since_target =
        (now.weekday().num_days_from_monday() + 7 - u32::from(weekday_from_monday)) % 7;
    let candidate_date = now
        .date_naive()
        .checked_sub_signed(Duration::days(i64::from(days_since_target)))?;
    let candidate = resolve_wall_clock(&timezone, candidate_date, hour, minute)?;
    if candidate.timestamp_millis() <= now.timestamp_millis() {
        return Some(candidate);
    }
    resolve_wall_clock(
        &timezone,
        candidate_date.checked_sub_signed(Duration::days(7))?,
        hour,
        minute,
    )
}

fn resolve_wall_clock<Tz>(
    timezone: &Tz,
    date: NaiveDate,
    hour: u8,
    minute: u8,
) -> Option<DateTime<Tz>>
where
    Tz: TimeZone,
    Tz::Offset: Copy,
{
    if hour >= 24 || minute >= 60 {
        return None;
    }
    let requested = date.and_hms_opt(u32::from(hour), u32::from(minute), 0)?;
    for minute_offset in 0..=180 {
        let candidate = requested.checked_add_signed(Duration::minutes(minute_offset))?;
        match timezone.from_local_datetime(&candidate) {
            LocalResult::Single(value) => return Some(value),
            LocalResult::Ambiguous(first, second) => {
                return Some(if first.timestamp_millis() <= second.timestamp_millis() {
                    first
                } else {
                    second
                });
            }
            LocalResult::None => {}
        }
    }
    None
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RootRecord {
    pub id: Uuid,
    pub path: PathBuf,
    pub kind: RootKind,
    pub availability: RootAvailability,
    pub refresh_policy: RefreshPolicy,
    pub priority: Priority,
    pub enabled: bool,
    pub last_reconciled_unix_ms: Option<i64>,
    pub last_error: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RootRecoveryRemovalReport {
    pub root_id: Uuid,
    pub root_path: PathBuf,
    pub deleted_items: u64,
    pub catalog_generation_before: u64,
    pub catalog_generation_after_disable: u64,
    pub catalog_generation_after_remove: u64,
}

impl RootRecord {
    pub fn new(path: PathBuf, kind: RootKind) -> Self {
        Self {
            id: Uuid::new_v4(),
            path,
            kind,
            availability: RootAvailability::Available,
            refresh_policy: RefreshPolicy::Automatic,
            priority: Priority::Normal,
            enabled: true,
            last_reconciled_unix_ms: None,
            last_error: None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ItemRecord {
    pub id: ItemId,
    pub root_id: Uuid,
    pub path: String,
    pub parent_path: String,
    pub name: String,
    pub extension: String,
    pub kind: ItemKind,
    pub size: u64,
    pub modified_unix_ms: i64,
    pub available: bool,
    pub priority: Priority,
    pub stable_file_id: Option<String>,
    pub preview_hint: Option<String>,
}

impl ItemRecord {
    pub fn from_path_metadata(
        id: ItemId,
        root_id: Uuid,
        path: &Path,
        kind: ItemKind,
        size: u64,
        modified_unix_ms: i64,
        priority: Priority,
    ) -> Self {
        let name = path
            .file_name()
            .map(|v| v.to_string_lossy().into_owned())
            .unwrap_or_default();
        let extension = path
            .extension()
            .map(|v| v.to_string_lossy().to_lowercase())
            .unwrap_or_default();
        let parent_path = path
            .parent()
            .map(|v| v.to_string_lossy().into_owned())
            .unwrap_or_default();
        Self {
            id,
            root_id,
            path: path.to_string_lossy().into_owned(),
            parent_path,
            name,
            extension,
            kind,
            size,
            modified_unix_ms,
            available: true,
            priority,
            stable_file_id: None,
            preview_hint: None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DiscoveredItem {
    pub path: PathBuf,
    pub kind: ItemKind,
    pub size: u64,
    pub modified_unix_ms: i64,
    pub priority: Priority,
    pub stable_file_id: Option<String>,
    pub preview_hint: Option<String>,
    #[serde(default)]
    pub search_name: Option<String>,
}

impl DiscoveredItem {
    pub fn new(path: PathBuf, kind: ItemKind, size: u64, modified_unix_ms: i64) -> Self {
        Self {
            path,
            kind,
            size,
            modified_unix_ms,
            priority: Priority::Normal,
            stable_file_id: None,
            preview_hint: None,
            search_name: None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct SearchContext {
    pub current_folder: Option<String>,
    pub contextual: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FileFacetCategory {
    Folder,
    Document,
    Picture,
    Video,
    Audio,
    Other,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum ModifiedTimeFacet {
    #[serde(rename = "last_1_day")]
    Last1Day,
    #[serde(rename = "last_7_days")]
    Last7Days,
    #[serde(rename = "last_30_days")]
    Last30Days,
    #[serde(rename = "last_365_days")]
    Last365Days,
    #[serde(rename = "older")]
    Older,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchRequest {
    pub query: String,
    pub limit: usize,
    pub candidate_limit: usize,
    pub include_unavailable: bool,
    pub root_ids: Vec<Uuid>,
    pub kinds: Vec<ItemKind>,
    #[serde(default)]
    pub categories: Vec<FileFacetCategory>,
    #[serde(default)]
    pub modified_time: Option<ModifiedTimeFacet>,
    pub context: SearchContext,
    pub now_unix_ms: i64,
    pub timezone_offset_minutes: i32,
}

impl Default for SearchRequest {
    fn default() -> Self {
        Self {
            query: String::new(),
            limit: 50,
            candidate_limit: 4_096,
            include_unavailable: true,
            root_ids: Vec::new(),
            kinds: Vec::new(),
            categories: Vec::new(),
            modified_time: None,
            context: SearchContext::default(),
            now_unix_ms: chrono::Utc::now().timestamp_millis(),
            timezone_offset_minutes: 0,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FacetCountsRequest {
    pub query: String,
    pub include_unavailable: bool,
    pub root_ids: Vec<Uuid>,
    #[serde(default)]
    pub categories: Vec<FileFacetCategory>,
    #[serde(default)]
    pub modified_time: Option<ModifiedTimeFacet>,
    pub now_unix_ms: i64,
    pub timezone_offset_minutes: i32,
}

impl Default for FacetCountsRequest {
    fn default() -> Self {
        Self {
            query: String::new(),
            include_unavailable: true,
            root_ids: Vec::new(),
            categories: Vec::new(),
            modified_time: None,
            now_unix_ms: chrono::Utc::now().timestamp_millis(),
            timezone_offset_minutes: 0,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FileFacetCount {
    pub category: FileFacetCategory,
    pub count: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModifiedTimeFacetCount {
    pub range: ModifiedTimeFacet,
    pub count: u64,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct FileQuickFilterCounts {
    pub folder: u64,
    pub file: u64,
    pub pdf: u64,
    pub today: u64,
    pub week: u64,
    pub large: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FileSearchFacetProjection {
    /// Rows satisfying the current query plus selected Type and Modified facets.
    pub matched_total: u64,
    /// Rows satisfying the query plus Modified facet while ignoring Type selection.
    pub category_scope_total: u64,
    /// Rows satisfying the query plus Type selection while ignoring Modified selection.
    pub modified_scope_total: u64,
    pub categories: Vec<FileFacetCount>,
    pub modified_time: Vec<ModifiedTimeFacetCount>,
    /// Counts for the six built-in Windows quick-filter predicates under the current
    /// query plus selected Type/Modified facets. They are projections only and never
    /// grant action or mutation authority.
    pub quick_filters: FileQuickFilterCounts,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FacetCountsResponse {
    pub matched_total: u64,
    pub categories: Vec<FileFacetCount>,
    pub modified_time: Vec<ModifiedTimeFacetCount>,
    pub considered_candidates: u64,
    pub elapsed_micros: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScoreBreakdown {
    pub text_tier: u8,
    pub text_quality: i32,
    pub priority_boost: i32,
    pub context_boost: i32,
    pub history_boost: i32,
    pub recency_boost: i32,
    pub combined: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchResult {
    pub item: ItemRecord,
    pub score: ScoreBreakdown,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchResponse {
    pub results: Vec<SearchResult>,
    pub parsed: crate::query::ParsedQuery,
    pub considered_candidates: usize,
    pub cancelled: bool,
    pub elapsed_micros: u64,
}

pub const MAX_FILE_SEARCH_PAGE_LIMIT: u32 = 1_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum FileSearchSortField {
    #[default]
    Relevance,
    Name,
    Path,
    Kind,
    Modified,
    Size,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum SortDirection {
    Ascending,
    #[default]
    Descending,
}

/// Exact, pageable File Search request.
///
/// Unlike [`SearchRequest`], this request never applies an interactive candidate budget. The
/// caller must still keep pages bounded with `limit`; the core validates that `limit` is in
/// `1..=MAX_FILE_SEARCH_PAGE_LIMIT`.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct FileSearchRequest {
    pub query: String,
    pub include_unavailable: bool,
    pub root_ids: Vec<Uuid>,
    pub categories: Vec<FileFacetCategory>,
    pub modified_time: Option<ModifiedTimeFacet>,
    pub now_unix_ms: i64,
    pub timezone_offset_minutes: i32,
    pub offset: u64,
    pub limit: u32,
    pub sort_field: FileSearchSortField,
    pub sort_direction: SortDirection,
    pub expected_catalog_generation: Option<u64>,
    /// Request cross-faceted counts from the same exact File Search candidate scan.
    /// Kept opt-in so non-UI callers preserve the pre-facet hot path.
    pub include_facets: bool,
}

impl Default for FileSearchRequest {
    fn default() -> Self {
        Self {
            query: String::new(),
            include_unavailable: true,
            root_ids: Vec::new(),
            categories: Vec::new(),
            modified_time: None,
            now_unix_ms: chrono::Utc::now().timestamp_millis(),
            timezone_offset_minutes: 0,
            offset: 0,
            limit: 100,
            sort_field: FileSearchSortField::Relevance,
            sort_direction: SortDirection::Descending,
            expected_catalog_generation: None,
            include_facets: false,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FileSearchResponse {
    pub results: Vec<SearchResult>,
    pub total_matches: u64,
    pub offset: u64,
    pub limit: u32,
    pub has_more: bool,
    pub catalog_generation: u64,
    pub considered_candidates: u64,
    pub parsed: crate::query::ParsedQuery,
    pub elapsed_micros: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub facets: Option<FileSearchFacetProjection>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Recommendation {
    pub item: ItemRecord,
    pub activation_count: u32,
    pub last_activated_unix_ms: i64,
    pub last_source: ActivationSource,
    pub score: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct CoreDiagnostics {
    pub schema_version: u32,
    pub catalog_generation: u64,
    pub item_count: usize,
    pub root_count: usize,
    pub available_root_count: usize,
    pub unavailable_root_count: usize,
    pub index_bytes: u64,
    pub history_entries: usize,
    pub recovery_count: u64,
    pub last_recovery_path: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::FixedOffset;

    fn fixed_datetime(
        timezone: FixedOffset,
        year: i32,
        month: u32,
        day: u32,
        hour: u32,
        minute: u32,
    ) -> DateTime<FixedOffset> {
        timezone
            .with_ymd_and_hms(year, month, day, hour, minute, 0)
            .single()
            .unwrap()
    }

    #[test]
    fn timed_refresh_due_interval_uses_last_successful_reconcile() {
        let now = 2_000_000_i64;
        assert!(!scheduled_refresh_due(&RefreshPolicy::Never, None, now));
        assert!(!scheduled_refresh_due(&RefreshPolicy::Automatic, None, now));
        assert!(scheduled_refresh_due(
            &RefreshPolicy::Interval { seconds: 1 },
            None,
            now
        ));
        assert!(!scheduled_refresh_due(
            &RefreshPolicy::Interval { seconds: 1 },
            Some(now - 999),
            now
        ));
        assert!(scheduled_refresh_due(
            &RefreshPolicy::Interval { seconds: 1 },
            Some(now - 1_000),
            now
        ));
        assert!(scheduled_refresh_due(
            &RefreshPolicy::Interval { seconds: 1 },
            Some(now + 1_000),
            now
        ));
    }

    #[test]
    fn daily_refresh_becomes_due_at_the_local_wall_clock_once_per_day() {
        let timezone = FixedOffset::west_opt(4 * 60 * 60).unwrap();
        let policy = RefreshPolicy::Daily {
            hour: 4,
            minute: 30,
        };
        let previous_success = fixed_datetime(timezone, 2026, 9, 27, 4, 31).timestamp_millis();
        let before = fixed_datetime(timezone, 2026, 9, 28, 4, 29);
        let after = fixed_datetime(timezone, 2026, 9, 28, 4, 31);
        assert!(!scheduled_refresh_due_at(
            &policy,
            Some(previous_success),
            before
        ));
        assert!(scheduled_refresh_due_at(
            &policy,
            Some(previous_success),
            after
        ));

        let current_success = fixed_datetime(timezone, 2026, 9, 28, 4, 30).timestamp_millis();
        let later = fixed_datetime(timezone, 2026, 9, 28, 18, 0);
        assert!(!scheduled_refresh_due_at(
            &policy,
            Some(current_success),
            later
        ));
    }

    #[test]
    fn weekly_refresh_becomes_due_at_the_configured_local_weekday_and_time() {
        let timezone = FixedOffset::west_opt(4 * 60 * 60).unwrap();
        let policy = RefreshPolicy::Weekly {
            weekday_from_monday: 0,
            hour: 4,
            minute: 30,
        };
        let previous_success = fixed_datetime(timezone, 2026, 9, 21, 4, 31).timestamp_millis();
        let monday_before = fixed_datetime(timezone, 2026, 9, 28, 4, 29);
        let monday_after = fixed_datetime(timezone, 2026, 9, 28, 4, 31);
        assert!(!scheduled_refresh_due_at(
            &policy,
            Some(previous_success),
            monday_before
        ));
        assert!(scheduled_refresh_due_at(
            &policy,
            Some(previous_success),
            monday_after
        ));

        let current_success = fixed_datetime(timezone, 2026, 9, 28, 4, 30).timestamp_millis();
        let tuesday = fixed_datetime(timezone, 2026, 9, 29, 18, 0);
        assert!(!scheduled_refresh_due_at(
            &policy,
            Some(current_success),
            tuesday
        ));
    }
}
