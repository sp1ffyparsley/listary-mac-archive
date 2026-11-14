use chrono::{Datelike, Duration, FixedOffset, TimeZone, Utc};
use serde::{Deserialize, Serialize};

use crate::text::normalize;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct QueryTerm {
    pub text: String,
    pub exact: bool,
    pub path_term: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct NumericRange<T> {
    pub min: Option<T>,
    pub max: Option<T>,
    pub min_inclusive: bool,
    pub max_inclusive: bool,
}

impl<T> Default for NumericRange<T> {
    fn default() -> Self {
        Self {
            min: None,
            max: None,
            min_inclusive: true,
            max_inclusive: true,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq, Eq)]
pub struct ParsedQuery {
    pub positive_terms: Vec<QueryTerm>,
    pub negative_terms: Vec<QueryTerm>,
    pub include_extensions: Vec<String>,
    pub exclude_extensions: Vec<String>,
    pub modified_unix_ms: Vec<NumericRange<i64>>,
    pub size_bytes: Vec<NumericRange<u64>>,
    pub unparsed_operators: Vec<String>,
}

#[derive(Debug, Clone)]
struct LexToken {
    raw: String,
    quoted: bool,
    negative: bool,
}

pub fn parse(input: &str, now_unix_ms: i64, timezone_offset_minutes: i32) -> ParsedQuery {
    let mut parsed = ParsedQuery::default();
    for token in lex(input) {
        let mut raw = token.raw.clone();
        let operator_raw = raw.replace('：', ":");
        let lower = normalize(&operator_raw);

        if lower.starts_with("ext:") {
            let values = operator_raw[4..].to_string();
            let target = if token.negative {
                &mut parsed.exclude_extensions
            } else {
                &mut parsed.include_extensions
            };
            for ext in values.split([';', ',']) {
                let ext = normalize(ext.trim().trim_start_matches('.'));
                if !ext.is_empty() {
                    target.push(ext);
                }
            }
            continue;
        }

        if lower.starts_with("*.") && lower.len() > 2 {
            let ext = normalize(lower.trim_start_matches("*."));
            if token.negative {
                parsed.exclude_extensions.push(ext);
            } else {
                parsed.include_extensions.push(ext);
            }
            continue;
        }

        // Cling-style extension shorthand stays syntax sugar over the canonical
        // extension filter owner; quoted dotfiles remain literal search terms.
        if !token.quoted && lower.starts_with('.') && lower.len() > 1 {
            let candidate = lower.trim_start_matches('.');
            if !candidate.is_empty()
                && candidate.len() <= 64
                && candidate
                    .chars()
                    .all(|ch| ch.is_alphanumeric() || matches!(ch, '.' | '_' | '-'))
            {
                if token.negative {
                    parsed.exclude_extensions.push(candidate.to_string());
                } else {
                    parsed.include_extensions.push(candidate.to_string());
                }
                continue;
            }
        }

        if lower.starts_with("date:") {
            match parse_date_range(&operator_raw[5..], now_unix_ms, timezone_offset_minutes) {
                Some(range) => parsed.modified_unix_ms.push(range),
                None => parsed.unparsed_operators.push(token.raw.clone()),
            }
            continue;
        }

        if lower.starts_with("size:") {
            match parse_size_range(&operator_raw[5..]) {
                Some(range) => parsed.size_bytes.push(range),
                None => parsed.unparsed_operators.push(token.raw.clone()),
            }
            continue;
        }

        let path_term = raw.ends_with('/') || raw.ends_with('\\');
        if path_term {
            while raw.ends_with('/') || raw.ends_with('\\') {
                raw.pop();
            }
        }
        let normalized = normalize(raw.trim());
        if normalized.is_empty() {
            continue;
        }
        let term = QueryTerm {
            text: normalized,
            exact: token.quoted,
            path_term,
        };
        if token.negative {
            parsed.negative_terms.push(term);
        } else {
            parsed.positive_terms.push(term);
        }
    }

    parsed.include_extensions.sort();
    parsed.include_extensions.dedup();
    parsed.exclude_extensions.sort();
    parsed.exclude_extensions.dedup();
    parsed
}

fn lex(input: &str) -> Vec<LexToken> {
    let mut tokens = Vec::new();
    let mut chars = input.chars().peekable();
    while chars.peek().is_some() {
        while matches!(chars.peek(), Some(c) if c.is_whitespace()) {
            chars.next();
        }
        if chars.peek().is_none() {
            break;
        }

        let mut negative = false;
        if matches!(chars.peek(), Some('-')) {
            chars.next();
            negative = true;
        }

        let quoted = matches!(chars.peek(), Some('"'));
        let mut raw = String::new();
        if quoted {
            chars.next();
            let mut escaped = false;
            for ch in chars.by_ref() {
                if escaped {
                    raw.push(ch);
                    escaped = false;
                } else if ch == '\\' {
                    escaped = true;
                } else if ch == '"' {
                    break;
                } else {
                    raw.push(ch);
                }
            }
        } else {
            while matches!(chars.peek(), Some(c) if !c.is_whitespace()) {
                raw.push(chars.next().unwrap());
            }
        }
        if !raw.is_empty() {
            tokens.push(LexToken {
                raw,
                quoted,
                negative,
            });
        }
    }
    tokens
}

fn fixed_offset(minutes: i32) -> FixedOffset {
    let seconds = minutes.clamp(-1_439, 1_439) * 60;
    FixedOffset::east_opt(seconds).unwrap_or_else(|| FixedOffset::east_opt(0).unwrap())
}

fn day_start_ms(now_unix_ms: i64, offset_minutes: i32) -> i64 {
    let tz = fixed_offset(offset_minutes);
    let now = Utc
        .timestamp_millis_opt(now_unix_ms)
        .single()
        .unwrap_or_else(Utc::now)
        .with_timezone(&tz);
    tz.with_ymd_and_hms(now.year(), now.month(), now.day(), 0, 0, 0)
        .single()
        .unwrap()
        .timestamp_millis()
}

pub(crate) fn rolling_day_range(now_unix_ms: i64, offset_minutes: i32, days: i64) -> (i64, i64) {
    let days = days.max(1);
    let start_today = day_start_ms(now_unix_ms, offset_minutes);
    let start = start_today - Duration::days(days - 1).num_milliseconds();
    let end = start_today + Duration::days(1).num_milliseconds() - 1;
    (start, end)
}

fn parse_date_literal(value: &str, now_unix_ms: i64, offset_minutes: i32) -> Option<(i64, i64)> {
    let value = normalize(value.trim());
    let start_today = day_start_ms(now_unix_ms, offset_minutes);
    match value.as_str() {
        "today" => {
            return Some((
                start_today,
                start_today + Duration::days(1).num_milliseconds() - 1,
            ));
        }
        "yesterday" => {
            let start = start_today - Duration::days(1).num_milliseconds();
            return Some((start, start_today - 1));
        }
        "week" => {
            return Some(rolling_day_range(now_unix_ms, offset_minutes, 7));
        }
        "month" => {
            return Some(rolling_day_range(now_unix_ms, offset_minutes, 30));
        }
        "year" => {
            return Some(rolling_day_range(now_unix_ms, offset_minutes, 365));
        }
        _ => {}
    }

    let tz = fixed_offset(offset_minutes);
    let parts: Vec<&str> = value.split('-').collect();
    let year: i32 = parts.first()?.parse().ok()?;
    if parts.len() == 1 {
        let start = tz
            .with_ymd_and_hms(year, 1, 1, 0, 0, 0)
            .single()?
            .timestamp_millis();
        let end = tz
            .with_ymd_and_hms(year + 1, 1, 1, 0, 0, 0)
            .single()?
            .timestamp_millis()
            - 1;
        return Some((start, end));
    }
    let month: u32 = parts.get(1)?.parse().ok()?;
    if parts.len() == 2 {
        let start = tz
            .with_ymd_and_hms(year, month, 1, 0, 0, 0)
            .single()?
            .timestamp_millis();
        let (ny, nm) = if month == 12 {
            (year + 1, 1)
        } else {
            (year, month + 1)
        };
        let end = tz
            .with_ymd_and_hms(ny, nm, 1, 0, 0, 0)
            .single()?
            .timestamp_millis()
            - 1;
        return Some((start, end));
    }
    let day: u32 = parts.get(2)?.parse().ok()?;
    let start = tz
        .with_ymd_and_hms(year, month, day, 0, 0, 0)
        .single()?
        .timestamp_millis();
    Some((start, start + Duration::days(1).num_milliseconds() - 1))
}

fn parse_date_range(
    value: &str,
    now_unix_ms: i64,
    offset_minutes: i32,
) -> Option<NumericRange<i64>> {
    let value = value.trim();
    if let Some((left, right)) = value.split_once("..") {
        let (start, _) = parse_date_literal(left, now_unix_ms, offset_minutes)?;
        let (_, end) = parse_date_literal(right, now_unix_ms, offset_minutes)?;
        return Some(NumericRange {
            min: Some(start),
            max: Some(end),
            min_inclusive: true,
            max_inclusive: true,
        });
    }
    if let Some(rest) = value.strip_prefix(">=") {
        let (start, _) = parse_date_literal(rest, now_unix_ms, offset_minutes)?;
        return Some(NumericRange {
            min: Some(start),
            min_inclusive: true,
            ..Default::default()
        });
    }
    if let Some(rest) = value.strip_prefix('>') {
        let (_, end) = parse_date_literal(rest, now_unix_ms, offset_minutes)?;
        return Some(NumericRange {
            min: Some(end),
            min_inclusive: false,
            ..Default::default()
        });
    }
    if let Some(rest) = value.strip_prefix("<=") {
        let (_, end) = parse_date_literal(rest, now_unix_ms, offset_minutes)?;
        return Some(NumericRange {
            max: Some(end),
            max_inclusive: true,
            ..Default::default()
        });
    }
    if let Some(rest) = value.strip_prefix('<') {
        let (start, _) = parse_date_literal(rest, now_unix_ms, offset_minutes)?;
        return Some(NumericRange {
            max: Some(start),
            max_inclusive: false,
            ..Default::default()
        });
    }
    let (start, end) = parse_date_literal(value, now_unix_ms, offset_minutes)?;
    Some(NumericRange {
        min: Some(start),
        max: Some(end),
        min_inclusive: true,
        max_inclusive: true,
    })
}

fn parse_size_value(value: &str) -> Option<u64> {
    let v = normalize(value.trim());
    let split = v
        .find(|c: char| !c.is_ascii_digit() && c != '.')
        .unwrap_or(v.len());
    let (num, unit) = v.split_at(split);
    let number: f64 = num.parse().ok()?;
    let multiplier = match unit.trim() {
        "" => 1024f64.powi(2),
        "b" => 1f64,
        "k" | "kb" => 1024f64,
        "m" | "mb" => 1024f64.powi(2),
        "g" | "gb" => 1024f64.powi(3),
        "t" | "tb" => 1024f64.powi(4),
        _ => return None,
    };
    Some((number * multiplier).round() as u64)
}

fn parse_size_range(value: &str) -> Option<NumericRange<u64>> {
    let value = value.trim();
    if let Some((left, right)) = value.split_once("..") {
        return Some(NumericRange {
            min: Some(parse_size_value(left)?),
            max: Some(parse_size_value(right)?),
            min_inclusive: true,
            max_inclusive: true,
        });
    }
    if let Some(rest) = value.strip_prefix(">=") {
        return Some(NumericRange {
            min: Some(parse_size_value(rest)?),
            min_inclusive: true,
            ..Default::default()
        });
    }
    if let Some(rest) = value.strip_prefix('>') {
        return Some(NumericRange {
            min: Some(parse_size_value(rest)?),
            min_inclusive: false,
            ..Default::default()
        });
    }
    if let Some(rest) = value.strip_prefix("<=") {
        return Some(NumericRange {
            max: Some(parse_size_value(rest)?),
            max_inclusive: true,
            ..Default::default()
        });
    }
    if let Some(rest) = value.strip_prefix('<') {
        return Some(NumericRange {
            max: Some(parse_size_value(rest)?),
            max_inclusive: false,
            ..Default::default()
        });
    }
    let exact = parse_size_value(value)?;
    Some(NumericRange {
        min: Some(exact),
        max: None,
        min_inclusive: true,
        max_inclusive: true,
    })
}

pub fn in_range<T: Ord>(value: &T, range: &NumericRange<T>) -> bool {
    if let Some(min) = range.min.as_ref() {
        if range.min_inclusive {
            if value < min {
                return false;
            }
        } else if value <= min {
            return false;
        }
    }
    if let Some(max) = range.max.as_ref() {
        if range.max_inclusive {
            if value > max {
                return false;
            }
        } else if value >= max {
            return false;
        }
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lexes_exact_negative_and_filters() {
        let parsed = parse(
            r#"snow "fall scene" -draft -"bad take" ext:mp4;mov size:>10mb"#,
            0,
            0,
        );
        assert_eq!(parsed.positive_terms.len(), 2);
        assert_eq!(parsed.negative_terms.len(), 2);
        assert_eq!(parsed.include_extensions, vec!["mov", "mp4"]);
        assert_eq!(parsed.size_bytes[0].min, Some(10 * 1024 * 1024));
    }

    #[test]
    fn parses_path_terms_and_wildcard_extensions() {
        let parsed = parse(r#"projects/ *.png -ext:tmp"#, 0, 0);
        assert!(parsed.positive_terms[0].path_term);
        assert_eq!(parsed.include_extensions, vec!["png"]);
        assert_eq!(parsed.exclude_extensions, vec!["tmp"]);
    }

    #[test]
    fn parses_dot_extension_shorthand_without_stealing_quoted_dotfiles() {
        let parsed = parse(r#"invoice .pdf -.tmp .tar.gz ".gitignore""#, 0, 0);
        assert_eq!(parsed.include_extensions, vec!["pdf", "tar.gz"]);
        assert_eq!(parsed.exclude_extensions, vec!["tmp"]);
        assert_eq!(parsed.positive_terms.len(), 2);
        assert_eq!(parsed.positive_terms[0].text, "invoice");
        assert_eq!(parsed.positive_terms[1].text, ".gitignore");
        assert!(parsed.positive_terms[1].exact);
    }

    #[test]
    fn malformed_operator_is_retained_as_diagnostic() {
        let parsed = parse("hello size:not-a-size world", 0, 0);
        assert_eq!(parsed.positive_terms.len(), 2);
        assert_eq!(parsed.unparsed_operators, vec!["size:not-a-size"]);
    }

    #[test]
    fn repeated_filters_intersect_and_fullwidth_colon_is_supported() {
        let parsed = parse(
            "ext：.tar.gz,pdf date:>=2026-01 date:<2027 size:10 size:<20mb",
            1_790_000_000_000,
            -240,
        );
        assert_eq!(parsed.include_extensions, vec!["pdf", "tar.gz"]);
        assert_eq!(parsed.modified_unix_ms.len(), 2);
        assert_eq!(parsed.size_bytes.len(), 2);
        assert_eq!(parsed.size_bytes[0].min, Some(10 * 1024 * 1024));
        assert_eq!(parsed.size_bytes[0].max, None);
    }

    #[test]
    fn unclosed_quote_consumes_the_rest() {
        let parsed = parse("snow \"fall scene rough", 0, 0);
        assert_eq!(parsed.positive_terms.len(), 2);
        assert_eq!(parsed.positive_terms[1].text, "fall scene rough");
        assert!(parsed.positive_terms[1].exact);
    }

    #[test]
    fn relative_week_month_year_are_local_rolling_day_ranges() {
        let tz = FixedOffset::west_opt(4 * 60 * 60).unwrap();
        let now = tz
            .with_ymd_and_hms(2026, 9, 20, 15, 0, 0)
            .single()
            .unwrap()
            .timestamp_millis();
        let start_today = tz
            .with_ymd_and_hms(2026, 9, 20, 0, 0, 0)
            .single()
            .unwrap()
            .timestamp_millis();
        let end_today = start_today + Duration::days(1).num_milliseconds() - 1;

        for (literal, days) in [("week", 7), ("month", 30), ("year", 365)] {
            let parsed = parse(&format!("date:{literal}"), now, -240);
            assert_eq!(parsed.modified_unix_ms.len(), 1);
            let range = &parsed.modified_unix_ms[0];
            assert_eq!(
                range.min,
                Some(start_today - Duration::days(days - 1).num_milliseconds())
            );
            assert_eq!(range.max, Some(end_today));
        }
    }
}
