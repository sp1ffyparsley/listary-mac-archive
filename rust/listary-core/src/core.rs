use crate::{
    cache::{CacheShapeStats, CacheSnapshotLoad, CatalogCache},
    cancel::CancellationToken,
    commands_actions::{
        ActionContext, CommandContext, CustomAction, CustomCommand, CustomizationStore,
        ExpandedInvocation,
    },
    error::{CoreError, Result},
    facets::{FileSearchFacetAccumulator, category_selected, modified_time_selected},
    favorites::{
        FavoriteMenu, FavoritesStore, MAX_DYNAMIC_LIMIT, OpenFolderCandidate,
        RecentFolderCandidate, ResolvedFavoriteMenu, canonicalize_favorite_menu,
        canonicalize_open_folders, favorite_command_ids, favorite_pins, toggle_favorite_pin,
    },
    filters::{FilterPreset, FilterResolution, FilterStore},
    history::{HistoryEntry, HistoryStore, normalized_path_is_excluded},
    indexer::{CrawlOptions, CrawlReport, crawl_changed_path, crawl_root, outermost_app_bundle},
    logging::BoundedLog,
    matcher::match_cached,
    model::{
        ActivationSource, CoreDiagnostics, DiscoveredItem, FacetCountsRequest, FacetCountsResponse,
        FileSearchRequest, FileSearchResponse, FileSearchSortField, ItemId, ItemKind, ItemRecord,
        MAX_FILE_SEARCH_PAGE_LIMIT, Priority, Recommendation, RefreshPolicy, RootAvailability,
        RootKind, RootRecord, RootRecoveryRemovalReport, ScoreBreakdown, SearchContext,
        SearchRequest, SearchResponse, SearchResult, SortDirection,
    },
    priority_rules::{PriorityRule, PriorityRuleStore},
    query, rank,
    store::{ProfileLease, Store},
    web_search::{WebSearchProvider, WebSearchRequest, WebSearchResolution, WebSearchStore},
    workspace_catalog::{
        WorkspaceCatalogRefresh, WorkspaceCatalogSnapshot, workspace_provider_launch_path,
    },
};
use parking_lot::{Mutex, RwLock};
use roaring::RoaringBitmap;
use serde::{Deserialize, Serialize};
use std::{
    cmp::Ordering,
    collections::{HashMap, HashSet},
    fs,
    path::{Path, PathBuf},
    sync::Arc,
    time::Instant,
};
use uuid::Uuid;

pub const CORE_SCHEMA_VERSION: u32 = crate::store::CATALOG_SCHEMA_VERSION;
pub const MAX_RECONCILE_PATHS: usize = 4_096;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StartupCacheSource {
    Snapshot,
    Missing,
    StaleGeneration,
    Invalid,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct StartupMetrics {
    pub total_micros: u64,
    pub state_dir_micros: u64,
    pub log_init_micros: u64,
    pub store_open_micros: u64,
    pub generation_read_micros: u64,
    pub cache_probe_micros: u64,
    pub cache_open_micros: u64,
    pub cache_header_micros: u64,
    pub cache_deserialize_micros: u64,
    pub cache_rehydrate_micros: u64,
    pub cache_rehydrate_dense_micros: u64,
    pub cache_rehydrate_ordered_paths_micros: u64,
    pub cache_snapshot_bytes: u64,
    pub cache_payload_bytes: u64,
    pub cache_source: Option<StartupCacheSource>,
    pub stale_snapshot_generation: Option<u64>,
    pub expected_catalog_generation: Option<u64>,
    pub rocksdb_item_load_micros: u64,
    pub rocksdb_items_loaded: usize,
    pub cache_rebuild_micros: u64,
    pub cache_publish_micros: u64,
    pub roots_load_micros: u64,
    pub next_id_micros: u64,
    pub history_load_micros: u64,
    pub filters_load_micros: u64,
    pub priority_rules_load_micros: u64,
    pub customization_load_micros: u64,
    pub favorites_load_micros: u64,
    pub web_search_load_micros: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CoreConfig {
    pub state_dir: PathBuf,
    pub log_max_bytes: u64,
    pub log_generations: usize,
}

impl CoreConfig {
    pub fn new(state_dir: PathBuf) -> Self {
        Self {
            state_dir,
            log_max_bytes: 10 * 1024 * 1024,
            log_generations: 3,
        }
    }
}

#[derive(Debug)]
pub struct Core {
    config: CoreConfig,
    store: Arc<Store>,
    cache: Arc<RwLock<CatalogCache>>,
    roots: Arc<RwLock<HashMap<Uuid, RootRecord>>>,
    active_workspace_provider_roots: Arc<RwLock<HashSet<Uuid>>>,
    history: Arc<Mutex<HistoryStore>>,
    filters: Arc<RwLock<FilterStore>>,
    // Global lock order whenever both are held: priority_rules, then cache.
    priority_rules: Arc<RwLock<PriorityRuleStore>>,
    customization: Arc<RwLock<CustomizationStore>>,
    favorites: Arc<RwLock<FavoritesStore>>,
    web_search: Arc<RwLock<WebSearchStore>>,
    menu_settings_guard: Arc<Mutex<()>>,
    // Held from before each canonical catalog write until its cache/root mirrors are current,
    // and across database flush + derived-cache publication.
    catalog_mutation_guard: Arc<Mutex<()>>,
    next_id: Arc<Mutex<ItemId>>,
    log: Arc<BoundedLog>,
}

impl Core {
    pub fn open(config: CoreConfig) -> Result<Self> {
        Self::open_profiled(config).map(|(core, _)| core)
    }

    pub fn open_profiled(config: CoreConfig) -> Result<(Self, StartupMetrics)> {
        let profile_lease = Store::acquire_shared_profile_lease(&config.state_dir)?;
        Self::open_profiled_with_lease(config, Some(profile_lease))
    }

    pub(crate) fn open_under_exclusive_profile_lease(config: CoreConfig) -> Result<Self> {
        Self::open_profiled_with_lease(config, None).map(|(core, _)| core)
    }

    pub fn recover_remove_root(
        config: CoreConfig,
        root_id: Uuid,
        expected_path: &Path,
    ) -> Result<RootRecoveryRemovalReport> {
        let profile_lease = Store::acquire_exclusive_profile_lease(&config.state_dir)?;
        let store = Store::open_with_profile_lease(&config.state_dir, Some(profile_lease))?;
        store.recover_remove_root(root_id, expected_path)
    }

    fn open_profiled_with_lease(
        config: CoreConfig,
        profile_lease: Option<ProfileLease>,
    ) -> Result<(Self, StartupMetrics)> {
        let total_started = Instant::now();
        let mut metrics = StartupMetrics::default();

        let state_dir_started = Instant::now();
        std::fs::create_dir_all(&config.state_dir).map_err(|source| CoreError::Filesystem {
            path: config.state_dir.clone(),
            source,
        })?;
        metrics.state_dir_micros = elapsed_micros(state_dir_started);

        let log_started = Instant::now();
        let log = Arc::new(BoundedLog::new(
            &config.state_dir.join("logs"),
            config.log_max_bytes,
            config.log_generations,
        )?);
        metrics.log_init_micros = elapsed_micros(log_started);

        let store_started = Instant::now();
        let store = Arc::new(Store::open_with_profile_lease(
            &config.state_dir,
            profile_lease,
        )?);
        metrics.store_open_micros = elapsed_micros(store_started);

        let cache_snapshot_path = config.state_dir.join("cache").join("search-cache.bin");
        let generation_started = Instant::now();
        let generation = store.catalog_generation()?;
        let catalog_identity = store.catalog_identity()?;
        metrics.generation_read_micros = elapsed_micros(generation_started);
        metrics.expected_catalog_generation = Some(generation);

        let cache_probe_started = Instant::now();
        let (snapshot_load, snapshot_profile) =
            CatalogCache::load_snapshot(&cache_snapshot_path, generation, catalog_identity);
        metrics.cache_probe_micros = elapsed_micros(cache_probe_started);
        metrics.cache_open_micros = snapshot_profile.open_micros;
        metrics.cache_header_micros = snapshot_profile.header_micros;
        metrics.cache_deserialize_micros = snapshot_profile.deserialize_micros;
        metrics.cache_rehydrate_micros = snapshot_profile.rehydrate_micros;
        metrics.cache_rehydrate_dense_micros = snapshot_profile.rehydrate_dense_micros;
        metrics.cache_rehydrate_ordered_paths_micros =
            snapshot_profile.rehydrate_ordered_paths_micros;
        metrics.cache_snapshot_bytes = snapshot_profile.snapshot_bytes;
        metrics.cache_payload_bytes = snapshot_profile.payload_bytes;

        let cache = match snapshot_load {
            CacheSnapshotLoad::Loaded(cache) => {
                metrics.cache_source = Some(StartupCacheSource::Snapshot);
                *cache
            }
            CacheSnapshotLoad::Missing => {
                metrics.cache_source = Some(StartupCacheSource::Missing);
                let load_started = Instant::now();
                let items = store.load_items()?;
                metrics.rocksdb_item_load_micros = elapsed_micros(load_started);
                metrics.rocksdb_items_loaded = items.len();
                let rebuild_started = Instant::now();
                let cache = CatalogCache::from_items(items);
                metrics.cache_rebuild_micros = elapsed_micros(rebuild_started);
                let publish_started = Instant::now();
                cache.save_snapshot(&cache_snapshot_path, generation, catalog_identity)?;
                metrics.cache_publish_micros = elapsed_micros(publish_started);
                cache
            }
            CacheSnapshotLoad::StaleGeneration {
                snapshot_generation,
                expected_generation,
            } => {
                metrics.cache_source = Some(StartupCacheSource::StaleGeneration);
                metrics.stale_snapshot_generation = Some(snapshot_generation);
                metrics.expected_catalog_generation = Some(expected_generation);
                let load_started = Instant::now();
                let items = store.load_items()?;
                metrics.rocksdb_item_load_micros = elapsed_micros(load_started);
                metrics.rocksdb_items_loaded = items.len();
                let rebuild_started = Instant::now();
                let cache = CatalogCache::from_items(items);
                metrics.cache_rebuild_micros = elapsed_micros(rebuild_started);
                let publish_started = Instant::now();
                cache.save_snapshot(&cache_snapshot_path, generation, catalog_identity)?;
                metrics.cache_publish_micros = elapsed_micros(publish_started);
                cache
            }
            CacheSnapshotLoad::Invalid => {
                metrics.cache_source = Some(StartupCacheSource::Invalid);
                let load_started = Instant::now();
                let items = store.load_items()?;
                metrics.rocksdb_item_load_micros = elapsed_micros(load_started);
                metrics.rocksdb_items_loaded = items.len();
                let rebuild_started = Instant::now();
                let cache = CatalogCache::from_items(items);
                metrics.cache_rebuild_micros = elapsed_micros(rebuild_started);
                let publish_started = Instant::now();
                cache.save_snapshot(&cache_snapshot_path, generation, catalog_identity)?;
                metrics.cache_publish_micros = elapsed_micros(publish_started);
                cache
            }
        };

        let roots_started = Instant::now();
        let roots = store
            .load_roots()?
            .into_iter()
            .map(|root| (root.id, root))
            .collect::<HashMap<_, _>>();
        metrics.roots_load_micros = elapsed_micros(roots_started);

        let history_started = Instant::now();
        let history = HistoryStore::open(&config.state_dir)?;
        metrics.history_load_micros = elapsed_micros(history_started);
        let next_id_started = Instant::now();
        let next_id = store
            .next_id()?
            .max(cache.max_id().unwrap_or(0).saturating_add(1))
            .max(
                history
                    .max_referenced_item_id()
                    .unwrap_or(0)
                    .saturating_add(1),
            );
        metrics.next_id_micros = elapsed_micros(next_id_started);

        let filters_started = Instant::now();
        let filters = FilterStore::open(&config.state_dir)?;
        metrics.filters_load_micros = elapsed_micros(filters_started);
        let priority_rules_started = Instant::now();
        let mut priority_rules = PriorityRuleStore::open(&config.state_dir)?;
        priority_rules.rebuild_overrides(&cache);
        metrics.priority_rules_load_micros = elapsed_micros(priority_rules_started);
        let customization_started = Instant::now();
        let customization = CustomizationStore::open(&config.state_dir)?;
        metrics.customization_load_micros = elapsed_micros(customization_started);
        let favorites_started = Instant::now();
        let favorites = FavoritesStore::open(&config.state_dir)?;
        metrics.favorites_load_micros = elapsed_micros(favorites_started);
        let web_search_started = Instant::now();
        let web_search = WebSearchStore::open(&config.state_dir)?;
        metrics.web_search_load_micros = elapsed_micros(web_search_started);
        log.event(
            "core_open",
            &format!("items={} roots={}", cache.len(), roots.len()),
        );
        let core = Self {
            config,
            store,
            cache: Arc::new(RwLock::new(cache)),
            roots: Arc::new(RwLock::new(roots)),
            active_workspace_provider_roots: Arc::new(RwLock::new(HashSet::new())),
            history: Arc::new(Mutex::new(history)),
            filters: Arc::new(RwLock::new(filters)),
            priority_rules: Arc::new(RwLock::new(priority_rules)),
            customization: Arc::new(RwLock::new(customization)),
            favorites: Arc::new(RwLock::new(favorites)),
            web_search: Arc::new(RwLock::new(web_search)),
            menu_settings_guard: Arc::new(Mutex::new(())),
            catalog_mutation_guard: Arc::new(Mutex::new(())),
            next_id: Arc::new(Mutex::new(next_id)),
            log,
        };
        metrics.total_micros = elapsed_micros(total_started);
        Ok((core, metrics))
    }

    pub fn state_dir(&self) -> &Path {
        &self.config.state_dir
    }

    pub fn cache_shape_stats(&self) -> CacheShapeStats {
        self.cache.read().shape_stats()
    }

    pub fn list_roots(&self) -> Vec<RootRecord> {
        let mut roots: Vec<_> = self.roots.read().values().cloned().collect();
        roots.sort_by(|a, b| a.path.cmp(&b.path));
        roots
    }

    fn enabled_root_ids(&self) -> HashSet<Uuid> {
        let mut ids = self
            .roots
            .read()
            .values()
            .filter_map(|root| root.enabled.then_some(root.id))
            .collect::<HashSet<_>>();
        ids.extend(self.active_workspace_provider_roots.read().iter().copied());
        ids
    }

    pub fn list_filters(&self) -> Vec<FilterPreset> {
        self.filters.read().list()
    }

    pub fn put_filter(&self, preset: FilterPreset) -> Result<FilterPreset> {
        self.filters.write().put(preset)
    }

    pub fn remove_filter(&self, filter_id: Uuid) -> Result<()> {
        self.filters.write().remove(filter_id)
    }

    pub fn list_priority_rules(&self) -> Vec<PriorityRule> {
        self.priority_rules.read().list()
    }

    pub fn replace_priority_rules(&self, rules: Vec<PriorityRule>) -> Result<Vec<PriorityRule>> {
        let mut priority_rules = self.priority_rules.write();
        let rules = priority_rules.replace(rules)?;
        let cache = self.cache.read();
        priority_rules.rebuild_overrides(&cache);
        Ok(rules)
    }

    pub fn list_commands(&self) -> Vec<CustomCommand> {
        self.customization.read().list_commands()
    }

    pub fn put_command(&self, command: CustomCommand) -> Result<CustomCommand> {
        let _guard = self.menu_settings_guard.lock();
        self.customization.write().put_command(command)
    }

    pub fn remove_command(&self, command_id: Uuid) -> Result<()> {
        let _guard = self.menu_settings_guard.lock();
        if self.favorites.read().references_command(command_id) {
            return Err(CoreError::InvalidRequest(format!(
                "command is referenced by favorites menu: {command_id}"
            )));
        }
        self.customization.write().remove_command(command_id)
    }

    pub fn expand_command(
        &self,
        command_id: Uuid,
        context: &CommandContext,
    ) -> Result<ExpandedInvocation> {
        self.customization
            .read()
            .expand_command(command_id, context)
    }

    pub fn list_actions(&self) -> Vec<CustomAction> {
        self.customization.read().list_actions()
    }

    pub fn put_action(&self, action: CustomAction) -> Result<CustomAction> {
        self.customization.write().put_action(action)
    }

    pub fn remove_action(&self, action_id: Uuid) -> Result<()> {
        self.customization.write().remove_action(action_id)
    }

    pub fn expand_action(
        &self,
        action_id: Uuid,
        context: &ActionContext,
    ) -> Result<ExpandedInvocation> {
        self.customization.read().expand_action(action_id, context)
    }

    pub fn get_favorites(&self) -> FavoriteMenu {
        self.favorites.read().get()
    }

    pub fn put_favorites(&self, menu: FavoriteMenu) -> Result<FavoriteMenu> {
        let _guard = self.menu_settings_guard.lock();
        let menu = canonicalize_favorite_menu(menu)?;
        let known_commands = self
            .customization
            .read()
            .list_commands()
            .into_iter()
            .map(|command| command.id)
            .collect::<HashSet<_>>();
        for command_id in favorite_command_ids(&menu) {
            if !known_commands.contains(&command_id) {
                return Err(CoreError::InvalidRequest(format!(
                    "favorites menu references unknown command: {command_id}"
                )));
            }
        }
        self.favorites.write().put(menu)
    }

    pub fn toggle_pin(&self, item_id: ItemId) -> Result<bool> {
        let item = self
            .cache
            .read()
            .get(item_id)
            .ok_or_else(|| CoreError::InvalidRequest(format!("unknown item id {item_id}")))?;
        let _guard = self.menu_settings_guard.lock();
        let mut favorites = self.favorites.write();
        let mut menu = favorites.get();
        let pinned = toggle_favorite_pin(&mut menu, &item.path, Some(item.name))?;
        favorites.put(menu)?;
        Ok(pinned)
    }

    pub fn resolve_favorites(
        &self,
        now_unix_ms: i64,
        open_folders: Vec<OpenFolderCandidate>,
    ) -> Result<ResolvedFavoriteMenu> {
        let open_folders = canonicalize_open_folders(open_folders)?;
        let enabled_root_ids = self.enabled_root_ids();
        let priority_rules = self.priority_rules.read();
        let cache = self.cache.read();
        let recent_folders = self
            .history
            .lock()
            .recommendations_matching(
                now_unix_ms,
                MAX_DYNAMIC_LIMIT,
                |id| {
                    cache.get(id).map(|mut item| {
                        item.priority = priority_rules.effective_priority_record(&item);
                        item
                    })
                },
                |item| {
                    item.kind == ItemKind::Folder
                        && item.available
                        && enabled_root_ids.contains(&item.root_id)
                        && item.priority != Priority::Ignored
                },
            )
            .into_iter()
            .map(|recommendation| RecentFolderCandidate {
                item: recommendation.item,
            })
            .collect::<Vec<_>>();
        drop(cache);
        let _guard = self.menu_settings_guard.lock();
        let command_titles = self
            .customization
            .read()
            .list_commands()
            .into_iter()
            .map(|command| (command.id, command.title))
            .collect::<HashMap<_, _>>();
        self.favorites
            .read()
            .resolve(&recent_folders, &open_folders, |command_id| {
                command_titles.get(&command_id).cloned()
            })
    }

    pub fn list_web_search_providers(&self) -> Vec<WebSearchProvider> {
        self.web_search.read().list()
    }

    pub fn replace_web_search_providers(
        &self,
        providers: Vec<WebSearchProvider>,
    ) -> Result<Vec<WebSearchProvider>> {
        self.web_search.write().replace(providers)
    }

    pub fn resolve_web_search(&self, request: &WebSearchRequest) -> Result<WebSearchResolution> {
        self.web_search.read().resolve(request)
    }

    pub fn add_root(&self, path: PathBuf, kind: RootKind) -> Result<RootRecord> {
        let root = RootRecord::new(path, kind);
        validate_root(&root)?;
        let _catalog_guard = self.catalog_mutation_guard.lock();
        let requested = comparable_path(&root.path);
        if self
            .roots
            .read()
            .values()
            .any(|root| comparable_paths_overlap(&comparable_path(&root.path), requested.as_str()))
        {
            return Err(CoreError::InvalidRequest(format!(
                "root overlaps an existing root: {}",
                root.path.display()
            )));
        }
        self.store.put_root(&root)?;
        self.roots.write().insert(root.id, root.clone());
        self.log.event("root_add", &format!("root_id={}", root.id));
        Ok(root)
    }

    pub fn put_root(&self, root: RootRecord) -> Result<()> {
        validate_root(&root)?;
        let _catalog_guard = self.catalog_mutation_guard.lock();
        let existing = self
            .roots
            .read()
            .get(&root.id)
            .cloned()
            .ok_or_else(|| CoreError::RootNotFound(root.id.to_string()))?;
        let requested = comparable_path(&root.path);
        if self.roots.read().values().any(|other| {
            other.id != root.id
                && comparable_paths_overlap(&comparable_path(&other.path), requested.as_str())
        }) {
            return Err(CoreError::InvalidRequest(format!(
                "root overlaps another root: {}",
                root.path.display()
            )));
        }

        let indexed_ids = self.cache.read().ids_for_root(root.id);
        let path_changed = comparable_path(&existing.path) != requested;
        if path_changed && !indexed_ids.is_empty() {
            return Err(CoreError::InvalidRequest(
                "an indexed root path cannot be changed in place; remove and add the root instead"
                    .into(),
            ));
        }

        // The FFI accepts a complete record for compatibility, but runtime-owned health fields
        // are never caller-writable. Only user configuration is merged here.
        let mut updated = root;
        updated.availability = existing.availability;
        updated.last_error = existing.last_error.clone();
        updated.last_reconciled_unix_ms = existing.last_reconciled_unix_ms;
        if path_changed {
            updated.availability = RootAvailability::Stale;
            updated.last_error = Some("root path changed; reconciliation required".into());
            updated.last_reconciled_unix_ms = None;
        }

        let changed_items = if updated.priority != existing.priority {
            let cache = self.cache.read();
            indexed_ids
                .iter()
                .filter_map(|id| cache.get(id))
                .map(|mut item| {
                    item.priority = updated.priority;
                    item
                })
                .collect::<Vec<_>>()
        } else {
            Vec::new()
        };

        if changed_items.is_empty() && updated == existing {
            return Ok(());
        }

        if changed_items.is_empty() {
            self.store.put_root(&updated)?;
        } else {
            self.store.put_root_and_items(&updated, &changed_items)?;
            let mut priority_rules = self.priority_rules.write();
            for item in &changed_items {
                priority_rules.refresh_record(item);
            }
            let mut cache = self.cache.write();
            for item in changed_items {
                cache.insert(item);
            }
        }
        self.roots.write().insert(updated.id, updated);
        Ok(())
    }

    pub fn remove_root(&self, root_id: Uuid) -> Result<()> {
        let _catalog_guard = self.catalog_mutation_guard.lock();
        let mut roots = self.roots.write();
        if !roots.contains_key(&root_id) {
            return Err(CoreError::RootNotFound(root_id.to_string()));
        }
        let removed: Vec<_> = {
            let cache = self.cache.read();
            cache
                .ids_for_root(root_id)
                .iter()
                .filter_map(|id| cache.get(id))
                .collect()
        };
        let next_id = *self.next_id.lock();
        self.store
            .delete_root_snapshot(root_id, &removed, next_id)?;
        {
            let mut priority_rules = self.priority_rules.write();
            for item in &removed {
                priority_rules.remove_id(item.id);
            }
            let mut cache = self.cache.write();
            for item in &removed {
                cache.remove(item.id);
            }
        }
        roots.remove(&root_id);
        self.log.event("root_remove", &format!("root_id={root_id}"));
        Ok(())
    }

    pub fn reconcile_root(&self, root_id: Uuid, token: &CancellationToken) -> Result<CrawlReport> {
        self.reconcile_root_with_generation_transition(root_id, token)
            .map(|(report, _, _)| report)
    }

    pub fn reconcile_root_with_generation_transition(
        &self,
        root_id: Uuid,
        token: &CancellationToken,
    ) -> Result<(CrawlReport, u64, u64)> {
        let (root, expected_generation) = {
            let _catalog_guard = self.catalog_mutation_guard.lock();
            token.check()?;
            let root = self
                .roots
                .read()
                .get(&root_id)
                .cloned()
                .ok_or_else(|| CoreError::RootNotFound(root_id.to_string()))?;
            (root, self.store.catalog_generation()?)
        };

        match crawl_root(&root, token, &CrawlOptions::default()) {
            Ok((items, report)) => {
                token.check()?;
                let committed_generation = self.commit_reconciled_root(
                    root_id,
                    items,
                    &report,
                    expected_generation,
                    token,
                )?;
                self.log.event(
                    "root_reconcile",
                    &format!(
                        "root_id={} discovered={} errors={}",
                        root_id, report.discovered, report.errors
                    ),
                );
                Ok((report, expected_generation, committed_generation))
            }
            Err(CoreError::Cancelled) => Err(CoreError::Cancelled),
            Err(error) => {
                self.set_root_unavailable_if_generation(
                    root_id,
                    error.to_string(),
                    expected_generation,
                )?;
                Err(error)
            }
        }
    }

    pub fn rebuild_all(
        &self,
        token: &CancellationToken,
    ) -> Result<Vec<(Uuid, std::result::Result<CrawlReport, String>)>> {
        let roots: Vec<_> = self
            .list_roots()
            .into_iter()
            .filter(|root| root.enabled)
            .collect();
        let mut reports = Vec::with_capacity(roots.len());
        for root in roots {
            token.check()?;
            match self.reconcile_root(root.id, token) {
                Ok(report) => reports.push((root.id, Ok(report))),
                Err(CoreError::Cancelled) => return Err(CoreError::Cancelled),
                Err(error) => reports.push((root.id, Err(error.to_string()))),
            }
        }
        Ok(reports)
    }

    pub fn reconcile_paths(
        &self,
        root_id: Uuid,
        paths: Vec<PathBuf>,
        token: &CancellationToken,
    ) -> Result<CrawlReport> {
        self.reconcile_paths_at_generation(root_id, paths, None, token)
    }

    pub fn reconcile_paths_at_generation(
        &self,
        root_id: Uuid,
        paths: Vec<PathBuf>,
        expected_catalog_generation: Option<u64>,
        token: &CancellationToken,
    ) -> Result<CrawlReport> {
        self.reconcile_paths_with_generation_transition(
            root_id,
            paths,
            expected_catalog_generation,
            token,
        )
        .map(|(report, _, _)| report)
    }

    pub fn reconcile_paths_with_generation_transition(
        &self,
        root_id: Uuid,
        paths: Vec<PathBuf>,
        expected_catalog_generation: Option<u64>,
        token: &CancellationToken,
    ) -> Result<(CrawlReport, u64, u64)> {
        if paths.len() > MAX_RECONCILE_PATHS {
            return Err(CoreError::InvalidRequest(format!(
                "dirty-path reconciliation exceeds the {MAX_RECONCILE_PATHS}-path batch limit"
            )));
        }
        let _catalog_guard = self.catalog_mutation_guard.lock();
        token.check()?;
        let actual_generation = self.store.catalog_generation()?;
        if let Some(expected) = expected_catalog_generation
            && actual_generation != expected
        {
            return Err(CoreError::StaleCatalog {
                expected,
                actual: actual_generation,
            });
        }
        let root = self
            .roots
            .read()
            .get(&root_id)
            .cloned()
            .ok_or_else(|| CoreError::RootNotFound(root_id.to_string()))?;
        let paths = minimize_changed_paths(&root.path, paths)?;
        let mut removed_by_id = HashMap::new();
        let mut discovered_by_path = HashMap::new();
        let mut report = CrawlReport::default();

        for path in paths {
            token.check()?;
            {
                let cache = self.cache.read();
                for id in cache.ids_under_path(&path) {
                    if let Some(item) = cache.get(id) {
                        removed_by_id.insert(id, item);
                    }
                }
            }
            let (items, partial) =
                crawl_changed_path(&root, &path, token, &CrawlOptions::default())?;
            report.merge(partial);
            for item in items {
                discovered_by_path.insert(item.path.to_string_lossy().into_owned(), item);
            }
        }
        token.check()?;

        let mut next_id = self.next_id.lock();
        let existing_by_path: HashMap<String, ItemRecord> = removed_by_id
            .values()
            .map(|item| (item.path.clone(), item.clone()))
            .collect();
        let discovered = discovered_by_path.into_values().collect::<Vec<_>>();
        let assigned_ids = assign_snapshot_item_ids(
            &existing_by_path,
            &discovered,
            report.failed_paths(),
            &mut next_id,
        )?;
        let mut new_items = Vec::with_capacity(discovered.len());
        for (discovered, id) in discovered.into_iter().zip(assigned_ids) {
            let mut item = ItemRecord::from_path_metadata(
                id,
                root_id,
                &discovered.path,
                discovered.kind,
                discovered.size,
                discovered.modified_unix_ms,
                discovered.priority,
            );
            item.stable_file_id = discovered.stable_file_id;
            item.preview_hint = discovered.preview_hint;
            new_items.push(item);
        }
        let removed = snapshot_removed_items(&existing_by_path, &new_items, report.failed_paths());
        let root_update = (report.errors > 0).then(|| {
            let mut stale = root.clone();
            stale.availability = RootAvailability::Stale;
            stale.last_error = Some(format!(
                "{} localized crawl error(s); previous records retained for unreadable paths",
                report.errors
            ));
            stale.last_reconciled_unix_ms = Some(chrono::Utc::now().timestamp_millis());
            stale
        });
        let items_changed = !removed.is_empty()
            || new_items.iter().any(|item| {
                existing_by_path
                    .get(&item.path)
                    .is_none_or(|existing| !item_record_equivalent(existing, item))
            });
        if !items_changed && root_update.is_none() {
            drop(next_id);
            return Ok((report, actual_generation, actual_generation));
        }
        if let Some(root) = root_update.as_ref() {
            self.store
                .write_snapshot_and_root(root, &new_items, &removed, *next_id)?;
        } else {
            self.store
                .write_snapshot(root_id, &new_items, &removed, *next_id)?;
        }
        drop(next_id);

        {
            let mut priority_rules = self.priority_rules.write();
            for item in &removed {
                priority_rules.remove_id(item.id);
            }
            for item in &new_items {
                priority_rules.refresh_record(item);
            }
        }
        let mut cache = self.cache.write();
        for item in removed {
            cache.remove(item.id);
        }
        for item in new_items {
            cache.insert(item);
        }
        drop(cache);
        if let Some(root) = root_update {
            self.roots.write().insert(root.id, root);
        }
        Ok((
            report,
            actual_generation,
            actual_generation.saturating_add(1),
        ))
    }

    pub fn replace_root_snapshot(
        &self,
        root_id: Uuid,
        discovered: Vec<DiscoveredItem>,
    ) -> Result<()> {
        let _catalog_guard = self.catalog_mutation_guard.lock();
        self.replace_root_snapshot_while_guarded(root_id, discovered, None, &[])
    }

    pub fn replace_workspace_catalog_snapshot(
        &self,
        snapshot: WorkspaceCatalogSnapshot,
        token: &CancellationToken,
    ) -> Result<WorkspaceCatalogRefresh> {
        token.check()?;
        let discovered = snapshot.discovered_items(&self.config.state_dir)?;
        let root_id = snapshot.root_id;
        let workspace_id = snapshot.workspace_id.clone();
        let source_provider = snapshot.source_provider.clone();
        let _catalog_guard = self.catalog_mutation_guard.lock();
        token.check()?;

        let existing_by_path: HashMap<String, ItemRecord> = {
            let cache = self.cache.read();
            cache
                .ids_for_root(root_id)
                .iter()
                .filter_map(|id| cache.get(id).map(|item| (item.path.clone(), item)))
                .collect()
        };
        let mut next_id_guard = self.next_id.lock();
        let mut seen = HashSet::with_capacity(discovered.len());
        let mut new_items = Vec::with_capacity(discovered.len());
        let assigned_ids =
            assign_snapshot_item_ids(&existing_by_path, &discovered, &[], &mut next_id_guard)?;

        for (discovered, id) in discovered.into_iter().zip(assigned_ids) {
            token.check()?;
            let path = discovered.path.to_string_lossy().into_owned();
            if !seen.insert(path.clone()) {
                return Err(CoreError::InvalidRequest(format!(
                    "workspace provider produced duplicate synthetic path: {path}"
                )));
            }
            let mut item = ItemRecord::from_path_metadata(
                id,
                root_id,
                &discovered.path,
                discovered.kind,
                discovered.size,
                discovered.modified_unix_ms,
                discovered.priority,
            );
            if let Some(search_name) = discovered.search_name {
                item.name = search_name;
            }
            item.stable_file_id = discovered.stable_file_id;
            item.preview_hint = discovered.preview_hint;
            new_items.push(item);
        }

        token.check()?;
        let unchanged = new_items.len() == existing_by_path.len()
            && new_items.iter().all(|item| {
                existing_by_path
                    .get(&item.path)
                    .is_some_and(|existing| workspace_provider_item_equivalent(existing, item))
            });
        if unchanged {
            let catalog_generation = self.store.catalog_generation()?;
            drop(next_id_guard);
            self.active_workspace_provider_roots.write().insert(root_id);
            return Ok(WorkspaceCatalogRefresh {
                workspace_id,
                source_provider,
                root_id,
                item_count: new_items.len(),
                removed_count: 0,
                catalog_generation,
            });
        }

        let removed = snapshot_removed_items(&existing_by_path, &new_items, &[]);
        let retained_ids = new_items.iter().map(|item| item.id).collect::<HashSet<_>>();
        let removed_ids = removed
            .iter()
            .map(|item| item.id)
            .filter(|id| !retained_ids.contains(id))
            .collect::<HashSet<_>>();
        token.check()?;
        self.store
            .write_snapshot(root_id, &new_items, &removed, *next_id_guard)?;
        let catalog_generation = self.store.catalog_generation()?;
        drop(next_id_guard);

        {
            let mut priority_rules = self.priority_rules.write();
            for item in &removed {
                priority_rules.remove_id(item.id);
            }
            for item in &new_items {
                priority_rules.refresh_record(item);
            }
        }
        {
            let mut cache = self.cache.write();
            for item in &removed {
                cache.remove(item.id);
            }
            for item in new_items.iter().cloned() {
                cache.insert(item);
            }
        }
        self.history.lock().remove_items(&removed_ids)?;
        self.active_workspace_provider_roots.write().insert(root_id);
        self.log.event(
            "workspace_catalog_refresh",
            &format!(
                "provider={} items={} removed={}",
                source_provider,
                new_items.len(),
                removed_ids.len()
            ),
        );
        Ok(WorkspaceCatalogRefresh {
            workspace_id,
            source_provider,
            root_id,
            item_count: new_items.len(),
            removed_count: removed_ids.len(),
            catalog_generation,
        })
    }

    fn commit_reconciled_root(
        &self,
        root_id: Uuid,
        discovered: Vec<DiscoveredItem>,
        report: &CrawlReport,
        expected_generation: u64,
        token: &CancellationToken,
    ) -> Result<u64> {
        let _catalog_guard = self.catalog_mutation_guard.lock();
        token.check()?;
        let actual_generation = self.store.catalog_generation()?;
        if actual_generation != expected_generation {
            return Err(CoreError::StaleCatalog {
                expected: expected_generation,
                actual: actual_generation,
            });
        }
        let mut updated = self
            .roots
            .read()
            .get(&root_id)
            .cloned()
            .ok_or_else(|| CoreError::RootNotFound(root_id.to_string()))?;
        updated.last_reconciled_unix_ms = Some(chrono::Utc::now().timestamp_millis());
        if report.errors == 0 {
            updated.availability = RootAvailability::Available;
            updated.last_error = None;
        } else {
            updated.availability = RootAvailability::Stale;
            updated.last_error = Some(format!(
                "{} localized crawl error(s); previous records retained for unreadable paths",
                report.errors
            ));
        }
        self.replace_root_snapshot_while_guarded(
            root_id,
            discovered,
            Some(updated),
            report.failed_paths(),
        )?;
        Ok(actual_generation.saturating_add(1))
    }

    fn replace_root_snapshot_while_guarded(
        &self,
        root_id: Uuid,
        discovered: Vec<DiscoveredItem>,
        root_update: Option<RootRecord>,
        preserved_paths: &[PathBuf],
    ) -> Result<()> {
        if !self.roots.read().contains_key(&root_id) {
            return Err(CoreError::RootNotFound(root_id.to_string()));
        }
        let existing_by_path: HashMap<String, ItemRecord> = {
            let cache = self.cache.read();
            cache
                .ids_for_root(root_id)
                .iter()
                .filter_map(|id| cache.get(id).map(|item| (item.path.clone(), item)))
                .collect()
        };
        let mut next_id_guard = self.next_id.lock();
        let mut seen = HashSet::with_capacity(discovered.len());
        let discovered = discovered
            .into_iter()
            .filter(|item| seen.insert(item.path.to_string_lossy().into_owned()))
            .collect::<Vec<_>>();
        let assigned_ids = assign_snapshot_item_ids(
            &existing_by_path,
            &discovered,
            preserved_paths,
            &mut next_id_guard,
        )?;
        let mut new_items = Vec::with_capacity(discovered.len());

        for (discovered, id) in discovered.into_iter().zip(assigned_ids) {
            let mut item = ItemRecord::from_path_metadata(
                id,
                root_id,
                &discovered.path,
                discovered.kind,
                discovered.size,
                discovered.modified_unix_ms,
                discovered.priority,
            );
            item.stable_file_id = discovered.stable_file_id;
            item.preview_hint = discovered.preview_hint;
            new_items.push(item);
        }

        let removed = snapshot_removed_items(&existing_by_path, &new_items, preserved_paths);
        if let Some(root) = root_update.as_ref() {
            self.store
                .write_snapshot_and_root(root, &new_items, &removed, *next_id_guard)?;
        } else {
            self.store
                .write_snapshot(root_id, &new_items, &removed, *next_id_guard)?;
        }
        drop(next_id_guard);

        {
            let mut priority_rules = self.priority_rules.write();
            for item in &removed {
                priority_rules.remove_id(item.id);
            }
            for item in &new_items {
                priority_rules.refresh_record(item);
            }
        }
        let mut cache = self.cache.write();
        for item in removed {
            cache.remove(item.id);
        }
        for item in new_items {
            cache.insert(item);
        }
        drop(cache);
        if let Some(root) = root_update {
            self.roots.write().insert(root.id, root);
        }
        Ok(())
    }

    pub fn upsert_items(
        &self,
        root_id: Uuid,
        discovered: Vec<DiscoveredItem>,
    ) -> Result<Vec<ItemId>> {
        self.upsert_items_inner(root_id, discovered, || {})
    }

    #[cfg(feature = "test-fault-injection")]
    #[doc(hidden)]
    pub fn test_upsert_items_with_post_commit_hook<F>(
        &self,
        root_id: Uuid,
        discovered: Vec<DiscoveredItem>,
        after_store_commit: F,
    ) -> Result<Vec<ItemId>>
    where
        F: FnOnce(),
    {
        self.upsert_items_inner(root_id, discovered, after_store_commit)
    }

    fn upsert_items_inner<F>(
        &self,
        root_id: Uuid,
        discovered: Vec<DiscoveredItem>,
        after_store_commit: F,
    ) -> Result<Vec<ItemId>>
    where
        F: FnOnce(),
    {
        let _catalog_guard = self.catalog_mutation_guard.lock();
        if !self.roots.read().contains_key(&root_id) {
            return Err(CoreError::RootNotFound(root_id.to_string()));
        }
        let mut next = self.next_id.lock();
        let mut items = Vec::with_capacity(discovered.len());
        let mut ids = Vec::with_capacity(discovered.len());
        {
            let cache = self.cache.read();
            for d in discovered {
                let path = d.path.to_string_lossy().into_owned();
                let id = match cache.id_for_path(&path) {
                    Some(id) => id,
                    None => allocate_next_item_id(&mut next)?,
                };
                let mut item = ItemRecord::from_path_metadata(
                    id,
                    root_id,
                    &d.path,
                    d.kind,
                    d.size,
                    d.modified_unix_ms,
                    d.priority,
                );
                item.stable_file_id = d.stable_file_id;
                item.preview_hint = d.preview_hint;
                ids.push(id);
                items.push(item);
            }
        }
        self.store.put_items(&items, Some(*next))?;
        after_store_commit();
        drop(next);
        {
            let mut priority_rules = self.priority_rules.write();
            for item in &items {
                priority_rules.refresh_record(item);
            }
        }
        let mut cache = self.cache.write();
        for item in items {
            cache.insert(item);
        }
        Ok(ids)
    }

    pub fn set_root_unavailable(&self, root_id: Uuid, reason: String) -> Result<()> {
        let _catalog_guard = self.catalog_mutation_guard.lock();
        self.set_root_unavailable_while_guarded(root_id, reason)
    }

    fn set_root_unavailable_if_generation(
        &self,
        root_id: Uuid,
        reason: String,
        expected_generation: u64,
    ) -> Result<()> {
        let _catalog_guard = self.catalog_mutation_guard.lock();
        let actual_generation = self.store.catalog_generation()?;
        if actual_generation != expected_generation {
            return Err(CoreError::StaleCatalog {
                expected: expected_generation,
                actual: actual_generation,
            });
        }
        self.set_root_unavailable_while_guarded(root_id, reason)
    }

    fn set_root_unavailable_while_guarded(&self, root_id: Uuid, reason: String) -> Result<()> {
        let mut root = self
            .roots
            .read()
            .get(&root_id)
            .cloned()
            .ok_or_else(|| CoreError::RootNotFound(root_id.to_string()))?;
        root.availability = RootAvailability::Unavailable;
        root.last_error = Some(reason);
        let changed = self.cache.read().root_availability_updates(root_id, false);
        self.store.put_root_and_items(&root, &changed)?;
        self.roots.write().insert(root_id, root);
        if !changed.is_empty() {
            let mut cache = self.cache.write();
            for item in changed {
                cache.insert(item);
            }
        }
        Ok(())
    }

    pub fn mark_root_available(&self, root_id: Uuid) -> Result<()> {
        let _catalog_guard = self.catalog_mutation_guard.lock();
        let mut root = self
            .roots
            .read()
            .get(&root_id)
            .cloned()
            .ok_or_else(|| CoreError::RootNotFound(root_id.to_string()))?;
        root.availability = RootAvailability::Available;
        root.last_error = None;
        let changed = self.cache.read().root_availability_updates(root_id, true);
        self.store.put_root_and_items(&root, &changed)?;
        self.roots.write().insert(root_id, root);
        if !changed.is_empty() {
            let mut cache = self.cache.write();
            for item in changed {
                cache.insert(item);
            }
        }
        Ok(())
    }

    pub fn search(
        &self,
        request: &SearchRequest,
        token: &CancellationToken,
    ) -> Result<SearchResponse> {
        let started = Instant::now();
        let enabled_root_ids = self.enabled_root_ids();
        let priority_rules = self.priority_rules.read();
        let filter_resolution = self.filters.read().resolve_query(&request.query);
        let parsed = query::parse(
            &filter_resolution.expanded_query,
            request.now_unix_ms,
            request.timezone_offset_minutes,
        );
        token.check()?;

        if parsed.positive_terms.is_empty()
            && parsed.include_extensions.is_empty()
            && parsed.modified_unix_ms.is_empty()
            && parsed.size_bytes.is_empty()
            && request.query.trim().is_empty()
            && request.categories.is_empty()
            && request.modified_time.is_none()
        {
            let recommendations =
                self.recommendations_matching(request.now_unix_ms, request.limit, |item| {
                    (request.include_unavailable || item.available)
                        && (request.root_ids.is_empty() || request.root_ids.contains(&item.root_id))
                        && (request.kinds.is_empty() || request.kinds.contains(&item.kind))
                });
            let history = self.history.lock();
            let results = recommendations
                .into_iter()
                .map(|rec| SearchResult {
                    score: crate::model::ScoreBreakdown {
                        text_tier: 7,
                        text_quality: 0,
                        priority_boost: rec.item.priority.boost(),
                        context_boost: 0,
                        history_boost: history.history_boost(rec.item.id, request.now_unix_ms).0,
                        recency_boost: history.history_boost(rec.item.id, request.now_unix_ms).1,
                        combined: rec.score,
                    },
                    item: rec.item,
                })
                .collect();
            return Ok(SearchResponse {
                results,
                parsed,
                considered_candidates: 0,
                cancelled: false,
                elapsed_micros: started.elapsed().as_micros().min(u64::MAX as u128) as u64,
            });
        }

        let cache = self.cache.read();
        let mut candidate_ids = cache.candidate_ids(&parsed);
        let mut strong_candidate_ids = cache.strong_candidate_ids(&parsed);
        let mut exact_candidate_ids = cache.exact_candidate_ids(&parsed);
        if let Some(allowed) = search_scope_mask(
            &cache,
            &enabled_root_ids,
            &request.root_ids,
            &request.kinds,
            &filter_resolution,
        ) {
            candidate_ids &= &allowed;
            strong_candidate_ids &= &allowed;
            exact_candidate_ids &= &allowed;
        }
        let active_provider_roots = self.active_workspace_provider_roots.read().clone();
        let shadowed_launcher_ids =
            workspace_provider_shadowed_ids(&cache, &candidate_ids, &active_provider_roots);
        candidate_ids -= &shadowed_launcher_ids;
        strong_candidate_ids -= &shadowed_launcher_ids;
        exact_candidate_ids -= &shadowed_launcher_ids;
        let mut effective_ignored = priority_rules.ignored_candidate_ids(&candidate_ids);
        let base_ignored = &candidate_ids & cache.ids_for_base_priority(Priority::Ignored);
        for id in base_ignored {
            if cache.get_cached(id).is_some_and(|item| {
                priority_rules.effective_priority_cached(item) == Priority::Ignored
            }) {
                effective_ignored.insert(id);
            }
        }
        candidate_ids -= &effective_ignored;
        strong_candidate_ids -= &effective_ignored;
        exact_candidate_ids -= &effective_ignored;

        let mut high_candidate_ids = priority_rules.high_candidate_ids(&candidate_ids);
        let base_high = &candidate_ids & cache.ids_for_base_priority(Priority::High);
        for id in base_high {
            if cache.get_cached(id).is_some_and(|item| {
                priority_rules.effective_priority_cached(item) == Priority::High
            }) {
                high_candidate_ids.insert(id);
            }
        }
        let candidate_limit = request.candidate_limit.max(request.limit).max(1);
        let history = self.history.lock();
        let mut selected_ids = Vec::with_capacity(candidate_limit.min(16_384));
        let mut selected = RoaringBitmap::new();
        for id in exact_candidate_ids.iter() {
            if selected_ids.len() == candidate_limit {
                break;
            }
            if selected.insert(id) {
                selected_ids.push(id);
            }
        }
        for id in history.priority_item_ids(request.now_unix_ms, 512) {
            if selected_ids.len() == candidate_limit {
                break;
            }
            if candidate_ids.contains(id) && selected.insert(id) {
                selected_ids.push(id);
            }
        }
        if request.context.contextual
            && let Some(current_folder) = request.context.current_folder.as_deref()
        {
            let contextual = &candidate_ids & cache.ids_for_parent(current_folder);
            for id in contextual.iter() {
                if selected_ids.len() == candidate_limit {
                    break;
                }
                if selected.insert(id) {
                    selected_ids.push(id);
                }
            }
        }

        // High priority is a bounded admission lane, not a replacement for text relevance.
        // Reserve enough space to admit late High items while leaving at least one slot for
        // stronger normal text matches at tiny interactive budgets.
        let remaining = candidate_limit.saturating_sub(selected_ids.len());
        let has_unselected_high = high_candidate_ids.iter().any(|id| !selected.contains(id));
        let high_lane_budget = if remaining >= 2 && has_unselected_high {
            (remaining / 4).clamp(1, 64)
        } else {
            0
        };
        let strong_lane_end = candidate_limit.saturating_sub(high_lane_budget);
        for id in strong_candidate_ids.iter() {
            if selected_ids.len() == strong_lane_end {
                break;
            }
            if selected.insert(id) {
                selected_ids.push(id);
            }
        }
        let mut admitted_high = 0usize;
        for id in high_candidate_ids.iter() {
            if admitted_high == high_lane_budget || selected_ids.len() == candidate_limit {
                break;
            }
            if selected.insert(id) {
                selected_ids.push(id);
                admitted_high += 1;
            }
        }
        for id in strong_candidate_ids.iter() {
            if selected_ids.len() == candidate_limit {
                break;
            }
            if selected.insert(id) {
                selected_ids.push(id);
            }
        }
        for id in candidate_ids.iter() {
            if selected_ids.len() == candidate_limit {
                break;
            }
            if selected.insert(id) {
                selected_ids.push(id);
            }
        }

        let mut scored = Vec::with_capacity(candidate_limit.min(16_384));
        let mut considered = 0usize;
        for id in selected_ids {
            if considered.is_multiple_of(256) {
                token.check()?;
            }
            considered += 1;
            let Some(cached) = cache.get_cached(id) else {
                continue;
            };
            let effective_priority = priority_rules.effective_priority_cached(cached);
            if !request.include_unavailable && !cached.available {
                continue;
            }
            if history.is_path_excluded(&cached.path) {
                continue;
            }
            if !category_selected(cached, &request.categories)
                || !modified_time_selected(
                    cached.modified_unix_ms,
                    request.modified_time,
                    request.now_unix_ms,
                    request.timezone_offset_minutes,
                )
            {
                continue;
            }
            let Some(text_match) = match_cached(cached, &parsed, effective_priority) else {
                continue;
            };
            let score = rank::score_cached(
                cached,
                effective_priority,
                text_match,
                &history,
                &request.context,
                request.now_unix_ms,
            );
            scored.push((id, score, cached.name().len(), cached.tie_key));
        }
        scored.sort_unstable_by(|a, b| {
            b.1.combined
                .cmp(&a.1.combined)
                .then_with(|| a.1.text_tier.cmp(&b.1.text_tier))
                .then_with(|| b.1.text_quality.cmp(&a.1.text_quality))
                .then_with(|| a.2.cmp(&b.2))
                .then_with(|| a.3.cmp(&b.3))
                .then_with(|| a.0.cmp(&b.0))
        });
        scored.truncate(request.limit);
        let results = scored
            .into_iter()
            .filter_map(|(id, score, _, _)| {
                cache.get(id).map(|mut item| {
                    item.priority = priority_rules.effective_priority_record(&item);
                    SearchResult { item, score }
                })
            })
            .collect();
        Ok(SearchResponse {
            results,
            parsed,
            considered_candidates: considered,
            cancelled: false,
            elapsed_micros: started.elapsed().as_micros().min(u64::MAX as u128) as u64,
        })
    }

    /// Runs exact, pageable File Search against one serialized catalog snapshot.
    ///
    /// This deliberately does not reuse the launcher's bounded admission lanes. It does reuse
    /// the canonical candidate index, saved-filter resolver, matcher, priority rules, history
    /// exclusions, and ranker. The catalog mutation guard remains held until the response is
    /// assembled so the returned generation identifies the exact cache snapshot that was read.
    pub fn file_search(
        &self,
        request: &FileSearchRequest,
        token: &CancellationToken,
    ) -> Result<FileSearchResponse> {
        if request.limit == 0 {
            return Err(CoreError::InvalidRequest(
                "file search limit must be greater than zero".into(),
            ));
        }
        if request.limit > MAX_FILE_SEARCH_PAGE_LIMIT {
            return Err(CoreError::InvalidRequest(format!(
                "file search limit {} exceeds maximum {MAX_FILE_SEARCH_PAGE_LIMIT}",
                request.limit
            )));
        }

        let started = Instant::now();
        let _catalog_guard = self.catalog_mutation_guard.lock();
        token.check()?;
        let catalog_generation = self.store.catalog_generation()?;
        if let Some(expected) = request.expected_catalog_generation
            && expected != catalog_generation
        {
            return Err(CoreError::StaleCatalog {
                expected,
                actual: catalog_generation,
            });
        }

        let filter_resolution = self.filters.read().resolve_query(&request.query);
        let parsed = query::parse(
            &filter_resolution.expanded_query,
            request.now_unix_ms,
            request.timezone_offset_minutes,
        );
        let enabled_root_ids = self.enabled_root_ids();
        let priority_rules = self.priority_rules.read();
        let cache = self.cache.read();
        let history = self.history.lock();

        let mut candidate_ids = cache.candidate_ids(&parsed);
        if let Some(allowed) = search_scope_mask(
            &cache,
            &enabled_root_ids,
            &request.root_ids,
            &[],
            &filter_resolution,
        ) {
            candidate_ids &= &allowed;
        }
        let active_provider_roots = self.active_workspace_provider_roots.read().clone();
        let shadowed_launcher_ids =
            workspace_provider_shadowed_ids(&cache, &candidate_ids, &active_provider_roots);
        candidate_ids -= &shadowed_launcher_ids;

        let mut considered_candidates = 0u64;
        let mut scored = Vec::with_capacity(candidate_ids.len().min(usize::MAX as u64) as usize);
        let mut facet_accumulator = request
            .include_facets
            .then(FileSearchFacetAccumulator::default);
        let context = SearchContext::default();
        for id in candidate_ids.iter() {
            if considered_candidates.is_multiple_of(256) {
                token.check()?;
            }
            considered_candidates = considered_candidates.saturating_add(1);
            let Some(cached) = cache.get_cached(id) else {
                continue;
            };
            let effective_priority = priority_rules.effective_priority_cached(cached);
            if !request.include_unavailable && !cached.available {
                continue;
            }
            if history.is_path_excluded(&cached.path) {
                continue;
            }
            let category_is_selected = category_selected(cached, &request.categories);
            let modified_time_is_selected = modified_time_selected(
                cached.modified_unix_ms,
                request.modified_time,
                request.now_unix_ms,
                request.timezone_offset_minutes,
            );
            // Preserve the old non-facet hot path: selected Type/Modified facets can
            // reject an item before matcher work. The data-aware sidebar instead
            // needs sibling counts, so it matches once and derives result + facet
            // state from that same candidate traversal.
            if facet_accumulator.is_none() {
                if !(category_is_selected && modified_time_is_selected) {
                    continue;
                }
            } else if !category_is_selected && !modified_time_is_selected {
                continue;
            }
            let Some(text_match) = match_cached(cached, &parsed, effective_priority) else {
                continue;
            };
            if let Some(accumulator) = facet_accumulator.as_mut() {
                accumulator.observe_query_match(
                    cached,
                    &request.categories,
                    request.modified_time,
                    request.now_unix_ms,
                    request.timezone_offset_minutes,
                );
            }
            if !(category_is_selected && modified_time_is_selected) {
                continue;
            }
            let score = rank::score_cached(
                cached,
                effective_priority,
                text_match,
                &history,
                &context,
                request.now_unix_ms,
            );
            scored.push(FileSearchCandidate { id, score });
        }
        token.check()?;

        let total_matches = scored.len() as u64;
        let facets = facet_accumulator.map(FileSearchFacetAccumulator::into_projection);
        debug_assert!(
            facets
                .as_ref()
                .is_none_or(|projection| projection.matched_total == total_matches)
        );
        let requested_end = request
            .offset
            .saturating_add(request.limit as u64)
            .min(total_matches);
        let prefix_len = if request.offset >= total_matches {
            0usize
        } else {
            usize::try_from(requested_end).unwrap_or(scored.len())
        };
        let start_index = if prefix_len == 0 {
            0usize
        } else {
            usize::try_from(request.offset).unwrap_or(prefix_len)
        };
        let ordered_prefix = cancellable_top_prefix_by(scored, prefix_len, token, |a, b| {
            file_search_candidate_order(a, b, &cache, request.sort_field, request.sort_direction)
        })?;
        let results = ordered_prefix[start_index..prefix_len]
            .iter()
            .filter_map(|candidate| {
                cache.get(candidate.id).map(|mut item| {
                    item.priority = priority_rules.effective_priority_record(&item);
                    SearchResult {
                        item,
                        score: candidate.score.clone(),
                    }
                })
            })
            .collect();
        token.check()?;

        Ok(FileSearchResponse {
            results,
            total_matches,
            offset: request.offset,
            limit: request.limit,
            has_more: requested_end < total_matches,
            catalog_generation,
            considered_candidates,
            parsed,
            elapsed_micros: elapsed_micros(started),
            facets,
        })
    }

    pub fn facet_counts(
        &self,
        request: &FacetCountsRequest,
        token: &CancellationToken,
    ) -> Result<FacetCountsResponse> {
        let started = Instant::now();
        let enabled_root_ids = self.enabled_root_ids();
        let priority_rules = self.priority_rules.read();
        let filter_resolution = self.filters.read().resolve_query(&request.query);
        let parsed = query::parse(
            &filter_resolution.expanded_query,
            request.now_unix_ms,
            request.timezone_offset_minutes,
        );
        token.check()?;

        let excluded_path_prefixes = self.history.lock().excluded_path_prefixes_snapshot();
        let cache = self.cache.read();
        let mut candidate_ids = cache.candidate_ids(&parsed);
        if let Some(allowed) = search_scope_mask(
            &cache,
            &enabled_root_ids,
            &request.root_ids,
            &[],
            &filter_resolution,
        ) {
            candidate_ids &= &allowed;
        }
        let active_provider_roots = self.active_workspace_provider_roots.read().clone();
        let shadowed_launcher_ids =
            workspace_provider_shadowed_ids(&cache, &candidate_ids, &active_provider_roots);
        candidate_ids -= &shadowed_launcher_ids;

        let mut facet_accumulator = FileSearchFacetAccumulator::default();
        let mut considered_candidates = 0u64;

        for id in candidate_ids.iter() {
            if considered_candidates.is_multiple_of(256) {
                token.check()?;
            }
            considered_candidates = considered_candidates.saturating_add(1);
            let Some(cached) = cache.get_cached(id) else {
                continue;
            };
            let effective_priority = priority_rules.effective_priority_cached(cached);
            if !request.include_unavailable && !cached.available {
                continue;
            }
            if normalized_path_is_excluded(cached.normalized_path.as_ref(), &excluded_path_prefixes)
            {
                continue;
            }
            if match_cached(cached, &parsed, effective_priority).is_none() {
                continue;
            }

            facet_accumulator.observe_query_match(
                cached,
                &request.categories,
                request.modified_time,
                request.now_unix_ms,
                request.timezone_offset_minutes,
            );
        }
        token.check()?;
        let facets = facet_accumulator.into_projection();

        Ok(FacetCountsResponse {
            matched_total: facets.matched_total,
            categories: facets.categories,
            modified_time: facets.modified_time,
            considered_candidates,
            elapsed_micros: elapsed_micros(started),
        })
    }

    pub fn record_activation(&self, item_id: ItemId, now_unix_ms: i64) -> Result<()> {
        self.record_activation_with_source(item_id, now_unix_ms, ActivationSource::Launcher)
    }

    pub fn record_activation_with_source(
        &self,
        item_id: ItemId,
        now_unix_ms: i64,
        source: ActivationSource,
    ) -> Result<()> {
        if self.cache.read().get(item_id).is_none() {
            return Err(CoreError::InvalidRequest(format!(
                "unknown item id {item_id}"
            )));
        }
        self.history
            .lock()
            .record_activation_with_source(item_id, now_unix_ms, source)
    }

    pub fn hide_recommendation(&self, item_id: ItemId) -> Result<()> {
        self.history.lock().hide(item_id)
    }

    pub fn exclude_recommendation_path(&self, prefix: String) -> Result<()> {
        self.history.lock().exclude_path_prefix(prefix)
    }

    pub fn recommendations(&self, now_unix_ms: i64, limit: usize) -> Vec<Recommendation> {
        self.recommendations_matching(now_unix_ms, limit, |_| true)
    }

    fn recommendations_matching<P>(
        &self,
        now_unix_ms: i64,
        limit: usize,
        mut matches: P,
    ) -> Vec<Recommendation>
    where
        P: FnMut(&ItemRecord) -> bool,
    {
        if limit == 0 {
            return Vec::new();
        }
        let pins = {
            let menu = self.favorites.read().get();
            favorite_pins(&menu).to_vec()
        };
        let enabled_root_ids = self.enabled_root_ids();
        let priority_rules = self.priority_rules.read();
        let cache = self.cache.read();
        let active_provider_roots = self.active_workspace_provider_roots.read().clone();
        let shadowed_launcher_ids =
            workspace_provider_shadowed_ids(&cache, &cache.all_ids(), &active_provider_roots);
        let history = self.history.lock();
        let mut out = Vec::with_capacity(limit);
        let mut seen = HashSet::new();

        for pin in &pins {
            let Some(item_id) = cache.id_for_path(&pin.path) else {
                continue;
            };
            let Some(mut item) = cache.get(item_id) else {
                continue;
            };
            item.priority = priority_rules.effective_priority_record(&item);
            if history.is_path_excluded(&item.path)
                || item.priority == Priority::Ignored
                || !enabled_root_ids.contains(&item.root_id)
                || shadowed_launcher_ids.contains(item.id)
                || !matches(&item)
            {
                continue;
            }
            if seen.insert(item.id) {
                out.push(history.recommendation_for_item(item, now_unix_ms));
            }
            if out.len() == limit {
                return out;
            }
        }

        let automatic_limit = limit.saturating_add(pins.len()).min(usize::MAX / 2);
        let automatic = history.recommendations_matching(
            now_unix_ms,
            automatic_limit,
            |id| {
                cache.get(id).map(|mut item| {
                    item.priority = priority_rules.effective_priority_record(&item);
                    item
                })
            },
            |item| {
                item.priority != Priority::Ignored
                    && enabled_root_ids.contains(&item.root_id)
                    && !shadowed_launcher_ids.contains(item.id)
                    && matches(item)
            },
        );
        for recommendation in automatic {
            if seen.insert(recommendation.item.id) {
                out.push(recommendation);
            }
            if out.len() == limit {
                break;
            }
        }
        out
    }

    pub fn get_item(&self, item_id: ItemId) -> Option<ItemRecord> {
        let _catalog_guard = self.catalog_mutation_guard.lock();
        let priority_rules = self.priority_rules.read();
        let cache = self.cache.read();
        cache.get(item_id).map(|mut item| {
            item.priority = priority_rules.effective_priority_record(&item);
            item
        })
    }

    pub fn get_item_by_path(&self, path: &str) -> Option<ItemRecord> {
        let _catalog_guard = self.catalog_mutation_guard.lock();
        let priority_rules = self.priority_rules.read();
        let cache = self.cache.read();
        let id = cache.id_for_path(path)?;
        cache.get(id).map(|mut item| {
            item.priority = priority_rules.effective_priority_record(&item);
            item
        })
    }

    pub(crate) fn portable_history_snapshot(
        &self,
    ) -> (Vec<(ItemId, HistoryEntry)>, Vec<ItemId>, Vec<String>) {
        self.history.lock().portable_snapshot()
    }

    pub(crate) fn replace_portable_history(
        &self,
        entries: HashMap<ItemId, HistoryEntry>,
        hidden_ids: HashSet<ItemId>,
        excluded_path_prefixes: Vec<String>,
    ) -> Result<()> {
        self.history
            .lock()
            .replace_portable_state(entries, hidden_ids, excluded_path_prefixes)
    }

    pub fn diagnostics(&self) -> Result<CoreDiagnostics> {
        let roots = self.roots.read();
        let history_entries = self.history.lock().len();
        let recovery = self.store.recovery_metadata()?;
        Ok(CoreDiagnostics {
            schema_version: CORE_SCHEMA_VERSION,
            catalog_generation: self.store.catalog_generation()?,
            item_count: self.cache.read().len(),
            root_count: roots.len(),
            available_root_count: roots
                .values()
                .filter(|r| r.availability == RootAvailability::Available)
                .count(),
            unavailable_root_count: roots
                .values()
                .filter(|r| r.availability != RootAvailability::Available)
                .count(),
            index_bytes: self.store.index_bytes(),
            history_entries,
            recovery_count: recovery.recovery_count,
            last_recovery_path: recovery.last_recovery_path,
        })
    }

    pub fn catalog_generation(&self) -> Result<u64> {
        self.store.catalog_generation()
    }

    pub fn flush(&self) -> Result<()> {
        let _catalog_guard = self.catalog_mutation_guard.lock();
        self.store.flush()?;
        self.persist_cache_snapshot_while_catalog_guarded()
    }

    fn persist_cache_snapshot_while_catalog_guarded(&self) -> Result<()> {
        let generation = self.store.catalog_generation()?;
        let catalog_identity = self.store.catalog_identity()?;
        self.cache.read().save_snapshot(
            &self.config.state_dir.join("cache").join("search-cache.bin"),
            generation,
            catalog_identity,
        )
    }
}

#[derive(Clone)]
struct FileSearchCandidate {
    id: ItemId,
    score: ScoreBreakdown,
}

fn file_search_candidate_order(
    a: &FileSearchCandidate,
    b: &FileSearchCandidate,
    cache: &CatalogCache,
    field: FileSearchSortField,
    direction: SortDirection,
) -> Ordering {
    let a_item = cache
        .get_cached(a.id)
        .expect("file search candidate remains in guarded catalog");
    let b_item = cache
        .get_cached(b.id)
        .expect("file search candidate remains in guarded catalog");

    if field == FileSearchSortField::Relevance {
        let best_first = b
            .score
            .combined
            .cmp(&a.score.combined)
            .then_with(|| a.score.text_tier.cmp(&b.score.text_tier))
            .then_with(|| b.score.text_quality.cmp(&a.score.text_quality))
            .then_with(|| a_item.name().len().cmp(&b_item.name().len()))
            .then_with(|| a_item.tie_key.cmp(&b_item.tie_key))
            .then_with(|| a.id.cmp(&b.id));
        return match direction {
            SortDirection::Descending => best_first,
            SortDirection::Ascending => best_first.reverse(),
        };
    }

    let primary = match field {
        FileSearchSortField::Relevance => unreachable!("relevance handled above"),
        FileSearchSortField::Name => a_item.normalized_name.cmp(&b_item.normalized_name),
        FileSearchSortField::Path => a_item.normalized_path.cmp(&b_item.normalized_path),
        FileSearchSortField::Kind => {
            item_kind_sort_key(a_item.kind).cmp(&item_kind_sort_key(b_item.kind))
        }
        FileSearchSortField::Modified => a_item.modified_unix_ms.cmp(&b_item.modified_unix_ms),
        FileSearchSortField::Size => a_item.size.cmp(&b_item.size),
    };
    let primary = match direction {
        SortDirection::Ascending => primary,
        SortDirection::Descending => primary.reverse(),
    };
    primary
        .then_with(|| a_item.normalized_name.cmp(&b_item.normalized_name))
        .then_with(|| a_item.normalized_path.cmp(&b_item.normalized_path))
        .then_with(|| a.id.cmp(&b.id))
}

fn item_kind_sort_key(kind: ItemKind) -> u8 {
    match kind {
        ItemKind::File => 0,
        ItemKind::Folder => 1,
        ItemKind::App => 2,
        ItemKind::Package => 3,
    }
}

/// Keeps only the fully ordered prefix required by a File Search page. The heap root is the
/// worst retained candidate, so scanning remains O(n log k) for a requested prefix of k rows.
/// A final cancellable sort of that prefix preserves the exact comparator contract used by the
/// previous full-result merge sort.
fn cancellable_top_prefix_by<T, F>(
    values: Vec<T>,
    prefix_len: usize,
    token: &CancellationToken,
    compare: F,
) -> Result<Vec<T>>
where
    T: Clone,
    F: Fn(&T, &T) -> Ordering,
{
    token.check()?;
    if prefix_len == 0 || values.is_empty() {
        return Ok(Vec::new());
    }
    if prefix_len >= values.len() || prefix_len.saturating_mul(4) >= values.len() {
        let mut ordered = values;
        cancellable_sort_by(&mut ordered, token, compare)?;
        ordered.truncate(prefix_len);
        return Ok(ordered);
    }

    let mut heap = Vec::with_capacity(prefix_len);
    for (index, value) in values.into_iter().enumerate() {
        if index.is_multiple_of(256) {
            token.check()?;
        }
        if heap.len() < prefix_len {
            let insertion_index = heap.len();
            heap.push(value);
            sift_worst_up_by(&mut heap, insertion_index, &compare);
            continue;
        }
        if compare(&value, &heap[0]) == Ordering::Less {
            heap[0] = value;
            sift_worst_down_by(&mut heap, 0, &compare);
        }
    }
    token.check()?;
    cancellable_sort_by(&mut heap, token, compare)?;
    Ok(heap)
}

fn sift_worst_up_by<T, F>(heap: &mut [T], mut index: usize, compare: &F)
where
    F: Fn(&T, &T) -> Ordering,
{
    while index > 0 {
        let parent = (index - 1) / 2;
        if compare(&heap[parent], &heap[index]) != Ordering::Less {
            break;
        }
        heap.swap(parent, index);
        index = parent;
    }
}

fn sift_worst_down_by<T, F>(heap: &mut [T], mut index: usize, compare: &F)
where
    F: Fn(&T, &T) -> Ordering,
{
    loop {
        let left = index.saturating_mul(2).saturating_add(1);
        if left >= heap.len() {
            break;
        }
        let right = left + 1;
        let worse_child =
            if right < heap.len() && compare(&heap[left], &heap[right]) == Ordering::Less {
                right
            } else {
                left
            };
        if compare(&heap[index], &heap[worse_child]) != Ordering::Less {
            break;
        }
        heap.swap(index, worse_child);
        index = worse_child;
    }
}

/// Merge-sorts in bounded chunks so cancellation never waits behind an unbounded monolithic
/// `slice::sort` call on very large exact result sets.
fn cancellable_sort_by<T, F>(
    values: &mut Vec<T>,
    token: &CancellationToken,
    compare: F,
) -> Result<()>
where
    T: Clone,
    F: Fn(&T, &T) -> Ordering,
{
    const SORT_BATCH: usize = 1_024;
    for chunk in values.chunks_mut(SORT_BATCH) {
        token.check()?;
        chunk.sort_unstable_by(&compare);
        token.check()?;
    }
    if values.len() <= SORT_BATCH {
        return Ok(());
    }

    let length = values.len();
    let mut width = SORT_BATCH;
    let mut source = std::mem::take(values);
    let mut destination = Vec::with_capacity(length);
    while width < length {
        destination.clear();
        let mut start = 0usize;
        let mut emitted_since_check = 0usize;
        while start < length {
            token.check()?;
            let middle = start.saturating_add(width).min(length);
            let end = middle.saturating_add(width).min(length);
            let mut left = start;
            let mut right = middle;
            while left < middle && right < end {
                if emitted_since_check == 256 {
                    token.check()?;
                    emitted_since_check = 0;
                }
                if compare(&source[left], &source[right]) != Ordering::Greater {
                    destination.push(source[left].clone());
                    left += 1;
                } else {
                    destination.push(source[right].clone());
                    right += 1;
                }
                emitted_since_check += 1;
            }
            while left < middle {
                if emitted_since_check == 256 {
                    token.check()?;
                    emitted_since_check = 0;
                }
                destination.push(source[left].clone());
                left += 1;
                emitted_since_check += 1;
            }
            while right < end {
                if emitted_since_check == 256 {
                    token.check()?;
                    emitted_since_check = 0;
                }
                destination.push(source[right].clone());
                right += 1;
                emitted_since_check += 1;
            }
            start = end;
        }
        std::mem::swap(&mut source, &mut destination);
        width = width.saturating_mul(2);
    }
    token.check()?;
    *values = source;
    Ok(())
}

fn ids_for_extension_group(cache: &CatalogCache, extensions: &[String]) -> RoaringBitmap {
    let mut allowed = RoaringBitmap::new();
    for extension in extensions {
        if extension.contains('.') {
            let suffix = format!(".{extension}");
            for id in cache.all_ids() {
                if let Some(item) = cache.get_cached(id)
                    && item.normalized_name.ends_with(&suffix)
                {
                    allowed.insert(id);
                }
            }
        } else {
            allowed |= cache.ids_for_extension(extension);
        }
    }
    allowed
}

fn search_scope_mask(
    cache: &CatalogCache,
    enabled_root_ids: &HashSet<Uuid>,
    root_ids: &[Uuid],
    kinds: &[ItemKind],
    filter_resolution: &FilterResolution,
) -> Option<RoaringBitmap> {
    let mut enabled = RoaringBitmap::new();
    for root_id in enabled_root_ids {
        enabled |= cache.ids_for_root(*root_id);
    }
    let mut scope = Some(enabled);
    if !root_ids.is_empty() {
        let mut allowed = RoaringBitmap::new();
        for root_id in root_ids {
            allowed |= cache.ids_for_root(*root_id);
        }
        intersect_scope(&mut scope, allowed);
    }
    if !kinds.is_empty() {
        let mut allowed = RoaringBitmap::new();
        for kind in kinds {
            allowed |= cache.ids_for_kind(*kind);
        }
        intersect_scope(&mut scope, allowed);
    }
    if let Some(kinds) = &filter_resolution.allowed_kinds {
        let mut allowed = RoaringBitmap::new();
        for kind in kinds {
            allowed |= cache.ids_for_kind(*kind);
        }
        intersect_scope(&mut scope, allowed);
    }
    for paths in &filter_resolution.path_groups {
        let mut allowed = RoaringBitmap::new();
        for path in paths {
            allowed |= cache.ids_under_path(Path::new(path));
        }
        intersect_scope(&mut scope, allowed);
    }
    for extensions in &filter_resolution.extension_groups {
        intersect_scope(&mut scope, ids_for_extension_group(cache, extensions));
    }
    scope
}

fn workspace_provider_shadowed_ids(
    cache: &CatalogCache,
    candidates: &RoaringBitmap,
    active_provider_roots: &HashSet<Uuid>,
) -> RoaringBitmap {
    if active_provider_roots.is_empty() || candidates.is_empty() {
        return RoaringBitmap::new();
    }
    let mut provider_ids = RoaringBitmap::new();
    for root_id in active_provider_roots {
        provider_ids |= cache.ids_for_root(*root_id);
    }
    provider_ids &= candidates;

    let mut shadowed = RoaringBitmap::new();
    for provider_id in provider_ids {
        let Some(provider) = cache.get_cached(provider_id) else {
            continue;
        };
        let Some(launch_path) = provider
            .preview_hint
            .as_deref()
            .and_then(workspace_provider_launch_path)
        else {
            continue;
        };
        let Some(raw_id) = cache.id_for_path(&launch_path) else {
            continue;
        };
        if raw_id != provider_id && candidates.contains(raw_id) {
            shadowed.insert(raw_id);
        }
    }
    shadowed
}

fn workspace_provider_item_equivalent(existing: &ItemRecord, next: &ItemRecord) -> bool {
    item_record_equivalent(existing, next)
}

fn item_record_equivalent(existing: &ItemRecord, next: &ItemRecord) -> bool {
    existing.id == next.id
        && existing.root_id == next.root_id
        && existing.path == next.path
        && existing.kind == next.kind
        && existing.size == next.size
        && existing.modified_unix_ms == next.modified_unix_ms
        && existing.available == next.available
        && existing.priority == next.priority
        && existing.stable_file_id == next.stable_file_id
        && existing.preview_hint == next.preview_hint
}

fn intersect_scope(scope: &mut Option<RoaringBitmap>, allowed: RoaringBitmap) {
    *scope = Some(match scope.take() {
        Some(current) => current & allowed,
        None => allowed,
    });
}

fn elapsed_micros(started: Instant) -> u64 {
    started.elapsed().as_micros().min(u64::MAX as u128) as u64
}

fn path_is_under_any(path: &Path, prefixes: &[PathBuf]) -> bool {
    prefixes.iter().any(|prefix| path.starts_with(prefix))
}

fn allocate_next_item_id(next: &mut ItemId) -> Result<ItemId> {
    let id = (*next).max(1);
    *next = id.checked_add(1).ok_or_else(|| {
        CoreError::Index(
            "catalog item ID space is exhausted; rebuild or migrate the profile".into(),
        )
    })?;
    Ok(id)
}

/// Reserve unchanged identities first, then unambiguous moves, then residual
/// hardlink occurrences, then legacy path matches. A path can be reused by a
/// different file within the same watcher batch, and hardlinks make stable IDs
/// non-unique in either snapshot. Once exact-path claims remove all but one old
/// and one new occurrence for a shared stable ID, that remaining pair is no
/// longer ambiguous and may keep the old item ID.
fn assign_snapshot_item_ids(
    existing: &HashMap<String, ItemRecord>,
    discovered: &[DiscoveredItem],
    preserved_paths: &[PathBuf],
    next_id: &mut ItemId,
) -> Result<Vec<ItemId>> {
    let paths = discovered
        .iter()
        .map(|item| item.path.to_string_lossy().into_owned())
        .collect::<Vec<_>>();
    let seen = paths.iter().map(String::as_str).collect::<HashSet<_>>();
    let mut claimed = existing
        .values()
        .filter(|item| {
            !seen.contains(item.path.as_str())
                && path_is_under_any(Path::new(&item.path), preserved_paths)
        })
        .map(|item| item.id)
        .collect::<HashSet<_>>();
    let mut assigned = vec![None; discovered.len()];
    let mut stable_counts = HashMap::new();
    for item in discovered {
        if let Some(stable) = item.stable_file_id.as_deref() {
            *stable_counts.entry(stable).or_insert(0usize) += 1;
        }
    }
    for (index, item) in discovered.iter().enumerate() {
        if let Some(old) = existing.get(&paths[index])
            && item.stable_file_id.is_some()
            && old.stable_file_id == item.stable_file_id
            && claimed.insert(old.id)
        {
            assigned[index] = Some(old.id);
        }
    }
    let unique_old = unique_items_by_stable_id(existing.values());
    for (index, item) in discovered.iter().enumerate() {
        if assigned[index].is_none()
            && let Some(stable) = item.stable_file_id.as_deref()
            && stable_counts.get(stable) == Some(&1)
            && let Some(old) = unique_old.get(stable)
            && claimed.insert(old.id)
        {
            assigned[index] = Some(old.id);
        }
    }
    let mut remaining_old_by_stable = HashMap::<String, Vec<ItemId>>::new();
    for old in existing.values() {
        if claimed.contains(&old.id) {
            continue;
        }
        if let Some(stable) = old.stable_file_id.as_deref() {
            remaining_old_by_stable
                .entry(stable.to_owned())
                .or_default()
                .push(old.id);
        }
    }
    let mut remaining_new_by_stable = HashMap::<String, Vec<usize>>::new();
    for (index, item) in discovered.iter().enumerate() {
        if assigned[index].is_some() {
            continue;
        }
        if let Some(stable) = item.stable_file_id.as_deref() {
            remaining_new_by_stable
                .entry(stable.to_owned())
                .or_default()
                .push(index);
        }
    }
    for (stable, indexes) in remaining_new_by_stable {
        if indexes.len() != 1 {
            continue;
        }
        let Some(old_ids) = remaining_old_by_stable.get(&stable) else {
            continue;
        };
        if old_ids.len() != 1 {
            continue;
        }
        let old_id = old_ids[0];
        if claimed.insert(old_id) {
            assigned[indexes[0]] = Some(old_id);
        }
    }
    for (index, item) in discovered.iter().enumerate() {
        if assigned[index].is_none()
            && let Some(old) = existing.get(&paths[index])
            && (old.stable_file_id.is_none() || item.stable_file_id.is_none())
            && claimed.insert(old.id)
        {
            assigned[index] = Some(old.id);
        }
    }
    assigned
        .into_iter()
        .map(|id| match id {
            Some(id) => Ok(id),
            None => allocate_next_item_id(next_id),
        })
        .collect()
}

/// Delete displaced item IDs AND old path mappings before inserting the new
/// snapshot. Path-only deletion checks leave replaced IDs and moved paths alive.
fn snapshot_removed_items(
    existing: &HashMap<String, ItemRecord>,
    items: &[ItemRecord],
    preserved_paths: &[PathBuf],
) -> Vec<ItemRecord> {
    let by_path = items
        .iter()
        .map(|item| (item.path.as_str(), item.id))
        .collect::<HashMap<_, _>>();
    existing
        .values()
        .filter(|old| match by_path.get(old.path.as_str()) {
            Some(id) => *id != old.id,
            None => !path_is_under_any(Path::new(&old.path), preserved_paths),
        })
        .cloned()
        .collect()
}

fn unique_items_by_stable_id<'a>(
    items: impl IntoIterator<Item = &'a ItemRecord>,
) -> HashMap<String, ItemRecord> {
    let mut unique = HashMap::new();
    let mut duplicates = HashSet::new();
    for item in items {
        let Some(stable_id) = item.stable_file_id.as_ref() else {
            continue;
        };
        if duplicates.contains(stable_id) {
            continue;
        }
        if unique.insert(stable_id.clone(), item.clone()).is_some() {
            unique.remove(stable_id);
            duplicates.insert(stable_id.clone());
        }
    }
    unique
}

fn minimize_changed_paths(root: &Path, paths: Vec<PathBuf>) -> Result<Vec<PathBuf>> {
    validate_absolute_lexical_path(root, "root")?;
    let mut normalized = Vec::new();
    for path in paths {
        validate_absolute_lexical_path(&path, "changed path")?;
        if !path.starts_with(root) {
            return Err(CoreError::InvalidRequest(format!(
                "changed path is outside root: {}",
                path.display()
            )));
        }
        validate_existing_ancestor_containment(root, &path)?;
        let path = if let Some(bundle) = outermost_app_bundle(root, &path) {
            // Validate the original event first: promotion must not hide an
            // escaping symlink or lexical traversal. Validate the promoted
            // boundary too, because a linked bundle may point outside the root
            // while one of its children links back inside it.
            if bundle != path {
                validate_existing_ancestor_containment(root, bundle)?;
            }
            bundle.to_path_buf()
        } else {
            path
        };
        if !normalized
            .iter()
            .any(|parent: &PathBuf| path.starts_with(parent))
        {
            normalized.retain(|candidate: &PathBuf| !candidate.starts_with(&path));
            normalized.push(path);
        }
    }
    normalized.sort();
    Ok(normalized)
}

fn validate_absolute_lexical_path(path: &Path, label: &str) -> Result<()> {
    let rendered = path.to_string_lossy();
    let portable_absolute = path.is_absolute()
        || rendered.as_bytes().get(1).is_some_and(|byte| *byte == b':')
            && rendered
                .as_bytes()
                .get(2)
                .is_some_and(|byte| matches!(*byte, b'/' | b'\\'))
        || rendered.starts_with('/')
        || rendered.starts_with("\\\\");
    if !portable_absolute {
        return Err(CoreError::InvalidRequest(format!(
            "{label} must be absolute"
        )));
    }
    if rendered
        .split(['/', '\\'])
        .any(|component| matches!(component, "." | ".."))
    {
        return Err(CoreError::InvalidRequest(format!(
            "{label} must not contain dot traversal components"
        )));
    }
    Ok(())
}

fn validate_existing_ancestor_containment(root: &Path, path: &Path) -> Result<()> {
    if !root.exists() {
        return Ok(());
    }
    let canonical_root = fs::canonicalize(root).map_err(|source| CoreError::Filesystem {
        path: root.to_path_buf(),
        source,
    })?;
    let mut existing = path;
    while !existing.exists() {
        let Some(parent) = existing.parent() else {
            return Ok(());
        };
        existing = parent;
    }
    let canonical_existing =
        fs::canonicalize(existing).map_err(|source| CoreError::Filesystem {
            path: existing.to_path_buf(),
            source,
        })?;
    if !canonical_existing.starts_with(&canonical_root) {
        return Err(CoreError::InvalidRequest(
            "changed path resolves outside its configured root".into(),
        ));
    }
    Ok(())
}

fn comparable_path(path: &Path) -> String {
    crate::text::comparable_path(&path.to_string_lossy())
}

fn comparable_paths_overlap(left: &str, right: &str) -> bool {
    comparable_path_is_same_or_descendant(left, right)
        || comparable_path_is_same_or_descendant(right, left)
}

fn comparable_path_is_same_or_descendant(candidate: &str, prefix: &str) -> bool {
    candidate == prefix
        || candidate
            .strip_prefix(prefix)
            .is_some_and(|rest| prefix.ends_with('/') || rest.starts_with('/'))
}

pub(crate) fn validate_root(root: &RootRecord) -> Result<()> {
    validate_absolute_lexical_path(&root.path, "root path")?;
    match root.refresh_policy {
        RefreshPolicy::Interval { seconds: 0 } => Err(CoreError::InvalidRequest(
            "interval refresh policy requires seconds > 0".into(),
        )),
        RefreshPolicy::Daily { hour, minute } if hour >= 24 || minute >= 60 => Err(
            CoreError::InvalidRequest("daily refresh policy has invalid time".into()),
        ),
        RefreshPolicy::Weekly {
            weekday_from_monday,
            hour,
            minute,
        } if weekday_from_monday >= 7 || hour >= 24 || minute >= 60 => Err(
            CoreError::InvalidRequest("weekly refresh policy has invalid day/time".into()),
        ),
        _ => Ok(()),
    }
}

impl Drop for Core {
    fn drop(&mut self) {
        let checkpoint = {
            let _catalog_guard = self.catalog_mutation_guard.lock();
            self.store
                .flush()
                .and_then(|_| self.persist_cache_snapshot_while_catalog_guarded())
        };
        match checkpoint {
            Ok(()) => self.log.event("core_close", "flush_complete"),
            Err(error) => self
                .log
                .event("core_close_flush_failed", &error.to_string()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn open_test_core(state: &Path) -> Core {
        Core::open(CoreConfig::new(state.to_path_buf())).expect("test core opens")
    }

    fn search_names(core: &Core, query: &str) -> Vec<String> {
        core.search(
            &SearchRequest {
                query: query.to_owned(),
                limit: 50,
                ..SearchRequest::default()
            },
            &CancellationToken::new(),
        )
        .expect("test search succeeds")
        .results
        .into_iter()
        .map(|result| result.item.name)
        .collect()
    }

    #[test]
    fn lexical_path_validation_is_cross_platform_and_rejects_traversal() {
        assert!(validate_absolute_lexical_path(Path::new("/Volumes/Work"), "path").is_ok());
        assert!(validate_absolute_lexical_path(Path::new("X:/Work"), "path").is_ok());
        assert!(validate_absolute_lexical_path(Path::new("\\\\server\\share"), "path").is_ok());
        assert!(validate_absolute_lexical_path(Path::new("relative/path"), "path").is_err());
        assert!(validate_absolute_lexical_path(Path::new("X:/Work/../escape"), "path").is_err());
        assert!(validate_absolute_lexical_path(Path::new("/Volumes/./Work"), "path").is_err());
    }

    #[test]
    fn item_id_allocation_never_wraps_or_reuses_the_terminal_id() {
        let mut next = 1;
        assert_eq!(allocate_next_item_id(&mut next).unwrap(), 1);
        assert_eq!(next, 2);

        let mut exhausted = ItemId::MAX;
        assert!(matches!(
            allocate_next_item_id(&mut exhausted),
            Err(CoreError::Index(_))
        ));
        assert_eq!(exhausted, ItemId::MAX);
    }

    #[test]
    fn partial_file_search_order_matches_full_order_and_honors_precancel() {
        use std::cell::Cell;

        let values = (0u32..4_096).rev().collect::<Vec<_>>();
        let mut fully_sorted = values.clone();
        fully_sorted.sort_unstable();
        for prefix_len in [1usize, 17, 100, 1_023, 1_024, 2_048] {
            let prefix = cancellable_top_prefix_by(
                values.clone(),
                prefix_len,
                &CancellationToken::new(),
                Ord::cmp,
            )
            .unwrap();
            assert_eq!(prefix, fully_sorted[..prefix_len]);
        }

        let cancelled = CancellationToken::new();
        cancelled.cancel();
        assert!(matches!(
            cancellable_top_prefix_by(values.clone(), 100, &cancelled, Ord::cmp),
            Err(CoreError::Cancelled)
        ));

        let during_scan = CancellationToken::new();
        let cancel_from_compare = during_scan.clone();
        let comparisons = Cell::new(0usize);
        let result = cancellable_top_prefix_by(values, 100, &during_scan, |a, b| {
            let current = comparisons.get().saturating_add(1);
            comparisons.set(current);
            if current == 500 {
                cancel_from_compare.cancel();
            }
            a.cmp(b)
        });
        assert!(matches!(result, Err(CoreError::Cancelled)));
        assert!(comparisons.get() >= 500);
    }

    #[test]
    fn stale_full_reconcile_commit_cannot_overwrite_newer_catalog_mutation() {
        let state = TempDir::new().unwrap();
        let core = open_test_core(state.path());
        let root = core
            .add_root(PathBuf::from("X:/catalog-race"), RootKind::Local)
            .unwrap();
        let expected_generation = core.store.catalog_generation().unwrap();

        core.upsert_items(
            root.id,
            vec![DiscoveredItem::new(
                PathBuf::from("X:/catalog-race/newer.mov"),
                ItemKind::File,
                10,
                20,
            )],
        )
        .unwrap();

        let error = core
            .commit_reconciled_root(
                root.id,
                vec![DiscoveredItem::new(
                    PathBuf::from("X:/catalog-race/stale.mov"),
                    ItemKind::File,
                    1,
                    1,
                )],
                &CrawlReport::default(),
                expected_generation,
                &CancellationToken::new(),
            )
            .unwrap_err();
        assert!(matches!(error, CoreError::StaleCatalog { .. }));
        assert_eq!(search_names(&core, "newer"), vec!["newer.mov"]);
        assert!(search_names(&core, "stale").is_empty());
    }

    #[test]
    fn cancelled_full_reconcile_commit_rechecks_after_catalog_lock() {
        let state = TempDir::new().unwrap();
        let core = open_test_core(state.path());
        let root = core
            .add_root(PathBuf::from("X:/cancelled-commit"), RootKind::Local)
            .unwrap();
        let generation = core.store.catalog_generation().unwrap();
        let token = CancellationToken::new();
        token.cancel();

        let error = core
            .commit_reconciled_root(
                root.id,
                vec![DiscoveredItem::new(
                    PathBuf::from("X:/cancelled-commit/new.mov"),
                    ItemKind::File,
                    1,
                    1,
                )],
                &CrawlReport::default(),
                generation,
                &token,
            )
            .unwrap_err();

        assert!(matches!(error, CoreError::Cancelled));
        assert_eq!(core.store.catalog_generation().unwrap(), generation);
        assert!(search_names(&core, "new").is_empty());
    }

    #[test]
    fn identical_put_root_is_a_catalog_no_op() {
        let state = TempDir::new().unwrap();
        let core = open_test_core(state.path());
        let root = core
            .add_root(PathBuf::from("X:/root-no-op"), RootKind::Local)
            .unwrap();
        let generation = core.store.catalog_generation().unwrap();

        core.put_root(root).unwrap();

        assert_eq!(core.store.catalog_generation().unwrap(), generation);
    }

    #[test]
    fn dirty_path_batch_is_bounded_before_catalog_work() {
        let state = TempDir::new().unwrap();
        let core = open_test_core(state.path());
        let root = core
            .add_root(PathBuf::from("X:/bounded-dirty-paths"), RootKind::Local)
            .unwrap();
        let generation = core.store.catalog_generation().unwrap();
        let paths = (0..=MAX_RECONCILE_PATHS)
            .map(|index| PathBuf::from(format!("X:/bounded-dirty-paths/{index}.txt")))
            .collect::<Vec<_>>();

        let error = core
            .reconcile_paths(root.id, paths, &CancellationToken::new())
            .unwrap_err();

        assert!(matches!(error, CoreError::InvalidRequest(_)));
        assert_eq!(core.store.catalog_generation().unwrap(), generation);
    }

    #[test]
    fn root_only_health_mutations_advance_catalog_generation() {
        let state = TempDir::new().unwrap();
        let core = open_test_core(state.path());
        let root = core
            .add_root(PathBuf::from("X:/empty-health-root"), RootKind::Network)
            .unwrap();
        let added_generation = core.store.catalog_generation().unwrap();

        core.set_root_unavailable(root.id, "offline".into())
            .unwrap();
        let unavailable_generation = core.store.catalog_generation().unwrap();
        assert!(unavailable_generation > added_generation);

        core.mark_root_available(root.id).unwrap();
        assert!(core.store.catalog_generation().unwrap() > unavailable_generation);
    }

    #[test]
    fn snapshot_identity_assignment_is_order_independent_and_preserves_hardlinks() {
        let root_id = Uuid::new_v4();
        let base = TempDir::new().unwrap();
        let old = |name: &str, id, stable: Option<&str>| {
            let mut item = ItemRecord::from_path_metadata(
                id,
                root_id,
                &base.path().join(name),
                ItemKind::File,
                1,
                1,
                Priority::Normal,
            );
            item.stable_file_id = stable.map(str::to_owned);
            (item.path.clone(), item)
        };
        let new = |name: &str, stable: Option<&str>| {
            let mut item = DiscoveredItem::new(base.path().join(name), ItemKind::File, 1, 1);
            item.stable_file_id = stable.map(str::to_owned);
            item
        };
        let initial = HashMap::from([old("a", 1, Some("A")), old("b", 2, Some("B"))]);
        for mut discovered in [
            vec![new("c", Some("A")), new("a", Some("B"))],
            vec![new("b", Some("A")), new("a", Some("B"))],
        ] {
            for _ in 0..2 {
                let ids = assign_snapshot_item_ids(&initial, &discovered, &[], &mut 10).unwrap();
                for (item, id) in discovered.iter().zip(ids) {
                    assert_eq!(
                        id,
                        if item.stable_file_id.as_deref() == Some("A") {
                            1
                        } else {
                            2
                        }
                    );
                }
                discovered.reverse();
            }
        }
        let discovered = vec![new("c", Some("shared")), new("a", Some("shared"))];
        // A new link cannot inherit the sole old occurrence already claimed
        // by unchanged path 'a'.
        let one_old = HashMap::from([old("a", 1, Some("shared"))]);
        assert_eq!(
            assign_snapshot_item_ids(&one_old, &discovered, &[], &mut 10).unwrap(),
            vec![10, 1]
        );
        // Once exact 'a' claims 1, the sole remaining old 'b' (2) and new
        // 'c' form an unambiguous residual moved-occurrence pair. The current
        // documented identity contract reuses 2 instead of consuming 10.
        let two_old = HashMap::from([old("a", 1, Some("shared")), old("b", 2, Some("shared"))]);
        assert_eq!(
            assign_snapshot_item_ids(&two_old, &discovered, &[], &mut 10).unwrap(),
            vec![2, 1]
        );
    }

    #[test]
    fn snapshot_identity_assignment_reserves_unreadable_rows_and_known_moves() {
        let root_id = Uuid::new_v4();
        let base = TempDir::new().unwrap();
        let locked = base.path().join("locked");
        let mut old = ItemRecord::from_path_metadata(
            1,
            root_id,
            &locked.join("a"),
            ItemKind::File,
            1,
            1,
            Priority::Normal,
        );
        old.stable_file_id = Some("A".into());
        let mut moved = DiscoveredItem::new(base.path().join("moved"), ItemKind::File, 1, 1);
        moved.stable_file_id = Some("A".into());
        let legacy = DiscoveredItem::new(PathBuf::from(&old.path), ItemKind::File, 1, 1);
        let initial = HashMap::from([(old.path.clone(), old)]);
        assert_eq!(
            assign_snapshot_item_ids(&initial, &[moved.clone()], &[locked], &mut 10).unwrap(),
            vec![10]
        );
        assert_eq!(
            assign_snapshot_item_ids(&initial, &[legacy, moved], &[], &mut 10).unwrap(),
            vec![10, 1]
        );
    }

    #[test]
    fn partial_snapshot_commit_preserves_only_failed_subtrees() {
        let state = TempDir::new().unwrap();
        let core = open_test_core(state.path());
        let root = core
            .add_root(PathBuf::from("X:/partial"), RootKind::Local)
            .unwrap();
        core.replace_root_snapshot(
            root.id,
            vec![
                DiscoveredItem::new(
                    PathBuf::from("X:/partial/locked/known.mov"),
                    ItemKind::File,
                    10,
                    10,
                ),
                DiscoveredItem::new(
                    PathBuf::from("X:/partial/remove-me.mov"),
                    ItemKind::File,
                    10,
                    10,
                ),
            ],
        )
        .unwrap();

        let _catalog_guard = core.catalog_mutation_guard.lock();
        core.replace_root_snapshot_while_guarded(
            root.id,
            vec![DiscoveredItem::new(
                PathBuf::from("X:/partial/fresh.mov"),
                ItemKind::File,
                20,
                20,
            )],
            None,
            &[PathBuf::from("X:/partial/locked")],
        )
        .unwrap();
        drop(_catalog_guard);

        assert_eq!(search_names(&core, "known"), vec!["known.mov"]);
        assert_eq!(search_names(&core, "fresh"), vec!["fresh.mov"]);
        assert!(search_names(&core, "remove-me").is_empty());
    }
}
