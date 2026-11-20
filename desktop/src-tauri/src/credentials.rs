//! Session tokens live in the operating system's credential store (macOS Keychain, Windows Credential
//! Manager, the Secret Service on Linux), one entry per server, and never in files or the web view.

#[cfg(test)]
use std::collections::HashMap;
#[cfg(test)]
use std::sync::Mutex;

use crate::error::{AppError, Result};

pub const SERVICE: &str = "com.ontoresearch.desktop";

pub trait TokenStore: Send + Sync {
    fn get(&self, server: &str) -> Result<Option<String>>;
    fn set(&self, server: &str, token: &str) -> Result<()>;
    fn delete(&self, server: &str) -> Result<()>;
}

pub struct Keychain;

impl Keychain {
    fn entry(server: &str) -> Result<keyring::Entry> {
        keyring::Entry::new(SERVICE, server).map_err(|error| {
            log::warn!("keychain entry unavailable: {error}");
            AppError::Keychain
        })
    }
}

impl TokenStore for Keychain {
    fn get(&self, server: &str) -> Result<Option<String>> {
        match Self::entry(server)?.get_password() {
            Ok(token) => Ok(Some(token)),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(error) => {
                log::warn!("keychain read failed: {error}");
                Err(AppError::Keychain)
            }
        }
    }

    fn set(&self, server: &str, token: &str) -> Result<()> {
        Self::entry(server)?.set_password(token).map_err(|error| {
            log::warn!("keychain write failed: {error}");
            AppError::Keychain
        })
    }

    fn delete(&self, server: &str) -> Result<()> {
        match Self::entry(server)?.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(error) => {
                log::warn!("keychain delete failed: {error}");
                Err(AppError::Keychain)
            }
        }
    }
}

/// An in-memory store for tests.
#[cfg(test)]
#[derive(Default)]
pub struct MemoryStore(Mutex<HashMap<String, String>>);

#[cfg(test)]
impl TokenStore for MemoryStore {
    fn get(&self, server: &str) -> Result<Option<String>> {
        Ok(self.0.lock().unwrap().get(server).cloned())
    }

    fn set(&self, server: &str, token: &str) -> Result<()> {
        self.0.lock().unwrap().insert(server.to_owned(), token.to_owned());
        Ok(())
    }

    fn delete(&self, server: &str) -> Result<()> {
        self.0.lock().unwrap().remove(server);
        Ok(())
    }
}
