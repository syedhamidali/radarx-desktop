//! One-shot loopback listener that catches the OAuth redirect of "Sign in with Google / Apple /
//! Facebook". The frontend starts it, opens the provider's page in the system browser, and waits.
//!
//! It binds 127.0.0.1 only, handles exactly one request to `/auth/callback`, answers with a small
//! "you can close this tab" page and gives the query string (`code=...` or `error=...`) to the
//! frontend, which completes the PKCE exchange itself. It stops after five minutes or on cancel.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const WAIT: Duration = Duration::from_secs(300);

#[derive(Default)]
pub struct OAuthState {
    rx: Mutex<Option<Receiver<Result<String, String>>>>,
    cancel: Mutex<Arc<AtomicBool>>,
}

const PAGE: &str = "<!doctype html><meta charset=utf-8><title>radarx</title>\
<body style=\"font:16px system-ui;background:#070c16;color:#e0ebf7;display:grid;place-items:center;height:100vh;margin:0\">\
<div style=\"text-align:center\"><h2 style=\"color:#2bd9f5\">You are signed in</h2>\
<p>You can close this tab and return to radarx.</p></div></body>";

/// The query string of a `GET /auth/callback?...` request line, or None for any other request.
fn callback_query(request: &str) -> Option<String> {
    let line = request.lines().next()?;
    let mut parts = line.split_whitespace();
    if parts.next()? != "GET" {
        return None;
    }
    let target = parts.next()?;
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    (path == "/auth/callback").then(|| query.to_string())
}

fn serve(stream: &mut TcpStream) -> Option<String> {
    stream.set_read_timeout(Some(Duration::from_secs(5))).ok()?;
    let mut buf = [0u8; 8192];
    let n = stream.read(&mut buf).ok()?;
    let request = String::from_utf8_lossy(&buf[..n]);
    let query = callback_query(&request);
    let (status, body) = if query.is_some() { ("200 OK", PAGE) } else { ("404 Not Found", "") };
    let _ = write!(
        stream,
        "HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    query
}

#[tauri::command]
pub fn oauth_listen_start(state: tauri::State<'_, OAuthState>) -> Result<u16, String> {
    // stop a previous, unfinished attempt, then use a fresh flag for this one
    let mut slot = state.cancel.lock().unwrap();
    slot.store(true, Ordering::SeqCst);
    let cancel = Arc::new(AtomicBool::new(false));
    *slot = Arc::clone(&cancel);
    drop(slot);
    let listener = TcpListener::bind(("127.0.0.1", 0)).map_err(|e| format!("cannot listen: {e}"))?;
    listener.set_nonblocking(true).map_err(|e| e.to_string())?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();
    let (tx, rx) = mpsc::channel();
    *state.rx.lock().unwrap() = Some(rx);
    std::thread::spawn(move || {
        let deadline = Instant::now() + WAIT;
        loop {
            if cancel.load(Ordering::SeqCst) {
                let _ = tx.send(Err("sign-in cancelled".into()));
                return;
            }
            if Instant::now() > deadline {
                let _ = tx.send(Err("sign-in timed out".into()));
                return;
            }
            match listener.accept() {
                Ok((mut stream, _)) => {
                    let _ = stream.set_nonblocking(false);
                    if let Some(q) = serve(&mut stream) {
                        let _ = tx.send(Ok(q));
                        return;
                    }
                }
                Err(_) => std::thread::sleep(Duration::from_millis(100)),
            }
        }
    });
    Ok(port)
}

#[tauri::command]
pub async fn oauth_listen_wait(state: tauri::State<'_, OAuthState>) -> Result<String, String> {
    let rx = state.rx.lock().unwrap().take().ok_or("no sign-in is waiting")?;
    tauri::async_runtime::spawn_blocking(move || rx.recv().map_err(|_| "sign-in stopped".to_string())?)
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
pub fn oauth_listen_cancel(state: tauri::State<'_, OAuthState>) {
    state.cancel.lock().unwrap().store(true, Ordering::SeqCst);
}

#[cfg(test)]
mod tests {
    use super::callback_query;

    #[test]
    fn takes_the_query_of_the_callback() {
        assert_eq!(callback_query("GET /auth/callback?code=abc&x=1 HTTP/1.1\r\nHost: a\r\n"), Some("code=abc&x=1".into()));
        assert_eq!(callback_query("GET /auth/callback HTTP/1.1\r\n"), Some(String::new()));
    }

    #[test]
    fn ignores_everything_else() {
        assert_eq!(callback_query("GET /favicon.ico HTTP/1.1\r\n"), None);
        assert_eq!(callback_query("POST /auth/callback?code=a HTTP/1.1\r\n"), None);
        assert_eq!(callback_query("GET /auth/callback/extra?code=a HTTP/1.1\r\n"), None);
        assert_eq!(callback_query(""), None);
    }
}
