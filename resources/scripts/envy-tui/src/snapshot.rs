//! The last Home payload, saved so the next start can show it at once while
//! `envy status` runs again.

use std::{
    env, fs,
    io::{self, Write},
    path::{Path, PathBuf},
};

use serde_json::{json, Value};

const SCHEMA_VERSION: u64 = 1;

/// `$XDG_CACHE_HOME/envy/tui/home.json`, falling back to `~/.cache`.
pub fn default_path() -> Option<PathBuf> {
    let root = env::var_os("XDG_CACHE_HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .or_else(|| env::var_os("HOME").map(|home| PathBuf::from(home).join(".cache")))?;
    Some(root.join("envy").join("tui").join("home.json"))
}

/// The saved payload and the Unix time it was saved at.
pub fn load(path: &Path) -> Option<(Value, i64)> {
    let mut saved: Value = serde_json::from_slice(&fs::read(path).ok()?).ok()?;
    if saved.get("schemaVersion").and_then(Value::as_u64) != Some(SCHEMA_VERSION) {
        return None;
    }
    let saved_at = saved.get("savedAt").and_then(Value::as_i64)?;
    match saved.get_mut("payload").map(Value::take) {
        Some(payload @ Value::Object(_)) => Some((payload, saved_at)),
        _ => None,
    }
}

/// Replace the saved payload atomically, readable only by the user.
pub fn save(path: &Path, payload: &Value, saved_at: i64) -> io::Result<()> {
    let parent = path.parent().unwrap_or(Path::new("."));
    create_private_dir(parent)?;
    let temporary = parent.join(format!(".home.json.{}.tmp", std::process::id()));
    let result =
        write_private(&temporary, payload, saved_at).and_then(|()| fs::rename(&temporary, path));
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

fn write_private(path: &Path, payload: &Value, saved_at: i64) -> io::Result<()> {
    let body = json!({"schemaVersion": SCHEMA_VERSION, "savedAt": saved_at, "payload": payload});
    let mut options = fs::OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
    let mut file = options.open(path)?;
    file.write_all(&serde_json::to_vec(&body).map_err(io::Error::other)?)?;
    file.sync_all()
}

fn create_private_dir(path: &Path) -> io::Result<()> {
    let mut builder = fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    std::os::unix::fs::DirBuilderExt::mode(&mut builder, 0o700);
    builder.create(path)
}

#[cfg(test)]
pub mod tests {
    use super::*;

    /// A fresh directory under the system temporary directory.
    pub fn temporary_dir(name: &str) -> PathBuf {
        let path = env::temp_dir().join(format!("envy-tui-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&path);
        path
    }

    #[test]
    fn saved_payload_round_trips_privately() {
        let root = temporary_dir("snapshot");
        let path = root.join("tui/home.json");
        let payload = json!({"machine": "mac"});
        save(&path, &payload, 42).unwrap();
        assert_eq!(load(&path), Some((payload, 42)));
        save(&path, &json!({"machine": "air"}), 43).unwrap();
        assert_eq!(load(&path), Some((json!({"machine": "air"}), 43)));
        assert_eq!(
            fs::read_dir(path.parent().unwrap()).unwrap().count(),
            1,
            "no temporary file is left"
        );
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = |path: &Path| fs::metadata(path).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode(&path), 0o600);
            assert_eq!(mode(path.parent().unwrap()), 0o700);
        }
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn unreadable_or_foreign_files_are_ignored() {
        let root = temporary_dir("snapshot-invalid");
        fs::create_dir_all(&root).unwrap();
        let path = root.join("home.json");
        assert_eq!(load(&path), None);
        for body in [
            "not json".to_string(),
            json!({"schemaVersion": 2, "savedAt": 1, "payload": {}}).to_string(),
            json!({"schemaVersion": 1, "payload": {}}).to_string(),
            json!({"schemaVersion": 1, "savedAt": 1, "payload": []}).to_string(),
        ] {
            fs::write(&path, &body).unwrap();
            assert_eq!(load(&path), None, "{body}");
        }
        fs::remove_dir_all(root).unwrap();
    }
}
