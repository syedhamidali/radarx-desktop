/**
 * Saving the cross-section figure as a PNG.
 *
 * Desktop (Tauri): the native Save dialog picks the path (starting in the folder the Export panel
 * used last) and the sidecar writes the file, like every other export. Plain browser ("web mode"):
 * the browser downloads the image itself.
 */
import { addToast } from '../stores/toasts';
import { wsManager } from './websocket';
import { inTauri } from './env';

const LAST_DIR_KEY = 'xradar.export.lastDir.v1'; // shared with ExportPanel

function lastDir(): string | undefined {
  try {
    return localStorage.getItem(LAST_DIR_KEY) || undefined;
  } catch {
    return undefined;
  }
}

function rememberDir(path: string): void {
  const i = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'));
  if (i <= 0) return;
  try {
    localStorage.setItem(LAST_DIR_KEY, path.slice(0, i));
  } catch {
    /* storage unavailable: ignore */
  }
}

export function canvasToBlob(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('The image could not be created'))), 'image/png');
  });
}

function blobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => {
      const s = String(r.result ?? '');
      resolve(s.slice(s.indexOf(',') + 1));
    };
    r.onerror = () => reject(r.error ?? new Error('read failed'));
    r.readAsDataURL(blob);
  });
}

function saveViaSidecar(path: string, base64: string): Promise<string> {
  return new Promise((resolve, reject) => {
    if (!wsManager.isConnected) {
      reject(new Error('Sidecar not connected'));
      return;
    }
    const cleanup = () => {
      clearTimeout(timer);
      offSaved();
      offErr();
    };
    const offSaved = wsManager.onMessage('file_saved', (m) => {
      cleanup();
      resolve(String(m.path ?? path));
    });
    const offErr = wsManager.onMessage('error', (m) => {
      cleanup();
      reject(new Error(String(m.message ?? 'save failed')));
    });
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error('No response from the sidecar'));
    }, 30_000);
    void wsManager.send({ type: 'save_image_file', path, data_base64: base64, overwrite: true });
  });
}

function downloadBlob(filename: string, blob: Blob): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/** Save `blob` as `<name>.png`. Toasts the outcome; true when saved, false on cancel or failure. */
export async function savePng(name: string, blob: Blob): Promise<boolean> {
  const filename = name.endsWith('.png') ? name : `${name}.png`;
  try {
    if (inTauri()) {
      const { save } = await import('@tauri-apps/plugin-dialog');
      const dir = lastDir();
      const path = await save({
        title: 'Save cross-section image',
        defaultPath: dir ? `${dir}/${filename}` : filename,
        filters: [{ name: 'PNG image', extensions: ['png'] }],
      });
      if (!path) return false;
      rememberDir(path);
      const saved = await saveViaSidecar(path, await blobToBase64(blob));
      addToast('success', `Saved ${saved}`);
      return true;
    }
    downloadBlob(filename, blob);
    addToast('success', `Downloaded ${filename}`);
    return true;
  } catch (err) {
    addToast('error', `Could not save the image: ${err instanceof Error ? err.message : String(err)}`);
    return false;
  }
}
