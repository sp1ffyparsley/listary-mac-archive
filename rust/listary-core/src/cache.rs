use crate::{
    model::{ItemId, ItemKind, ItemRecord, Priority},
    text::{normalize, tokens, trigram_codes},
    transliteration::pinyin_index_postings,
};
use bincode::Options;
use roaring::RoaringBitmap;
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    fs::{self, File},
    io::{BufReader, BufWriter, Read, Seek, SeekFrom, Write},
    path::Path,
    sync::Arc,
    time::Instant,
};
use uuid::Uuid;

const CACHE_SNAPSHOT_MAGIC: &[u8; 8] = b"LSTCCH06";
const CACHE_SNAPSHOT_SCHEMA: u32 = 6;
const CACHE_SNAPSHOT_HEADER_BYTES: u64 = 48;
const CACHE_SNAPSHOT_PAYLOAD_META_OFFSET: u64 = 36;
const CACHE_SNAPSHOT_DESERIALIZE_BUFFER_BYTES: usize = 256 * 1024;

#[derive(Debug, Clone, Serialize)]
pub struct CacheShapeComponent {
    pub name: &'static str,
    pub entries: usize,
    pub capacity: usize,
    pub persisted_in_snapshot: bool,
    pub inline_lower_bound_bytes: u64,
    pub string_used_bytes: u64,
    pub string_capacity_bytes: u64,
    pub vector_heap_bytes: u64,
    pub roaring_serialized_bytes: u64,
    pub serialized_bytes: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct CacheShapeStats {
    pub item_count: usize,
    pub max_item_id: Option<ItemId>,
    pub id_span: u64,
    pub id_density: f64,
    pub catalog_cache_struct_bytes: usize,
    pub item_record_struct_bytes: usize,
    pub cached_item_struct_bytes: usize,
    pub exact_posting_struct_bytes: usize,
    pub exact_one_count: usize,
    pub exact_many_count: usize,
    pub components: Vec<CacheShapeComponent>,
    pub total_inline_lower_bound_bytes: u64,
    pub total_string_used_bytes: u64,
    pub total_string_capacity_bytes: u64,
    pub total_vector_heap_bytes: u64,
    pub total_roaring_serialized_bytes: u64,
    pub total_serialized_component_bytes: u64,
    pub accounted_proxy_bytes: u64,
}

#[derive(Debug, Default)]
pub(crate) struct CacheSnapshotProfile {
    pub open_micros: u64,
    pub header_micros: u64,
    pub deserialize_micros: u64,
    pub rehydrate_micros: u64,
    pub rehydrate_dense_micros: u64,
    pub rehydrate_ordered_paths_micros: u64,
    pub snapshot_bytes: u64,
    pub payload_bytes: u64,
}

#[derive(Debug, Default)]
struct CacheRehydrateProfile {
    dense_micros: u64,
    ordered_paths_micros: u64,
}

#[derive(Debug)]
pub(crate) enum CacheSnapshotLoad {
    Loaded(Box<CatalogCache>),
    Missing,
    StaleGeneration {
        snapshot_generation: u64,
        expected_generation: u64,
    },
    Invalid,
}

#[derive(Debug, Default)]
pub struct CatalogCache {
    items: Vec<CachedItem>,
    item_positions: Vec<u32>,
    ordered_paths: BTreeMap<Arc<str>, ExactPosting>,
    exact_names: HashMap<u64, ExactPosting>,
    short_prefixes: HashMap<String, RoaringBitmap>,
    name_trigrams: HashMap<u32, RoaringBitmap>,
    path_trigrams: HashMap<u32, RoaringBitmap>,
    char_index: HashMap<char, RoaringBitmap>,
    path_char_index: HashMap<char, RoaringBitmap>,
    extensions: HashMap<String, RoaringBitmap>,
    roots: HashMap<Uuid, RoaringBitmap>,
    kinds: HashMap<ItemKind, RoaringBitmap>,
    priorities: HashMap<Priority, RoaringBitmap>,
    parents: HashMap<String, RoaringBitmap>,
    all: RoaringBitmap,
}

#[derive(Serialize)]
struct CacheSnapshotRef<'a> {
    items: &'a [CachedItem],
    exact_names: &'a HashMap<u64, ExactPosting>,
    short_prefixes: &'a HashMap<String, RoaringBitmap>,
    name_trigrams: &'a HashMap<u32, RoaringBitmap>,
    path_trigrams: &'a HashMap<u32, RoaringBitmap>,
    char_index: &'a HashMap<char, RoaringBitmap>,
    path_char_index: &'a HashMap<char, RoaringBitmap>,
    extensions: &'a HashMap<String, RoaringBitmap>,
    roots: &'a HashMap<Uuid, RoaringBitmap>,
    kinds: &'a HashMap<ItemKind, RoaringBitmap>,
    parents: &'a HashMap<String, RoaringBitmap>,
    all: &'a RoaringBitmap,
}

#[derive(Deserialize)]
struct CacheSnapshotOwned {
    items: Vec<CachedItem>,
    exact_names: HashMap<u64, ExactPosting>,
    short_prefixes: HashMap<String, RoaringBitmap>,
    name_trigrams: HashMap<u32, RoaringBitmap>,
    path_trigrams: HashMap<u32, RoaringBitmap>,
    char_index: HashMap<char, RoaringBitmap>,
    path_char_index: HashMap<char, RoaringBitmap>,
    extensions: HashMap<String, RoaringBitmap>,
    roots: HashMap<Uuid, RoaringBitmap>,
    kinds: HashMap<ItemKind, RoaringBitmap>,
    parents: HashMap<String, RoaringBitmap>,
    all: RoaringBitmap,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
enum ExactPosting {
    One(ItemId),
    Many(RoaringBitmap),
}

impl ExactPosting {
    fn insert(&mut self, id: ItemId) {
        match self {
            Self::One(existing) if *existing == id => {}
            Self::One(existing) => {
                let mut bitmap = RoaringBitmap::new();
                bitmap.insert(*existing);
                bitmap.insert(id);
                *self = Self::Many(bitmap);
            }
            Self::Many(bitmap) => {
                bitmap.insert(id);
            }
        }
    }

    fn remove(&mut self, id: ItemId) -> bool {
        match self {
            Self::One(existing) => *existing == id,
            Self::Many(bitmap) => {
                bitmap.remove(id);
                match bitmap.len() {
                    0 => true,
                    1 => {
                        let remaining = bitmap.iter().next().expect("posting has one id");
                        *self = Self::One(remaining);
                        false
                    }
                    _ => false,
                }
            }
        }
    }

    fn insert_ids(&self, ids: &mut RoaringBitmap) {
        match self {
            Self::One(id) => {
                ids.insert(*id);
            }
            Self::Many(bitmap) => *ids |= bitmap,
        }
    }

    fn for_each_id(&self, mut visit: impl FnMut(ItemId)) {
        match self {
            Self::One(id) => visit(*id),
            Self::Many(bitmap) => {
                for id in bitmap {
                    visit(id);
                }
            }
        }
    }
}

#[derive(Debug, Serialize, Deserialize)]
pub(crate) struct CachedItem {
    pub(crate) id: ItemId,
    pub(crate) root_id: Uuid,
    pub(crate) path: Arc<str>,
    pub(crate) normalized_name: String,
    pub(crate) normalized_path: Arc<str>,
    pub(crate) token_blob: String,
    pub(crate) tie_key: u64,
    pub(crate) kind: ItemKind,
    pub(crate) size: u64,
    pub(crate) modified_unix_ms: i64,
    pub(crate) available: bool,
    pub(crate) priority: crate::model::Priority,
    pub(crate) stable_file_id: Option<String>,
    pub(crate) preview_hint: Option<String>,
}

const MISSING_ITEM_POSITION: u32 = u32::MAX;

impl CachedItem {
    fn from_item(item: ItemRecord) -> Self {
        let normalized_name = normalize(&item.name);
        let normalized_path: Arc<str> = Arc::from(normalize(&item.path));
        let token_blob = tokens(&item.name).join("\0");
        let tie_key = stable_hash(&item.path);
        Self {
            id: item.id,
            root_id: item.root_id,
            path: Arc::from(item.path),
            normalized_name,
            normalized_path,
            token_blob,
            tie_key,
            kind: item.kind,
            size: item.size,
            modified_unix_ms: item.modified_unix_ms,
            available: item.available,
            priority: item.priority,
            stable_file_id: item.stable_file_id,
            preview_hint: item.preview_hint,
        }
    }

    pub(crate) fn name(&self) -> &str {
        path_name(&self.path)
    }

    pub(crate) fn parent_path(&self) -> &str {
        path_parent(&self.path)
    }

    pub(crate) fn normalized_parent_path(&self) -> &str {
        path_parent(&self.normalized_path)
    }

    pub(crate) fn extension(&self) -> &str {
        let Some(dot) = self.normalized_name.rfind('.') else {
            return "";
        };
        if dot == 0 || dot + 1 >= self.normalized_name.len() {
            return "";
        }
        &self.normalized_name[dot + 1..]
    }

    pub(crate) fn token_prefix_matches(&self, value: &str) -> bool {
        self.token_blob
            .split('\0')
            .any(|token| token.starts_with(value))
    }

    pub(crate) fn acronym_starts_with(&self, value: &str) -> bool {
        let mut expected = value.chars();
        for token in self
            .token_blob
            .split('\0')
            .filter(|token| !token.is_empty())
        {
            let Some(target) = expected.next() else {
                return true;
            };
            if !token.starts_with(target) {
                return false;
            }
        }
        expected.next().is_none()
    }

    pub(crate) fn to_item_record(&self) -> ItemRecord {
        let name = self.name().to_owned();
        ItemRecord {
            id: self.id,
            root_id: self.root_id,
            path: self.path.to_string(),
            parent_path: self.parent_path().to_owned(),
            extension: self.extension().to_owned(),
            name,
            kind: self.kind,
            size: self.size,
            modified_unix_ms: self.modified_unix_ms,
            available: self.available,
            priority: self.priority,
            stable_file_id: self.stable_file_id.clone(),
            preview_hint: self.preview_hint.clone(),
        }
    }
}

impl CatalogCache {
    pub(crate) fn load_snapshot(
        path: &Path,
        expected_generation: u64,
        expected_catalog_identity: Uuid,
    ) -> (CacheSnapshotLoad, CacheSnapshotProfile) {
        let mut profile = CacheSnapshotProfile::default();
        let _ = fs::remove_file(path.with_extension("bin.tmp"));
        let open_started = Instant::now();
        let file = match File::open(path) {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                profile.open_micros = elapsed_micros(open_started);
                return (CacheSnapshotLoad::Missing, profile);
            }
            Err(_) => {
                profile.open_micros = elapsed_micros(open_started);
                return (CacheSnapshotLoad::Invalid, profile);
            }
        };
        profile.open_micros = elapsed_micros(open_started);
        profile.snapshot_bytes = file.metadata().map(|metadata| metadata.len()).unwrap_or(0);

        let mut reader = BufReader::new(file);
        let header_started = Instant::now();
        let mut magic = [0u8; CACHE_SNAPSHOT_MAGIC.len()];
        let mut schema = [0u8; std::mem::size_of::<u32>()];
        let mut generation = [0u8; std::mem::size_of::<u64>()];
        let mut catalog_identity = [0u8; 16];
        let mut payload_bytes = [0u8; std::mem::size_of::<u64>()];
        let mut payload_checksum = [0u8; std::mem::size_of::<u32>()];
        if reader.read_exact(&mut magic).is_err()
            || reader.read_exact(&mut schema).is_err()
            || reader.read_exact(&mut generation).is_err()
            || reader.read_exact(&mut catalog_identity).is_err()
            || reader.read_exact(&mut payload_bytes).is_err()
            || reader.read_exact(&mut payload_checksum).is_err()
        {
            profile.header_micros = elapsed_micros(header_started);
            return (CacheSnapshotLoad::Invalid, profile);
        }
        profile.header_micros = elapsed_micros(header_started);
        if &magic != CACHE_SNAPSHOT_MAGIC || u32::from_le_bytes(schema) != CACHE_SNAPSHOT_SCHEMA {
            return (CacheSnapshotLoad::Invalid, profile);
        }
        let snapshot_generation = u64::from_le_bytes(generation);
        let payload_bytes = u64::from_le_bytes(payload_bytes);
        let payload_checksum = u32::from_le_bytes(payload_checksum);
        profile.payload_bytes = payload_bytes;
        if catalog_identity.as_slice() != expected_catalog_identity.as_bytes() {
            return (CacheSnapshotLoad::Invalid, profile);
        }
        if snapshot_generation != expected_generation {
            return (
                CacheSnapshotLoad::StaleGeneration {
                    snapshot_generation,
                    expected_generation,
                },
                profile,
            );
        }
        if profile.snapshot_bytes != CACHE_SNAPSHOT_HEADER_BYTES.saturating_add(payload_bytes) {
            return (CacheSnapshotLoad::Invalid, profile);
        }

        let deserialize_started = Instant::now();
        let checksum_reader = ChecksumReader::new(reader.take(payload_bytes));
        let mut payload_reader =
            BufReader::with_capacity(CACHE_SNAPSHOT_DESERIALIZE_BUFFER_BYTES, checksum_reader);
        let options = bincode::DefaultOptions::new()
            .with_fixint_encoding()
            .allow_trailing_bytes()
            .with_limit(payload_bytes);
        let snapshot: CacheSnapshotOwned = match options.deserialize_from(&mut payload_reader) {
            Ok(snapshot) => snapshot,
            Err(_) => {
                profile.deserialize_micros = elapsed_micros(deserialize_started);
                return (CacheSnapshotLoad::Invalid, profile);
            }
        };
        profile.deserialize_micros = elapsed_micros(deserialize_started);
        let buffered_bytes = payload_reader.buffer().len();
        let checksum_reader = payload_reader.into_inner();
        let (bytes_read, checksum) = checksum_reader.finish();
        if buffered_bytes != 0 || bytes_read != payload_bytes || checksum != payload_checksum {
            return (CacheSnapshotLoad::Invalid, profile);
        }
        let rehydrate_started = Instant::now();
        let Some((cache, rehydrate_profile)) = Self::from_snapshot(snapshot) else {
            profile.rehydrate_micros = elapsed_micros(rehydrate_started);
            return (CacheSnapshotLoad::Invalid, profile);
        };
        profile.rehydrate_dense_micros = rehydrate_profile.dense_micros;
        profile.rehydrate_ordered_paths_micros = rehydrate_profile.ordered_paths_micros;
        profile.rehydrate_micros = elapsed_micros(rehydrate_started);
        (CacheSnapshotLoad::Loaded(Box::new(cache)), profile)
    }

    pub fn save_snapshot(
        &self,
        path: &Path,
        catalog_generation: u64,
        catalog_identity: Uuid,
    ) -> crate::error::Result<()> {
        let parent = path.parent().unwrap_or_else(|| Path::new("."));
        fs::create_dir_all(parent).map_err(|source| crate::error::CoreError::Filesystem {
            path: parent.to_path_buf(),
            source,
        })?;
        let temp = path.with_extension("bin.tmp");
        let file = File::create(&temp).map_err(|source| crate::error::CoreError::Filesystem {
            path: temp.clone(),
            source,
        })?;
        let mut writer = BufWriter::new(file);
        writer
            .write_all(CACHE_SNAPSHOT_MAGIC)
            .and_then(|_| writer.write_all(&CACHE_SNAPSHOT_SCHEMA.to_le_bytes()))
            .and_then(|_| writer.write_all(&catalog_generation.to_le_bytes()))
            .and_then(|_| writer.write_all(catalog_identity.as_bytes()))
            .and_then(|_| writer.write_all(&0u64.to_le_bytes()))
            .and_then(|_| writer.write_all(&0u32.to_le_bytes()))
            .map_err(|source| crate::error::CoreError::Filesystem {
                path: temp.clone(),
                source,
            })?;
        let snapshot = CacheSnapshotRef {
            items: &self.items,
            exact_names: &self.exact_names,
            short_prefixes: &self.short_prefixes,
            name_trigrams: &self.name_trigrams,
            path_trigrams: &self.path_trigrams,
            char_index: &self.char_index,
            path_char_index: &self.path_char_index,
            extensions: &self.extensions,
            roots: &self.roots,
            kinds: &self.kinds,
            parents: &self.parents,
            all: &self.all,
        };
        let (payload_bytes, payload_checksum) = {
            let mut checksum_writer = ChecksumWriter::new(&mut writer);
            bincode::serialize_into(&mut checksum_writer, &snapshot)?;
            checksum_writer.finish()
        };
        writer
            .flush()
            .map_err(|source| crate::error::CoreError::Filesystem {
                path: temp.clone(),
                source,
            })?;
        writer
            .seek(SeekFrom::Start(CACHE_SNAPSHOT_PAYLOAD_META_OFFSET))
            .and_then(|_| writer.write_all(&payload_bytes.to_le_bytes()))
            .and_then(|_| writer.write_all(&payload_checksum.to_le_bytes()))
            .and_then(|_| writer.flush())
            .map_err(|source| crate::error::CoreError::Filesystem {
                path: temp.clone(),
                source,
            })?;
        writer
            .get_ref()
            .sync_all()
            .map_err(|source| crate::error::CoreError::Filesystem {
                path: temp.clone(),
                source,
            })?;
        crate::test_fault::hit("cache_after_temp_write");
        if path.exists() {
            fs::remove_file(path).map_err(|source| crate::error::CoreError::Filesystem {
                path: path.to_path_buf(),
                source,
            })?;
        }
        crate::test_fault::hit("cache_after_target_remove");
        fs::rename(&temp, path).map_err(|source| crate::error::CoreError::Filesystem {
            path: path.to_path_buf(),
            source,
        })?;
        Ok(())
    }

    fn from_snapshot(mut snapshot: CacheSnapshotOwned) -> Option<(Self, CacheRehydrateProfile)> {
        let dense_started = Instant::now();
        snapshot.items.shrink_to_fit();
        let max_id = snapshot.items.iter().map(|item| item.id).max().unwrap_or(0);
        let mut item_positions = vec![MISSING_ITEM_POSITION; max_id as usize + 1];
        let mut priorities: HashMap<Priority, RoaringBitmap> = HashMap::new();
        for (position, item) in snapshot.items.iter().enumerate() {
            let slot = item_positions.get_mut(item.id as usize)?;
            if *slot != MISSING_ITEM_POSITION || position > u32::MAX as usize {
                return None;
            }
            *slot = position as u32;
            if !snapshot.all.contains(item.id) {
                return None;
            }
            priorities.entry(item.priority).or_default().insert(item.id);
        }
        let dense_micros = elapsed_micros(dense_started);

        let ordered_paths_started = Instant::now();
        let ordered_paths = build_ordered_paths(&snapshot.items);
        let ordered_paths_micros = elapsed_micros(ordered_paths_started);
        if snapshot.all.len() != snapshot.items.len() as u64 {
            return None;
        }
        Some((
            Self {
                items: snapshot.items,
                item_positions,
                ordered_paths,
                exact_names: snapshot.exact_names,
                short_prefixes: snapshot.short_prefixes,
                name_trigrams: snapshot.name_trigrams,
                path_trigrams: snapshot.path_trigrams,
                char_index: snapshot.char_index,
                path_char_index: snapshot.path_char_index,
                extensions: snapshot.extensions,
                roots: snapshot.roots,
                kinds: snapshot.kinds,
                priorities,
                parents: snapshot.parents,
                all: snapshot.all,
            },
            CacheRehydrateProfile {
                dense_micros,
                ordered_paths_micros,
            },
        ))
    }

    pub fn len(&self) -> usize {
        self.items.len()
    }

    pub(crate) fn shape_stats(&self) -> CacheShapeStats {
        let mut components = Vec::new();

        let mut item_strings = StringBytes::default();
        for cached in &self.items {
            item_strings.used = item_strings.used.saturating_add(cached.path.len() as u64);
            item_strings.capacity = item_strings
                .capacity
                .saturating_add(cached.path.len() as u64);
            add_string_bytes(&mut item_strings, &cached.normalized_name);
            item_strings.used = item_strings
                .used
                .saturating_add(cached.normalized_path.len() as u64);
            item_strings.capacity = item_strings
                .capacity
                .saturating_add(cached.normalized_path.len() as u64);
            add_string_bytes(&mut item_strings, &cached.token_blob);
            if let Some(value) = cached.stable_file_id.as_ref() {
                add_string_bytes(&mut item_strings, value);
            }
            if let Some(value) = cached.preview_hint.as_ref() {
                add_string_bytes(&mut item_strings, value);
            }
        }
        components.push(CacheShapeComponent {
            name: "items_dense",
            entries: self.items.len(),
            capacity: self.items.capacity(),
            persisted_in_snapshot: true,
            inline_lower_bound_bytes: self
                .items
                .capacity()
                .saturating_mul(std::mem::size_of::<CachedItem>())
                as u64,
            string_used_bytes: item_strings.used,
            string_capacity_bytes: item_strings.capacity,
            vector_heap_bytes: 0,
            roaring_serialized_bytes: 0,
            serialized_bytes: bincode::serialized_size(&self.items).unwrap_or(0),
        });

        components.push(CacheShapeComponent {
            name: "item_positions",
            entries: self.item_positions.len(),
            capacity: self.item_positions.capacity(),
            persisted_in_snapshot: false,
            inline_lower_bound_bytes: self
                .item_positions
                .capacity()
                .saturating_mul(std::mem::size_of::<u32>())
                as u64,
            string_used_bytes: 0,
            string_capacity_bytes: 0,
            vector_heap_bytes: 0,
            roaring_serialized_bytes: 0,
            serialized_bytes: bincode::serialized_size(&self.item_positions).unwrap_or(0),
        });
        components.push(arc_btree_map_component(
            "ordered_paths_shared",
            &self.ordered_paths,
        ));
        components.push(exact_hash_map_component("exact_names", &self.exact_names));
        components.push(bitmap_string_map_component(
            "short_prefixes",
            &self.short_prefixes,
        ));
        components.push(bitmap_plain_map_component(
            "name_trigrams",
            &self.name_trigrams,
        ));
        components.push(bitmap_plain_map_component(
            "path_trigrams",
            &self.path_trigrams,
        ));
        components.push(bitmap_plain_map_component("char_index", &self.char_index));
        components.push(bitmap_plain_map_component(
            "path_char_index",
            &self.path_char_index,
        ));
        components.push(bitmap_string_map_component("extensions", &self.extensions));
        components.push(bitmap_plain_map_component("roots", &self.roots));
        components.push(bitmap_plain_map_component("kinds", &self.kinds));
        let mut priorities = bitmap_plain_map_component("base_priorities", &self.priorities);
        priorities.persisted_in_snapshot = false;
        components.push(priorities);
        components.push(bitmap_string_map_component("parents", &self.parents));
        components.push(CacheShapeComponent {
            name: "all_ids",
            entries: self.all.len() as usize,
            capacity: 0,
            persisted_in_snapshot: true,
            inline_lower_bound_bytes: std::mem::size_of::<RoaringBitmap>() as u64,
            string_used_bytes: 0,
            string_capacity_bytes: 0,
            vector_heap_bytes: 0,
            roaring_serialized_bytes: self.all.serialized_size() as u64,
            serialized_bytes: bincode::serialized_size(&self.all).unwrap_or(0),
        });

        let total_inline_lower_bound_bytes: u64 = components
            .iter()
            .map(|component| component.inline_lower_bound_bytes)
            .sum();
        let total_string_used_bytes: u64 = components
            .iter()
            .map(|component| component.string_used_bytes)
            .sum();
        let total_string_capacity_bytes: u64 = components
            .iter()
            .map(|component| component.string_capacity_bytes)
            .sum();
        let total_vector_heap_bytes: u64 = components
            .iter()
            .map(|component| component.vector_heap_bytes)
            .sum();
        let total_roaring_serialized_bytes: u64 = components
            .iter()
            .map(|component| component.roaring_serialized_bytes)
            .sum();
        let total_serialized_component_bytes: u64 = components
            .iter()
            .filter(|component| component.persisted_in_snapshot)
            .map(|component| component.serialized_bytes)
            .sum();
        let accounted_proxy_bytes = total_inline_lower_bound_bytes
            .saturating_add(total_string_capacity_bytes)
            .saturating_add(total_vector_heap_bytes)
            .saturating_add(total_roaring_serialized_bytes);
        let max_item_id = self.max_id();
        let id_span = max_item_id.map_or(0, |id| id as u64 + 1);
        let id_density = if id_span == 0 {
            1.0
        } else {
            self.items.len() as f64 / id_span as f64
        };
        let exact_one_count = self
            .exact_names
            .values()
            .filter(|posting| matches!(posting, ExactPosting::One(_)))
            .count();
        let exact_many_count = self.exact_names.len().saturating_sub(exact_one_count);

        CacheShapeStats {
            item_count: self.items.len(),
            max_item_id,
            id_span,
            id_density,
            catalog_cache_struct_bytes: std::mem::size_of::<CatalogCache>(),
            item_record_struct_bytes: std::mem::size_of::<ItemRecord>(),
            cached_item_struct_bytes: std::mem::size_of::<CachedItem>(),
            exact_posting_struct_bytes: std::mem::size_of::<ExactPosting>(),
            exact_one_count,
            exact_many_count,
            components,
            total_inline_lower_bound_bytes,
            total_string_used_bytes,
            total_string_capacity_bytes,
            total_vector_heap_bytes,
            total_roaring_serialized_bytes,
            total_serialized_component_bytes,
            accounted_proxy_bytes,
        }
    }

    pub(crate) fn from_items(items: Vec<ItemRecord>) -> Self {
        let count = items.len();
        let max_id = items.iter().map(|item| item.id).max().unwrap_or(0);
        let mut cache = Self {
            items: Vec::with_capacity(count),
            item_positions: vec![MISSING_ITEM_POSITION; max_id as usize + 1],
            ordered_paths: BTreeMap::new(),
            exact_names: HashMap::with_capacity(count),
            short_prefixes: HashMap::new(),
            name_trigrams: HashMap::new(),
            path_trigrams: HashMap::new(),
            char_index: HashMap::new(),
            path_char_index: HashMap::new(),
            extensions: HashMap::new(),
            roots: HashMap::new(),
            kinds: HashMap::new(),
            priorities: HashMap::new(),
            parents: HashMap::new(),
            all: RoaringBitmap::new(),
        };
        for item in items {
            cache.insert(item);
        }
        cache
    }

    pub fn all_ids(&self) -> RoaringBitmap {
        self.all.clone()
    }

    pub(crate) fn max_id(&self) -> Option<ItemId> {
        self.all.max()
    }

    pub fn get(&self, id: ItemId) -> Option<ItemRecord> {
        self.get_cached(id).map(CachedItem::to_item_record)
    }

    pub(crate) fn get_cached(&self, id: ItemId) -> Option<&CachedItem> {
        let position = self.position(id)?;
        self.items.get(position)
    }

    pub fn id_for_path(&self, path: &str) -> Option<ItemId> {
        let identity = identity_path(path);
        let mut candidates = RoaringBitmap::new();
        for normalized in lookup_path_variants(path) {
            if let Some(posting) = self.ordered_paths.get(normalized.as_str()) {
                posting.insert_ids(&mut candidates);
            }
        }
        for id in candidates.iter() {
            if self
                .get_cached(id)
                .is_some_and(|item| identity_path(item.path.as_ref()) == identity)
            {
                return Some(id);
            }
        }
        #[cfg(windows)]
        {
            let requested = fold_case_identity(&identity);
            let mut folded_matches = candidates.iter().filter(|id| {
                self.get_cached(*id).is_some_and(|item| {
                    fold_case_identity(&identity_path(item.path.as_ref())) == requested
                })
            });
            let first = folded_matches.next()?;
            if folded_matches.next().is_none() {
                return Some(first);
            }
        }
        None
    }

    pub fn ids_under_path(&self, path: &Path) -> RoaringBitmap {
        let mut out = RoaringBitmap::new();
        let identity_prefix = identity_path(&path.to_string_lossy());
        for prefix in lookup_path_variants(&path.to_string_lossy()) {
            for (candidate, posting) in self.ordered_paths.range::<str, _>((
                std::ops::Bound::Included(prefix.as_str()),
                std::ops::Bound::Unbounded,
            )) {
                if same_or_descendant(candidate, &prefix) {
                    posting.for_each_id(|id| {
                        let Some(item) = self.get_cached(id) else {
                            return;
                        };
                        let candidate_identity = identity_path(item.path.as_ref());
                        #[cfg(windows)]
                        let matches = same_or_descendant(
                            &fold_case_identity(&candidate_identity),
                            &fold_case_identity(&identity_prefix),
                        );
                        #[cfg(not(windows))]
                        let matches = same_or_descendant(&candidate_identity, &identity_prefix);
                        if matches {
                            out.insert(id);
                        }
                    });
                } else if !candidate.starts_with(&prefix) {
                    break;
                }
            }
        }
        out
    }

    pub fn ids_for_root(&self, root_id: Uuid) -> RoaringBitmap {
        self.roots.get(&root_id).cloned().unwrap_or_default()
    }

    pub fn ids_for_kind(&self, kind: ItemKind) -> RoaringBitmap {
        self.kinds.get(&kind).cloned().unwrap_or_default()
    }

    pub(crate) fn ids_for_base_priority(&self, priority: Priority) -> RoaringBitmap {
        self.priorities.get(&priority).cloned().unwrap_or_default()
    }

    pub fn ids_for_parent(&self, parent_path: &str) -> RoaringBitmap {
        let mut out = RoaringBitmap::new();
        for normalized in lookup_path_variants(parent_path) {
            if let Some(ids) = self.parents.get(&normalized) {
                out |= ids;
            }
        }
        out
    }

    pub fn ids_for_extension(&self, extension: &str) -> RoaringBitmap {
        self.extensions.get(extension).cloned().unwrap_or_default()
    }

    pub fn insert(&mut self, item: ItemRecord) {
        let cached = CachedItem::from_item(item);
        let id = cached.id;
        if let Some(position) = self.position(id) {
            let old = std::mem::replace(&mut self.items[position], cached);
            self.remove_indexes(&old);
            remove_ordered_path(&mut self.ordered_paths, &old.normalized_path, old.id);
            self.add_indexes_for_position(position);
            let cached = &self.items[position];
            insert_ordered_path(
                &mut self.ordered_paths,
                cached.normalized_path.clone(),
                cached.id,
            );
            return;
        }

        let position = self.items.len();
        self.items.push(cached);
        self.ensure_position_slot(id);
        self.item_positions[id as usize] = position as u32;
        self.add_indexes_for_position(position);
        let cached = &self.items[position];
        insert_ordered_path(
            &mut self.ordered_paths,
            cached.normalized_path.clone(),
            cached.id,
        );
    }

    pub fn remove(&mut self, id: ItemId) -> Option<ItemRecord> {
        let position = self.position(id)?;
        let old = self.items.swap_remove(position);
        self.remove_indexes(&old);
        remove_ordered_path(&mut self.ordered_paths, &old.normalized_path, old.id);
        self.item_positions[id as usize] = MISSING_ITEM_POSITION;
        if position < self.items.len() {
            let moved_id = self.items[position].id;
            self.item_positions[moved_id as usize] = position as u32;
        }
        Some(old.to_item_record())
    }

    pub fn root_availability_updates(&self, root_id: Uuid, available: bool) -> Vec<ItemRecord> {
        let ids: Vec<ItemId> = self.ids_for_root(root_id).iter().collect();
        let mut changed = Vec::new();
        for id in ids {
            if let Some(old) = self.get_cached(id)
                && old.available != available
            {
                let mut new = old.to_item_record();
                new.available = available;
                changed.push(new);
            }
        }
        changed
    }

    pub fn candidate_ids(&self, query: &crate::query::ParsedQuery) -> RoaringBitmap {
        let mut candidate: Option<RoaringBitmap> = None;
        for term in &query.positive_terms {
            let ids = if term.path_term {
                self.candidates_for_text(&term.text, true)
            } else {
                self.candidates_for_text(&term.text, false)
            };
            candidate = Some(match candidate {
                None => ids,
                Some(existing) => existing & ids,
            });
        }
        let mut candidate = candidate.unwrap_or_else(|| self.all_ids());

        if !query.include_extensions.is_empty()
            && query
                .include_extensions
                .iter()
                .all(|ext| !ext.contains('.'))
        {
            let mut allowed = RoaringBitmap::new();
            for ext in &query.include_extensions {
                allowed |= self.ids_for_extension(ext);
            }
            candidate &= allowed;
        }
        for ext in query
            .exclude_extensions
            .iter()
            .filter(|ext| !ext.contains('.'))
        {
            candidate -= self.ids_for_extension(ext);
        }
        candidate
    }

    pub fn strong_candidate_ids(&self, query: &crate::query::ParsedQuery) -> RoaringBitmap {
        let mut candidate: Option<RoaringBitmap> = None;
        for term in &query.positive_terms {
            let ids = self.strong_candidates_for_text(&term.text, term.path_term);
            candidate = Some(match candidate {
                None => ids,
                Some(existing) => existing & ids,
            });
        }
        candidate.unwrap_or_default()
    }

    pub fn exact_candidate_ids(&self, query: &crate::query::ParsedQuery) -> RoaringBitmap {
        let mut plain = query.positive_terms.iter().filter(|term| !term.path_term);
        let Some(term) = plain.next() else {
            return RoaringBitmap::new();
        };
        if plain.next().is_some() {
            return RoaringBitmap::new();
        }
        self.exact_name_ids(&term.text)
    }

    fn candidates_for_text(&self, text: &str, path: bool) -> RoaringBitmap {
        let normalized = normalize(text);
        if normalized.is_empty() {
            return self.all_ids();
        }

        let mut out = RoaringBitmap::new();
        if !path {
            out |= self.exact_name_ids(&normalized);
            let prefix_key: String = normalized.chars().take(2).collect();
            if let Some(prefix) = self.short_prefixes.get(&prefix_key) {
                out |= prefix;
            }
        }

        let trigram_index = if path {
            &self.path_trigrams
        } else {
            &self.name_trigrams
        };
        let grams = trigram_codes(&normalized);
        if !grams.is_empty() {
            let mut gram_candidates: Option<RoaringBitmap> = None;
            for gram in grams.iter().take(4) {
                let Some(ids) = trigram_index.get(gram) else {
                    gram_candidates = Some(RoaringBitmap::new());
                    break;
                };
                gram_candidates = Some(match gram_candidates {
                    None => ids.clone(),
                    Some(existing) => existing & ids,
                });
            }
            if let Some(ids) = gram_candidates {
                out |= ids;
            }
        }

        // Fuzzy subsequence fallback: every query character must be present somewhere.
        let char_index = if path {
            &self.path_char_index
        } else {
            &self.char_index
        };
        let mut chars = HashSet::new();
        chars.extend(normalized.chars());
        let mut char_candidates: Option<RoaringBitmap> = None;
        for ch in chars {
            let Some(ids) = char_index.get(&ch) else {
                char_candidates = Some(RoaringBitmap::new());
                break;
            };
            char_candidates = Some(match char_candidates {
                None => ids.clone(),
                Some(existing) => existing & ids,
            });
        }
        if let Some(ids) = char_candidates {
            out |= ids;
        }
        out
    }

    fn strong_candidates_for_text(&self, text: &str, path: bool) -> RoaringBitmap {
        let normalized = normalize(text);
        if normalized.is_empty() {
            return RoaringBitmap::new();
        }
        let mut out = RoaringBitmap::new();
        if !path {
            out |= self.exact_name_ids(&normalized);
            if normalized.chars().count() <= 2
                && let Some(prefix_ids) = self.short_prefixes.get(&normalized)
            {
                for id in prefix_ids {
                    if self
                        .get_cached(id)
                        .is_some_and(|item| item.normalized_name.starts_with(&normalized))
                    {
                        out.insert(id);
                    }
                }
            }
        }
        let trigram_index = if path {
            &self.path_trigrams
        } else {
            &self.name_trigrams
        };
        let grams = trigram_codes(&normalized);
        if grams.is_empty() {
            return out;
        }
        let mut gram_candidates: Option<RoaringBitmap> = None;
        for gram in &grams {
            let Some(ids) = trigram_index.get(gram) else {
                return out;
            };
            gram_candidates = Some(match gram_candidates {
                None => ids.clone(),
                Some(existing) => existing & ids,
            });
        }
        if let Some(ids) = gram_candidates {
            out |= ids;
        }
        out
    }

    fn exact_name_ids(&self, normalized_name: &str) -> RoaringBitmap {
        let Some(posting) = self.exact_names.get(&stable_hash(normalized_name)) else {
            return RoaringBitmap::new();
        };
        let mut verified = RoaringBitmap::new();
        match posting {
            ExactPosting::One(id) => {
                if self
                    .get_cached(*id)
                    .is_some_and(|item| item.normalized_name == normalized_name)
                {
                    verified.insert(*id);
                }
            }
            ExactPosting::Many(ids) => {
                for id in ids {
                    if self
                        .get_cached(id)
                        .is_some_and(|item| item.normalized_name == normalized_name)
                    {
                        verified.insert(id);
                    }
                }
            }
        }
        verified
    }

    fn position(&self, id: ItemId) -> Option<usize> {
        let position = *self.item_positions.get(id as usize)?;
        (position != MISSING_ITEM_POSITION).then_some(position as usize)
    }

    fn ensure_position_slot(&mut self, id: ItemId) {
        let required = id as usize + 1;
        if self.item_positions.len() < required {
            self.item_positions.resize(required, MISSING_ITEM_POSITION);
        }
    }

    fn add_indexes_for_position(&mut self, position: usize) {
        let Self {
            items,
            exact_names,
            short_prefixes,
            name_trigrams,
            path_trigrams,
            char_index,
            path_char_index,
            extensions,
            roots,
            kinds,
            priorities,
            parents,
            all,
            ..
        } = self;
        let item = &items[position];
        let id = item.id;
        all.insert(id);
        roots.entry(item.root_id).or_default().insert(id);
        kinds.entry(item.kind).or_default().insert(id);
        priorities.entry(item.priority).or_default().insert(id);
        parents
            .entry(item.normalized_parent_path().to_owned())
            .or_default()
            .insert(id);
        let extension = item.extension();
        if !extension.is_empty() {
            extensions
                .entry(extension.to_owned())
                .or_default()
                .insert(id);
        }

        exact_names
            .entry(stable_hash(&item.normalized_name))
            .and_modify(|posting| posting.insert(id))
            .or_insert(ExactPosting::One(id));
        for len in 1..=2 {
            let key: String = item.normalized_name.chars().take(len).collect();
            if !key.is_empty() {
                short_prefixes.entry(key).or_default().insert(id);
            }
        }
        for gram in trigram_codes(&item.normalized_name) {
            name_trigrams.entry(gram).or_default().insert(id);
        }
        let mut seen = HashSet::new();
        for ch in item.normalized_name.chars() {
            if seen.insert(ch) {
                char_index.entry(ch).or_default().insert(id);
            }
        }
        let pinyin_name = pinyin_index_postings(&item.normalized_name, true);
        for key in pinyin_name.short_prefixes {
            short_prefixes.entry(key).or_default().insert(id);
        }
        for gram in pinyin_name.trigrams {
            name_trigrams.entry(gram).or_default().insert(id);
        }
        for ch in pinyin_name.chars {
            char_index.entry(ch).or_default().insert(id);
        }

        for gram in trigram_codes(&item.normalized_path) {
            path_trigrams.entry(gram).or_default().insert(id);
        }
        let mut seen_path = HashSet::new();
        for ch in item.normalized_path.chars() {
            if seen_path.insert(ch) {
                path_char_index.entry(ch).or_default().insert(id);
            }
        }
        let pinyin_path = pinyin_index_postings(&item.normalized_path, false);
        for gram in pinyin_path.trigrams {
            path_trigrams.entry(gram).or_default().insert(id);
        }
        for ch in pinyin_path.chars {
            path_char_index.entry(ch).or_default().insert(id);
        }
    }

    fn remove_indexes(&mut self, item: &CachedItem) {
        let id = item.id;
        self.all.remove(id);
        remove_bitmap_entry(&mut self.roots, &item.root_id, id);
        remove_bitmap_entry(&mut self.kinds, &item.kind, id);
        remove_bitmap_entry(&mut self.priorities, &item.priority, id);
        remove_bitmap_entry(
            &mut self.parents,
            &item.normalized_parent_path().to_owned(),
            id,
        );
        let extension = item.extension();
        if !extension.is_empty() {
            remove_bitmap_entry(&mut self.extensions, &extension.to_owned(), id);
        }

        remove_exact_posting(
            &mut self.exact_names,
            &stable_hash(&item.normalized_name),
            id,
        );
        for len in 1..=2 {
            let key: String = item.normalized_name.chars().take(len).collect();
            if !key.is_empty() {
                remove_bitmap_entry(&mut self.short_prefixes, &key, id);
            }
        }
        for gram in trigram_codes(&item.normalized_name) {
            remove_bitmap_entry(&mut self.name_trigrams, &gram, id);
        }
        let mut seen = HashSet::new();
        for ch in item.normalized_name.chars() {
            if seen.insert(ch) {
                remove_bitmap_entry(&mut self.char_index, &ch, id);
            }
        }
        let pinyin_name = pinyin_index_postings(&item.normalized_name, true);
        for key in pinyin_name.short_prefixes {
            remove_bitmap_entry(&mut self.short_prefixes, &key, id);
        }
        for gram in pinyin_name.trigrams {
            remove_bitmap_entry(&mut self.name_trigrams, &gram, id);
        }
        for ch in pinyin_name.chars {
            remove_bitmap_entry(&mut self.char_index, &ch, id);
        }

        for gram in trigram_codes(&item.normalized_path) {
            remove_bitmap_entry(&mut self.path_trigrams, &gram, id);
        }
        let mut seen_path = HashSet::new();
        for ch in item.normalized_path.chars() {
            if seen_path.insert(ch) {
                remove_bitmap_entry(&mut self.path_char_index, &ch, id);
            }
        }
        let pinyin_path = pinyin_index_postings(&item.normalized_path, false);
        for gram in pinyin_path.trigrams {
            remove_bitmap_entry(&mut self.path_trigrams, &gram, id);
        }
        for ch in pinyin_path.chars {
            remove_bitmap_entry(&mut self.path_char_index, &ch, id);
        }
    }
}

fn stable_hash(value: &str) -> u64 {
    let mut hash = 14_695_981_039_346_656_037u64;
    for byte in value.as_bytes() {
        hash ^= *byte as u64;
        hash = hash.wrapping_mul(1_099_511_628_211);
    }
    hash
}

fn elapsed_micros(started: Instant) -> u64 {
    started.elapsed().as_micros().min(u64::MAX as u128) as u64
}

struct ChecksumWriter<W> {
    inner: W,
    hasher: crc32fast::Hasher,
    bytes_written: u64,
}

impl<W> ChecksumWriter<W> {
    fn new(inner: W) -> Self {
        Self {
            inner,
            hasher: crc32fast::Hasher::new(),
            bytes_written: 0,
        }
    }

    fn finish(self) -> (u64, u32) {
        (self.bytes_written, self.hasher.finalize())
    }
}

impl<W: Write> Write for ChecksumWriter<W> {
    fn write(&mut self, buffer: &[u8]) -> std::io::Result<usize> {
        let written = self.inner.write(buffer)?;
        self.hasher.update(&buffer[..written]);
        self.bytes_written = self.bytes_written.saturating_add(written as u64);
        if written > 0 {
            crate::test_fault::hit("cache_during_payload_write");
        }
        Ok(written)
    }

    fn flush(&mut self) -> std::io::Result<()> {
        self.inner.flush()
    }
}

struct ChecksumReader<R> {
    inner: R,
    hasher: crc32fast::Hasher,
    bytes_read: u64,
}

impl<R> ChecksumReader<R> {
    fn new(inner: R) -> Self {
        Self {
            inner,
            hasher: crc32fast::Hasher::new(),
            bytes_read: 0,
        }
    }

    fn finish(self) -> (u64, u32) {
        (self.bytes_read, self.hasher.finalize())
    }
}

impl<R: Read> Read for ChecksumReader<R> {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        let read = self.inner.read(buffer)?;
        self.hasher.update(&buffer[..read]);
        self.bytes_read = self.bytes_read.saturating_add(read as u64);
        Ok(read)
    }
}

fn path_name(path: &str) -> &str {
    path.rsplit_once(['/', '\\'])
        .map(|(_, name)| name)
        .unwrap_or(path)
}

fn path_parent(path: &str) -> &str {
    let Some((parent, _)) = path.rsplit_once(['/', '\\']) else {
        return "";
    };
    if parent.is_empty() {
        return &path[..1];
    }
    if parent.len() == 2 && parent.as_bytes().get(1) == Some(&b':') {
        return &path[..3.min(path.len())];
    }
    parent
}

fn same_or_descendant(candidate: &str, prefix: &str) -> bool {
    if candidate == prefix {
        return true;
    }
    let Some(rest) = candidate.strip_prefix(prefix) else {
        return false;
    };
    rest.starts_with('/') || rest.starts_with('\\')
}

fn identity_path(path: &str) -> String {
    #[cfg(windows)]
    {
        path.replace('\\', "/")
    }
    #[cfg(not(windows))]
    {
        path.to_owned()
    }
}

#[cfg(windows)]
fn fold_case_identity(path: &str) -> String {
    path.chars().flat_map(char::to_lowercase).collect()
}

fn lookup_path_variants(path: &str) -> Vec<String> {
    let normalized = normalize(path);
    #[cfg(not(windows))]
    {
        vec![normalized]
    }
    #[cfg(windows)]
    {
        let mut variants = vec![normalized.clone()];
        let slash = normalized.replace('\\', "/");
        if slash != normalized {
            variants.push(slash);
        }
        let backslash = normalized.replace('/', "\\");
        if backslash != normalized && !variants.contains(&backslash) {
            variants.push(backslash);
        }
        variants
    }
}

fn remove_bitmap_entry<K: std::cmp::Eq + std::hash::Hash + Clone>(
    map: &mut HashMap<K, RoaringBitmap>,
    key: &K,
    id: ItemId,
) {
    let mut empty = false;
    if let Some(bitmap) = map.get_mut(key) {
        bitmap.remove(id);
        empty = bitmap.is_empty();
    }
    if empty {
        map.remove(key);
    }
}

fn remove_exact_posting<K: std::cmp::Eq + std::hash::Hash>(
    map: &mut HashMap<K, ExactPosting>,
    key: &K,
    id: ItemId,
) {
    let remove_key = map.get_mut(key).is_some_and(|posting| posting.remove(id));
    if remove_key {
        map.remove(key);
    }
}

fn insert_ordered_path(map: &mut BTreeMap<Arc<str>, ExactPosting>, path: Arc<str>, id: ItemId) {
    map.entry(path)
        .and_modify(|posting| posting.insert(id))
        .or_insert(ExactPosting::One(id));
}

const PARALLEL_ORDERED_PATH_MIN_ITEMS: usize = 262_144;

fn build_ordered_paths(items: &[CachedItem]) -> BTreeMap<Arc<str>, ExactPosting> {
    let parallel = items.len() >= PARALLEL_ORDERED_PATH_MIN_ITEMS
        && std::thread::available_parallelism().is_ok_and(|count| count.get() >= 2);
    build_ordered_paths_with_parallelism(items, parallel)
}

fn build_ordered_paths_with_parallelism(
    items: &[CachedItem],
    parallel: bool,
) -> BTreeMap<Arc<str>, ExactPosting> {
    if !parallel {
        return build_ordered_paths_from_refs(items.iter());
    }

    // Partition by the normalized path itself so every normalization collision stays in one
    // shard. This lets BTreeMap::append merge two independently built trees without replacing
    // part of an ExactPosting. Only two shards are used to keep startup CPU bounded.
    let mut left_items = Vec::with_capacity(items.len() / 2);
    let mut right_items = Vec::with_capacity(items.len() / 2);
    for item in items {
        if stable_hash(item.normalized_path.as_ref()) & 1 == 0 {
            left_items.push(item);
        } else {
            right_items.push(item);
        }
    }

    std::thread::scope(|scope| {
        let right = std::thread::Builder::new()
            .name("listary-path-rehydrate".into())
            .spawn_scoped(scope, move || build_ordered_paths_from_refs(right_items));
        let Ok(right) = right else {
            return build_ordered_paths_from_refs(items.iter());
        };
        let mut left = build_ordered_paths_from_refs(left_items);
        let Ok(mut right) = right.join() else {
            return build_ordered_paths_from_refs(items.iter());
        };
        left.append(&mut right);
        left
    })
}

fn build_ordered_paths_from_refs<'a>(
    items: impl IntoIterator<Item = &'a CachedItem>,
) -> BTreeMap<Arc<str>, ExactPosting> {
    let mut ordered_paths = BTreeMap::new();
    for item in items {
        insert_ordered_path(&mut ordered_paths, item.normalized_path.clone(), item.id);
    }
    ordered_paths
}

fn remove_ordered_path(map: &mut BTreeMap<Arc<str>, ExactPosting>, path: &Arc<str>, id: ItemId) {
    let remove_entry = map.get_mut(path).is_some_and(|posting| posting.remove(id));
    if remove_entry {
        map.remove(path);
    }
}

#[derive(Default)]
struct StringBytes {
    used: u64,
    capacity: u64,
}

fn add_string_bytes(out: &mut StringBytes, value: &String) {
    out.used = out.used.saturating_add(value.len() as u64);
    out.capacity = out.capacity.saturating_add(value.capacity() as u64);
}

fn hash_map_inline_lower_bound<K, V>(capacity: usize) -> u64 {
    let bucket_bytes = capacity.saturating_mul(std::mem::size_of::<(K, V)>());
    bucket_bytes.saturating_add(capacity) as u64
}

fn string_hash_map_component<V, F>(
    name: &'static str,
    map: &HashMap<String, V>,
    roaring_size: F,
) -> CacheShapeComponent
where
    V: Serialize,
    F: Fn(&V) -> u64,
{
    let mut strings = StringBytes::default();
    let mut roaring = 0u64;
    for (key, value) in map {
        add_string_bytes(&mut strings, key);
        roaring = roaring.saturating_add(roaring_size(value));
    }
    CacheShapeComponent {
        name,
        entries: map.len(),
        capacity: map.capacity(),
        persisted_in_snapshot: true,
        inline_lower_bound_bytes: hash_map_inline_lower_bound::<String, V>(map.capacity()),
        string_used_bytes: strings.used,
        string_capacity_bytes: strings.capacity,
        vector_heap_bytes: 0,
        roaring_serialized_bytes: roaring,
        serialized_bytes: bincode::serialized_size(map).unwrap_or(0),
    }
}

fn arc_btree_map_component(
    name: &'static str,
    map: &BTreeMap<Arc<str>, ExactPosting>,
) -> CacheShapeComponent {
    let roaring_serialized_bytes = map
        .values()
        .map(|posting| match posting {
            ExactPosting::One(_) => 0,
            ExactPosting::Many(bitmap) => bitmap.serialized_size() as u64,
        })
        .sum();
    CacheShapeComponent {
        name,
        entries: map.len(),
        capacity: map.len(),
        persisted_in_snapshot: false,
        inline_lower_bound_bytes: map
            .len()
            .saturating_mul(std::mem::size_of::<(Arc<str>, ExactPosting)>())
            as u64,
        // Arc<str> keys share the normalized-path allocation stored on CachedItem.
        string_used_bytes: 0,
        string_capacity_bytes: 0,
        vector_heap_bytes: 0,
        roaring_serialized_bytes,
        serialized_bytes: bincode::serialized_size(map).unwrap_or(0),
    }
}

fn exact_hash_map_component(
    name: &'static str,
    map: &HashMap<u64, ExactPosting>,
) -> CacheShapeComponent {
    let mut roaring = 0u64;
    for posting in map.values() {
        if let ExactPosting::Many(bitmap) = posting {
            roaring = roaring.saturating_add(bitmap.serialized_size() as u64);
        }
    }
    CacheShapeComponent {
        name,
        entries: map.len(),
        capacity: map.capacity(),
        persisted_in_snapshot: true,
        inline_lower_bound_bytes: hash_map_inline_lower_bound::<u64, ExactPosting>(map.capacity()),
        string_used_bytes: 0,
        string_capacity_bytes: 0,
        vector_heap_bytes: 0,
        roaring_serialized_bytes: roaring,
        serialized_bytes: bincode::serialized_size(map).unwrap_or(0),
    }
}

fn bitmap_string_map_component(
    name: &'static str,
    map: &HashMap<String, RoaringBitmap>,
) -> CacheShapeComponent {
    string_hash_map_component(name, map, |bitmap| bitmap.serialized_size() as u64)
}

fn bitmap_plain_map_component<K>(
    name: &'static str,
    map: &HashMap<K, RoaringBitmap>,
) -> CacheShapeComponent
where
    K: Serialize,
{
    CacheShapeComponent {
        name,
        entries: map.len(),
        capacity: map.capacity(),
        persisted_in_snapshot: true,
        inline_lower_bound_bytes: hash_map_inline_lower_bound::<K, RoaringBitmap>(map.capacity()),
        string_used_bytes: 0,
        string_capacity_bytes: 0,
        vector_heap_bytes: 0,
        roaring_serialized_bytes: map
            .values()
            .map(|bitmap| bitmap.serialized_size() as u64)
            .sum(),
        serialized_bytes: bincode::serialized_size(map).unwrap_or(0),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{ItemKind, Priority};
    use tempfile::TempDir;

    fn item(id: ItemId, root_id: Uuid, path: &str) -> ItemRecord {
        ItemRecord::from_path_metadata(
            id,
            root_id,
            Path::new(path),
            ItemKind::File,
            1,
            1,
            Priority::Normal,
        )
    }

    #[test]
    fn normalized_path_collisions_preserve_exact_identity_and_snapshot_rehydration() {
        let root_id = Uuid::new_v4();
        let nfc = "X:/unicode/caf\u{e9}.txt";
        let nfd = "X:/unicode/cafe\u{301}.txt";
        let upper = "X:/case/Foo.txt";
        let lower = "X:/case/foo.txt";
        let cache = CatalogCache::from_items(vec![
            item(1, root_id, nfc),
            item(2, root_id, nfd),
            item(3, root_id, upper),
            item(4, root_id, lower),
        ]);

        assert_eq!(cache.id_for_path(nfc), Some(1));
        assert_eq!(cache.id_for_path(nfd), Some(2));
        assert_eq!(cache.id_for_path(upper), Some(3));
        assert_eq!(cache.id_for_path(lower), Some(4));

        let state = TempDir::new().unwrap();
        let snapshot = state.path().join("search-cache.bin");
        let catalog_identity = Uuid::new_v4();
        cache.save_snapshot(&snapshot, 7, catalog_identity).unwrap();
        let (loaded, _) = CatalogCache::load_snapshot(&snapshot, 7, catalog_identity);
        let CacheSnapshotLoad::Loaded(mut loaded) = loaded else {
            panic!("collision-safe cache snapshot must load");
        };
        assert_eq!(loaded.id_for_path(nfc), Some(1));
        assert_eq!(loaded.id_for_path(nfd), Some(2));
        assert_eq!(loaded.id_for_path(upper), Some(3));
        assert_eq!(loaded.id_for_path(lower), Some(4));

        loaded.remove(1).unwrap();
        assert_eq!(loaded.id_for_path(nfc), None);
        assert_eq!(loaded.id_for_path(nfd), Some(2));
    }

    #[test]
    fn parallel_ordered_path_build_keeps_normalization_collisions_together() {
        let root_id = Uuid::new_v4();
        let items = vec![
            CachedItem::from_item(item(1, root_id, "X:/unicode/caf\u{e9}.txt")),
            CachedItem::from_item(item(2, root_id, "X:/unicode/cafe\u{301}.txt")),
            CachedItem::from_item(item(3, root_id, "X:/case/Foo.txt")),
            CachedItem::from_item(item(4, root_id, "X:/case/foo.txt")),
        ];
        let parallel = build_ordered_paths_with_parallelism(&items, true);
        let sequential = build_ordered_paths_with_parallelism(&items, false);
        assert_eq!(parallel.len(), sequential.len());
        assert_eq!(
            parallel.keys().map(Arc::as_ref).collect::<Vec<_>>(),
            sequential.keys().map(Arc::as_ref).collect::<Vec<_>>()
        );
        for (path, posting) in &parallel {
            let mut parallel_ids = RoaringBitmap::new();
            posting.insert_ids(&mut parallel_ids);
            let mut sequential_ids = RoaringBitmap::new();
            sequential
                .get(path)
                .expect("same ordered path exists in sequential build")
                .insert_ids(&mut sequential_ids);
            assert_eq!(parallel_ids, sequential_ids);
        }
    }
}
