use crate::error::{CoreError, Result};
use std::{
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    sync::Mutex,
};

#[derive(Debug)]
pub struct BoundedLog {
    path: PathBuf,
    max_bytes: u64,
    generations: usize,
    lock: Mutex<()>,
}

impl BoundedLog {
    pub fn new(dir: &Path, max_bytes: u64, generations: usize) -> Result<Self> {
        fs::create_dir_all(dir).map_err(|source| CoreError::Filesystem {
            path: dir.to_path_buf(),
            source,
        })?;
        Ok(Self {
            path: dir.join("core.log"),
            max_bytes: max_bytes.max(1_024),
            generations: generations.max(1),
            lock: Mutex::new(()),
        })
    }

    pub fn event(&self, event: &str, details: &str) {
        let _guard = self.lock.lock().ok();
        let _ = self.rotate_if_needed();
        if let Ok(mut file) = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
        {
            let timestamp = chrono::Utc::now().to_rfc3339();
            let safe_details = details.replace(['\r', '\n'], " ");
            let _ = writeln!(file, "{timestamp}\t{event}\t{safe_details}");
        }
    }

    fn rotate_if_needed(&self) -> std::io::Result<()> {
        if fs::metadata(&self.path)
            .map(|m| m.len() < self.max_bytes)
            .unwrap_or(true)
        {
            return Ok(());
        }
        for index in (1..self.generations).rev() {
            let from = self.path.with_extension(format!("log.{index}"));
            let to = self.path.with_extension(format!("log.{}", index + 1));
            if from.exists() {
                let _ = fs::remove_file(&to);
                fs::rename(from, to)?;
            }
        }
        let first = self.path.with_extension("log.1");
        let _ = fs::remove_file(&first);
        fs::rename(&self.path, first)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn rotates_with_a_bounded_number_of_generations() {
        let dir = TempDir::new().unwrap();
        let log = BoundedLog::new(dir.path(), 1_024, 2).unwrap();
        for i in 0..500 {
            log.event("fixture", &format!("event-{i}-{}", "x".repeat(40)));
        }
        let files: Vec<_> = fs::read_dir(dir.path())
            .unwrap()
            .filter_map(|entry| entry.ok())
            .collect();
        assert!(files.len() <= 3);
        assert!(files.iter().any(|entry| entry.file_name() == "core.log"));
        assert!(files.iter().any(|entry| entry.file_name() == "core.log.1"));
    }
}
