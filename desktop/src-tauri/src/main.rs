// Verdandi desktop shell (D66, Phase 0).
//
// Starts the Python engine sidecar (a PyInstaller bundle of
// desktop/sidecar.py, shipping as Tauri `externalBin` "engine") and kills it
// when the app exits. The UI talks to the sidecar over 127.0.0.1 HTTP and
// shows "engine offline" on its own if the process is missing — the window
// never blocks on the engine.
//
// RESILIENCE (2026-10-09): the spawn used to be one-shot and silent — if the
// sidecar failed (e.g. the hardened-runtime/PyInstaller signature issue that
// shipped in v0.1.3), the app stayed "engine offline" forever with no clue.
// Now every spawn/exit is logged to ~/.verdandi/logs/shell.log and a watchdog
// respawns the engine with backoff if it dies.
//
// DEV: build the sidecar first (`python desktop/build_sidecar.py`) which also
// copies it next to the dev binary, or point VERDANDI_SIDECAR_PATH at
// `python -m desktop.sidecar`.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::{create_dir_all, OpenOptions};
use std::io::Write;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use tauri::webview::DownloadEvent;
use tauri::{Emitter, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

struct Sidecar {
    child: Mutex<Option<Child>>,
    closing: AtomicBool,
}

fn logs_dir() -> std::path::PathBuf {
    let home = std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .unwrap_or_else(|_| ".".into());
    let dir = std::path::Path::new(&home).join(".verdandi").join("logs");
    let _ = create_dir_all(&dir);
    dir
}

fn log_line(message: &str) {
    if let Ok(mut file) = OpenOptions::new()
        .create(true)
        .append(true)
        .open(logs_dir().join("shell.log"))
    {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let _ = writeln!(file, "[{stamp}] {message}");
    }
    eprintln!("verdandi: {message}");
}

/// Spawn the engine with release-mode hygiene (Windows): no console window
/// and stdio captured into ~/.verdandi/logs/engine.log — the same file the
/// sidecar logs to, so startup crashes leave a trace. Dev builds keep the
/// terminal stdio (tauri dev) and never allocate a console anyway.
fn spawn_engine(path: &std::path::Path) -> Result<Child, String> {
    let mut cmd = Command::new(path);
    if !cfg!(debug_assertions) {
        if let Ok(log) = OpenOptions::new()
            .create(true)
            .append(true)
            .open(logs_dir().join("engine.log"))
        {
            if let Ok(stdout) = log.try_clone() {
                cmd.stdout(Stdio::from(stdout));
            }
            cmd.stderr(Stdio::from(log));
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
    }
    cmd.spawn().map_err(|e| format!("spawn {}: {e}", path.display()))
}

fn spawn_sidecar() -> Result<Child, String> {
    if let Ok(path) = std::env::var("VERDANDI_SIDECAR_PATH") {
        return spawn_engine(std::path::Path::new(&path));
    }
    let exe = std::env::current_exe().map_err(|e| format!("current_exe: {e}"))?;
    let dir = exe.parent().ok_or("no executable directory")?;
    for name in ["engine", "engine.exe"] {
        let candidate = dir.join(name);
        if candidate.exists() {
            return spawn_engine(&candidate);
        }
    }
    Err(format!("engine sidecar not found next to {}", dir.display()))
}

/// Respawn a dead engine with backoff; stops when the app is exiting.
fn watch_sidecar(app: tauri::AppHandle) {
    std::thread::spawn(move || {
        let mut delay = 2u64;
        loop {
            std::thread::sleep(Duration::from_secs(5));
            let Some(state) = app.try_state::<Sidecar>() else {
                continue;
            };
            if state.closing.load(Ordering::Relaxed) {
                return;
            }
            let mut guard = match state.child.lock() {
                Ok(g) => g,
                Err(_) => continue,
            };
            if let Some(child) = guard.as_mut() {
                match child.try_wait() {
                    Ok(Some(status)) => {
                        log_line(&format!("engine exited ({status}) — will restart"));
                        *guard = None;
                    }
                    Ok(None) => {
                        delay = 2; // healthy: reset the backoff
                        continue;
                    }
                    Err(e) => {
                        log_line(&format!("engine wait failed: {e}"));
                        *guard = None;
                    }
                }
            }
            match spawn_sidecar() {
                Ok(child) => {
                    log_line(&format!("engine started (pid {})", child.id()));
                    *guard = Some(child);
                }
                Err(e) => {
                    log_line(&format!("engine start failed: {e} (retry in {delay}s)"));
                    drop(guard);
                    std::thread::sleep(Duration::from_secs(delay));
                    delay = (delay * 2).min(60);
                }
            }
        }
    });
}

fn main() {
    tauri::Builder::default()
        // FIRST plugin (required by single-instance): a second launch focuses
        // the existing window instead of starting a rival engine on :8787.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup(|app| {
            let child = match spawn_sidecar() {
                Ok(child) => {
                    log_line(&format!("engine started (pid {})", child.id()));
                    Some(child)
                }
                Err(e) => {
                    log_line(&format!("engine start failed: {e}"));
                    None
                }
            };
            app.manage(Sidecar {
                child: Mutex::new(child),
                closing: AtomicBool::new(false),
            });
            watch_sidecar(app.handle().clone());

            // The main window is built here (not in tauri.conf.json) so the
            // download handler can be attached: on macOS wry CANCELS every
            // download unless a handler is registered — without this, the
            // diagnostics / results / export "Download" buttons did nothing.
            // The handler accepts the platform-suggested destination (macOS:
            // ~/Downloads/<download-attribute name>; Windows: WebView2's path)
            // and tells the UI when the file landed.
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Verdandi")
                .inner_size(1100.0, 780.0)
                .min_inner_size(900.0, 600.0)
                .on_download(|webview, event| {
                    match event {
                        DownloadEvent::Requested { url, destination } => {
                            log_line(&format!(
                                "download: {url} -> {}",
                                destination.display()
                            ));
                        }
                        DownloadEvent::Finished { url, path, success } => {
                            let path = path.map(|p| p.display().to_string());
                            log_line(&format!(
                                "download finished: {url} success={success} path={}",
                                path.as_deref().unwrap_or("-")
                            ));
                            let _ = webview.emit(
                                "download-finished",
                                serde_json::json!({
                                    "success": success,
                                    "path": path,
                                }),
                            );
                        }
                        _ => {}
                    }
                    true
                })
                .build()?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Verdandi")
        .run(|app_handle, event| {
            if matches!(event, RunEvent::ExitRequested { .. } | RunEvent::Exit) {
                if let Some(state) = app_handle.try_state::<Sidecar>() {
                    state.closing.store(true, Ordering::Relaxed);
                    if let Ok(mut guard) = state.child.lock() {
                        if let Some(mut child) = guard.take() {
                            let _ = child.kill();
                        }
                    }
                }
            }
        });
}
