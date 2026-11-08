//! Which workbench server the app talks to, persisted in the app's config directory.

use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use url::Url;

use crate::error::{AppError, Result};

/// Set at build time for release builds (`ONTO_DEFAULT_SERVER=https://… npm run build`).
pub const DEFAULT_SERVER: &str = match option_env!("ONTO_DEFAULT_SERVER") {
    Some(url) => url,
    None => "http://127.0.0.1:8766",
};

const LOOPBACK: [&str; 3] = ["localhost", "127.0.0.1", "[::1]"];

/// Normalise a server address to its origin (`https://host[:port]`), refusing anything that could leak the
/// session: plain HTTP to another machine, credentials in the URL, or a path that is not the workbench root.
pub fn normalize_server(input: &str) -> Result<String> {
    let invalid = || AppError::user("请输入工作台地址，例如 https://research.example.com");
    let url = Url::parse(input.trim()).map_err(|_| invalid())?;
    let host = url.host_str().ok_or_else(invalid)?;
    let loopback = LOOPBACK.contains(&host);
    match url.scheme() {
        "https" => {}
        "http" if loopback => {}
        "http" => return Err(AppError::user("工作台地址必须使用 HTTPS（本机地址除外）。")),
        _ => return Err(invalid()),
    }
    if !url.username().is_empty() || url.password().is_some() {
        return Err(AppError::user("工作台地址中不能包含账号或密码。"));
    }
    if !(url.path().is_empty() || url.path() == "/") || url.query().is_some() || url.fragment().is_some() {
        return Err(AppError::user("请只填写工作台的域名，不要带路径或参数。"));
    }
    Ok(url.origin().ascii_serialization())
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct Stored {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    server_url: Option<String>,
}

pub struct Settings {
    path: PathBuf,
    pub server: String,
}

impl Settings {
    pub fn load(dir: &Path) -> Self {
        let path = dir.join("settings.json");
        let stored: Stored =
            fs::read(&path).ok().and_then(|bytes| serde_json::from_slice(&bytes).ok()).unwrap_or_default();
        let server = stored.server_url.and_then(|url| normalize_server(&url).ok()).unwrap_or_else(default_server);
        Self { path, server }
    }

    pub fn is_default(&self) -> bool {
        self.server == default_server()
    }

    /// `None` returns to the build's default server.
    pub fn set_server(&mut self, url: Option<&str>) -> Result<()> {
        let server = match url {
            Some(url) => normalize_server(url)?,
            None => default_server(),
        };
        let stored = Stored { server_url: (server != default_server()).then(|| server.clone()) };
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent).map_err(|e| AppError::Internal(e.to_string()))?;
        }
        let json = serde_json::to_vec_pretty(&stored).map_err(|e| AppError::Internal(e.to_string()))?;
        fs::write(&self.path, json).map_err(|e| AppError::Internal(e.to_string()))?;
        self.server = server;
        Ok(())
    }
}

pub fn default_server() -> String {
    normalize_server(DEFAULT_SERVER).expect("ONTO_DEFAULT_SERVER must be a valid workbench origin")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_https_and_loopback_http_origins() {
        assert_eq!(normalize_server("https://research.example.com/").unwrap(), "https://research.example.com");
        assert_eq!(
            normalize_server(" https://research.example.com:8443 ").unwrap(),
            "https://research.example.com:8443"
        );
        assert_eq!(normalize_server("http://127.0.0.1:8766").unwrap(), "http://127.0.0.1:8766");
        assert_eq!(normalize_server("http://localhost:8766").unwrap(), "http://localhost:8766");
        assert_eq!(normalize_server("http://[::1]:8766").unwrap(), "http://[::1]:8766");
    }

    #[test]
    fn refuses_addresses_that_could_leak_a_session() {
        for bad in [
            "http://research.example.com",
            "ftp://research.example.com",
            "https://user:pass@research.example.com",
            "https://research.example.com/workbench",
            "https://research.example.com/?x=1",
            "https://research.example.com/#x",
            "research.example.com",
            "",
        ] {
            assert!(normalize_server(bad).is_err(), "{bad} was accepted");
        }
    }

    #[test]
    fn settings_round_trip_and_default() {
        let dir = std::env::temp_dir().join(format!("onto-settings-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let mut settings = Settings::load(&dir);
        assert!(settings.is_default());
        settings.set_server(Some("https://research.example.com")).unwrap();
        assert_eq!(Settings::load(&dir).server, "https://research.example.com");
        settings.set_server(None).unwrap();
        assert!(Settings::load(&dir).is_default());
        fs::remove_dir_all(&dir).unwrap();
    }
}
