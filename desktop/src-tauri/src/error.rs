//! Errors returned to the web view. Their text is shown to the user, so it is plain language and never
//! carries credentials, response bodies or internal paths.

use serde::{Serialize, Serializer};

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    /// A message meant for the user as written.
    #[error("{0}")]
    User(String),
    #[error("无法连接服务器，请检查网络或服务器地址。")]
    Network,
    #[error("服务器响应超时，请稍后重试。")]
    Timeout,
    /// The gateway answered 502/503/504: the server is down or still starting.
    #[error("工作台暂时不可用（{0}），可能正在启动，请稍后重试。")]
    Unavailable(u16),
    #[error("无法访问系统钥匙串，登录状态未能保存。")]
    Keychain,
    #[error("操作失败，请重试。")]
    Internal(String),
}

impl AppError {
    pub fn user(message: impl Into<String>) -> Self {
        Self::User(message.into())
    }
}

impl From<reqwest::Error> for AppError {
    fn from(error: reqwest::Error) -> Self {
        if error.is_timeout() {
            Self::Timeout
        } else if error.is_connect() || error.is_request() {
            Self::Network
        } else {
            Self::Internal(error.without_url().to_string())
        }
    }
}

impl From<tauri::Error> for AppError {
    fn from(error: tauri::Error) -> Self {
        Self::Internal(error.to_string())
    }
}

impl Serialize for AppError {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        if let Self::Internal(detail) = self {
            log::error!("internal error: {detail}");
        }
        serializer.serialize_str(&self.to_string())
    }
}

pub type Result<T> = std::result::Result<T, AppError>;
