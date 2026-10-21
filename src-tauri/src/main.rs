#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, Manager, State};

mod oauth;
mod updates;

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

/// The built frontend as plain files (`<resources>/frontend`), shipped by the installers so the
/// engine can draw movies (`rxd animate`, Movie Studio) with the app's own renderer in Chrome.
static BUNDLED_FRONTEND_DIR: OnceLock<PathBuf> = OnceLock::new();

/// The shipped frontend folder, when this is an installed app that has one.
fn bundled_frontend() -> Option<PathBuf> {
    let dir = BUNDLED_FRONTEND_DIR.get()?;
    dir.join("index.html").is_file().then(|| dir.clone())
}

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

/// File types the Share bar offers (src/lib/utils/share.ts): only these can be revealed.
const REVEALABLE: [&str; 8] = ["png", "jpg", "jpeg", "gif", "webp", "mp4", "mov", "webm"];

/// An existing regular file with an absolute path and one of the share extensions. The command is
/// reachable from the web view, so it must not become "open any path with the file manager".
fn revealable(p: &Path) -> bool {
    p.is_absolute()
        && p.is_file()
        && p.extension()
            .and_then(|e| e.to_str())
            .is_some_and(|e| REVEALABLE.contains(&e.to_ascii_lowercase().as_str()))
}

/// Shows a saved file in the system file manager (Finder / Explorer / the default file manager).
#[tauri::command]
fn reveal_in_folder(path: String) -> Result<(), String> {
    let p = PathBuf::from(&path);
    if !revealable(&p) {
        return Err("only a saved image or movie can be shown".into());
    }
    #[cfg(target_os = "macos")]
    let status = Command::new("open").arg("-R").arg(&p).status();
    #[cfg(target_os = "windows")]
    let status = Command::new("explorer").arg(format!("/select,{}", p.display())).status();
    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    let status = Command::new("xdg-open").arg(p.parent().unwrap_or(&p)).status();
    // Explorer reports 1 even on success, so only a failure to start is an error.
    status.map(|_| ()).map_err(|e| e.to_string())
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
    /// When the running sidecar was started (used to tell a crash loop from an ordinary crash).
    started: Mutex<Option<Instant>>,
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
        // the packaged engine has no package.json: it stamps this version into every file it writes
        .env("XRADAR_DESKTOP_VERSION", env!("CARGO_PKG_VERSION"))
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    // the renderer files for movies; a value set by the user wins
    if std::env::var_os("RADARX_APP_DIST").is_none() {
        if let Some(dist) = bundled_frontend() {
            command.env("RADARX_APP_DIST", dist);
        }
    }
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
                if let Ok(mut g) = state.started.lock() {
                    *g = Some(Instant::now());
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
    getrandom::fill(&mut bytes).expect("the operating system has no random number source");
    bytes.iter().map(|b| format!("{:02x}", b)).collect()
}

/// Plain-words description of how the engine process ended ("crashed (segmentation fault)").
fn describe_exit(status: &std::process::ExitStatus) -> String {
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        if let Some(sig) = status.signal() {
            let name = match sig {
                11 => "segmentation fault",
                10 | 7 => "bus error",
                6 => "aborted",
                9 => "killed",
                15 => "terminated",
                4 => "illegal instruction",
                8 => "arithmetic error",
                _ => "signal",
            };
            return format!("crashed ({}, signal {})", name, sig);
        }
    }
    match status.code() {
        Some(0) => "exited normally".to_string(),
        Some(code) => format!("exited with an error (code {})", code),
        None => format!("ended ({})", status),
    }
}

/// How often the monitor looks at the sidecar process.
const MONITOR_INTERVAL: Duration = Duration::from_secs(5);

/// An engine that dies within this long of being started counts as crash-looping (as opposed to
/// one that ran fine and then hit a bad file).
const CRASH_LOOP_WINDOW: Duration = Duration::from_secs(20);

/// Pause before the next restart attempt after `failures` consecutive failed or too-quick-to-die
/// starts: none for the first, then 5, 10, 20, 40 and never more than 60 seconds. Without it a
/// broken installation would be re-launched every 5 s forever.
fn restart_delay(failures: u32) -> Duration {
    if failures == 0 {
        return Duration::ZERO;
    }
    Duration::from_secs((5_u64 << (failures - 1).min(4)).min(60))
}

/// What the monitor remembers between two passes.
struct MonitorMemory {
    /// Consecutive restarts that failed or that did not survive `crash_window`.
    failures: u32,
    /// When the pending restart may be attempted.
    due: Option<Instant>,
    crash_window: Duration,
    /// How the last engine process ended, in plain words (for messages).
    last_exit: String,
}

impl Default for MonitorMemory {
    fn default() -> Self {
        Self { failures: 0, due: None, crash_window: CRASH_LOOP_WINDOW, last_exit: String::new() }
    }
}

#[derive(Debug, PartialEq)]
enum MonitorOutcome {
    /// Nothing to do (the sidecar runs, was never started, or the app is shutting down).
    Healthy,
    /// The sidecar is gone and the next attempt is delayed (backoff).
    Waiting,
    /// A new sidecar is up on this port (which may differ from the old one).
    Restarted(u16),
    /// The restart attempt failed; the message says why.
    Failed(String),
}

/// One supervision pass: if the sidecar process has exited, start a new one. The same port is
/// tried first (the UI reconnects there by itself); if something else took it, any free port is
/// used and the caller announces it. Never holds the process lock while waiting for READY.
fn monitor_step(state: &SidecarState, mem: &mut MonitorMemory) -> MonitorOutcome {
    if state.shutting_down.load(Ordering::SeqCst) {
        return MonitorOutcome::Healthy;
    }
    let port = match state.port.lock() {
        Ok(guard) => match *guard {
            Some(p) => p,
            None => return MonitorOutcome::Healthy, // never started, nothing to monitor
        },
        Err(e) => {
            eprintln!("[sidecar-monitor] Failed to lock port state: {}", e);
            return MonitorOutcome::Healthy;
        }
    };

    let exited = match state.process.lock() {
        Ok(mut guard) => match guard.as_mut() {
            Some(child) => match child.try_wait() {
                Ok(Some(status)) => {
                    if mem.due.is_none() {
                        mem.last_exit = describe_exit(&status);
                        eprintln!(
                            "[sidecar-monitor] The analysis engine stopped: {}. A native crash is \
                             logged in ~/.xradar-desktop/engine-crash.log",
                            describe_exit(&status)
                        );
                    }
                    true
                }
                Ok(None) => false, // still running
                Err(e) => {
                    eprintln!("[sidecar-monitor] Error checking process status: {}", e);
                    false
                }
            },
            // Handle was taken by shutdown_sidecar: nothing to restart.
            None => false,
        },
        Err(e) => {
            eprintln!("[sidecar-monitor] Failed to lock process state: {}", e);
            return MonitorOutcome::Healthy;
        }
    };
    if !exited {
        mem.due = None;
        return MonitorOutcome::Healthy;
    }

    if mem.due.is_none() {
        let quick = state
            .started
            .lock()
            .ok()
            .and_then(|g| *g)
            .is_some_and(|t| t.elapsed() < mem.crash_window);
        mem.failures = if quick { mem.failures + 1 } else { 0 };
        let delay = restart_delay(mem.failures);
        mem.due = Some(Instant::now() + delay);
        if !delay.is_zero() {
            let text = format!(
                "The analysis engine keeps stopping right after it starts (last time it {}); \
                 trying again in {} s.",
                mem.last_exit,
                delay.as_secs()
            );
            eprintln!("[sidecar-monitor] {}", text);
            if let Ok(mut g) = state.last_error.lock() {
                *g = Some(text);
            }
        }
    }
    if mem.due.is_some_and(|due| Instant::now() < due) {
        return MonitorOutcome::Waiting;
    }

    // The (up to minutes long) start runs without the process lock, so health checks stay
    // responsive; the spawn lock keeps it from racing a start requested by the UI.
    let _spawn_guard = match state.spawn_lock.lock() {
        Ok(g) => g,
        Err(_) => return MonitorOutcome::Healthy,
    };
    if state.shutting_down.load(Ordering::SeqCst) {
        return MonitorOutcome::Healthy;
    }
    let mut used_port = port;
    let mut result = spawn_sidecar(port, &state.token);
    if matches!(&result, Err(e) if e.retryable) {
        // Most likely something else took the port while the engine was down.
        if let Some(other) = portpicker::pick_unused_port() {
            used_port = other;
            result = spawn_sidecar(other, &state.token);
        }
    }
    match result {
        Ok(mut child) => {
            if state.shutting_down.load(Ordering::SeqCst) {
                terminate_child(&mut child);
                return MonitorOutcome::Healthy;
            }
            SIDECAR_PID.store(child.id(), Ordering::SeqCst);
            if let Ok(mut guard) = state.process.lock() {
                *guard = Some(child);
            }
            if let Ok(mut g) = state.port.lock() {
                *g = Some(used_port);
            }
            if let Ok(mut g) = state.started.lock() {
                *g = Some(Instant::now());
            }
            if state.shutting_down.load(Ordering::SeqCst) {
                shutdown_sidecar(state);
                return MonitorOutcome::Healthy;
            }
            if let Ok(mut g) = state.last_error.lock() {
                *g = None;
            }
            mem.due = None;
            MonitorOutcome::Restarted(used_port)
        }
        Err(e) => {
            mem.failures += 1;
            mem.due = Some(Instant::now() + restart_delay(mem.failures));
            let text = format!("The analysis engine stopped and could not restart: {}", e.message);
            if let Ok(mut g) = state.last_error.lock() {
                *g = Some(text.clone());
            }
            MonitorOutcome::Failed(text)
        }
    }
}

/// Background monitor: every few seconds, restart the sidecar if it has exited, and tell the UI
/// (`sidecar-ready` with the port, or `sidecar-error` with the reason) so it reconnects at once
/// instead of waiting out its own retry schedule.
fn start_sidecar_monitor(state: Arc<SidecarState>, app: AppHandle) {
    std::thread::spawn(move || {
        let mut mem = MonitorMemory::default();
        loop {
            std::thread::sleep(MONITOR_INTERVAL);
            if state.shutting_down.load(Ordering::SeqCst) {
                return;
            }
            match monitor_step(&state, &mut mem) {
                MonitorOutcome::Restarted(port) => {
                    eprintln!("[sidecar-monitor] Sidecar restarted successfully on port {}", port);
                    if let Err(e) = app.emit("sidecar-ready", port) {
                        eprintln!("Failed to emit sidecar-ready event: {}", e);
                    }
                }
                MonitorOutcome::Failed(text) => {
                    eprintln!("[sidecar-monitor] {}", text);
                    let _ = app.emit("sidecar-error", text);
                }
                MonitorOutcome::Healthy | MonitorOutcome::Waiting => {}
            }
        }
    });
}

/// What the UI needs to know before it offers updates: is the updater configured in this build
/// (public key + endpoint), and can this installation replace itself?
#[tauri::command]
fn updater_status(app: AppHandle) -> updates::UpdaterStatus {
    let exe = std::env::current_exe().unwrap_or_default();
    #[cfg(target_os = "linux")]
    let appimage = app.env().appimage.is_some();
    #[cfg(not(target_os = "linux"))]
    let appimage = false;
    updates::status(
        app.config().plugins.0.get("updater"),
        &app.package_info().version.to_string(),
        &exe,
        appimage,
    )
}

/// Windows only: the NSIS installer replaces files that a running engine would keep locked, and the
/// updater exits the app without running the normal shutdown. So the UI stops the engine just
/// before handing over to the installer. Elsewhere the engine keeps running until the relaunch.
#[tauri::command]
fn stop_engine_for_update(state: State<SharedSidecar>) {
    if cfg!(windows) {
        shutdown_sidecar(&state);
    }
}

/// Restart into the freshly installed version. Goes through the normal exit path, so the engine is
/// stopped (RunEvent::Exit) before the new process starts.
#[tauri::command]
fn relaunch_app(app: AppHandle) {
    app.request_restart();
}

/// Files the OS asked the app to open ("Open with", double click, drop on the Dock icon, command
/// line) before the window's UI was ready. The UI drains them once at startup; after that they are
/// delivered as `open-files` events.
struct PendingOpen {
    files: Mutex<Vec<String>>,
    ui_ready: AtomicBool,
}

/// Existing files among command-line arguments (flags such as `-psn_0_1234` are ignored).
fn paths_from_args<I: IntoIterator<Item = String>>(args: I) -> Vec<String> {
    args.into_iter()
        .filter(|a| !a.starts_with('-'))
        .filter(|a| std::path::Path::new(a).is_file())
        .collect()
}

/// Hand files to the UI: as an event if it is ready, otherwise queued until it asks.
fn deliver_open_files(app: &AppHandle, paths: Vec<String>) {
    if paths.is_empty() {
        return;
    }
    let state = app.state::<PendingOpen>();
    if state.ui_ready.load(Ordering::SeqCst) {
        if let Err(e) = app.emit("open-files", paths) {
            eprintln!("Failed to emit open-files: {}", e);
        }
    } else if let Ok(mut q) = state.files.lock() {
        q.extend(paths);
    }
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

/// Called once by the UI at startup: returns the files queued so far and switches to events.
#[tauri::command]
fn take_pending_open_files(state: State<'_, PendingOpen>) -> Vec<String> {
    state.ui_ready.store(true, Ordering::SeqCst);
    state
        .files
        .lock()
        .map(|mut q| std::mem::take(&mut *q))
        .unwrap_or_default()
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
        started: Mutex::new(None),
    });

    // Clones for the monitor thread (started in setup) and the exit handler
    let monitor_handle = Arc::clone(&shared);
    let exit_handle = Arc::clone(&shared);

    let app = tauri::Builder::default()
        // A second launch (e.g. "Open with" while the app runs) hands its files to the running app.
        .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
            deliver_open_files(app, paths_from_args(args.into_iter().skip(1)));
        }))
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(oauth::OAuthState::default())
        .manage(SharedSidecar(Arc::clone(&shared)))
        .manage(PendingOpen {
            files: Mutex::new(paths_from_args(std::env::args().skip(1))),
            ui_ready: AtomicBool::new(false),
        })
        .setup(move |app| {
            // Installed app: the packaged engine ships in the resource directory.
            if let Ok(resources) = app.path().resource_dir() {
                let _ = BUNDLED_ENGINE_DIR.set(resources.join("engine"));
                let _ = BUNDLED_FRONTEND_DIR.set(resources.join("frontend"));
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
            start_sidecar_monitor(monitor_handle, app.handle().clone());

            // Self-update, only when this build carries an updater key and endpoint (see updates.rs).
            if matches!(
                updates::configuration(app.config().plugins.0.get("updater")),
                updates::Configured::Yes
            ) {
                app.handle().plugin(tauri_plugin_updater::Builder::new().build())?;
            }

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
            take_pending_open_files,
            updater_status,
            stop_engine_for_update,
            relaunch_app,
            oauth::oauth_listen_start,
            oauth::oauth_listen_wait,
            oauth::oauth_listen_cancel,
            reveal_in_folder,
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(move |app_handle, event| {
        // macOS: "Open with", double click and drops on the Dock icon arrive as Opened events.
        #[cfg(any(target_os = "macos", target_os = "ios"))]
        if let tauri::RunEvent::Opened { urls } = &event {
            let paths = urls
                .iter()
                .filter_map(|u| u.to_file_path().ok())
                .map(|p| p.to_string_lossy().into_owned())
                .collect();
            deliver_open_files(app_handle, paths);
        }
        #[cfg(not(any(target_os = "macos", target_os = "ios")))]
        let _ = app_handle;
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
    fn only_saved_images_and_movies_can_be_revealed() {
        let dir = std::env::temp_dir().join(format!("radarx-reveal-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let png = dir.join("map.PNG");
        let mp4 = dir.join("film.mp4");
        let txt = dir.join("notes.txt");
        let none = dir.join("README");
        for f in [&png, &mp4, &txt, &none] {
            std::fs::write(f, b"x").unwrap();
        }
        assert!(revealable(&png) && revealable(&mp4));
        assert!(!revealable(&txt), "a text file");
        assert!(!revealable(&none), "no extension");
        assert!(!revealable(&dir), "a folder");
        assert!(!revealable(&dir.join("missing.png")), "missing");
        assert!(!revealable(Path::new("map.png")), "a relative path");
        assert!(!revealable(Path::new("/etc/passwd")), "a system file");
        std::fs::remove_dir_all(&dir).ok();
    }

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
            started: Mutex::new(None),
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
            started: Mutex::new(None),
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

    // ---- supervision, against a stand-in for the engine --------------------------------

    fn fresh_state() -> SidecarState {
        SidecarState {
            port: Mutex::new(None),
            process: Mutex::new(None),
            spawn_lock: Mutex::new(()),
            shutting_down: AtomicBool::new(false),
            token: "t".to_string(),
            last_error: Mutex::new(None),
            started: Mutex::new(None),
        }
    }

    /// The environment variables below are process-wide, so these tests take turns.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    /// A shell script that behaves like the engine's start-up: prints `READY:<port>`, then lives
    /// for `$STUB_LIFETIME` seconds (arguments are `<server.py> --port <port>`).
    #[cfg(unix)]
    fn stub_engine(dir: &std::path::Path) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        let script = dir.join("stub-engine.sh");
        std::fs::write(
            &script,
            "#!/bin/sh\necho \"READY:$3\"\nexec sleep \"${STUB_LIFETIME:-60}\"\n",
        )
        .unwrap();
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
        script
    }

    fn child_pid(state: &SidecarState) -> u32 {
        state.process.lock().unwrap().as_ref().expect("child").id()
    }

    fn kill_and_reap(state: &SidecarState) {
        let mut guard = state.process.lock().unwrap();
        let child = guard.as_mut().expect("child");
        child.kill().unwrap();
        child.wait().unwrap();
    }

    #[test]
    fn restart_delay_grows_and_is_capped() {
        assert_eq!(restart_delay(0), Duration::ZERO);
        let secs: Vec<u64> = (1..=8).map(|n| restart_delay(n).as_secs()).collect();
        assert_eq!(secs, vec![5, 10, 20, 40, 60, 60, 60, 60]);
        assert_eq!(restart_delay(u32::MAX), Duration::from_secs(60));
    }

    #[cfg(unix)]
    #[test]
    fn exit_descriptions_are_plain_words() {
        use std::os::unix::process::ExitStatusExt;
        use std::process::ExitStatus;
        assert_eq!(describe_exit(&ExitStatus::from_raw(11)), "crashed (segmentation fault, signal 11)");
        assert_eq!(describe_exit(&ExitStatus::from_raw(9)), "crashed (killed, signal 9)");
        assert_eq!(describe_exit(&ExitStatus::from_raw(3 << 8)), "exited with an error (code 3)");
        assert_eq!(describe_exit(&ExitStatus::from_raw(0)), "exited normally");
    }

    #[cfg(unix)]
    #[test]
    fn monitor_restarts_a_killed_engine_and_the_ui_can_find_it() {
        let _env = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let dir = std::env::temp_dir().join(format!("xradar-stub-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::env::set_var("XRADAR_PYTHON", stub_engine(&dir));
        std::env::set_var("STUB_LIFETIME", "60");

        let state = fresh_state();
        let port = ensure_sidecar(&state).expect("stub engine should start");
        let first = child_pid(&state);
        let mut mem = MonitorMemory { crash_window: Duration::ZERO, ..Default::default() };
        assert_eq!(monitor_step(&state, &mut mem), MonitorOutcome::Healthy);

        kill_and_reap(&state); // the engine crashes (or is killed from outside)
        assert_eq!(monitor_step(&state, &mut mem), MonitorOutcome::Restarted(port));
        assert_ne!(child_pid(&state), first, "a new process must be running");
        assert_eq!(*state.port.lock().unwrap(), Some(port));
        assert_eq!(*state.last_error.lock().unwrap(), None);
        assert_eq!(mem.failures, 0, "an engine that ran a while is an ordinary crash");
        assert_eq!(monitor_step(&state, &mut mem), MonitorOutcome::Healthy);

        shutdown_sidecar(&state);
        assert_eq!(monitor_step(&state, &mut mem), MonitorOutcome::Healthy);
        std::env::remove_var("XRADAR_PYTHON");
        std::env::remove_var("STUB_LIFETIME");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn monitor_backs_off_when_the_engine_keeps_dying_at_start() {
        let _env = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let dir = std::env::temp_dir().join(format!("xradar-stub2-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::env::set_var("XRADAR_PYTHON", stub_engine(&dir));
        std::env::set_var("STUB_LIFETIME", "0"); // prints READY and exits at once

        let state = fresh_state();
        ensure_sidecar(&state).expect("stub engine should start");
        std::thread::sleep(Duration::from_millis(300));
        let mut mem = MonitorMemory::default();

        let pid = child_pid(&state);
        assert_eq!(monitor_step(&state, &mut mem), MonitorOutcome::Waiting);
        assert_eq!(mem.failures, 1);
        assert_eq!(child_pid(&state), pid, "no new process during the pause");
        let message = state.last_error.lock().unwrap().clone().unwrap_or_default();
        assert!(message.contains("keeps stopping"), "{message}");

        // the pause is over: one more attempt, which dies again, and the pause doubles
        mem.due = Some(Instant::now());
        assert!(matches!(monitor_step(&state, &mut mem), MonitorOutcome::Restarted(_)));
        std::thread::sleep(Duration::from_millis(300));
        assert_eq!(monitor_step(&state, &mut mem), MonitorOutcome::Waiting);
        assert_eq!(mem.failures, 2);
        let wait = mem.due.unwrap().saturating_duration_since(Instant::now());
        assert!(wait > Duration::from_secs(8), "second pause should be ~10 s, was {wait:?}");

        shutdown_sidecar(&state);
        std::env::remove_var("XRADAR_PYTHON");
        std::env::remove_var("STUB_LIFETIME");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn a_failed_restart_reports_why_and_waits_before_trying_again() {
        let _env = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let dir = std::env::temp_dir().join(format!("xradar-stub3-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::env::set_var("XRADAR_PYTHON", stub_engine(&dir));
        std::env::set_var("STUB_LIFETIME", "60");

        let state = fresh_state();
        ensure_sidecar(&state).expect("stub engine should start");
        let mut mem = MonitorMemory { crash_window: Duration::ZERO, ..Default::default() };
        kill_and_reap(&state);
        // the engine can no longer be launched at all (broken install, deleted files)
        std::env::set_var("XRADAR_PYTHON", dir.join("does-not-exist"));

        match monitor_step(&state, &mut mem) {
            MonitorOutcome::Failed(text) => {
                assert!(text.contains("could not restart"), "{text}");
            }
            other => panic!("expected Failed, got {other:?}"),
        }
        assert!(state.last_error.lock().unwrap().is_some());
        assert_eq!(mem.failures, 1);
        assert_eq!(monitor_step(&state, &mut mem), MonitorOutcome::Waiting);

        // the install is repaired: the next attempt after the pause brings the engine back
        std::env::set_var("XRADAR_PYTHON", stub_engine(&dir));
        mem.due = Some(Instant::now());
        assert!(matches!(monitor_step(&state, &mut mem), MonitorOutcome::Restarted(_)));
        assert_eq!(*state.last_error.lock().unwrap(), None);

        shutdown_sidecar(&state);
        std::env::remove_var("XRADAR_PYTHON");
        std::env::remove_var("STUB_LIFETIME");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
