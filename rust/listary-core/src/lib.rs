mod cache;
mod cancel;
mod commands_actions;
mod core;
mod error;
mod facets;
mod favorites;
mod filters;
mod history;
mod indexer;
mod logging;
mod matcher;
pub mod model;
pub mod portable;
mod priority_rules;
pub mod query;
mod rank;
mod store;
mod test_fault;
mod text;
mod transliteration;
mod web_search;
mod workspace_catalog;

pub use cache::{CacheShapeComponent, CacheShapeStats};
pub use cancel::CancellationToken;
pub use commands_actions::{
    ActionContext, ActionSelectionMode, CommandContext, CustomAction, CustomCommand,
    ExpandedInvocation, HotkeyScope,
};
pub use core::{
    CORE_SCHEMA_VERSION, Core, CoreConfig, MAX_RECONCILE_PATHS, StartupCacheSource, StartupMetrics,
};
pub use error::{CoreError, Result};
pub use favorites::{
    FavoriteDynamicGroupKind, FavoriteFolderSource, FavoriteMenu, FavoriteMenuItem, FavoritePin,
    OpenFolderCandidate, ResolvedFavoriteMenu, ResolvedFavoriteMenuItem,
};
pub use filters::{FilterPreset, FilterTarget};
pub use indexer::{CrawlOptions, CrawlReport, crawl_changed_path, crawl_root};
pub use model::*;
pub use portable::{
    PORTABLE_CONFIG_SCHEMA, PORTABLE_CONFIG_SCHEMA_VERSION, PortableConfig, PortableExportReceipt,
    PortableHistory, PortableHistoryEntry, PortableItemIdentity, PortableOmissions,
    PortablePathMapping, PortablePathStyle, PortableRestoreOptions, PortableRestoreReceipt,
    PortableRoot, export_portable_config, read_portable_config, restore_portable_config,
    validate_portable_config, write_portable_config,
};
pub use priority_rules::{PriorityRule, PriorityRuleMatch, PriorityRuleTarget};
pub use web_search::{
    ResolvedWebSearch, ResolvedWebSuggestion, WebSearchProvider, WebSearchRequest,
    WebSearchResolution, WebSuggestionProvider,
};
pub use workspace_catalog::{
    WORKSPACE_PROVIDER_HINT_PREFIX, WorkspaceCatalogPlatform, WorkspaceCatalogRefresh,
    WorkspaceCatalogSnapshot, WorkspaceCatalogTarget, load_workspace_catalog,
};
