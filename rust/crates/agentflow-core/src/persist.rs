//! Async JSON-backed state persistence for AgentFlow actors.
//!
//! Each actor gets its own subdirectory under `state/{actor_name}/`.
//! Values are stored as pretty-printed JSON files: `{key}.json`.
//! Replaces Python's `pickle`-based storage with portable, human-readable JSON.
//!
//! # Atomic writes
//!
//! `save()` writes to a `.{key}.tmp` file first, then renames it into place.
//! This prevents a partial write from corrupting an existing value on crash.

use std::path::{Path, PathBuf};

use anyhow::Result;
use serde::{Serialize, de::DeserializeOwned};
use tokio::fs;

const STATE_ROOT: &str = "state";

/// Per-actor state store — async, JSON-backed, idempotent.
#[derive(Debug, Clone)]
pub struct StateStore {
    dir: PathBuf,
}

impl StateStore {
    /// Create a store scoped to `actor_name` inside the default `state/` root.
    pub fn new(actor_name: &str) -> Self {
        Self {
            dir: PathBuf::from(STATE_ROOT).join(actor_name),
        }
    }

    /// Create a store with an explicit root directory (useful in tests).
    pub fn with_root(root: impl AsRef<Path>, actor_name: &str) -> Self {
        Self {
            dir: root.as_ref().join(actor_name),
        }
    }

    /// Persist `value` under `key`. Creates the directory if it does not exist.
    pub async fn save<T: Serialize>(&self, key: &str, value: &T) -> Result<()> {
        fs::create_dir_all(&self.dir).await?;
        let json = serde_json::to_string_pretty(value)?;
        // Atomic write: tmp → rename.
        let tmp = self.dir.join(format!(".{key}.tmp"));
        let dst = self.dir.join(format!("{key}.json"));
        fs::write(&tmp, json).await?;
        fs::rename(&tmp, &dst).await?;
        Ok(())
    }

    /// Load the value stored under `key`. Returns `None` if the file does not exist.
    pub async fn load<T: DeserializeOwned>(&self, key: &str) -> Result<Option<T>> {
        let path = self.dir.join(format!("{key}.json"));
        match fs::read_to_string(&path).await {
            Ok(json) => Ok(Some(serde_json::from_str(&json)?)),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    /// Load a value, returning `T::default()` if the file does not exist.
    pub async fn load_or_default<T: DeserializeOwned + Default>(&self, key: &str) -> Result<T> {
        Ok(self.load::<T>(key).await?.unwrap_or_default())
    }

    /// Delete the file for `key`. Silently succeeds if the file does not exist.
    pub async fn delete(&self, key: &str) -> Result<()> {
        let path = self.dir.join(format!("{key}.json"));
        fs::remove_file(&path).await.ok();
        Ok(())
    }

    /// List all persisted keys in this actor's store (sorted alphabetically).
    pub async fn list_keys(&self) -> Result<Vec<String>> {
        match fs::read_dir(&self.dir).await {
            Ok(mut dir) => {
                let mut keys = Vec::new();
                while let Ok(Some(entry)) = dir.next_entry().await {
                    let name = entry.file_name();
                    let name = name.to_string_lossy();
                    if name.ends_with(".json") && !name.starts_with('.') {
                        keys.push(name.trim_end_matches(".json").to_string());
                    }
                }
                keys.sort();
                Ok(keys)
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(vec![]),
            Err(e) => Err(e.into()),
        }
    }

    /// Returns `true` if a file exists for `key`.
    pub async fn exists(&self, key: &str) -> bool {
        self.dir.join(format!("{key}.json")).exists()
    }

    /// Remove all persisted state for this actor (deletes the whole directory).
    pub async fn clear(&self) -> Result<()> {
        fs::remove_dir_all(&self.dir).await.ok();
        Ok(())
    }

    /// Return the path to the actor's state directory (useful for debugging).
    pub fn dir(&self) -> &Path {
        &self.dir
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::{Deserialize, Serialize};
    use tempfile::tempdir;

    #[derive(Debug, PartialEq, Serialize, Deserialize)]
    struct Payload {
        value: u64,
        name: String,
    }

    #[tokio::test]
    async fn round_trip() {
        let dir = tempdir().unwrap();
        let store = StateStore::with_root(dir.path(), "test-actor");

        let payload = Payload {
            value: 42,
            name: "hello".into(),
        };
        store.save("my_key", &payload).await.unwrap();

        let loaded: Payload = store.load("my_key").await.unwrap().unwrap();
        assert_eq!(loaded, payload);
    }

    #[tokio::test]
    async fn missing_key_returns_none() {
        let dir = tempdir().unwrap();
        let store = StateStore::with_root(dir.path(), "test-actor");
        let result: Option<String> = store.load("no_such_key").await.unwrap();
        assert!(result.is_none());
    }

    #[tokio::test]
    async fn delete_and_list() {
        let dir = tempdir().unwrap();
        let store = StateStore::with_root(dir.path(), "test-actor");

        store.save("a", &1u32).await.unwrap();
        store.save("b", &2u32).await.unwrap();
        store.save("c", &3u32).await.unwrap();

        let keys = store.list_keys().await.unwrap();
        assert_eq!(keys, vec!["a", "b", "c"]);

        store.delete("b").await.unwrap();
        let keys = store.list_keys().await.unwrap();
        assert_eq!(keys, vec!["a", "c"]);
    }
}
