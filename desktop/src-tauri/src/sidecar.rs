//! The bundled local workbench: a Python server (frozen with PyInstaller, see `desktop/sidecar/`) with its own
//! embedded Postgres, so the app works offline on a machine with nothing else installed.
//!
//! It starts only when the app points at the default local address (`http://127.0.0.1:8766`), nothing answers
//! there yet, and the binary is present next to the app's executable (Tauri puts `externalBin` files there).
//! A server someone started by hand (`./start-workbench.sh`) is used as it is. The child is stopped when the
//! app exits; its Postgres stops with it.

use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use url::Url;

/// The sidecar's file name (Tauri strips the target triple when it bundles an `externalBin`).
pub const BINARY: &str = if cfg!(windows) { "onto-workbench.exe" } else { "onto-workbench" };
/// The first start creates the database (initdb), which takes 10–20 seconds on a laptop.
const READY_DEADLINE: Duration = Duration::from_secs(90);

#[derive(Default)]
pub struct Sidecar {
    child: Mutex<Option<Child>>,
    addr: Mutex<Option<SocketAddr>>,
}

/// The loopback address and port of a local `http://` server, or None for anything else.
pub fn local_addr(server: &str) -> Option<SocketAddr> {
    let url = Url::parse(server).ok()?;
    if url.scheme() != "http" {
        return None;
    }
    let ip = match url.host_str()? {
        "127.0.0.1" | "localhost" => [127, 0, 0, 1].into(),
        _ => return None,
    };
    Some(SocketAddr::new(ip, url.port_or_known_default()?))
}

fn answers(addr: &SocketAddr) -> bool {
    TcpStream::connect_timeout(addr, Duration::from_millis(300)).is_ok()
}

/// Where the bundled binary would be: next to the app's own executable.
pub fn binary_path() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let path = exe.parent()?.join(BINARY);
    path.is_file().then_some(path)
}

impl Sidecar {
    /// Start the bundled server for `server` if it is local, not running and bundled; `log_dir` gets its output.
    /// The database and snapshots go to the server's own data folder (`ontology/localdb.py`: on macOS
    /// ~/Library/Application Support/onto-research), so an MCP client on the same machine finds the same data.
    pub fn start(&self, server: &str, log_dir: &Path) {
        let Some(addr) = local_addr(server) else { return };
        if answers(&addr) {
            log::info!("a workbench already answers on {addr}; using it");
            return;
        }
        let Some(binary) = binary_path() else {
            log::info!("no bundled workbench next to the app; start one with ./start-workbench.sh");
            return;
        };
        let _ = std::fs::create_dir_all(log_dir);
        let log = std::fs::File::create(log_dir.join("workbench.log")).ok();
        let mut cmd = Command::new(&binary);
        cmd.env("WORKBENCH_HOST", addr.ip().to_string())
            .env("WORKBENCH_PORT", addr.port().to_string())
            .env("ONTO_LOCAL_DB", "1")
            .env("WORKBENCH_SNAPSHOTS", "1")
            .env_remove("DATABASE_URL")
            .stdin(Stdio::null());
        match log.as_ref().and_then(|f| Some((f.try_clone().ok()?, f.try_clone().ok()?))) {
            Some((out, err)) => cmd.stdout(out).stderr(err),
            None => cmd.stdout(Stdio::null()).stderr(Stdio::null()),
        };
        match cmd.spawn() {
            Ok(child) => {
                log::info!("started the bundled workbench (pid {}) on {addr}", child.id());
                *self.child.lock().unwrap() = Some(child);
                *self.addr.lock().unwrap() = Some(addr);
            }
            Err(e) => log::error!("could not start the bundled workbench: {e}"),
        }
    }

    /// Wait until the started server accepts connections. Returns at once when none was started, and gives up
    /// early if the child exits (the page then shows the connection error).
    pub async fn ready(&self) {
        let Some(addr) = *self.addr.lock().unwrap() else { return };
        let started = Instant::now();
        while started.elapsed() < READY_DEADLINE {
            if answers(&addr) {
                return;
            }
            if let Some(child) = self.child.lock().unwrap().as_mut() {
                if let Ok(Some(status)) = child.try_wait() {
                    log::error!("the bundled workbench exited while starting ({status}); see workbench.log");
                    return;
                }
            }
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
        log::warn!("the bundled workbench did not answer within {READY_DEADLINE:?}");
    }

    /// Stop the child (and so its Postgres). Called when the app exits.
    pub fn stop(&self) {
        if let Some(mut child) = self.child.lock().unwrap().take() {
            terminate(&mut child);
            let deadline = Instant::now() + Duration::from_secs(8);
            while Instant::now() < deadline {
                if matches!(child.try_wait(), Ok(Some(_))) {
                    return;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

/// Ask the server to shut down cleanly (SIGTERM: uvicorn stops, pgserver stops Postgres); kill on Windows.
fn terminate(child: &mut Child) {
    #[cfg(unix)]
    {
        let _ = Command::new("kill").arg("-TERM").arg(child.id().to_string()).status();
    }
    #[cfg(not(unix))]
    {
        let _ = child.kill();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_loopback_http_servers_are_local() {
        assert_eq!(local_addr("http://127.0.0.1:8766"), Some("127.0.0.1:8766".parse().unwrap()));
        assert_eq!(local_addr("http://localhost:9000"), Some("127.0.0.1:9000".parse().unwrap()));
        assert_eq!(local_addr("https://research.example.com"), None);
        assert_eq!(local_addr("http://192.168.1.4:8766"), None);
        assert_eq!(local_addr("not a url"), None);
    }

    #[tokio::test]
    async fn nothing_started_means_nothing_to_wait_for() {
        let sidecar = Sidecar::default();
        sidecar.start("https://research.example.com", Path::new("/tmp"));
        sidecar.ready().await;
        sidecar.stop();
        assert!(sidecar.child.lock().unwrap().is_none());
    }
}
