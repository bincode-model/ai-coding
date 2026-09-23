use std::fs::File;
use std::io::{self, BufRead, BufReader, Read, Seek, SeekFrom};
use std::path::Path;

use chrono::{DateTime, FixedOffset};
use serde_json::Value;

/// Maximum number of characters for session titles (shared across providers).
pub const TITLE_MAX_CHARS: usize = 80;

/// Read the first `head_n` lines and last `tail_n` lines from a file.
/// Tail reads begin at a newline boundary, even when a JSON record is larger
/// than the normal 16 KB read chunk or contains multi-byte UTF-8 characters.
pub fn read_head_tail_lines(
    path: &Path,
    head_n: usize,
    tail_n: usize,
) -> io::Result<(Vec<String>, Vec<String>)> {
    let mut file = File::open(path)?;
    let file_len = file.metadata()?.len();

    // For small files, read all lines once and split
    if file_len < 16_384 {
        let reader = BufReader::new(file);
        let all = read_utf8_lines(reader, usize::MAX)?;
        let head = all.iter().take(head_n).cloned().collect();
        let skip = all.len().saturating_sub(tail_n);
        let tail = all.into_iter().skip(skip).collect();
        return Ok((head, tail));
    }

    // Read head lines from the beginning
    let head = read_utf8_lines(BufReader::new(&mut file), head_n)?;
    if tail_n == 0 {
        return Ok((head, Vec::new()));
    }

    let mut cursor = file_len;
    let mut newline_count = 0;
    let mut wanted_newlines = tail_n;
    let mut start = 0;
    let mut chunk = vec![0; 16_384];
    'chunks: while cursor > 0 {
        let count = cursor.min(chunk.len() as u64) as usize;
        cursor -= count as u64;
        file.seek(SeekFrom::Start(cursor))?;
        file.read_exact(&mut chunk[..count])?;
        if cursor + count as u64 == file_len && chunk[count - 1] == b'\n' {
            wanted_newlines = wanted_newlines.saturating_add(1);
        }
        for index in (0..count).rev() {
            if chunk[index] == b'\n' {
                newline_count += 1;
                if newline_count == wanted_newlines {
                    start = cursor + index as u64 + 1;
                    break 'chunks;
                }
            }
        }
    }
    file.seek(SeekFrom::Start(start))?;
    let tail = read_utf8_lines(BufReader::new(file.take(file_len - start)), tail_n)?;

    Ok((head, tail))
}

fn read_utf8_lines(mut reader: impl BufRead, limit: usize) -> io::Result<Vec<String>> {
    let mut lines = Vec::new();
    let mut bytes = Vec::new();
    for _ in 0..limit {
        bytes.clear();
        if reader.read_until(b'\n', &mut bytes)? == 0 {
            break;
        }
        if bytes.last() == Some(&b'\n') {
            bytes.pop();
            if bytes.last() == Some(&b'\r') {
                bytes.pop();
            }
        }
        // A damaged record must not hide all later valid records.
        if let Ok(line) = std::str::from_utf8(&bytes) {
            lines.push(line.to_string());
        }
    }
    Ok(lines)
}

pub fn parse_timestamp_to_ms(value: &Value) -> Option<i64> {
    // Integer: milliseconds (>1e12) or seconds
    if let Some(n) = value.as_i64() {
        return Some(if n > 1_000_000_000_000 { n } else { n * 1000 });
    }
    if let Some(n) = value.as_f64() {
        let n = n as i64;
        return Some(if n > 1_000_000_000_000 { n } else { n * 1000 });
    }
    // RFC3339 string
    let raw = value.as_str()?;
    DateTime::parse_from_rfc3339(raw)
        .ok()
        .map(|dt: DateTime<FixedOffset>| dt.timestamp_millis())
}

pub fn extract_text(content: &Value) -> String {
    match content {
        Value::String(text) => text.to_string(),
        Value::Array(items) => items
            .iter()
            .filter_map(extract_text_from_item)
            .filter(|text| !text.trim().is_empty())
            .collect::<Vec<_>>()
            .join("\n"),
        Value::Object(map) => map
            .get("text")
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string(),
        _ => String::new(),
    }
}

fn extract_text_from_item(item: &Value) -> Option<String> {
    let item_type = item.get("type").and_then(Value::as_str).unwrap_or("");

    // Anthropic uses tool_use; Pi's assistant messages use toolCall.
    if matches!(item_type, "tool_use" | "toolCall") {
        let name = item
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("unknown");
        return Some(format!("[Tool: {name}]"));
    }

    // tool_result: extract nested content
    if item_type == "tool_result" {
        if let Some(content) = item.get("content") {
            let text = extract_text(content);
            if !text.is_empty() {
                return Some(text);
            }
        }
        return None;
    }

    if let Some(text) = item.get("text").and_then(|v| v.as_str()) {
        return Some(text.to_string());
    }

    if let Some(text) = item.get("input_text").and_then(|v| v.as_str()) {
        return Some(text.to_string());
    }

    if let Some(text) = item.get("output_text").and_then(|v| v.as_str()) {
        return Some(text.to_string());
    }

    if let Some(content) = item.get("content") {
        let text = extract_text(content);
        if !text.is_empty() {
            return Some(text);
        }
    }

    None
}

pub fn truncate_summary(text: &str, max_chars: usize) -> String {
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return String::new();
    }
    if trimmed.chars().count() <= max_chars {
        return trimmed.to_string();
    }

    let mut result = trimmed.chars().take(max_chars).collect::<String>();
    result.push_str("...");
    result
}

pub fn path_basename(value: &str) -> Option<String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return None;
    }
    let normalized = trimmed.trim_end_matches(['/', '\\']);
    let last = normalized
        .split(['/', '\\'])
        .next_back()
        .filter(|segment| !segment.is_empty())?;
    Some(last.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parse_timestamp_to_ms_supports_integers_and_rfc3339() {
        assert_eq!(
            parse_timestamp_to_ms(&json!(1_771_061_953_033_i64)),
            Some(1_771_061_953_033)
        );
        assert_eq!(
            parse_timestamp_to_ms(&json!(1_771_061_953_i64)),
            Some(1_771_061_953_000)
        );
        assert_eq!(
            parse_timestamp_to_ms(&json!("1970-01-01T00:00:01Z")),
            Some(1_000)
        );
    }

    #[test]
    fn extract_text_supports_pi_tool_calls() {
        assert_eq!(
            extract_text(&json!([{ "type": "toolCall", "name": "read" }])),
            "[Tool: read]"
        );
    }

    #[test]
    fn tail_keeps_complete_utf8_records_after_a_multibyte_seek_boundary() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("unicode.jsonl");
        let long_line = format!("{{\"text\":\"{}\"}}", "中".repeat(12_000));
        let text = format!("head\n{long_line}\nlatest!\n");
        let old_cut = text.len() - 16_384;
        assert!(!text.is_char_boundary(old_cut));
        std::fs::write(&path, text).unwrap();

        let (head, tail) = read_head_tail_lines(&path, 1, 2).unwrap();
        assert_eq!(head, ["head"]);
        assert_eq!(tail, [long_line, "latest!".to_string()]);
    }

    #[test]
    fn tail_reads_a_last_record_larger_than_the_read_chunk() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("large.jsonl");
        let last = format!("{{\"output\":\"{}\"}}", "complete output ".repeat(10_000));
        for newline in ["", "\n", "\r\n"] {
            std::fs::write(&path, format!("first\n{last}{newline}")).unwrap();
            let (head, tail) = read_head_tail_lines(&path, 1, 1).unwrap();
            assert_eq!(head, ["first"]);
            assert_eq!(tail.as_slice(), std::slice::from_ref(&last));
        }
    }

    #[test]
    fn damaged_utf8_line_does_not_hide_later_valid_lines() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("damaged.jsonl");
        std::fs::write(&path, b"first\n\xff\nlatest\n").unwrap();
        let (head, tail) = read_head_tail_lines(&path, 3, 3).unwrap();
        assert_eq!(head, ["first", "latest"]);
        assert_eq!(tail, ["first", "latest"]);
    }
}
