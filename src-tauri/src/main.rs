#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;
use rand::RngCore;
use tauri::{Emitter, Manager, State};

/// How long to wait for the sidecar's `READY:<port>` line. Generous because the very first start
/// of an installed app is slow (the OS scans the freshly installed libraries, caches get built).
const READY_TIMEOUT: Duration = Duration::from_secs(180);
/// Spawn attempts when the sidecar dies right after start (e.g. the picked port was
/// grabbed by another process between `pick_unused_port` and the server's `bind`).
const MAX_SPAWN_ATTEMPTS: u32 = 3;

/// PID of the running sidecar (0 = none). Kept outside `SidecarState` so the panic
/// hook, which has no access to Tauri state, can still kill the child.
static SIDECAR_PID: AtomicU32 = AtomicU32::new(0);

/// Directory holding the packaged engine (set once at startup from the app's resource dir).
static BUNDLED_ENGINE_DIR: OnceLock<PathBuf> = OnceLock::new();

/// File name of the packaged engine executable.
const ENGINE_EXE: &str = if cfg!(windows) { "xradar-engine.exe" } else { "xradar-engine" };

/// The packaged, self-contained engine (built by `python/packaging/build_engine.py`), when this is
/// an installed app. `None` in development, where the project's Python environment is used.
fn bundled_engine() -> Option<PathBuf> {
    let exe = BUNDLED_ENGINE_DIR.get()?.join(ENGINE_EXE);
    exe.is_file().then_some(exe)
}

fn project_root() -> PathBuf {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest_dir
        .parent()
        .expect("CARGO_MANIFEST_DIR has no parent")
        .to_path_buf()
}

/// Locate the Python interpreter: `$XRADAR_PYTHON`, then the project venv (Unix and
/// Windows layouts), then whatever `python3` / `python` is on `PATH`.
fn find_python() -> PathBuf {
    if let Some(p) = std::env::var_os("XRADAR_PYTHON") {
        if !p.is_empty() {
            return PathBuf::from(p);
        }
    }
    let root = project_root();
    for rel in [
        "python/.venv/bin/python3",
        "python/.venv/bin/python",
        "python/.venv/Scripts/python.exe",
    ] {
        let candidate = root.join(rel);
        if candidate.exists() {
            return candidate;
        }
    }
    PathBuf::from(if cfg!(windows) { "python" } else { "python3" })
}

fn server_script() -> PathBuf {
    project_root().join("python/server.py")
}

struct SidecarState {
    port: Mutex<Option<u16>>,
    process: Mutex<Option<Child>>,
    /// Serialises (re)spawns from the setup thread, the `start_python_sidecar`
    /// command and the monitor. It is held across the blocking wait for READY,
    /// which is why it is separate from the `port` / `process` locks: those are
    /// only ever held briefly, so status queries never block behind a spawn.
    spawn_lock: Mutex<()>,
    /// Set once the app is exiting so nothing restarts the sidecar afterwards.
    shutting_down: AtomicBool,
    token: String,
    /// Why the sidecar could not be started / restarted (None while healthy). The window shows
    /// "Disconnected" either way, so the frontend asks for this to explain it.
    last_error: Mutex<Option<String>>,
}

impl Drop for SidecarState {
    fn drop(&mut self) {
        if let Ok(mut guard) = self.process.lock() {
            if let Some(mut child) = guard.take() {
                terminate_child(&mut child);
            }
        }
    }
}

/// Shared wrapper so the monitor thread can access the same SidecarState
/// that Tauri manages. Tauri's `State<T>` is backed by an Arc internally,
/// but we need our own Arc to hand to the std::thread we spawn.
struct SharedSidecar(Arc<SidecarState>);

impl std::ops::Deref for SharedSidecar {
    type Target = SidecarState;
    fn deref(&self) -> &Self::Target {
        &self.0
    }
}

/// Ask the child to stop (SIGTERM on Unix), then force-kill it if it lingers, and
/// reap it so no zombie is left behind. Dropping a `Child` does NOT kill it.
fn terminate_child(child: &mut Child) {
    if matches!(child.try_wait(), Ok(Some(_))) {
        return;
    }
    #[cfg(unix)]
    {
        let _ = Command::new("kill")
            .arg("-TERM")
            .arg(child.id().to_string())
            .status();
        for _ in 0..30 {
            if matches!(child.try_wait(), Ok(Some(_))) {
                return;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

/// Stop the sidecar for good (app exit). Safe to call more than once.
fn shutdown_sidecar(state: &SidecarState) {
    state.shutting_down.store(true, Ordering::SeqCst);
    // Taking the lock also waits out a monitor check that is in progress.
    let child = state.process.lock().ok().and_then(|mut g| g.take());
    if let Some(mut child) = child {
        terminate_child(&mut child);
    }
    SIDECAR_PID.store(0, Ordering::SeqCst);
}

/// Last-resort kill used by the panic hook (no access to Tauri state there).
fn kill_sidecar_by_pid() {
    let pid = SIDECAR_PID.swap(0, Ordering::SeqCst);
    if pid == 0 {
        return;
    }
    #[cfg(unix)]
    let _ = Command::new("kill").arg("-KILL").arg(pid.to_string()).status();
    #[cfg(windows)]
    let _ = Command::new("taskkill")
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .status();
}

/// A panic on the main thread takes the whole app down without running
/// `RunEvent::Exit`; make sure the Python process does not outlive it.
fn install_panic_hook() {
    let default_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        if std::thread::current().name() == Some("main") {
            kill_sidecar_by_pid();
        }
        default_hook(info);
    }));
}

/// Resolves when the process receives SIGINT/SIGTERM/SIGHUP (Ctrl-C in
/// `tauri dev`, `kill`, terminal closed), so the app can exit through the normal
/// path that also stops the sidecar instead of orphaning it.
#[cfg(unix)]
async fn termination_signal() {
    use tokio::signal::unix::{signal, SignalKind};
    let (Ok(mut term), Ok(mut int), Ok(mut hup)) = (
        signal(SignalKind::terminate()),
        signal(SignalKind::interrupt()),
        signal(SignalKind::hangup()),
    ) else {
        std::future::pending::<()>().await;
        return;
    };
    tokio::select! {
        _ = term.recv() => {}
        _ = int.recv() => {}
        _ = hup.recv() => {}
    }
}

#[cfg(not(unix))]
async fn termination_signal() {
    let _ = tokio::signal::ctrl_c().await;
}

struct SpawnError {
    message: String,
    /// True when trying again with a fresh port may help.
    retryable: bool,
}

impl SpawnError {
    fn fatal(message: String) -> Self {
        Self { message, retryable: false }
    }
    fn retryable(message: String) -> Self {
        Self { message, retryable: true }
    }
}

/// Spawn the Python sidecar and wait (with a real timeout) for its READY line.
///
/// stdout keeps being drained and forwarded to stderr by a background thread for
/// the life of the process: if the read end of the pipe were closed after READY,
/// the sidecar's next `print` would fail with a broken pipe.
fn spawn_sidecar(port: u16, token: &str) -> Result<Child, SpawnError> {
    // Installed app: the packaged engine. Development (or `XRADAR_PYTHON` set): python + server.py.
    let forced_python = std::env::var_os("XRADAR_PYTHON").is_some_and(|p| !p.is_empty());
    let (program, script) = match bundled_engine() {
        Some(exe) if !forced_python => (exe, None),
        _ => (find_python(), Some(server_script())),
    };

    eprintln!(
        "Starting sidecar: {} {} --port {}",
        program.display(),
        script.as_ref().map(|s| s.display().to_string()).unwrap_or_default(),
        port
    );

    let mut command = Command::new(&program);
    if let Some(script) = &script {
        command.arg(script);
    }
    command
        .arg("--port")
        .arg(port.to_string())
        .env("XRADAR_SESSION_TOKEN", token)
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW); // the engine is a console program; no black window
    }
    let mut child = command.spawn().map_err(|e| {
        SpawnError::fatal(format!(
            "Failed to start the analysis engine: {} (program={}, script={})",
            e,
            program.display(),
            script.as_ref().map(|s| s.display().to_string()).unwrap_or_default()
        ))
    })?;

    let stdout = match child.stdout.take() {
        Some(s) => s,
        None => {
            let _ = child.kill();
            let _ = child.wait();
            return Err(SpawnError::fatal("Failed to capture Python stdout".to_string()));
        }
    };

    let (tx, rx) = mpsc::channel::<()>();
    std::thread::spawn(move || {
        let mut announced = false;
        for line in BufReader::new(stdout).lines() {
            match line {
                Ok(text) => {
                    eprintln!("[python stdout] {}", text);
                    if !announced && text.starts_with("READY:") {
                        announced = true;
                        let _ = tx.send(());
                    }
                }
                Err(e) => {
                    eprintln!("Error reading Python stdout: {}", e);
                    break;
                }
            }
        }
        // EOF: dropping `tx` tells a still-waiting spawner that the process went away.
    });

    match rx.recv_timeout(READY_TIMEOUT) {
        Ok(()) => {
            eprintln!("Python sidecar ready on port {}", port);
            Ok(child)
        }
        Err(RecvTimeoutError::Timeout) => {
            let _ = child.kill();
            let _ = child.wait();
            Err(SpawnError::fatal(format!(
                "Python sidecar did not send READY signal within {} seconds; process killed",
                READY_TIMEOUT.as_secs()
            )))
        }
        Err(RecvTimeoutError::Disconnected) => {
            // stdout closed before READY: the process exited (or is about to).
            let _ = child.kill();
            let status = child
                .wait()
                .map(|s| s.to_string())
                .unwrap_or_else(|e| format!("unknown ({})", e));
            Err(SpawnError::retryable(format!(
                "Python sidecar exited prematurely with status: {}",
                status
            )))
        }
    }
}

/// Start the sidecar if it is not running yet and return its port. Idempotent and
/// safe to call concurrently: at most one process is ever started.
fn ensure_sidecar(state: &SidecarState) -> Result<u16, String> {
    let _spawn_guard = state
        .spawn_lock
        .lock()
        .map_err(|e| format!("Failed to lock spawn state: {}", e))?;

    if state.shutting_down.load(Ordering::SeqCst) {
        return Err("Application is shutting down".to_string());
    }
    if let Some(p) = *state
        .port
        .lock()
        .map_err(|e| format!("Failed to lock port state: {}", e))?
    {
        return Ok(p);
    }

    let mut last_error = String::from("Failed to start Python sidecar");
    for attempt in 1..=MAX_SPAWN_ATTEMPTS {
        let port = portpicker::pick_unused_port().ok_or("No free port available")?;
        match spawn_sidecar(port, &state.token) {
            Ok(mut child) => {
                if state.shutting_down.load(Ordering::SeqCst) {
                    terminate_child(&mut child);
                    return Err("Application is shutting down".to_string());
                }
                SIDECAR_PID.store(child.id(), Ordering::SeqCst);
                *state
                    .process
                    .lock()
                    .map_err(|e| format!("Failed to lock process state: {}", e))? = Some(child);
                *state
                    .port
                    .lock()
                    .map_err(|e| format!("Failed to lock port state: {}", e))? = Some(port);
                // Shutdown may have started between the check above and the store.
                if state.shutting_down.load(Ordering::SeqCst) {
                    shutdown_sidecar(state);
                    return Err("Application is shutting down".to_string());
                }
                if let Ok(mut g) = state.last_error.lock() {
                    *g = None;
                }
                return Ok(port);
            }
            Err(e) => {
                eprintln!(
                    "Sidecar start attempt {}/{} failed: {}",
                    attempt, MAX_SPAWN_ATTEMPTS, e.message
                );
                last_error = e.message;
                if !e.retryable {
                    break;
                }
            }
        }
    }
    if let Ok(mut g) = state.last_error.lock() {
        *g = Some(last_error.clone());
    }
    Err(last_error)
}

#[tauri::command]
fn get_sidecar_port(state: State<SharedSidecar>) -> Result<u16, String> {
    state
        .port
        .lock()
        .map_err(|e| format!("Failed to lock port state: {}", e))?
        .ok_or_else(|| "Sidecar port not available yet".to_string())
}

/// The last sidecar start/restart failure, if any (see `SidecarState::last_error`).
#[tauri::command]
fn get_sidecar_error(state: State<SharedSidecar>) -> Option<String> {
    state.last_error.lock().ok().and_then(|g| g.clone())
}

#[tauri::command]
fn get_sidecar_token(state: State<SharedSidecar>) -> Result<String, String> {
    Ok(state.token.clone())
}

/// Starts the sidecar (or returns the port of the running one). Async so waiting
/// for READY never blocks the main/UI thread.
#[tauri::command]
async fn start_python_sidecar(state: State<'_, SharedSidecar>) -> Result<u16, String> {
    let shared = Arc::clone(&state.0);
    tauri::async_runtime::spawn_blocking(move || ensure_sidecar(&shared))
        .await
        .map_err(|e| format!("Sidecar start task failed: {}", e))?
}

#[tauri::command]
fn open_file_dialog(path: String) -> Result<String, String> {
    let path_buf = std::path::PathBuf::from(&path);
    if path_buf.exists() {
        Ok(path)
    } else {
        Err(format!("File not found: {}", path))
    }
}

/// Health check command the frontend can call to verify the sidecar is alive.
/// Returns the port if the process is running, or an error if it has exited or
/// was never started.
#[tauri::command]
fn sidecar_health_check(state: State<SharedSidecar>) -> Result<u16, String> {
    let port = state
        .port
        .lock()
        .map_err(|e| format!("Failed to lock port state: {}", e))?
        .ok_or_else(|| "Sidecar was never started".to_string())?;

    let mut proc_guard = state
        .process
        .lock()
        .map_err(|e| format!("Failed to lock process state: {}", e))?;

    match proc_guard.as_mut() {
        None => Err("Sidecar process handle is missing".to_string()),
        Some(child) => match child.try_wait() {
            Ok(Some(status)) => Err(format!("Sidecar process exited with status: {}", status)),
            Ok(None) => Ok(port), // still running
            Err(e) => Err(format!("Failed to query sidecar process status: {}", e)),
        },
    }
}

fn generate_sidecar_token() -> String {
    let mut bytes = [0_u8; 32];
    rand::thread_rng().fill_bytes(&mut bytes);
    bytes.iter().map(|b| format!("{:02x}", b)).collect()
}

/// Background monitor that checks if the sidecar is still alive every 5 seconds.
/// If the process has exited, it automatically restarts it on the same port.
fn start_sidecar_monitor(state: Arc<SidecarState>) {
    std::thread::spawn(move || {
        loop {
            std::thread::sleep(Duration::from_secs(5));

            if state.shutting_down.load(Ordering::SeqCst) {
                return;
            }

            let port = {
                match state.port.lock() {
                    Ok(guard) => match *guard {
                        Some(p) => p,
                        None => continue, // sidecar was never started, nothing to monitor
                    },
                    Err(e) => {
                        eprintln!("[sidecar-monitor] Failed to lock port state: {}", e);
                        continue;
                    }
                }
            };

            let needs_restart = {
                match state.process.lock() {
                    Ok(mut guard) => match guard.as_mut() {
                        Some(child) => match child.try_wait() {
                            Ok(Some(status)) => {
                                eprintln!(
                                    "[sidecar-monitor] Sidecar exited with status: {}. Restarting...",
                                    status
                                );
                                true
                            }
                            Ok(None) => false, // still running
                            Err(e) => {
                                eprintln!(
                                    "[sidecar-monitor] Error checking process status: {}",
                                    e
                                );
                                false
                            }
                        },
                        // Handle was taken by shutdown_sidecar: nothing to restart.
                        None => false,
                    },
                    Err(e) => {
                        eprintln!("[sidecar-monitor] Failed to lock process state: {}", e);
                        continue;
                    }
                }
            };

            if needs_restart {
                // The (up to 30 s) restart runs without holding the process lock, so
                // health checks stay responsive; the spawn lock prevents racing a
                // concurrent start.
                let _spawn_guard = match state.spawn_lock.lock() {
                    Ok(g) => g,
                    Err(_) => continue,
                };
                if state.shutting_down.load(Ordering::SeqCst) {
                    return;
                }
                match spawn_sidecar(port, &state.token) {
                    Ok(mut child) => {
                        if state.shutting_down.load(Ordering::SeqCst) {
                            terminate_child(&mut child);
                            return;
                        }
                        SIDECAR_PID.store(child.id(), Ordering::SeqCst);
                        if let Ok(mut guard) = state.process.lock() {
                            *guard = Some(child);
                        }
                        if state.shutting_down.load(Ordering::SeqCst) {
                            shutdown_sidecar(&state);
                            return;
                        }
                        if let Ok(mut g) = state.last_error.lock() {
                            *g = None;
                        }
                        eprintln!(
                            "[sidecar-monitor] Sidecar restarted successfully on port {}",
                            port
                        );
                    }
                    Err(e) => {
                        eprintln!("[sidecar-monitor] Failed to restart sidecar: {}", e.message);
                        if let Ok(mut g) = state.last_error.lock() {
                            *g = Some(format!("The analysis engine stopped and could not restart: {}", e.message));
                        }
                    }
                }
            }
        }
    });
}

fn main() {
    install_panic_hook();

    let shared = Arc::new(SidecarState {
        port: Mutex::new(None),
        process: Mutex::new(None),
        spawn_lock: Mutex::new(()),
        shutting_down: AtomicBool::new(false),
        token: generate_sidecar_token(),
        last_error: Mutex::new(None),
    });

    // Clones for the monitor thread (started in setup) and the exit handler
    let monitor_handle = Arc::clone(&shared);
    let exit_handle = Arc::clone(&shared);

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(SharedSidecar(Arc::clone(&shared)))
        .setup(move |app| {
            // Installed app: the packaged engine ships in the resource directory.
            if let Ok(resources) = app.path().resource_dir() {
                let _ = BUNDLED_ENGINE_DIR.set(resources.join("engine"));
            }
            let state_arc = Arc::clone(&app.state::<SharedSidecar>().0);
            let app_handle = app.handle().clone();

            // Delete stale port file from previous runs so the frontend doesn't
            // connect to a dead port while we're spawning the new sidecar.
            let stale_port_file = project_root().join("public/.sidecar-port");
            let _ = std::fs::remove_file(&stale_port_file);

            // Spawn the sidecar on a background thread so the window appears instantly.
            // Emits "sidecar-ready" event to the frontend when the port is available.
            std::thread::spawn(move || match ensure_sidecar(&state_arc) {
                Ok(port) => {
                    eprintln!("Python sidecar started on port {}", port);

                    // Notify the frontend via event — more reliable than polling
                    if let Err(e) = app_handle.emit("sidecar-ready", port) {
                        eprintln!("Failed to emit sidecar-ready event: {}", e);
                    }

                    // Also write port to a file the frontend can fetch via Vite
                    // (development only: an installed app has no source checkout to write into)
                    let port_file = project_root().join("public/.sidecar-port");
                    if port_file.parent().is_some_and(|d| d.is_dir()) {
                        if let Err(e) = std::fs::write(&port_file, port.to_string()) {
                            eprintln!("Failed to write port file: {}", e);
                        } else {
                            eprintln!("Wrote port {} to {}", port, port_file.display());
                        }
                    }
                }
                Err(e) => {
                    eprintln!("Warning: Failed to start Python sidecar: {}", e);
                    // ensure_sidecar stored the reason; also push it to a listening frontend.
                    if let Err(err) = app_handle.emit("sidecar-error", e) {
                        eprintln!("Failed to emit sidecar-error event: {}", err);
                    }
                }
            });

            // Start the background monitor thread
            start_sidecar_monitor(monitor_handle);

            // Ctrl-C / SIGTERM: exit through the normal path so the sidecar is stopped.
            let signal_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                termination_signal().await;
                eprintln!("Termination signal received; shutting down");
                signal_handle.exit(0);
            });

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            start_python_sidecar,
            get_sidecar_port,
            get_sidecar_error,
            get_sidecar_token,
            open_file_dialog,
            sidecar_health_check,
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(move |_app_handle, event| {
        // Window close -> last window gone -> Exit: stop the Python sidecar with it.
        if let tauri::RunEvent::Exit = event {
            shutdown_sidecar(&exit_handle);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_is_64_hex_chars_and_unique() {
        let a = generate_sidecar_token();
        let b = generate_sidecar_token();
        assert_eq!(a.len(), 64);
        assert!(a.chars().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(a, b);
    }

    #[cfg(unix)]
    #[test]
    fn terminate_child_stops_and_reaps_the_process() {
        let mut child = Command::new("sleep").arg("60").spawn().expect("spawn sleep");
        terminate_child(&mut child);
        // already reaped by terminate_child: try_wait reports the cached exit status
        assert!(matches!(child.try_wait(), Ok(Some(_))));
    }

    #[cfg(unix)]
    #[test]
    fn shutdown_sidecar_kills_child_and_blocks_restarts() {
        let state = SidecarState {
            port: Mutex::new(Some(1)),
            process: Mutex::new(Some(Command::new("sleep").arg("60").spawn().expect("spawn"))),
            spawn_lock: Mutex::new(()),
            shutting_down: AtomicBool::new(false),
            token: "t".to_string(),
            last_error: Mutex::new(None),
        };
        shutdown_sidecar(&state);
        assert!(state.process.lock().unwrap().is_none());
        assert!(state.shutting_down.load(Ordering::SeqCst));
        // port is cleared by ensure only when starting; with shutdown set it must refuse
        *state.port.lock().unwrap() = None;
        assert!(ensure_sidecar(&state).is_err());
    }

    /// Needs the project's Python environment, hence ignored by default:
    /// `cargo test -- --ignored`.
    #[test]
    #[ignore]
    fn real_sidecar_starts_and_is_stopped() {
        let state = SidecarState {
            port: Mutex::new(None),
            process: Mutex::new(None),
            spawn_lock: Mutex::new(()),
            shutting_down: AtomicBool::new(false),
            token: "test-token".to_string(),
            last_error: Mutex::new(None),
        };
        let port = ensure_sidecar(&state).expect("sidecar should start");
        assert_eq!(ensure_sidecar(&state), Ok(port)); // idempotent: no second process
        std::thread::sleep(Duration::from_secs(1));
        {
            let mut guard = state.process.lock().unwrap();
            let child = guard.as_mut().expect("child handle");
            assert!(matches!(child.try_wait(), Ok(None)), "sidecar should still run");
        }
        shutdown_sidecar(&state);
        assert!(std::net::TcpStream::connect(("127.0.0.1", port)).is_err());
    }
}
