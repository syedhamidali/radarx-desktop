//! One-shot loopback listener that catches the OAuth redirect of "Sign in with Google / Apple /
//! Facebook". The frontend starts it, opens the provider's page in the system browser, and waits.
//!
//! It binds 127.0.0.1 only and waits for ONE request to `/auth/callback` that carries a `code` or an
//! `error` parameter; every other request is answered 404 and ignored, and every connection is served
//! on its own short-lived thread (an idle connection delays nothing). The real redirect gets a small
//! "you can close this tab" page and its query string goes to the frontend, which completes the PKCE
//! exchange itself. It stops after five minutes or on cancel.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
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
///
/// A request counts only when it carries a `code` or an `error` parameter: that is what the provider's
/// redirect has. Anything else (a browser's favicon or preconnect request, another program poking the
/// port, `/auth/callback` with no parameters) is not the answer and must not end the wait.
fn callback_query(request: &str) -> Option<String> {
    let line = request.lines().next()?;
    let mut parts = line.split_whitespace();
    if parts.next()? != "GET" {
        return None;
    }
    let target = parts.next()?;
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    if path != "/auth/callback" {
        return None;
    }
    let has_answer = query
        .split('&')
        .filter_map(|kv| kv.split_once('='))
        .any(|(k, v)| (k == "code" || k == "error") && !v.is_empty());
    has_answer.then(|| query.to_string())
}

/// How long one connection may take to send its request line. Short: an idle connection (a browser
/// "preconnect") must not hold anything up.
const READ_TIMEOUT: Duration = Duration::from_secs(1);
/// At most this many connections are served at once; more are dropped at once.
const MAX_CONNECTIONS: usize = 16;

/// Serves one connection; returns the callback query when this was the real redirect.
fn serve(stream: &mut TcpStream) -> Option<String> {
    stream.set_read_timeout(Some(READ_TIMEOUT)).ok()?;
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

/// Accepts connections until one carries the real redirect, `cancel` is set or `wait` has passed.
/// Every connection is served on its own short-lived thread, so idle or slow ones delay nothing, and
/// a request that is not the redirect is answered 404 and ignored.
fn listen_loop(listener: TcpListener, cancel: Arc<AtomicBool>, wait: Duration) -> Result<String, String> {
    let (tx, rx) = mpsc::channel::<String>();
    let active = Arc::new(AtomicUsize::new(0));
    let deadline = Instant::now() + wait;
    loop {
        if cancel.load(Ordering::SeqCst) {
            return Err("sign-in cancelled".into());
        }
        if let Ok(query) = rx.try_recv() {
            return Ok(query);
        }
        if Instant::now() > deadline {
            return Err("sign-in timed out".into());
        }
        match listener.accept() {
            Ok((mut stream, _)) => {
                if active.load(Ordering::SeqCst) >= MAX_CONNECTIONS {
                    continue; // dropped
                }
                active.fetch_add(1, Ordering::SeqCst);
                let (tx, active) = (tx.clone(), Arc::clone(&active));
                std::thread::spawn(move || {
                    let _ = stream.set_nonblocking(false);
                    if let Some(query) = serve(&mut stream) {
                        let _ = tx.send(query);
                    }
                    active.fetch_sub(1, Ordering::SeqCst);
                });
            }
            Err(_) => std::thread::sleep(Duration::from_millis(20)),
        }
    }
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
        let _ = tx.send(listen_loop(listener, cancel, WAIT));
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
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpStream;

    #[test]
    fn takes_the_query_of_the_callback() {
        assert_eq!(callback_query("GET /auth/callback?code=abc&x=1 HTTP/1.1\r\nHost: a\r\n"), Some("code=abc&x=1".into()));
        assert_eq!(callback_query("GET /auth/callback?state=s&error=access_denied HTTP/1.1\r\n"), Some("state=s&error=access_denied".into()));
    }

    #[test]
    fn ignores_everything_else() {
        assert_eq!(callback_query("GET /favicon.ico HTTP/1.1\r\n"), None);
        assert_eq!(callback_query("POST /auth/callback?code=a HTTP/1.1\r\n"), None);
        assert_eq!(callback_query("GET /auth/callback/extra?code=a HTTP/1.1\r\n"), None);
        assert_eq!(callback_query(""), None);
    }

    #[test]
    fn a_callback_without_an_answer_is_not_the_redirect() {
        // review: any request to the port used to end the listener with an empty result
        assert_eq!(callback_query("GET /auth/callback HTTP/1.1\r\n"), None);
        assert_eq!(callback_query("GET /auth/callback?state=only HTTP/1.1\r\n"), None);
        assert_eq!(callback_query("GET /auth/callback?code= HTTP/1.1\r\n"), None);
        assert_eq!(callback_query("GET /auth/callback?xcode=1 HTTP/1.1\r\n"), None);
    }

    fn get(port: u16, target: &str) -> String {
        let mut s = TcpStream::connect(("127.0.0.1", port)).unwrap();
        s.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
        write!(s, "GET {target} HTTP/1.1\r\nHost: x\r\n\r\n").unwrap();
        let mut out = String::new();
        let _ = s.read_to_string(&mut out);
        out
    }

    fn start(wait: Duration) -> (u16, Arc<AtomicBool>, std::thread::JoinHandle<Result<String, String>>) {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        listener.set_nonblocking(true).unwrap();
        let port = listener.local_addr().unwrap().port();
        let cancel = Arc::new(AtomicBool::new(false));
        let c = Arc::clone(&cancel);
        (port, cancel, std::thread::spawn(move || listen_loop(listener, c, wait)))
    }

    #[test]
    fn idle_connections_do_not_delay_the_real_redirect() {
        // review: three idle connections delayed the callback by 14.8 s (one at a time, 5 s each)
        let (port, _cancel, handle) = start(Duration::from_secs(30));
        let idle: Vec<TcpStream> = (0..3).map(|_| TcpStream::connect(("127.0.0.1", port)).unwrap()).collect();
        std::thread::sleep(Duration::from_millis(100));
        let started = Instant::now();
        let answer = get(port, "/auth/callback?code=REAL");
        assert!(answer.starts_with("HTTP/1.1 200"), "{answer}");
        assert_eq!(handle.join().unwrap(), Ok("code=REAL".to_string()));
        assert!(started.elapsed() < Duration::from_secs(3), "took {:?}", started.elapsed());
        drop(idle);
    }

    #[test]
    fn a_bogus_request_does_not_end_the_wait() {
        // review: the first request to any path used to win and the real redirect then failed
        let (port, _cancel, handle) = start(Duration::from_secs(30));
        assert!(get(port, "/auth/callback").starts_with("HTTP/1.1 404"));
        assert!(get(port, "/favicon.ico").starts_with("HTTP/1.1 404"));
        assert!(get(port, "/auth/callback?state=x").starts_with("HTTP/1.1 404"));
        assert!(!handle.is_finished(), "the listener ended on a request that was not the redirect");
        assert!(get(port, "/auth/callback?code=REAL&state=x").starts_with("HTTP/1.1 200"));
        assert_eq!(handle.join().unwrap(), Ok("code=REAL&state=x".to_string()));
    }

    #[test]
    fn cancel_and_timeout_end_the_wait() {
        let (_port, cancel, handle) = start(Duration::from_secs(30));
        cancel.store(true, Ordering::SeqCst);
        assert_eq!(handle.join().unwrap(), Err("sign-in cancelled".to_string()));
        let (_port, _cancel, handle) = start(Duration::from_millis(150));
        assert_eq!(handle.join().unwrap(), Err("sign-in timed out".to_string()));
    }
}
