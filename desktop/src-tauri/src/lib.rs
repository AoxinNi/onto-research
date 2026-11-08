//! ONTO Research desktop app: the workbench pages bundled into a native window, talking to the
//! CloudBase-hosted workbench (or a local one) through the Rust side.
//!
//! Layers, one bundled page each: `index.html` sign-in and registration, `home.html` project overview,
//! `workspace.html?project=<id>` the project workspace.

mod backend;
mod config;
mod credentials;
mod error;
mod socket;
mod wechat;

use std::sync::{Arc, Mutex};

use serde::Serialize;
use tauri::ipc::Channel;
use tauri::{AppHandle, Manager, State, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_opener::OpenerExt;
use tokio::sync::oneshot;
use url::Url;

use backend::{safe_filename, ApiResponse, Backend, BootState};
use config::Settings;
use credentials::Keychain;
use error::{AppError, Result};
use socket::{SocketEvent, Sockets};

const MAIN_WINDOW: &str = "main";
const APP_NAME: &str = "ONTO Research";

struct AppState {
    settings: Mutex<Settings>,
    backend: Backend,
    sockets: Sockets,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ServerInfo {
    url: String,
    is_default: bool,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct Boot {
    server: ServerInfo,
    #[serde(flatten)]
    state: BootState,
}

impl AppState {
    fn server_info(&self) -> ServerInfo {
        let settings = self.settings.lock().unwrap();
        ServerInfo { url: settings.server.clone(), is_default: settings.is_default() }
    }
}

// ------------------------------------------------------------------ commands

#[tauri::command]
fn server_get(state: State<'_, AppState>) -> ServerInfo {
    state.server_info()
}

#[tauri::command]
fn server_set(url: Option<String>, state: State<'_, AppState>) -> Result<ServerInfo> {
    let server = {
        let mut settings = state.settings.lock().unwrap();
        settings.set_server(url.as_deref())?;
        settings.server.clone()
    };
    state.backend.set_server(server);
    Ok(state.server_info())
}

#[tauri::command]
async fn session_boot(state: State<'_, AppState>) -> Result<Boot> {
    let boot = state.backend.boot().await?;
    Ok(Boot { server: state.server_info(), state: boot })
}

#[tauri::command]
async fn api_request(
    method: String,
    path: String,
    body: Option<String>,
    state: State<'_, AppState>,
) -> Result<ApiResponse> {
    state.backend.request(&method, &path, body).await
}

/// Fetch first (so errors show before any dialog), then ask where to save.
#[tauri::command]
async fn download_file(
    path: String,
    filename: Option<String>,
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<bool> {
    let (bytes, server_name) = state.backend.fetch_file(&path).await?;
    let name = safe_filename(filename.as_deref().or(server_name.as_deref()).unwrap_or("export.json"));
    let (tx, rx) = oneshot::channel();
    let mut dialog = app.dialog().file().set_title("导出").set_file_name(&name);
    if name.ends_with(".json") {
        dialog = dialog.add_filter("JSON", &["json"]);
    }
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        dialog = dialog.set_parent(&window);
    }
    dialog.save_file(move |chosen| {
        let _ = tx.send(chosen);
    });
    let Some(chosen) = rx.await.ok().flatten() else { return Ok(false) };
    let target = chosen.into_path().map_err(|_| AppError::user("无法写入所选位置。"))?;
    tokio::task::spawn_blocking(move || std::fs::write(target, bytes))
        .await
        .map_err(|e| AppError::Internal(e.to_string()))?
        .map_err(|_| AppError::user("无法写入所选位置，请检查权限后重试。"))?;
    Ok(true)
}

#[tauri::command]
async fn confirm_dialog(message: String, app: AppHandle) -> Result<bool> {
    let (tx, rx) = oneshot::channel();
    let mut dialog = app
        .dialog()
        .message(message)
        .title(APP_NAME)
        .kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::OkCancelCustom("确定".into(), "取消".into()));
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        dialog = dialog.parent(&window);
    }
    dialog.show(move |ok| {
        let _ = tx.send(ok);
    });
    Ok(rx.await.unwrap_or(false))
}

/// Outside links (news articles, docs) open in the user's browser, never inside the app.
#[tauri::command]
fn open_external(url: String, app: AppHandle) -> Result<()> {
    let parsed = Url::parse(&url).map_err(|_| AppError::user("链接无效。"))?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return Err(AppError::user("只能打开网页链接。"));
    }
    app.opener().open_url(parsed.as_str(), None::<&str>).map_err(|e| AppError::Internal(e.to_string()))
}

#[tauri::command]
fn socket_open(path: String, on_event: Channel<SocketEvent>, state: State<'_, AppState>) -> Result<u32> {
    state.sockets.open(&state.backend, &path, on_event)
}

#[tauri::command]
fn socket_send(id: u32, text: String, state: State<'_, AppState>) -> Result<()> {
    state.sockets.send(id, text)
}

#[tauri::command]
fn socket_close(id: u32, state: State<'_, AppState>) {
    state.sockets.close(id);
}

#[tauri::command]
async fn wechat_login(app: AppHandle, state: State<'_, AppState>) -> Result<()> {
    wechat::sign_in(&app, &state.backend).await
}

// ------------------------------------------------------------------ app

/// The main window only ever shows the bundled pages. Under `tauri dev` those pages are served by the
/// CLI's dev server (`dev_origin`, e.g. `http://127.0.0.1:1430`); release builds pass `None`.
fn is_app_page(url: &Url, dev_origin: Option<&Url>) -> bool {
    if dev_origin.is_some_and(|dev| dev.origin() == url.origin()) {
        return true;
    }
    match url.scheme() {
        "tauri" => url.host_str() == Some("localhost"),
        "http" | "https" => url.host_str() == Some("tauri.localhost"),
        _ => false,
    }
}

fn focus_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

pub fn run() {
    let log = tauri_plugin_log::Builder::new()
        .level(if cfg!(debug_assertions) { log::LevelFilter::Debug } else { log::LevelFilter::Info })
        .level_for("tungstenite", log::LevelFilter::Warn)
        .level_for("tokio_tungstenite", log::LevelFilter::Warn)
        .level_for("hyper_util", log::LevelFilter::Warn)
        .level_for("reqwest", log::LevelFilter::Warn)
        .max_file_size(2_000_000)
        .rotation_strategy(tauri_plugin_log::RotationStrategy::KeepOne)
        .targets([
            tauri_plugin_log::Target::new(tauri_plugin_log::TargetKind::Stdout),
            tauri_plugin_log::Target::new(tauri_plugin_log::TargetKind::LogDir { file_name: None }),
        ])
        .build();

    tauri::Builder::default()
        // Must be first: a second launch focuses the running app instead of starting another.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| focus_main(app)))
        .plugin(log)
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_window_state::Builder::default().with_denylist(&[wechat::WINDOW]).build())
        .setup(|app| {
            let config_dir = app.path().app_config_dir()?;
            let settings = Settings::load(&config_dir);
            let backend = Backend::new(settings.server.clone(), Arc::new(Keychain))?;
            log::info!("{APP_NAME} {} starting; server {}", env!("CARGO_PKG_VERSION"), settings.server);
            app.manage(AppState { settings: Mutex::new(settings), backend, sockets: Sockets::default() });

            let dev_origin = if tauri::is_dev() { app.config().build.dev_url.clone() } else { None };
            WebviewWindowBuilder::new(app, MAIN_WINDOW, WebviewUrl::App("index.html".into()))
                .title(APP_NAME)
                .inner_size(1360.0, 860.0)
                .min_inner_size(960.0, 640.0)
                .center()
                .on_navigation(move |url| {
                    let allowed = is_app_page(url, dev_origin.as_ref());
                    if !allowed {
                        log::warn!("blocked navigation to {}", url.origin().ascii_serialization());
                    }
                    allowed
                })
                .build()?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            server_get,
            server_set,
            session_boot,
            api_request,
            download_file,
            confirm_dialog,
            open_external,
            socket_open,
            socket_send,
            socket_close,
            wechat_login,
        ])
        .run(tauri::generate_context!())
        .expect("error while running the ONTO Research desktop app");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn main_window_stays_on_bundled_pages() {
        let page = |url: &str| is_app_page(&url.parse().unwrap(), None);
        assert!(page("tauri://localhost/home.html"));
        assert!(page("http://tauri.localhost/workspace.html?project=a"));
        assert!(!page("https://research.example/"));
        assert!(!page("tauri://evil/home.html"));
        assert!(!page("file:///etc/passwd"));
        assert!(!page("http://127.0.0.1:1430/index.html"));
    }

    #[test]
    fn dev_builds_also_allow_the_cli_dev_server() {
        let dev: Url = "http://127.0.0.1:1430".parse().unwrap();
        let page = |url: &str| is_app_page(&url.parse().unwrap(), Some(&dev));
        assert!(page("http://127.0.0.1:1430/home.html?view=settings"));
        assert!(page("tauri://localhost/home.html"));
        assert!(!page("http://127.0.0.1:8766/"));
        assert!(!page("http://127.0.0.1:1431/index.html"));
    }
}
