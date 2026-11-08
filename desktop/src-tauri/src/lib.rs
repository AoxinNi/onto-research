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

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::ipc::Channel;
use tauri::{AppHandle, DragDropEvent, Emitter, Manager, State, WebviewUrl, WebviewWindowBuilder, WindowEvent};
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
/// Files larger than this are refused before they are read; the server has its own, usually lower, limit.
const MAX_FILE: u64 = 64 * 1024 * 1024;
const DROP_TTL: Duration = Duration::from_secs(120);

struct AppState {
    settings: Mutex<Settings>,
    backend: Backend,
    sockets: Sockets,
    drops: Drops,
}

/// Files dropped on the window. The page learns only their names and a one-time ticket; it uploads one by
/// asking for it by ticket and index, so it never names a path on disk.
#[derive(Default)]
struct Drops {
    next: Mutex<u64>,
    pending: Mutex<HashMap<String, (Instant, Vec<PathBuf>)>>,
}

impl Drops {
    fn add(&self, paths: Vec<PathBuf>) -> String {
        let mut next = self.next.lock().unwrap();
        *next += 1;
        let ticket = format!("drop-{}-{}", *next, std::process::id());
        let mut pending = self.pending.lock().unwrap();
        pending.retain(|_, (at, _)| at.elapsed() < DROP_TTL);
        pending.insert(ticket.clone(), (Instant::now(), paths));
        ticket
    }

    fn take(&self, ticket: &str, index: usize) -> Option<PathBuf> {
        let mut pending = self.pending.lock().unwrap();
        let (at, paths) = pending.get_mut(ticket)?;
        if at.elapsed() >= DROP_TTL {
            pending.remove(ticket);
            return None;
        }
        // Each file of a drop can be sent once.
        paths.get_mut(index).map(std::mem::take).filter(|p| !p.as_os_str().is_empty())
    }
}

#[derive(Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct DroppedFile {
    index: usize,
    name: String,
}

#[derive(Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct DropPayload {
    ticket: String,
    files: Vec<DroppedFile>,
    x: f64,
    y: f64,
}

#[derive(Serialize, Clone)]
struct HoverPayload {
    x: f64,
    y: f64,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct Uploaded {
    filename: String,
    #[serde(flatten)]
    response: ApiResponse,
}

fn file_name(path: &std::path::Path) -> String {
    path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_else(|| "file".into())
}

async fn read_file(path: PathBuf) -> Result<Vec<u8>> {
    tokio::task::spawn_blocking(move || -> Result<Vec<u8>> {
        let meta = std::fs::metadata(&path).map_err(|_| AppError::user("无法读取这个文件。"))?;
        if !meta.is_file() {
            return Err(AppError::user("只能上传文件，不能上传文件夹。"));
        }
        if meta.len() > MAX_FILE {
            return Err(AppError::user("文件过大。"));
        }
        std::fs::read(&path).map_err(|_| AppError::user("无法读取这个文件。"))
    })
    .await
    .map_err(|e| AppError::Internal(e.to_string()))?
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

/// Let the person pick files, then POST each one's bytes to `path`. Returns one response per file, in order.
#[tauri::command]
async fn upload_file(
    path: String,
    extensions: Vec<String>,
    multiple: bool,
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<Vec<Uploaded>> {
    backend::validate_path(&path, false)?;
    let (tx, rx) = oneshot::channel();
    let mut dialog = app.dialog().file().set_title("选择文件");
    let exts: Vec<&str> =
        extensions.iter().map(String::as_str).filter(|e| e.chars().all(char::is_alphanumeric)).collect();
    if !exts.is_empty() {
        dialog = dialog.add_filter("文件", &exts);
    }
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        dialog = dialog.set_parent(&window);
    }
    if multiple {
        dialog.pick_files(move |chosen| {
            let _ = tx.send(chosen.unwrap_or_default());
        });
    } else {
        dialog.pick_file(move |chosen| {
            let _ = tx.send(chosen.into_iter().collect());
        });
    }
    let chosen = rx.await.unwrap_or_default();
    let mut out = Vec::new();
    for file in chosen {
        let local = file.into_path().map_err(|_| AppError::user("无法读取所选文件。"))?;
        let filename = file_name(&local);
        let bytes = read_file(local).await?;
        let response = state.backend.send_bytes(&path, &filename, bytes).await?;
        out.push(Uploaded { filename, response });
    }
    Ok(out)
}

/// Upload one file of a drop the window received (see `Drops`).
#[tauri::command]
async fn upload_dropped(ticket: String, index: usize, path: String, state: State<'_, AppState>) -> Result<ApiResponse> {
    backend::validate_path(&path, false)?;
    let local = state.drops.take(&ticket, index).ok_or_else(|| AppError::user("拖入的文件已失效，请重新拖入。"))?;
    let filename = file_name(&local);
    let bytes = read_file(local).await?;
    state.backend.send_bytes(&path, &filename, bytes).await
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

/// The main window only ever shows the bundled pages.
fn is_app_page(url: &Url) -> bool {
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
            app.manage(AppState {
                settings: Mutex::new(settings),
                backend,
                sockets: Sockets::default(),
                drops: Drops::default(),
            });

            let window = WebviewWindowBuilder::new(app, MAIN_WINDOW, WebviewUrl::App("index.html".into()))
                .title(APP_NAME)
                .inner_size(1360.0, 860.0)
                .min_inner_size(960.0, 640.0)
                .center()
                .on_navigation(|url| {
                    let allowed = is_app_page(url);
                    if !allowed {
                        log::warn!("blocked navigation to {}", url.origin().ascii_serialization());
                    }
                    allowed
                })
                .build()?;
            // Dropped files: the page gets names, a ticket and the drop point (in CSS pixels), never paths.
            let handle = app.handle().clone();
            window.on_window_event(move |event| {
                let WindowEvent::DragDrop(drop) = event else { return };
                let Some(window) = handle.get_webview_window(MAIN_WINDOW) else { return };
                let scale = window.scale_factor().unwrap_or(1.0);
                match drop {
                    DragDropEvent::Enter { position, .. } | DragDropEvent::Over { position } => {
                        let _ =
                            window.emit("files-hover", HoverPayload { x: position.x / scale, y: position.y / scale });
                    }
                    DragDropEvent::Drop { paths, position } => {
                        let files = paths
                            .iter()
                            .enumerate()
                            .map(|(index, p)| DroppedFile { index, name: file_name(p) })
                            .collect();
                        let ticket = handle.state::<AppState>().drops.add(paths.clone());
                        let payload = DropPayload { ticket, files, x: position.x / scale, y: position.y / scale };
                        let _ = window.emit("files-dropped", payload);
                    }
                    DragDropEvent::Leave => {
                        let _ = window.emit("files-leave", ());
                    }
                    _ => {}
                }
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            server_get,
            server_set,
            session_boot,
            api_request,
            download_file,
            upload_file,
            upload_dropped,
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
        assert!(is_app_page(&"tauri://localhost/home.html".parse().unwrap()));
        assert!(is_app_page(&"http://tauri.localhost/workspace.html?project=a".parse().unwrap()));
        assert!(!is_app_page(&"https://research.example/".parse().unwrap()));
        assert!(!is_app_page(&"tauri://evil/home.html".parse().unwrap()));
        assert!(!is_app_page(&"file:///etc/passwd".parse().unwrap()));
    }

    #[test]
    fn a_drop_ticket_gives_each_file_once() {
        let drops = Drops::default();
        let ticket = drops.add(vec!["/tmp/a.pdf".into(), "/tmp/b.bib".into()]);
        assert_eq!(drops.take(&ticket, 1), Some(PathBuf::from("/tmp/b.bib")));
        assert_eq!(drops.take(&ticket, 1), None);
        assert_eq!(drops.take(&ticket, 2), None);
        assert_eq!(drops.take("drop-0-0", 0), None);
        assert_eq!(drops.take(&ticket, 0), Some(PathBuf::from("/tmp/a.pdf")));
    }
}
