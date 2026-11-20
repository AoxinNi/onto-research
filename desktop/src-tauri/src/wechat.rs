//! WeChat sign-in in a dedicated native window. The window shows WeChat's QR page; when WeChat redirects to
//! the workbench callback, the app stops that navigation, reads `state` and `code`, and completes the sign-in
//! with the challenge it got when it started the flow. The window is a remote page with no IPC access.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tokio::sync::oneshot;
use url::Url;

use crate::backend::Backend;
use crate::error::{AppError, Result};

pub const WINDOW: &str = "wechat-login";
const TIMEOUT: Duration = Duration::from_secs(600);

enum Outcome {
    Callback { state: String, code: Option<String> },
    Closed,
}

fn is_callback(url: &Url, callback: &Url) -> bool {
    url.origin() == callback.origin() && url.path() == callback.path()
}

fn callback_params(url: &Url) -> Outcome {
    let mut state = None;
    let mut code = None;
    for (key, value) in url.query_pairs() {
        match key.as_ref() {
            "state" if state.is_none() => state = Some(value.into_owned()),
            "code" if code.is_none() => code = Some(value.into_owned()),
            _ => {}
        }
    }
    Outcome::Callback { state: state.unwrap_or_default(), code }
}

pub async fn sign_in(app: &AppHandle, backend: &Backend) -> Result<()> {
    let (qr_url, challenge) = backend.wechat_start().await?;
    let callback = backend.wechat_callback()?;
    let qr_url: Url = qr_url.parse().map_err(|_| AppError::user("微信登录地址无效。"))?;

    if let Some(existing) = app.get_webview_window(WINDOW) {
        let _ = existing.destroy();
    }
    let (tx, rx) = oneshot::channel::<Outcome>();
    let tx = Arc::new(Mutex::new(Some(tx)));
    let finish = {
        let tx = Arc::clone(&tx);
        move |outcome: Outcome| {
            if let Some(tx) = tx.lock().unwrap().take() {
                let _ = tx.send(outcome);
            }
        }
    };
    let on_navigate = {
        let finish = finish.clone();
        move |url: &Url| {
            if is_callback(url, &callback) {
                finish(callback_params(url));
                return false;
            }
            url.scheme() == "https"
        }
    };

    let mut builder = WebviewWindowBuilder::new(app, WINDOW, WebviewUrl::External(qr_url))
        .title("微信登录")
        .inner_size(480.0, 600.0)
        .resizable(false)
        .center()
        .incognito(true)
        .on_navigation(on_navigate);
    if let Some(main) = app.get_webview_window("main") {
        builder = builder.parent(&main)?;
    }
    let window = builder.build()?;
    window.on_window_event(move |event| {
        if matches!(event, WindowEvent::Destroyed | WindowEvent::CloseRequested { .. }) {
            finish(Outcome::Closed);
        }
    });

    let outcome = tokio::time::timeout(TIMEOUT, rx).await;
    let _ = window.destroy();
    match outcome {
        Ok(Ok(Outcome::Callback { state, code: Some(code) })) => {
            backend.wechat_complete(&state, &code, &challenge).await
        }
        Ok(Ok(Outcome::Callback { code: None, .. })) => Err(AppError::user("微信授权已取消，可以重新扫码。")),
        Ok(Ok(Outcome::Closed)) | Ok(Err(_)) => Err(AppError::user("微信登录已取消。")),
        Err(_) => Err(AppError::user("扫码超时，请重新开始。")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_workbench_callback_is_intercepted() {
        let callback: Url = "https://research.example/auth/wechat/callback".parse().unwrap();
        assert!(is_callback(
            &"https://research.example/auth/wechat/callback?state=s&code=c".parse().unwrap(),
            &callback
        ));
        assert!(!is_callback(&"https://evil.example/auth/wechat/callback?code=c".parse().unwrap(), &callback));
        assert!(!is_callback(&"https://research.example/auth/wechat/other".parse().unwrap(), &callback));
        match callback_params(&"https://research.example/auth/wechat/callback?state=s&code=c&code=d".parse().unwrap()) {
            Outcome::Callback { state, code } => assert_eq!((state.as_str(), code.as_deref()), ("s", Some("c"))),
            Outcome::Closed => panic!("expected a callback"),
        }
    }
}
