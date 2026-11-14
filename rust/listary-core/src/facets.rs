use crate::{
    cache::CachedItem,
    model::{
        FileFacetCategory, FileFacetCount, FileQuickFilterCounts, FileSearchFacetProjection,
        ItemKind, ModifiedTimeFacet, ModifiedTimeFacetCount,
    },
    query,
};

pub(crate) const FILE_FACET_CATEGORIES: [FileFacetCategory; 6] = [
    FileFacetCategory::Folder,
    FileFacetCategory::Document,
    FileFacetCategory::Picture,
    FileFacetCategory::Video,
    FileFacetCategory::Audio,
    FileFacetCategory::Other,
];

pub(crate) const MODIFIED_TIME_FACETS: [ModifiedTimeFacet; 5] = [
    ModifiedTimeFacet::Last1Day,
    ModifiedTimeFacet::Last7Days,
    ModifiedTimeFacet::Last30Days,
    ModifiedTimeFacet::Last365Days,
    ModifiedTimeFacet::Older,
];

const DOCUMENT_EXTENSIONS: &[&str] = &[
    "azw", "azw3", "csv", "doc", "docm", "docx", "epub", "key", "markdown", "md", "mobi",
    "numbers", "odp", "ods", "odt", "pages", "pdf", "ppt", "pptm", "pptx", "rtf", "tex", "tsv",
    "txt", "xls", "xlsm", "xlsx",
];
const PICTURE_EXTENSIONS: &[&str] = &[
    "ai", "arw", "avif", "bmp", "cr2", "cr3", "dng", "eps", "gif", "heic", "heif", "ico", "jpeg",
    "jpg", "nef", "orf", "png", "psd", "raw", "rw2", "svg", "tif", "tiff", "webp",
];
const VIDEO_EXTENSIONS: &[&str] = &[
    "3gp", "avi", "flv", "m2ts", "m4v", "mkv", "mov", "mp4", "mpeg", "mpg", "mts", "ogv", "ts",
    "webm", "wmv",
];
const AUDIO_EXTENSIONS: &[&str] = &[
    "aac", "aif", "aifc", "aiff", "alac", "flac", "m4a", "mp3", "oga", "ogg", "opus", "wav", "wma",
];
const LARGE_FILE_BYTES: u64 = 100 * 1024 * 1024;

#[derive(Debug, Default)]
pub(crate) struct FileSearchFacetAccumulator {
    category_values: [u64; 6],
    modified_time_values: [u64; 5],
    matched_total: u64,
    category_scope_total: u64,
    modified_scope_total: u64,
    quick_filters: FileQuickFilterCounts,
}

impl FileSearchFacetAccumulator {
    pub(crate) fn observe_query_match(
        &mut self,
        item: &CachedItem,
        selected_categories: &[FileFacetCategory],
        selected_modified_time: Option<ModifiedTimeFacet>,
        now_unix_ms: i64,
        timezone_offset_minutes: i32,
    ) {
        let category = category_for(item);
        let category_is_selected =
            selected_categories.is_empty() || selected_categories.contains(&category);
        let modified_time_is_selected = modified_time_selected(
            item.modified_unix_ms,
            selected_modified_time,
            now_unix_ms,
            timezone_offset_minutes,
        );

        if modified_time_is_selected {
            self.category_scope_total = self.category_scope_total.saturating_add(1);
            increment_category_count(&mut self.category_values, category);
        }
        if category_is_selected {
            self.modified_scope_total = self.modified_scope_total.saturating_add(1);
            increment_modified_time_counts(
                &mut self.modified_time_values,
                item.modified_unix_ms,
                now_unix_ms,
                timezone_offset_minutes,
            );
        }
        if !(category_is_selected && modified_time_is_selected) {
            return;
        }

        self.matched_total = self.matched_total.saturating_add(1);
        if item.kind == ItemKind::Folder {
            self.quick_filters.folder = self.quick_filters.folder.saturating_add(1);
        }
        if item.kind == ItemKind::File {
            self.quick_filters.file = self.quick_filters.file.saturating_add(1);
            if item.extension() == "pdf" {
                self.quick_filters.pdf = self.quick_filters.pdf.saturating_add(1);
            }
            if item.size > LARGE_FILE_BYTES {
                self.quick_filters.large = self.quick_filters.large.saturating_add(1);
            }
        }
        if modified_time_matches(
            item.modified_unix_ms,
            ModifiedTimeFacet::Last1Day,
            now_unix_ms,
            timezone_offset_minutes,
        ) {
            self.quick_filters.today = self.quick_filters.today.saturating_add(1);
        }
        if modified_time_matches(
            item.modified_unix_ms,
            ModifiedTimeFacet::Last7Days,
            now_unix_ms,
            timezone_offset_minutes,
        ) {
            self.quick_filters.week = self.quick_filters.week.saturating_add(1);
        }
    }

    pub(crate) fn into_projection(self) -> FileSearchFacetProjection {
        FileSearchFacetProjection {
            matched_total: self.matched_total,
            category_scope_total: self.category_scope_total,
            modified_scope_total: self.modified_scope_total,
            categories: category_counts(&self.category_values),
            modified_time: modified_time_counts(&self.modified_time_values),
            quick_filters: self.quick_filters,
        }
    }
}

pub(crate) fn category_for(item: &CachedItem) -> FileFacetCategory {
    category_for_kind_and_extension(item.kind, item.extension())
}

fn category_for_kind_and_extension(
    kind: crate::model::ItemKind,
    extension: &str,
) -> FileFacetCategory {
    use crate::model::ItemKind;

    if kind == ItemKind::Folder {
        return FileFacetCategory::Folder;
    }
    if kind != ItemKind::File {
        return FileFacetCategory::Other;
    }

    if DOCUMENT_EXTENSIONS.binary_search(&extension).is_ok() {
        FileFacetCategory::Document
    } else if PICTURE_EXTENSIONS.binary_search(&extension).is_ok() {
        FileFacetCategory::Picture
    } else if VIDEO_EXTENSIONS.binary_search(&extension).is_ok() {
        FileFacetCategory::Video
    } else if AUDIO_EXTENSIONS.binary_search(&extension).is_ok() {
        FileFacetCategory::Audio
    } else {
        FileFacetCategory::Other
    }
}

pub(crate) fn category_selected(item: &CachedItem, selected: &[FileFacetCategory]) -> bool {
    selected.is_empty() || selected.contains(&category_for(item))
}

pub(crate) fn modified_time_selected(
    modified_unix_ms: i64,
    selected: Option<ModifiedTimeFacet>,
    now_unix_ms: i64,
    timezone_offset_minutes: i32,
) -> bool {
    selected.is_none_or(|facet| {
        modified_time_matches(
            modified_unix_ms,
            facet,
            now_unix_ms,
            timezone_offset_minutes,
        )
    })
}

pub(crate) fn category_counts(values: &[u64; 6]) -> Vec<FileFacetCount> {
    FILE_FACET_CATEGORIES
        .into_iter()
        .zip(values.iter().copied())
        .map(|(category, count)| FileFacetCount { category, count })
        .collect()
}

pub(crate) fn modified_time_counts(values: &[u64; 5]) -> Vec<ModifiedTimeFacetCount> {
    MODIFIED_TIME_FACETS
        .into_iter()
        .zip(values.iter().copied())
        .map(|(range, count)| ModifiedTimeFacetCount { range, count })
        .collect()
}

pub(crate) fn increment_category_count(values: &mut [u64; 6], category: FileFacetCategory) {
    let index = match category {
        FileFacetCategory::Folder => 0,
        FileFacetCategory::Document => 1,
        FileFacetCategory::Picture => 2,
        FileFacetCategory::Video => 3,
        FileFacetCategory::Audio => 4,
        FileFacetCategory::Other => 5,
    };
    values[index] = values[index].saturating_add(1);
}

pub(crate) fn increment_modified_time_counts(
    values: &mut [u64; 5],
    modified_unix_ms: i64,
    now_unix_ms: i64,
    timezone_offset_minutes: i32,
) {
    for (index, facet) in MODIFIED_TIME_FACETS.into_iter().enumerate() {
        if modified_time_matches(
            modified_unix_ms,
            facet,
            now_unix_ms,
            timezone_offset_minutes,
        ) {
            values[index] = values[index].saturating_add(1);
        }
    }
}

fn modified_time_matches(
    modified_unix_ms: i64,
    facet: ModifiedTimeFacet,
    now_unix_ms: i64,
    timezone_offset_minutes: i32,
) -> bool {
    match facet {
        ModifiedTimeFacet::Last1Day => {
            in_rolling_range(modified_unix_ms, now_unix_ms, timezone_offset_minutes, 1)
        }
        ModifiedTimeFacet::Last7Days => {
            in_rolling_range(modified_unix_ms, now_unix_ms, timezone_offset_minutes, 7)
        }
        ModifiedTimeFacet::Last30Days => {
            in_rolling_range(modified_unix_ms, now_unix_ms, timezone_offset_minutes, 30)
        }
        ModifiedTimeFacet::Last365Days => {
            in_rolling_range(modified_unix_ms, now_unix_ms, timezone_offset_minutes, 365)
        }
        ModifiedTimeFacet::Older => {
            let (start, _) = query::rolling_day_range(now_unix_ms, timezone_offset_minutes, 365);
            modified_unix_ms < start
        }
    }
}

fn in_rolling_range(
    modified_unix_ms: i64,
    now_unix_ms: i64,
    timezone_offset_minutes: i32,
    days: i64,
) -> bool {
    let (start, end) = query::rolling_day_range(now_unix_ms, timezone_offset_minutes, days);
    (start..=end).contains(&modified_unix_ms)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::ItemKind;

    #[test]
    fn classifies_standard_file_groups_with_other_fallback() {
        assert_eq!(
            category_for_kind_and_extension(ItemKind::Folder, ""),
            FileFacetCategory::Folder
        );
        assert_eq!(
            category_for_kind_and_extension(ItemKind::File, "pdf"),
            FileFacetCategory::Document
        );
        assert_eq!(
            category_for_kind_and_extension(ItemKind::File, "heic"),
            FileFacetCategory::Picture
        );
        assert_eq!(
            category_for_kind_and_extension(ItemKind::File, "mov"),
            FileFacetCategory::Video
        );
        assert_eq!(
            category_for_kind_and_extension(ItemKind::File, "flac"),
            FileFacetCategory::Audio
        );
        assert_eq!(
            category_for_kind_and_extension(ItemKind::File, "7z"),
            FileFacetCategory::Other
        );
        assert_eq!(
            category_for_kind_and_extension(ItemKind::App, "app"),
            FileFacetCategory::Other
        );
    }
}
