use crate::{
    cache::CachedItem,
    query::{ParsedQuery, QueryTerm, in_range},
    text::normalize,
    transliteration::{PinyinMatchData, PinyinSearchVariant, PinyinVariantKind, pinyin_match_data},
};

#[cfg(test)]
use crate::model::ItemRecord;
#[cfg(test)]
use crate::text::{initials, tokens};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TextMatch {
    pub tier: u8,
    pub quality: i32,
}

#[cfg(test)]
pub fn matches_filters(item: &ItemRecord, query: &ParsedQuery) -> bool {
    if item.priority == crate::model::Priority::Ignored {
        return false;
    }
    if !query.include_extensions.is_empty()
        && !query
            .include_extensions
            .iter()
            .any(|ext| extension_matches(item, ext))
    {
        return false;
    }
    if query
        .exclude_extensions
        .iter()
        .any(|ext| extension_matches(item, ext))
    {
        return false;
    }
    for range in &query.modified_unix_ms {
        if !in_range(&item.modified_unix_ms, range) {
            return false;
        }
    }
    if !query.size_bytes.is_empty() && item.kind != crate::model::ItemKind::File {
        return false;
    }
    for range in &query.size_bytes {
        if !in_range(&item.size, range) {
            return false;
        }
    }

    let normalized_name = normalize(&item.name);
    let normalized_path = normalize(&item.path);
    for term in &query.negative_terms {
        let haystack = if term.path_term {
            &normalized_path
        } else {
            &normalized_name
        };
        if haystack.contains(&term.text) {
            return false;
        }
    }
    true
}

fn matches_cached_filters(
    item: &CachedItem,
    query: &ParsedQuery,
    priority: crate::model::Priority,
) -> bool {
    if priority == crate::model::Priority::Ignored {
        return false;
    }
    if !query.include_extensions.is_empty()
        && !query.include_extensions.iter().any(|extension| {
            if extension.contains('.') {
                item.normalized_name
                    .ends_with(&format!(".{}", normalize(extension)))
            } else {
                item.extension() == normalize(extension)
            }
        })
    {
        return false;
    }
    if query.exclude_extensions.iter().any(|extension| {
        if extension.contains('.') {
            item.normalized_name
                .ends_with(&format!(".{}", normalize(extension)))
        } else {
            item.extension() == normalize(extension)
        }
    }) {
        return false;
    }
    for range in &query.modified_unix_ms {
        if !in_range(&item.modified_unix_ms, range) {
            return false;
        }
    }
    if !query.size_bytes.is_empty() && item.kind != crate::model::ItemKind::File {
        return false;
    }
    for range in &query.size_bytes {
        if !in_range(&item.size, range) {
            return false;
        }
    }
    for term in &query.negative_terms {
        let haystack = if term.path_term {
            item.normalized_path.as_ref()
        } else {
            item.normalized_name.as_str()
        };
        if haystack.contains(&term.text) {
            return false;
        }
    }
    true
}

#[cfg(test)]
fn extension_matches(item: &ItemRecord, extension: &str) -> bool {
    if extension.contains('.') {
        normalize(&item.name).ends_with(&format!(".{}", normalize(extension)))
    } else {
        item.extension == normalize(extension)
    }
}

#[cfg(test)]
pub fn match_item(item: &ItemRecord, query: &ParsedQuery) -> Option<TextMatch> {
    let normalized_name = normalize(&item.name);
    let normalized_path = normalize(&item.path);
    let item_tokens = tokens(&item.name);
    let acronym = initials(&item.name);
    if !matches_filters(item, query) {
        return None;
    }
    let direct = match_precomputed(
        &normalized_name,
        &normalized_path,
        |value| item_tokens.iter().any(|token| token.starts_with(value)),
        |value| acronym.starts_with(value),
        query,
    );
    direct.or_else(|| {
        match_with_pinyin(
            &normalized_name,
            &normalized_path,
            |value| item_tokens.iter().any(|token| token.starts_with(value)),
            |value| acronym.starts_with(value),
            query,
        )
    })
}

pub(crate) fn match_cached(
    item: &CachedItem,
    query: &ParsedQuery,
    priority: crate::model::Priority,
) -> Option<TextMatch> {
    if !matches_cached_filters(item, query, priority) {
        return None;
    }
    let direct = match_precomputed(
        &item.normalized_name,
        &item.normalized_path,
        |value| item.token_prefix_matches(value),
        |value| item.acronym_starts_with(value),
        query,
    );
    direct.or_else(|| {
        match_with_pinyin(
            &item.normalized_name,
            &item.normalized_path,
            |value| item.token_prefix_matches(value),
            |value| item.acronym_starts_with(value),
            query,
        )
    })
}

fn match_precomputed<F, G>(
    normalized_name: &str,
    normalized_path: &str,
    token_prefix_matches: F,
    acronym_starts_with: G,
    query: &ParsedQuery,
) -> Option<TextMatch>
where
    F: Fn(&str) -> bool,
    G: Fn(&str) -> bool,
{
    if query.positive_terms.is_empty() {
        return Some(TextMatch {
            tier: 7,
            quality: 0,
        });
    }

    let plain_term_count = query
        .positive_terms
        .iter()
        .filter(|term| !term.path_term)
        .count();
    let mut tier = 0u8;
    let mut quality = 0i32;

    if plain_term_count == 1 {
        let term = query
            .positive_terms
            .iter()
            .find(|term| !term.path_term)
            .expect("plain term count was one");
        if term.exact && normalized_name == term.text {
            quality += 2_000;
        } else if term.exact {
            if normalized_name.contains(&term.text) {
                tier = tier.max(2);
                quality += 1_500 - normalized_name.len().saturating_sub(term.text.len()) as i32;
            } else {
                return None;
            }
        } else if normalized_name == term.text {
            quality += 2_000;
        } else if normalized_name.starts_with(&term.text) {
            tier = tier.max(1);
            quality += 1_800 - normalized_name.len().saturating_sub(term.text.len()) as i32;
        } else if normalized_name.contains(&term.text) {
            tier = tier.max(2);
            quality += 1_500 - normalized_name.len().saturating_sub(term.text.len()) as i32;
        } else if token_prefix_matches(&term.text) {
            tier = tier.max(3);
            quality += 1_250;
        } else if acronym_starts_with(&term.text) && term.text.len() >= 2 {
            tier = tier.max(3);
            quality += 1_200;
        } else {
            let score = subsequence_score(normalized_name, &term.text)?;
            tier = tier.max(4);
            quality += score;
        }
    } else {
        for term in query.positive_terms.iter().filter(|term| !term.path_term) {
            let single = match_single_term(
                normalized_name,
                &token_prefix_matches,
                &acronym_starts_with,
                term,
            )?;
            tier = tier.max(single.tier.max(3));
            quality += single.quality;
        }
    }

    for term in query.positive_terms.iter().filter(|t| t.path_term) {
        if normalized_path.contains(&term.text) {
            tier = tier.max(5);
            quality += 700;
        } else {
            let score = subsequence_score(normalized_path, &term.text)?;
            tier = tier.max(6);
            quality += score / 2;
        }
    }

    Some(TextMatch { tier, quality })
}

fn match_with_pinyin<F, G>(
    normalized_name: &str,
    normalized_path: &str,
    token_prefix_matches: F,
    acronym_starts_with: G,
    query: &ParsedQuery,
) -> Option<TextMatch>
where
    F: Fn(&str) -> bool,
    G: Fn(&str) -> bool,
{
    if query.positive_terms.is_empty() {
        return None;
    }
    let name_pinyin = pinyin_match_data(normalized_name);
    let has_path_terms = query.positive_terms.iter().any(|term| term.path_term);
    let path_pinyin = has_path_terms
        .then(|| pinyin_match_data(normalized_path))
        .flatten();
    if name_pinyin.is_none() && path_pinyin.is_none() {
        return None;
    }

    let plain_term_count = query
        .positive_terms
        .iter()
        .filter(|term| !term.path_term)
        .count();
    let mut tier = 0u8;
    let mut quality = 0i32;
    for term in query.positive_terms.iter().filter(|term| !term.path_term) {
        let mut best = match_single_term(
            normalized_name,
            &token_prefix_matches,
            &acronym_starts_with,
            term,
        );
        if !term.exact {
            if name_pinyin
                .as_ref()
                .is_some_and(|data| data.mixed_prefix_matches(&term.text))
            {
                best = better_match(
                    best,
                    TextMatch {
                        tier: 3,
                        quality: 1_350,
                    },
                );
            }
            for variant in pinyin_variants(&name_pinyin) {
                if let Some(candidate) =
                    match_single_term(&variant.text, &|_| false, &|_| false, term)
                {
                    best = better_match(best, adjust_pinyin_match(candidate, variant));
                }
            }
        }
        let best = best?;
        tier = tier.max(if plain_term_count == 1 {
            best.tier
        } else {
            best.tier.max(3)
        });
        quality += best.quality;
    }

    for term in query.positive_terms.iter().filter(|term| term.path_term) {
        let mut best = match_path_term(normalized_path, term);
        if !term.exact {
            for variant in pinyin_variants(&path_pinyin) {
                if let Some(mut candidate) = match_path_term(&variant.text, term) {
                    candidate.tier = candidate.tier.max(6);
                    candidate.quality -= match variant.kind {
                        PinyinVariantKind::Full => 100,
                        PinyinVariantKind::Initials => 200,
                    };
                    best = better_match(best, candidate);
                }
            }
        }
        let best = best?;
        tier = tier.max(best.tier);
        quality += best.quality;
    }

    Some(TextMatch { tier, quality })
}

fn pinyin_variants(data: &Option<PinyinMatchData>) -> &[PinyinSearchVariant] {
    data.as_ref()
        .map(|value| value.variants.as_slice())
        .unwrap_or_default()
}

fn adjust_pinyin_match(mut candidate: TextMatch, variant: &PinyinSearchVariant) -> TextMatch {
    match variant.kind {
        PinyinVariantKind::Full => {
            candidate.tier = candidate.tier.max(3);
            candidate.quality -= 150;
        }
        PinyinVariantKind::Initials => {
            candidate.tier = candidate.tier.max(4);
            candidate.quality -= 250;
        }
    }
    candidate
}

fn better_match(current: Option<TextMatch>, candidate: TextMatch) -> Option<TextMatch> {
    match current {
        None => Some(candidate),
        Some(existing)
            if candidate.tier < existing.tier
                || (candidate.tier == existing.tier && candidate.quality > existing.quality) =>
        {
            Some(candidate)
        }
        Some(existing) => Some(existing),
    }
}

fn match_path_term(path: &str, term: &QueryTerm) -> Option<TextMatch> {
    if path.contains(&term.text) {
        Some(TextMatch {
            tier: 5,
            quality: 700,
        })
    } else {
        subsequence_score(path, &term.text).map(|quality| TextMatch {
            tier: 6,
            quality: quality / 2,
        })
    }
}

fn match_single_term<F, G>(
    name: &str,
    token_prefix_matches: &F,
    acronym_starts_with: &G,
    term: &QueryTerm,
) -> Option<TextMatch>
where
    F: Fn(&str) -> bool,
    G: Fn(&str) -> bool,
{
    if term.exact {
        return name.contains(&term.text).then_some(TextMatch {
            tier: 2,
            quality: 1_500,
        });
    }
    if name == term.text {
        return Some(TextMatch {
            tier: 0,
            quality: 2_000,
        });
    }
    if name.starts_with(&term.text) {
        return Some(TextMatch {
            tier: 1,
            quality: 1_800,
        });
    }
    if name.contains(&term.text) {
        return Some(TextMatch {
            tier: 2,
            quality: 1_500,
        });
    }
    if token_prefix_matches(&term.text) {
        return Some(TextMatch {
            tier: 3,
            quality: 1_250,
        });
    }
    if acronym_starts_with(&term.text) && term.text.len() >= 2 {
        return Some(TextMatch {
            tier: 3,
            quality: 1_200,
        });
    }
    subsequence_score(name, &term.text).map(|quality| TextMatch { tier: 4, quality })
}

fn subsequence_score(haystack: &str, needle: &str) -> Option<i32> {
    if needle.is_empty() {
        return Some(0);
    }
    let hay: Vec<char> = haystack.chars().collect();
    let ndl: Vec<char> = needle.chars().collect();
    let mut pos = 0usize;
    let mut previous = None;
    let mut gap_cost = 0usize;
    let mut first = None;

    for target in ndl {
        let mut found = None;
        while pos < hay.len() {
            if hay[pos] == target {
                found = Some(pos);
                pos += 1;
                break;
            }
            pos += 1;
        }
        let current = found?;
        first.get_or_insert(current);
        if let Some(prev) = previous {
            gap_cost += current.saturating_sub(prev + 1);
        }
        previous = Some(current);
    }

    let span = previous.unwrap_or(0).saturating_sub(first.unwrap_or(0));
    let length_penalty = hay.len().saturating_sub(needle.chars().count());
    Some(
        1_000
            - (gap_cost.min(100) as i32 * 8)
            - (span.min(200) as i32 * 2)
            - length_penalty.min(200) as i32,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{ItemKind, Priority};
    use uuid::Uuid;

    fn item(name: &str) -> ItemRecord {
        ItemRecord {
            id: 1,
            root_id: Uuid::nil(),
            path: format!(r"C:\Media\{name}"),
            parent_path: r"C:\Media".into(),
            name: name.into(),
            extension: name.rsplit_once('.').map(|(_, e)| e).unwrap_or("").into(),
            kind: ItemKind::File,
            size: 100,
            modified_unix_ms: 1,
            available: true,
            priority: Priority::Normal,
            stable_file_id: None,
            preview_hint: None,
        }
    }

    #[test]
    fn exact_beats_prefix_and_fuzzy_tiers() {
        let q = crate::query::parse("snowfall", 0, 0);
        assert_eq!(match_item(&item("snowfall"), &q).unwrap().tier, 0);
        assert_eq!(match_item(&item("snowfall edit.mp4"), &q).unwrap().tier, 1);
        assert!(match_item(&item("sxnxoxwfxaxlxl.mp4"), &q).unwrap().tier >= 4);
    }

    #[test]
    fn acronym_matching_works() {
        let q = crate::query::parse("psd", 0, 0);
        let m = match_item(&item("Project Scene Database"), &q).unwrap();
        assert_eq!(m.tier, 3);
    }
}
