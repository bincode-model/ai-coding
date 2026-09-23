//! Explicitly opted-in, read-only acceptance against a real saved transcript.
//! Prints only counts; copied transcript snapshots are removed by TempDir.
use std::collections::HashMap;

use super::{providers, search};

#[test]
#[ignore = "requires AI_CODING_ACCEPTANCE_SOURCE pointing to an authorized local Codex transcript"]
fn complete_saved_codex_transcript_and_final_ai_reply_are_searchable() {
    let path = std::env::var("AI_CODING_ACCEPTANCE_SOURCE").expect("explicit transcript path");
    let bytes = std::fs::read(&path).unwrap();
    let snapshot = tempfile::tempdir().unwrap();
    let snapshot_path = snapshot.path().join("acceptance.jsonl");
    std::fs::write(&snapshot_path, &bytes).unwrap();
    let actual = providers::codex::load_messages(&snapshot_path).unwrap();
    let mut loaded_counts = HashMap::new();
    for message in &actual {
        *loaded_counts
            .entry((message.role.clone(), message.content.clone()))
            .or_insert(0usize) += 1;
    }
    let mut original_counts = HashMap::new();
    let mut final_ai_reply = None;
    let mut session_id = None;
    for line in bytes.split(|byte| *byte == b'\n') {
        let Ok(value) = serde_json::from_slice::<serde_json::Value>(line) else {
            continue;
        };
        let payload = &value["payload"];
        if value["type"] == "session_meta" {
            session_id = payload["id"].as_str().map(str::to_owned);
        }
        if value["type"] != "response_item" || payload["type"] != "message" {
            continue;
        }
        let role = payload["role"].as_str().unwrap_or("");
        if !matches!(role, "user" | "assistant") {
            continue;
        }
        let content = if let Some(text) = payload["content"].as_str() {
            text.to_string()
        } else {
            payload["content"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|item| item["text"].as_str())
                .collect::<Vec<_>>()
                .join("\n")
        };
        if content.trim().is_empty() {
            continue;
        }
        if role == "assistant" {
            final_ai_reply = Some(content.clone());
        }
        *original_counts
            .entry((role.to_owned(), content))
            .or_insert(0usize) += 1;
    }
    assert!(
        !original_counts.is_empty(),
        "fixture must contain real conversation messages"
    );
    for (message, count) in &original_counts {
        assert!(
            loaded_counts.get(message).copied().unwrap_or(0) >= *count,
            "a saved user/AI message was omitted"
        );
    }
    let meta = providers::codex::parse_session(&snapshot_path)
        .expect("CLI transcript remains discoverable");
    assert_eq!(Some(meta.session_id.clone()), session_id);
    let query: String = final_ai_reply
        .expect("saved AI reply")
        .trim()
        .chars()
        .take(32)
        .collect();
    let hits = search::search_contents(
        &[search::SessionRef {
            provider_id: "codex".into(),
            session_id: meta.session_id,
            source_path: snapshot_path.to_string_lossy().into_owned(),
        }],
        &query,
        1,
        search::SessionSearchMode::Exact,
    );
    assert_eq!(
        hits.len(),
        1,
        "final AI reply must match exact fulltext search"
    );
    println!("READ_ONLY_ACCEPTANCE raw_user_ai_occurrences={} loaded_messages={} final_ai_search_hits={} snapshot_bytes={}", original_counts.values().sum::<usize>(), actual.len(), hits.len(), bytes.len());
}
