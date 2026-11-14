use crate::text::{normalize, trigram_codes};
use pinyin::ToPinyin;
use std::collections::HashSet;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum PinyinVariantKind {
    Full,
    Initials,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct PinyinSearchVariant {
    pub(crate) text: String,
    pub(crate) kind: PinyinVariantKind,
}

#[derive(Debug)]
pub(crate) struct PinyinMatchData {
    pub(crate) variants: Vec<PinyinSearchVariant>,
    segments: Vec<Segment>,
}

impl PinyinMatchData {
    pub(crate) fn mixed_prefix_matches(&self, query: &str) -> bool {
        mixed_prefix_matches(&self.segments, query)
    }
}

#[derive(Debug, Default)]
pub(crate) struct PinyinIndexPostings {
    pub(crate) short_prefixes: Vec<String>,
    pub(crate) trigrams: Vec<u32>,
    pub(crate) chars: Vec<char>,
}

#[derive(Debug, Clone, Copy)]
enum Segment {
    Literal(char),
    Pinyin {
        full: &'static str,
        initial: &'static str,
    },
}

#[cfg(test)]
pub(crate) fn pinyin_search_variants(input: &str) -> Vec<PinyinSearchVariant> {
    pinyin_match_data(input)
        .map(|data| data.variants)
        .unwrap_or_default()
}

pub(crate) fn pinyin_match_data(input: &str) -> Option<PinyinMatchData> {
    let (normalized, segments) = normalized_segments(input)?;
    let full = render(&segments, false);
    let initials = render(&segments, true);
    let mut variants = Vec::with_capacity(2);
    if full != normalized {
        variants.push(PinyinSearchVariant {
            text: full.clone(),
            kind: PinyinVariantKind::Full,
        });
    }
    if initials != normalized && initials != full {
        variants.push(PinyinSearchVariant {
            text: initials,
            kind: PinyinVariantKind::Initials,
        });
    }
    Some(PinyinMatchData { variants, segments })
}

#[cfg(test)]
pub(crate) fn pinyin_mixed_prefix_matches(input: &str, query: &str) -> bool {
    pinyin_match_data(input).is_some_and(|data| data.mixed_prefix_matches(query))
}

fn mixed_prefix_matches(segments: &[Segment], query: &str) -> bool {
    if query.is_empty() {
        return false;
    }
    let query: Vec<char> = query.chars().collect();
    let mut positions = HashSet::from([0usize]);
    for segment in segments {
        let mut next = HashSet::new();
        for position in positions {
            if position >= query.len() {
                return true;
            }
            for_each_option(*segment, |option| {
                if let Some(consumed) = consume_prefix(option, &query[position..]) {
                    if position + consumed >= query.len() {
                        next.insert(query.len());
                    } else if consumed == option.chars().count() {
                        next.insert(position + consumed);
                    }
                }
            });
        }
        if next.contains(&query.len()) {
            return true;
        }
        if next.is_empty() {
            return false;
        }
        positions = next;
    }
    positions.contains(&query.len())
}

pub(crate) fn pinyin_index_postings(
    input: &str,
    include_short_prefixes: bool,
) -> PinyinIndexPostings {
    let Some((_, segments)) = normalized_segments(input) else {
        return PinyinIndexPostings::default();
    };
    let full = render(&segments, false);
    let initials = render(&segments, true);

    let mut trigrams: HashSet<u32> = trigram_codes(&full).into_iter().collect();
    trigrams.extend(trigram_codes(&initials));
    add_mixed_trigrams(&segments, &mut trigrams);

    let mut chars: HashSet<char> = full.chars().collect();
    chars.extend(initials.chars());

    let mut short_prefixes = if include_short_prefixes {
        mixed_short_prefixes(&segments)
    } else {
        Vec::new()
    };
    short_prefixes.sort();
    short_prefixes.dedup();

    let mut trigrams: Vec<_> = trigrams.into_iter().collect();
    trigrams.sort_unstable();
    let mut chars: Vec<_> = chars.into_iter().collect();
    chars.sort_unstable();
    PinyinIndexPostings {
        short_prefixes,
        trigrams,
        chars,
    }
}

fn normalized_segments(input: &str) -> Option<(String, Vec<Segment>)> {
    if input.is_ascii() {
        return None;
    }
    let normalized = normalize(input);
    let mut segments = Vec::with_capacity(normalized.chars().count());
    let mut has_pinyin = false;
    for ch in normalized.chars() {
        if let Some(value) = ch.to_pinyin() {
            has_pinyin = true;
            segments.push(Segment::Pinyin {
                full: value.plain(),
                initial: value.first_letter(),
            });
        } else {
            segments.push(Segment::Literal(ch));
        }
    }
    has_pinyin.then_some((normalized, segments))
}

fn render(segments: &[Segment], initials: bool) -> String {
    let mut output = String::new();
    for segment in segments {
        match segment {
            Segment::Literal(ch) => output.push(*ch),
            Segment::Pinyin { full, initial } => {
                output.push_str(if initials { initial } else { full });
            }
        }
    }
    output
}

fn for_each_option(segment: Segment, mut callback: impl FnMut(&str)) {
    match segment {
        Segment::Literal(ch) => {
            let mut buffer = [0u8; 4];
            callback(ch.encode_utf8(&mut buffer));
        }
        Segment::Pinyin { full, initial } => {
            callback(full);
            if initial != full {
                callback(initial);
            }
        }
    }
}

fn consume_prefix(option: &str, query: &[char]) -> Option<usize> {
    let mut consumed = 0usize;
    for ch in option.chars() {
        if consumed >= query.len() {
            break;
        }
        if query[consumed] != ch {
            return None;
        }
        consumed += 1;
    }
    Some(consumed)
}

fn mixed_short_prefixes(segments: &[Segment]) -> Vec<String> {
    let mut states = HashSet::from([String::new()]);
    for segment in segments {
        let mut next = HashSet::new();
        for prefix in states {
            if prefix.chars().count() >= 2 {
                next.insert(prefix);
                continue;
            }
            for_each_option(*segment, |option| {
                let mut candidate = prefix.clone();
                for ch in option.chars() {
                    if candidate.chars().count() >= 2 {
                        break;
                    }
                    candidate.push(ch);
                }
                next.insert(candidate);
            });
        }
        states = next;
        if states.iter().all(|value| value.chars().count() >= 2) {
            break;
        }
    }

    let mut out = Vec::new();
    for prefix in states {
        let one: String = prefix.chars().take(1).collect();
        if !one.is_empty() {
            out.push(one);
        }
        let two: String = prefix.chars().take(2).collect();
        if two.chars().count() == 2 {
            out.push(two);
        }
    }
    out
}

fn add_mixed_trigrams(segments: &[Segment], trigrams: &mut HashSet<u32>) {
    let mut suffixes = HashSet::from([Vec::<char>::new()]);
    for segment in segments {
        let mut next = HashSet::new();
        for suffix in &suffixes {
            for_each_option(*segment, |option| {
                let mut rolling = suffix.clone();
                for ch in option.chars() {
                    rolling.push(ch);
                    if rolling.len() >= 3 {
                        let len = rolling.len();
                        trigrams.insert(trigram_code([
                            rolling[len - 3],
                            rolling[len - 2],
                            rolling[len - 1],
                        ]));
                    }
                    if rolling.len() > 2 {
                        rolling.remove(0);
                    }
                }
                next.insert(rolling);
            });
        }
        suffixes = next;
    }
}

fn trigram_code(chars: [char; 3]) -> u32 {
    let mut hash = 2_166_136_261u32;
    for ch in chars {
        hash ^= ch as u32;
        hash = hash.wrapping_mul(16_777_619);
    }
    hash
}

#[cfg(test)]
mod tests {
    use super::*;

    fn texts(value: &str) -> Vec<(String, PinyinVariantKind)> {
        pinyin_search_variants(value)
            .into_iter()
            .map(|variant| (variant.text, variant.kind))
            .collect()
    }

    #[test]
    fn emits_full_and_initial_pinyin_and_preserves_mixed_text() {
        let variants = texts("中国 Scene.mov");
        assert!(variants.contains(&("zhongguo scene.mov".into(), PinyinVariantKind::Full)));
        assert!(variants.contains(&("zg scene.mov".into(), PinyinVariantKind::Initials)));
        assert!(pinyin_search_variants("Scene.mov").is_empty());
    }

    #[test]
    fn mixed_prefix_matching_combines_initials_and_full_pinyin_without_alias_explosion() {
        assert!(pinyin_mixed_prefix_matches("公司手册.txt", "gsshouce"));
        assert!(pinyin_mixed_prefix_matches("公司手册.txt", "gongsisc"));
        assert!(pinyin_mixed_prefix_matches("公司手册.txt", "gssc"));
        assert!(!pinyin_mixed_prefix_matches("公司手册.txt", "gsshoucef"));
    }

    #[test]
    fn mixed_index_postings_cover_cross_notation_trigrams() {
        let postings = pinyin_index_postings("公司手册.txt", true);
        let expected: HashSet<_> = trigram_codes("gsshouce").into_iter().collect();
        let actual: HashSet<_> = postings.trigrams.into_iter().collect();
        assert!(expected.is_subset(&actual));
        assert!(postings.short_prefixes.contains(&"gs".into()));
    }
}
