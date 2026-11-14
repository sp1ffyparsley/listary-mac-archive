use unicode_normalization::UnicodeNormalization;

pub fn normalize(input: &str) -> String {
    input
        .nfkc()
        .flat_map(char::to_lowercase)
        .collect::<String>()
}

pub fn comparable_path(input: &str) -> String {
    let mut value = normalize(input).replace('\\', "/");
    while value.ends_with('/') && value.len() > 1 && !is_windows_drive_root(&value) {
        value.pop();
    }
    value
}

fn is_windows_drive_root(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 3 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':' && bytes[2] == b'/'
}

pub fn tokens(input: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut current = String::new();
    let mut prev_lower = false;

    for ch in input.nfkc() {
        let is_alnum = ch.is_alphanumeric();
        let is_upper = ch.is_uppercase();
        if is_alnum {
            if is_upper && prev_lower && !current.is_empty() {
                out.push(normalize(&current));
                current.clear();
            }
            current.extend(ch.to_lowercase());
            prev_lower = ch.is_lowercase();
        } else {
            if !current.is_empty() {
                out.push(normalize(&current));
                current.clear();
            }
            prev_lower = false;
        }
    }
    if !current.is_empty() {
        out.push(normalize(&current));
    }
    out
}

#[cfg(test)]
pub fn initials(input: &str) -> String {
    tokens(input)
        .into_iter()
        .filter_map(|t| t.chars().next())
        .collect()
}

pub fn trigram_codes(input: &str) -> Vec<u32> {
    let chars: Vec<char> = input.chars().collect();
    if chars.len() < 3 {
        return Vec::new();
    }
    let mut out = Vec::with_capacity(chars.len() - 2);
    for window in chars.windows(3) {
        let mut hash = 2_166_136_261u32;
        for ch in window {
            hash ^= *ch as u32;
            hash = hash.wrapping_mul(16_777_619);
        }
        out.push(hash);
    }
    out.sort_unstable();
    out.dedup();
    out
}
