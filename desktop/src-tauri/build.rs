fn main() {
    // Declare the app's own commands so each one needs an explicit grant in capabilities/ (deny by default).
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(&[
        "server_get",
        "server_set",
        "session_boot",
        "api_request",
        "download_file",
        "confirm_dialog",
        "open_external",
        "socket_open",
        "socket_send",
        "socket_close",
        "wechat_login",
    ])))
    .expect("failed to run tauri-build");
}
