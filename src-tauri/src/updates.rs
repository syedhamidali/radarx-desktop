//! Self-update support. The check, download, signature verification and install are done by
//! `tauri-plugin-updater`; this module only decides whether updating is configured and possible in
//! this build, and gives the UI the two things the plugin does not: the status, and a clean relaunch.
//!
//! The update endpoint and the public key live in ONE place, `plugins.updater` in `tauri.conf.json`.

use serde::Serialize;
use serde_json::Value;

/// What `plugins.updater.pubkey` holds until the maintainers put the project's real public key in.
/// While it is still this value (or empty) the updater is not started at all, so an unconfigured
/// development or fork build never tries to update itself.
pub const PUBKEY_PLACEHOLDER: &str = "UPDATER_PUBLIC_KEY_NOT_CONFIGURED";

/// Tail of the GitHub endpoint, replaced by the release-page path to build the "releases" link.
const MANIFEST_TAIL: &str = "/releases/latest/download/latest.json";

#[derive(Debug, PartialEq)]
pub enum Configured {
    Yes,
    No(&'static str),
}

/// Is `plugins.updater` (the JSON from tauri.conf.json) complete enough to update from?
pub fn configuration(plugin_config: Option<&Value>) -> Configured {
    let Some(cfg) = plugin_config else {
        return Configured::No("no updater section in the app configuration");
    };
    let pubkey = cfg.get("pubkey").and_then(Value::as_str).unwrap_or("").trim();
    if pubkey.is_empty() || pubkey == PUBKEY_PLACEHOLDER {
        return Configured::No("no updater public key in this build");
    }
    let has_endpoint = cfg
        .get("endpoints")
        .and_then(Value::as_array)
        .is_some_and(|e| e.iter().any(|u| u.as_str().is_some_and(|s| !s.trim().is_empty())));
    if !has_endpoint {
        return Configured::No("no update endpoint in this build");
    }
    Configured::Yes
}

/// `https://github.com/o/r/releases/latest/download/latest.json` -> `https://github.com/o/r/releases/latest`.
/// Only https endpoints that follow the GitHub layout give a link; anything else gives None.
pub fn releases_page(endpoint: &str) -> Option<String> {
    let base = endpoint.strip_suffix(MANIFEST_TAIL)?;
    base.starts_with("https://")
        .then(|| format!("{base}/releases/latest"))
}

/// Can this installation replace itself?
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct InstallMode {
    /// "auto": the app can download, verify and install the update itself.
    /// "manual": it can only tell the user a new version exists (and link to the release page).
    pub mode: &'static str,
    /// Why it is manual / what the user should do.
    pub note: Option<String>,
}

impl InstallMode {
    fn auto() -> Self {
        Self { mode: "auto", note: None }
    }
    fn manual(note: &str) -> Self {
        Self { mode: "manual", note: Some(note.to_string()) }
    }
}

/// `exe` is the running executable, `appimage` the AppImage path when running from one (Linux).
pub fn install_mode(exe: &std::path::Path, appimage: bool) -> InstallMode {
    let _ = (exe, appimage);
    #[cfg(target_os = "linux")]
    if !appimage {
        return InstallMode::manual(
            "This copy was installed from a .deb or .rpm package, which cannot update itself. \
             Install the new package from the release page.",
        );
    }
    #[cfg(target_os = "macos")]
    {
        let p = exe.to_string_lossy();
        if p.starts_with("/Volumes/") || p.contains("/AppTranslocation/") {
            return InstallMode::manual(
                "The app is running from the disk image or a quarantined location. \
                 Drag it to your Applications folder, start it from there, then update.",
            );
        }
    }
    InstallMode::auto()
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdaterStatus {
    pub configured: bool,
    /// Why the updater is off (when `configured` is false).
    pub reason: Option<String>,
    pub current_version: String,
    pub install: InstallMode,
    /// Where to read about / download releases by hand (derived from the endpoint).
    pub releases_url: Option<String>,
    /// Operating system ("macos", "windows", "linux").
    pub os: &'static str,
    /// Platform key to look up in `latest.json` instead of the default one. Set for a Linux
    /// .deb/.rpm install: releases carry no entry for it (the updater cannot build or install those),
    /// so the version check reads the Windows entry, which every release has. Nothing is downloaded
    /// from it: such an install is "manual".
    pub check_target: Option<&'static str>,
}

pub fn status(
    plugin_config: Option<&Value>,
    current_version: &str,
    exe: &std::path::Path,
    appimage: bool,
) -> UpdaterStatus {
    let (configured, reason) = match configuration(plugin_config) {
        Configured::Yes => (true, None),
        Configured::No(why) => (false, Some(why.to_string())),
    };
    let releases_url = plugin_config
        .and_then(|c| c.get("endpoints"))
        .and_then(Value::as_array)
        .and_then(|e| e.iter().filter_map(Value::as_str).find_map(releases_page));
    let install = install_mode(exe, appimage);
    let check_target = (cfg!(target_os = "linux") && !appimage).then_some("windows-x86_64");
    UpdaterStatus {
        configured,
        reason,
        current_version: current_version.to_string(),
        install,
        releases_url,
        os: std::env::consts::OS,
        check_target,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::path::Path;

    fn good() -> Value {
        json!({
            "pubkey": "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IEFCQ0Q=",
            "endpoints": ["https://github.com/o/r/releases/latest/download/latest.json"],
        })
    }

    #[test]
    fn placeholder_missing_or_empty_pubkey_means_not_configured() {
        assert!(matches!(configuration(None), Configured::No(_)));
        let mut c = good();
        c["pubkey"] = json!(PUBKEY_PLACEHOLDER);
        assert!(matches!(configuration(Some(&c)), Configured::No(_)));
        c["pubkey"] = json!("   ");
        assert!(matches!(configuration(Some(&c)), Configured::No(_)));
        c.as_object_mut().unwrap().remove("pubkey");
        assert!(matches!(configuration(Some(&c)), Configured::No(_)));
    }

    #[test]
    fn needs_an_endpoint() {
        let mut c = good();
        c["endpoints"] = json!([]);
        assert!(matches!(configuration(Some(&c)), Configured::No(_)));
        c["endpoints"] = json!([""]);
        assert!(matches!(configuration(Some(&c)), Configured::No(_)));
        assert_eq!(configuration(Some(&good())), Configured::Yes);
    }

    #[test]
    fn releases_page_follows_the_endpoint_so_a_rename_is_one_edit() {
        assert_eq!(
            releases_page("https://github.com/syedhamidali/radarx-desktop/releases/latest/download/latest.json"),
            Some("https://github.com/syedhamidali/radarx-desktop/releases/latest".into())
        );
        assert_eq!(
            releases_page("https://github.com/syedhamidali/radarx-desktop/releases/latest/download/latest.json"),
            Some("https://github.com/syedhamidali/radarx-desktop/releases/latest".into())
        );
        assert_eq!(releases_page("http://127.0.0.1:8000/latest.json"), None);
        assert_eq!(releases_page("http://x/releases/latest/download/latest.json"), None);
    }

    #[test]
    fn status_reports_version_and_link() {
        let s = status(Some(&good()), "1.2.3", Path::new("/Applications/x.app/Contents/MacOS/x"), false);
        assert!(s.configured && s.reason.is_none());
        assert_eq!(s.current_version, "1.2.3");
        assert_eq!(s.releases_url.as_deref(), Some("https://github.com/o/r/releases/latest"));
        let off = status(None, "1.2.3", Path::new("/x"), false);
        assert!(!off.configured && off.reason.is_some());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn a_disk_image_or_translocated_copy_cannot_replace_itself() {
        assert_eq!(install_mode(Path::new("/Applications/x.app/Contents/MacOS/x"), false).mode, "auto");
        assert_eq!(install_mode(Path::new("/Volumes/xradar/x.app/Contents/MacOS/x"), false).mode, "manual");
        assert_eq!(
            install_mode(Path::new("/private/var/folders/AppTranslocation/ABC/d/x.app/Contents/MacOS/x"), false).mode,
            "manual"
        );
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn only_an_appimage_can_update_itself_on_linux() {
        assert_eq!(install_mode(Path::new("/usr/bin/radarx-desktop"), false).mode, "manual");
        assert_eq!(install_mode(Path::new("/tmp/.mount_x/usr/bin/radarx-desktop"), true).mode, "auto");
    }
}
