//! The app's only path to the workbench server. It adds the desktop client header and the bearer session,
//! keeps one-time sign-in challenges in memory, and strips every credential out of what the web view sees.

use std::sync::{Arc, Mutex, RwLock};
use std::time::Duration;

use reqwest::header::{HeaderMap, HeaderValue, ACCEPT, AUTHORIZATION, CONTENT_DISPOSITION, CONTENT_TYPE};
use reqwest::{Method, StatusCode};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use url::Url;

use crate::credentials::TokenStore;
use crate::error::{AppError, Result};

pub const CLIENT_HEADER: &str = "x-onto-client";
pub const CLIENT_VERSION: &str = concat!("desktop/", env!("CARGO_PKG_VERSION"));
const MAX_PATH: usize = 2048;
const MAX_BODY: usize = 64 * 1024 * 1024;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(180);

#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct ApiResponse {
    pub status: u16,
    pub body: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content_type: Option<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct BootState {
    pub mode: String,
    pub api: u32,
    pub providers: Vec<String>,
    pub authenticated: bool,
}

#[derive(Deserialize)]
struct Options {
    mode: Option<String>,
    api: Option<u32>,
    #[serde(default)]
    providers: Vec<String>,
}

#[derive(Deserialize)]
struct Session {
    token: String,
}

/// What a sign-in path needs from the backend beyond the plain request.
#[derive(Debug, Clone, Copy, PartialEq)]
enum AuthStep {
    None,
    Login,
    SmsSend,
    SmsComplete,
    Logout,
}

/// The paths the web view may reach. WeChat goes through its own command (a native window), never here.
pub fn validate_path(path: &str, socket: bool) -> Result<()> {
    let refuse = || AppError::user("不支持的请求地址。");
    if path.len() > MAX_PATH || path.contains("..") || path.contains("//") || path.contains('\\') || path.contains('#')
    {
        return Err(refuse());
    }
    if path.chars().any(|c| c.is_whitespace() || c.is_control()) {
        return Err(refuse());
    }
    let allowed = if socket {
        path == "/ws" || path.starts_with("/ws?")
    } else {
        (path.starts_with("/api/") || path.starts_with("/auth/")) && !path.starts_with("/auth/wechat/")
    };
    if allowed {
        Ok(())
    } else {
        Err(refuse())
    }
}

fn auth_step(method: &Method, path: &str) -> AuthStep {
    if method != Method::POST {
        return AuthStep::None;
    }
    match path.split('?').next().unwrap_or_default() {
        "/auth/login" => AuthStep::Login,
        "/auth/sms/send" => AuthStep::SmsSend,
        "/auth/sms/complete" => AuthStep::SmsComplete,
        "/auth/logout" => AuthStep::Logout,
        _ => AuthStep::None,
    }
}

/// Add the SMS challenge the server handed out earlier to the completion request.
fn with_challenge(body: Option<&str>, challenge: &str) -> Result<String> {
    let mut object: Map<String, Value> =
        body.and_then(|b| serde_json::from_str(b).ok()).ok_or_else(|| AppError::user("请求格式无效。"))?;
    object.insert("challenge".into(), Value::String(challenge.to_owned()));
    Ok(Value::Object(object).to_string())
}

/// Take the challenge out of the SMS send response, so the web view never holds it.
fn take_challenge(body: &str) -> Option<(String, String)> {
    let mut object: Map<String, Value> = serde_json::from_str(body).ok()?;
    let challenge = object.remove("challenge")?.as_str()?.to_owned();
    Some((challenge, Value::Object(object).to_string()))
}

pub fn safe_filename(name: &str) -> String {
    let cleaned: String =
        name.chars().map(|c| if c.is_control() || "/\\:*?\"<>|".contains(c) { '_' } else { c }).collect();
    let cleaned = cleaned.trim().trim_start_matches('.').to_owned();
    if cleaned.is_empty() {
        "export.json".into()
    } else {
        cleaned.chars().take(120).collect()
    }
}

fn disposition_filename(headers: &HeaderMap) -> Option<String> {
    let value = headers.get(CONTENT_DISPOSITION)?.to_str().ok()?;
    let name = value.split(';').find_map(|part| part.trim().strip_prefix("filename="))?;
    Some(safe_filename(name.trim_matches('"')))
}

pub struct Backend {
    http: reqwest::Client,
    server: RwLock<String>,
    store: Arc<dyn TokenStore>,
    /// `None`: not read from the store yet.
    token: Mutex<Option<Option<String>>>,
    sms_challenge: Mutex<Option<String>>,
}

impl Backend {
    pub fn new(server: String, store: Arc<dyn TokenStore>) -> Result<Self> {
        let mut headers = HeaderMap::new();
        headers.insert(CLIENT_HEADER, HeaderValue::from_static(CLIENT_VERSION));
        let http = reqwest::Client::builder()
            .default_headers(headers)
            .user_agent(concat!("ONTO-Research-Desktop/", env!("CARGO_PKG_VERSION")))
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(10))
            .timeout(REQUEST_TIMEOUT)
            .build()
            .map_err(|e| AppError::Internal(e.to_string()))?;
        Ok(Self { http, server: RwLock::new(server), store, token: Mutex::new(None), sms_challenge: Mutex::new(None) })
    }

    pub fn server(&self) -> String {
        self.server.read().unwrap().clone()
    }

    /// Switch servers. Each server keeps its own stored session.
    pub fn set_server(&self, server: String) {
        *self.server.write().unwrap() = server;
        *self.token.lock().unwrap() = None;
        *self.sms_challenge.lock().unwrap() = None;
    }

    fn token(&self) -> Option<String> {
        let mut cached = self.token.lock().unwrap();
        if cached.is_none() {
            *cached = Some(self.store.get(&self.server()).unwrap_or(None));
        }
        cached.clone().flatten()
    }

    fn save_token(&self, token: &str) -> Result<()> {
        // Keep the session for this run even if the system store refuses it; report the failure.
        *self.token.lock().unwrap() = Some(Some(token.to_owned()));
        self.store.set(&self.server(), token)
    }

    pub fn forget_token(&self) {
        *self.token.lock().unwrap() = Some(None);
        if let Err(error) = self.store.delete(&self.server()) {
            log::warn!("could not remove stored session: {error}");
        }
    }

    pub fn has_token(&self) -> bool {
        self.token().is_some()
    }

    fn url(&self, path: &str) -> Result<Url> {
        let base = Url::parse(&self.server()).map_err(|e| AppError::Internal(e.to_string()))?;
        let url = base.join(path).map_err(|_| AppError::user("不支持的请求地址。"))?;
        if url.origin() != base.origin() {
            return Err(AppError::user("不支持的请求地址。"));
        }
        Ok(url)
    }

    async fn send(&self, method: Method, path: &str, body: Option<String>) -> Result<reqwest::Response> {
        let mut request = self.http.request(method.clone(), self.url(path)?).header(ACCEPT, "application/json");
        if let Some(token) = self.token() {
            request = request.header(AUTHORIZATION, format!("Bearer {token}"));
        }
        if let Some(body) = body {
            request = request.header(CONTENT_TYPE, "application/json").body(body);
        }
        let response = request.send().await?;
        log::info!("{method} {} -> {}", path.split('?').next().unwrap_or_default(), response.status().as_u16());
        Ok(response)
    }

    async fn read(response: reqwest::Response) -> Result<(StatusCode, Option<String>, Vec<u8>)> {
        let status = response.status();
        if response.content_length().is_some_and(|n| n as usize > MAX_BODY) {
            return Err(AppError::user("服务器返回的内容过大。"));
        }
        let content_type = response.headers().get(CONTENT_TYPE).and_then(|v| v.to_str().ok()).map(str::to_owned);
        let bytes = response.bytes().await?;
        if bytes.len() > MAX_BODY {
            return Err(AppError::user("服务器返回的内容过大。"));
        }
        Ok((status, content_type, bytes.to_vec()))
    }

    /// A request from the web view. Sign-in responses are rewritten so tokens and challenges stay here.
    pub async fn request(&self, method: &str, path: &str, body: Option<String>) -> Result<ApiResponse> {
        validate_path(path, false)?;
        let method = Method::from_bytes(method.as_bytes())
            .ok()
            .filter(|m| [Method::GET, Method::POST, Method::PUT, Method::PATCH, Method::DELETE].contains(m))
            .ok_or_else(|| AppError::user("不支持的请求方法。"))?;
        if body.as_ref().is_some_and(|b| b.len() > MAX_BODY) {
            return Err(AppError::user("上传的内容过大。"));
        }
        let step = auth_step(&method, path);
        let body = match step {
            AuthStep::SmsComplete => {
                let challenge = self.sms_challenge.lock().unwrap().clone();
                let challenge = challenge.ok_or_else(|| AppError::user("请先发送验证码。"))?;
                Some(with_challenge(body.as_deref(), &challenge)?)
            }
            _ => body,
        };

        let response = self.send(method, path, body).await;
        if step == AuthStep::Logout {
            // Signing out always clears this device, even when the server cannot be reached.
            self.forget_token();
            return Ok(ApiResponse { status: 204, body: String::new(), content_type: None });
        }
        let (status, content_type, bytes) = Self::read(response?).await?;
        let text = String::from_utf8_lossy(&bytes).into_owned();

        if status.is_success() {
            match step {
                AuthStep::Login | AuthStep::SmsComplete => {
                    let session: Session = serde_json::from_str(&text)
                        .map_err(|_| AppError::user("服务器版本过旧，不支持桌面端登录。"))?;
                    self.save_token(&session.token)?;
                    if step == AuthStep::SmsComplete {
                        *self.sms_challenge.lock().unwrap() = None;
                    }
                    return Ok(ApiResponse { status: 204, body: String::new(), content_type: None });
                }
                AuthStep::SmsSend => {
                    let (challenge, rest) =
                        take_challenge(&text).ok_or_else(|| AppError::user("服务器版本过旧，不支持桌面端注册。"))?;
                    *self.sms_challenge.lock().unwrap() = Some(challenge);
                    return Ok(ApiResponse { status: status.as_u16(), body: rest, content_type });
                }
                _ => {}
            }
        } else if status == StatusCode::UNAUTHORIZED && step == AuthStep::None {
            self.forget_token();
        }
        Ok(ApiResponse { status: status.as_u16(), body: text, content_type })
    }

    /// Download a file the server offers (an export). Returns its bytes and the server's file name.
    pub async fn fetch_file(&self, path: &str) -> Result<(Vec<u8>, Option<String>)> {
        validate_path(path, false)?;
        let response = self.send(Method::GET, path, None).await?;
        let filename = disposition_filename(response.headers());
        let (status, _, bytes) = Self::read(response).await?;
        if status == StatusCode::UNAUTHORIZED {
            self.forget_token();
            return Err(AppError::user("登录已过期，请重新登录。"));
        }
        if !status.is_success() {
            let detail = serde_json::from_slice::<Value>(&bytes)
                .ok()
                .and_then(|v| v.get("detail").and_then(Value::as_str).map(str::to_owned));
            return Err(AppError::User(detail.unwrap_or_else(|| format!("下载失败（{}）。", status.as_u16()))));
        }
        Ok((bytes, filename))
    }

    /// Which kind of server this is and whether the stored session is still valid.
    pub async fn boot(&self) -> Result<BootState> {
        let response = self.send(Method::GET, "/auth/options", None).await?;
        let status = response.status();
        let (_, _, bytes) = Self::read(response).await?;
        if status == StatusCode::NOT_FOUND {
            return Err(AppError::user("该地址不是 ONTO-Research 工作台，或工作台版本过旧。"));
        }
        if !status.is_success() {
            return Err(AppError::User(format!("工作台暂时不可用（{}），请稍后重试。", status.as_u16())));
        }
        let options: Options =
            serde_json::from_slice(&bytes).map_err(|_| AppError::user("该地址不是 ONTO-Research 工作台。"))?;
        let mode = options.mode.unwrap_or_else(|| "cloud".into());
        let api = options.api.unwrap_or(0);
        let authenticated = mode == "local" || (self.has_token() && self.session_valid().await?);
        Ok(BootState { mode, api, providers: options.providers, authenticated })
    }

    async fn session_valid(&self) -> Result<bool> {
        let status = self.send(Method::GET, "/auth/session", None).await?.status();
        if status == StatusCode::UNAUTHORIZED {
            self.forget_token();
        }
        Ok(status.is_success())
    }

    /// The handshake request for the workbench socket, with the session attached.
    pub fn socket_request(&self, path: &str) -> Result<tokio_tungstenite::tungstenite::handshake::client::Request> {
        use tokio_tungstenite::tungstenite::client::IntoClientRequest;

        validate_path(path, true)?;
        let mut url = self.url(path)?;
        let scheme = if url.scheme() == "https" { "wss" } else { "ws" };
        url.set_scheme(scheme).map_err(|_| AppError::Internal("bad socket scheme".into()))?;
        let mut request = url.as_str().into_client_request().map_err(|e| AppError::Internal(e.to_string()))?;
        let headers = request.headers_mut();
        headers.insert(CLIENT_HEADER, HeaderValue::from_static(CLIENT_VERSION));
        if let Some(token) = self.token() {
            let value = HeaderValue::from_str(&format!("Bearer {token}")).map_err(|_| AppError::Keychain)?;
            headers.insert(AUTHORIZATION, value);
        }
        Ok(request)
    }

    /// Start a WeChat sign-in: the QR page to show and the challenge that binds the callback to this app.
    pub async fn wechat_start(&self) -> Result<(String, String)> {
        #[derive(Deserialize)]
        struct Start {
            url: String,
            challenge: String,
        }
        let response = self.send(Method::POST, "/auth/wechat/start", Some("{}".into())).await?;
        let (status, _, bytes) = Self::read(response).await?;
        if !status.is_success() {
            return Err(detail_error(&bytes, "微信登录暂时不可用。"));
        }
        let start: Start =
            serde_json::from_slice(&bytes).map_err(|_| AppError::user("服务器版本过旧，不支持桌面端微信登录。"))?;
        let url = Url::parse(&start.url).map_err(|_| AppError::user("微信登录地址无效。"))?;
        if url.scheme() != "https" || url.host_str() != Some("open.weixin.qq.com") {
            return Err(AppError::user("微信登录地址无效。"));
        }
        Ok((start.url, start.challenge))
    }

    pub async fn wechat_complete(&self, state: &str, code: &str, challenge: &str) -> Result<()> {
        let body = serde_json::json!({ "state": state, "code": code, "challenge": challenge }).to_string();
        let response = self.send(Method::POST, "/auth/wechat/complete", Some(body)).await?;
        let (status, _, bytes) = Self::read(response).await?;
        if !status.is_success() {
            return Err(detail_error(&bytes, "微信登录未完成，请重新扫码。"));
        }
        let session: Session =
            serde_json::from_slice(&bytes).map_err(|_| AppError::user("微信登录未完成，请重新扫码。"))?;
        self.save_token(&session.token)
    }

    /// The workbench's WeChat callback URL, which the sign-in window intercepts instead of loading.
    pub fn wechat_callback(&self) -> Result<Url> {
        self.url("/auth/wechat/callback")
    }
}

fn detail_error(bytes: &[u8], fallback: &str) -> AppError {
    let detail = serde_json::from_slice::<Value>(bytes)
        .ok()
        .and_then(|v| v.get("detail").and_then(Value::as_str).map(str::to_owned));
    AppError::User(detail.unwrap_or_else(|| fallback.to_owned()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_workbench_paths_are_reachable() {
        for ok in ["/api/projects", "/api/graph?project=a-1", "/auth/login", "/auth/session"] {
            assert!(validate_path(ok, false).is_ok(), "{ok}");
        }
        for bad in [
            "/",
            "/static/app.js",
            "api/projects",
            "/api/../auth",
            "//evil.example/api",
            "/api/x#y",
            "/api/a b",
            "/api/\\x",
            "/auth/wechat/start",
            "/auth/wechat/complete",
            "https://evil.example/api/",
        ] {
            assert!(validate_path(bad, false).is_err(), "{bad}");
        }
        assert!(validate_path("/ws?project=a&session=c_1", true).is_ok());
        assert!(validate_path("/api/projects", true).is_err());
        assert!(validate_path(&format!("/api/{}", "a".repeat(MAX_PATH)), false).is_err());
    }

    #[test]
    fn auth_steps_are_post_only() {
        assert_eq!(auth_step(&Method::POST, "/auth/login"), AuthStep::Login);
        assert_eq!(auth_step(&Method::GET, "/auth/login"), AuthStep::None);
        assert_eq!(auth_step(&Method::POST, "/auth/sms/complete"), AuthStep::SmsComplete);
        assert_eq!(auth_step(&Method::POST, "/auth/logout"), AuthStep::Logout);
        assert_eq!(auth_step(&Method::POST, "/api/projects"), AuthStep::None);
    }

    #[test]
    fn challenges_are_added_and_removed_out_of_sight() {
        let body = with_challenge(Some(r#"{"phone":"138","code":"123456"}"#), "secret").unwrap();
        let value: Value = serde_json::from_str(&body).unwrap();
        assert_eq!(value["challenge"], "secret");
        assert_eq!(value["phone"], "138");
        assert!(with_challenge(Some("[]"), "secret").is_err());
        assert!(with_challenge(None, "secret").is_err());

        let (challenge, rest) = take_challenge(r#"{"retry_after":60,"challenge":"secret"}"#).unwrap();
        assert_eq!(challenge, "secret");
        assert!(!rest.contains("secret"));
        assert!(take_challenge(r#"{"retry_after":60}"#).is_none());
    }

    #[test]
    fn filenames_cannot_escape_the_chosen_folder() {
        assert_eq!(safe_filename("project.json"), "project.json");
        assert_eq!(safe_filename("../../etc/passwd"), "_.._etc_passwd");
        assert_eq!(safe_filename("..."), "export.json");
        assert_eq!(safe_filename(" a:b?.json "), "a_b_.json");
        let mut headers = HeaderMap::new();
        headers.insert(CONTENT_DISPOSITION, HeaderValue::from_static("attachment; filename=\"btc.json\""));
        assert_eq!(disposition_filename(&headers).as_deref(), Some("btc.json"));
    }

    mod http {
        use super::super::*;
        use crate::credentials::MemoryStore;
        use wiremock::matchers::{body_json, header, header_exists, method, path};
        use wiremock::{Mock, MockServer, Request, ResponseTemplate};

        const TOKEN: &str = "tok_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

        async fn backend() -> (MockServer, Backend, Arc<MemoryStore>) {
            let server = MockServer::start().await;
            let store = Arc::new(MemoryStore::default());
            let backend = Backend::new(server.uri(), store.clone()).unwrap();
            (server, backend, store)
        }

        fn no_bearer(request: &Request) -> bool {
            !request.headers.contains_key("authorization")
        }

        #[tokio::test]
        async fn login_keeps_the_token_out_of_the_web_view_and_sends_it_afterwards() {
            let (server, backend, store) = backend().await;
            Mock::given(method("POST"))
                .and(path("/auth/login"))
                .and(header(CLIENT_HEADER, CLIENT_VERSION))
                .and(no_bearer)
                .respond_with(
                    ResponseTemplate::new(200).set_body_json(serde_json::json!({"token": TOKEN, "expires_in": 60})),
                )
                .mount(&server)
                .await;
            Mock::given(method("GET"))
                .and(path("/api/projects"))
                .and(header("authorization", format!("Bearer {TOKEN}").as_str()))
                .respond_with(ResponseTemplate::new(200).set_body_raw("[]", "application/json"))
                .mount(&server)
                .await;

            let login = backend
                .request("POST", "/auth/login", Some(r#"{"username":"u","password":"p"}"#.into()))
                .await
                .unwrap();
            assert_eq!(login, ApiResponse { status: 204, body: String::new(), content_type: None });
            assert_eq!(store.get(&server.uri()).unwrap().as_deref(), Some(TOKEN));

            let projects = backend.request("GET", "/api/projects", None).await.unwrap();
            assert_eq!((projects.status, projects.body.as_str()), (200, "[]"));
            assert_eq!(projects.content_type.as_deref(), Some("application/json"));
        }

        #[tokio::test]
        async fn an_expired_session_is_forgotten_and_logout_always_clears_it() {
            let (server, backend, store) = backend().await;
            store.set(&server.uri(), TOKEN).unwrap();
            Mock::given(method("GET"))
                .and(path("/api/projects"))
                .respond_with(ResponseTemplate::new(401).set_body_json(serde_json::json!({"detail": "请先登录。"})))
                .mount(&server)
                .await;
            let response = backend.request("GET", "/api/projects", None).await.unwrap();
            assert_eq!(response.status, 401);
            assert!(store.get(&server.uri()).unwrap().is_none());

            store.set(&server.uri(), TOKEN).unwrap();
            let fresh = Backend::new(server.uri(), store.clone()).unwrap();
            Mock::given(method("POST"))
                .and(path("/auth/logout"))
                .respond_with(ResponseTemplate::new(503))
                .mount(&server)
                .await;
            assert_eq!(fresh.request("POST", "/auth/logout", None).await.unwrap().status, 204);
            assert!(store.get(&server.uri()).unwrap().is_none());
        }

        #[tokio::test]
        async fn sms_challenge_round_trips_without_reaching_the_page() {
            let (server, backend, store) = backend().await;
            Mock::given(method("POST"))
                .and(path("/auth/sms/send"))
                .respond_with(
                    ResponseTemplate::new(200)
                        .set_body_json(serde_json::json!({"retry_after": 60, "challenge": "ch_secret"})),
                )
                .mount(&server)
                .await;
            Mock::given(method("POST"))
                .and(path("/auth/sms/complete"))
                .and(body_json(serde_json::json!({"phone": "138", "code": "123456", "challenge": "ch_secret"})))
                .respond_with(
                    ResponseTemplate::new(200).set_body_json(serde_json::json!({"token": TOKEN, "expires_in": 60})),
                )
                .mount(&server)
                .await;

            let early =
                backend.request("POST", "/auth/sms/complete", Some(r#"{"phone":"138","code":"123456"}"#.into())).await;
            assert!(early.is_err());
            let sent = backend.request("POST", "/auth/sms/send", Some(r#"{"phone":"138"}"#.into())).await.unwrap();
            assert_eq!(sent.status, 200);
            assert!(!sent.body.contains("ch_secret"));
            let done = backend
                .request("POST", "/auth/sms/complete", Some(r#"{"phone":"138","code":"123456"}"#.into()))
                .await
                .unwrap();
            assert_eq!(done.status, 204);
            assert_eq!(store.get(&server.uri()).unwrap().as_deref(), Some(TOKEN));
        }

        #[tokio::test]
        async fn boot_tells_local_cloud_and_foreign_servers_apart() {
            let (server, backend, store) = backend().await;
            Mock::given(method("GET"))
                .and(path("/auth/options"))
                .respond_with(
                    ResponseTemplate::new(200)
                        .set_body_json(serde_json::json!({"mode": "cloud", "api": 1, "providers": ["sms"]})),
                )
                .mount(&server)
                .await;
            Mock::given(method("GET"))
                .and(path("/auth/session"))
                .and(header_exists("authorization"))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"authenticated": true})))
                .mount(&server)
                .await;
            let anonymous = backend.boot().await.unwrap();
            assert_eq!(
                anonymous,
                BootState { mode: "cloud".into(), api: 1, providers: vec!["sms".into()], authenticated: false }
            );
            store.set(&server.uri(), TOKEN).unwrap();
            let signed_in = Backend::new(server.uri(), store.clone()).unwrap();
            assert!(signed_in.boot().await.unwrap().authenticated);

            let (local, backend, _) =
                backend_with_options(serde_json::json!({"mode": "local", "api": 1, "providers": []})).await;
            assert!(backend.boot().await.unwrap().authenticated);
            drop(local);

            let foreign = MockServer::start().await;
            let backend = Backend::new(foreign.uri(), Arc::new(MemoryStore::default())).unwrap();
            assert!(backend.boot().await.is_err());
        }

        async fn backend_with_options(options: serde_json::Value) -> (MockServer, Backend, Arc<MemoryStore>) {
            let (server, backend, store) = backend().await;
            Mock::given(method("GET"))
                .and(path("/auth/options"))
                .respond_with(ResponseTemplate::new(200).set_body_json(options))
                .mount(&server)
                .await;
            (server, backend, store)
        }

        #[tokio::test]
        async fn redirects_are_not_followed() {
            let (server, backend, _) = backend().await;
            Mock::given(method("GET"))
                .and(path("/api/projects"))
                .respond_with(ResponseTemplate::new(302).insert_header("location", "https://evil.example/steal"))
                .mount(&server)
                .await;
            assert_eq!(backend.request("GET", "/api/projects", None).await.unwrap().status, 302);
            assert_eq!(server.received_requests().await.unwrap().len(), 1);
        }

        #[tokio::test]
        async fn socket_handshake_carries_the_session_and_client_header() {
            let (server, backend, store) = backend().await;
            store.set(&server.uri(), TOKEN).unwrap();
            let request = backend.socket_request("/ws?project=a").unwrap();
            assert!(request.uri().to_string().starts_with("ws://127.0.0.1:"));
            assert_eq!(request.headers()["authorization"], format!("Bearer {TOKEN}").as_str());
            assert_eq!(request.headers()[CLIENT_HEADER], CLIENT_VERSION);
            assert!(backend.socket_request("/api/projects").is_err());
        }
    }
}
