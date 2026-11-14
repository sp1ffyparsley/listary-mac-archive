use std::path::PathBuf;

#[derive(Debug, thiserror::Error)]
pub enum CoreError {
    #[error("core is already open for this state directory")]
    AlreadyOpen,
    #[error("operation cancelled")]
    Cancelled,
    #[error("catalog generation is stale: expected {expected}, actual {actual}")]
    StaleCatalog { expected: u64, actual: u64 },
    #[error("root not found: {0}")]
    RootNotFound(String),
    #[error("filter not found: {0}")]
    FilterNotFound(String),
    #[error("command not found: {0}")]
    CommandNotFound(String),
    #[error("action not found: {0}")]
    ActionNotFound(String),
    #[error("invalid request: {0}")]
    InvalidRequest(String),
    #[error("index error: {0}")]
    Index(String),
    #[error("serialization error: {0}")]
    Serialization(String),
    #[error("filesystem error at {path}: {source}")]
    Filesystem {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
}

impl From<rocksdb::Error> for CoreError {
    fn from(value: rocksdb::Error) -> Self {
        Self::Index(value.to_string())
    }
}

impl From<bincode::Error> for CoreError {
    fn from(value: bincode::Error) -> Self {
        Self::Serialization(value.to_string())
    }
}

impl From<serde_json::Error> for CoreError {
    fn from(value: serde_json::Error) -> Self {
        Self::Serialization(value.to_string())
    }
}

pub type Result<T> = std::result::Result<T, CoreError>;
