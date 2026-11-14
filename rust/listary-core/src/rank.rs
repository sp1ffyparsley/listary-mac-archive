use crate::{
    cache::CachedItem,
    history::HistoryStore,
    matcher::TextMatch,
    model::{ItemId, Priority, ScoreBreakdown, SearchContext},
    text::normalize,
};

pub(crate) fn score_cached(
    item: &CachedItem,
    priority: Priority,
    text: TextMatch,
    history: &HistoryStore,
    context: &SearchContext,
    now_unix_ms: i64,
) -> ScoreBreakdown {
    score_fields(
        item.id,
        priority,
        item.parent_path(),
        text,
        history,
        context,
        now_unix_ms,
    )
}

fn score_fields(
    item_id: ItemId,
    priority: Priority,
    parent_path: &str,
    text: TextMatch,
    history: &HistoryStore,
    context: &SearchContext,
    now_unix_ms: i64,
) -> ScoreBreakdown {
    let priority_boost = priority.boost();
    let context_boost = if context.contextual {
        context
            .current_folder
            .as_deref()
            .map(normalize)
            .filter(|folder| normalize(parent_path) == *folder)
            .map(|_| 30_000)
            .unwrap_or(0)
    } else {
        0
    };
    let (history_boost, recency_boost) = history.history_boost(item_id, now_unix_ms);

    // Global launcher text tiers dominate history/recency/priority boosts. Contextual
    // file-manager search is intentionally different: current-folder locality receives a
    // large but still bounded boost so Finder-style search can prioritize the active folder.
    let text_component = ((7i64 - text.tier as i64) * 10_000) + text.quality as i64;
    let combined = text_component
        + priority_boost as i64
        + context_boost as i64
        + history_boost as i64
        + recency_boost as i64;

    ScoreBreakdown {
        text_tier: text.tier,
        text_quality: text.quality,
        priority_boost,
        context_boost,
        history_boost,
        recency_boost,
        combined,
    }
}
