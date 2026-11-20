//! The workbench WebSocket, opened from Rust so the session header never reaches the web view. Each socket is
//! a task bridging the server connection and a Tauri channel to the page.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde::Serialize;
use tauri::ipc::Channel;
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::Message;

use crate::backend::Backend;
use crate::error::{AppError, Result};

const CONNECT_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Clone, Serialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum SocketEvent {
    Open,
    Message { text: String },
    Error { message: String },
    Close { code: u16, reason: String },
}

enum Outgoing {
    Text(String),
    Close,
}

#[derive(Default)]
pub struct Sockets {
    next: AtomicU32,
    open: Arc<Mutex<HashMap<u32, mpsc::UnboundedSender<Outgoing>>>>,
}

impl Sockets {
    pub fn open(&self, backend: &Backend, path: &str, events: Channel<SocketEvent>) -> Result<u32> {
        let request = backend.socket_request(path)?;
        let id = self.next.fetch_add(1, Ordering::Relaxed) + 1;
        let (tx, mut rx) = mpsc::unbounded_channel();
        self.open.lock().unwrap().insert(id, tx);
        let open = Arc::clone(&self.open);

        tauri::async_runtime::spawn(async move {
            let close = |code: u16, reason: &str| SocketEvent::Close { code, reason: reason.to_owned() };
            let connected = tokio::time::timeout(CONNECT_TIMEOUT, tokio_tungstenite::connect_async(request)).await;
            let mut stream = match connected {
                Ok(Ok((stream, _))) => stream,
                Ok(Err(error)) => {
                    log::warn!("socket {id} failed to connect: {error}");
                    let _ = events.send(SocketEvent::Error { message: "无法连接工作台。".into() });
                    let _ = events.send(close(1006, ""));
                    open.lock().unwrap().remove(&id);
                    return;
                }
                Err(_) => {
                    let _ = events.send(SocketEvent::Error { message: "连接工作台超时。".into() });
                    let _ = events.send(close(1006, ""));
                    open.lock().unwrap().remove(&id);
                    return;
                }
            };
            if events.send(SocketEvent::Open).is_err() {
                let _ = stream.close(None).await;
                open.lock().unwrap().remove(&id);
                return;
            }

            let mut closing = false;
            let final_event = loop {
                tokio::select! {
                    incoming = stream.next() => match incoming {
                        Some(Ok(Message::Text(text))) => {
                            if events.send(SocketEvent::Message { text: text.to_string() }).is_err() {
                                let _ = stream.close(None).await;
                                break None;
                            }
                        }
                        Some(Ok(Message::Close(frame))) => {
                            let (code, reason) = frame
                                .map(|f| (u16::from(f.code), f.reason.to_string()))
                                .unwrap_or((u16::from(CloseCode::Normal), String::new()));
                            break Some(close(code, &reason));
                        }
                        Some(Ok(_)) => {} // ping/pong are answered by tungstenite; binary is unused
                        Some(Err(error)) => {
                            log::warn!("socket {id} error: {error}");
                            break Some(close(1006, ""));
                        }
                        None => break Some(close(1006, "")),
                    },
                    outgoing = rx.recv(), if !closing => match outgoing {
                        Some(Outgoing::Text(text)) => {
                            if stream.send(Message::Text(text.into())).await.is_err() {
                                break Some(close(1006, ""));
                            }
                        }
                        Some(Outgoing::Close) | None => {
                            closing = true;
                            let _ = stream.close(None).await;
                        }
                    },
                }
            };
            open.lock().unwrap().remove(&id);
            if let Some(event) = final_event {
                let _ = events.send(event);
            }
        });
        Ok(id)
    }

    pub fn send(&self, id: u32, text: String) -> Result<()> {
        let open = self.open.lock().unwrap();
        let tx = open.get(&id).ok_or_else(|| AppError::user("连接已断开。"))?;
        tx.send(Outgoing::Text(text)).map_err(|_| AppError::user("连接已断开。"))
    }

    pub fn close(&self, id: u32) {
        if let Some(tx) = self.open.lock().unwrap().get(&id) {
            let _ = tx.send(Outgoing::Close);
        }
    }
}
