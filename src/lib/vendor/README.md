# Vendored JavaScript

Third-party code copied into the repository unchanged (no runtime download, no npm dependency), each in its own
folder with its LICENSE. Every folder is listed in `scripts/licenses/static.toml` (kind `vendored`), so it appears
in `THIRD_PARTY_NOTICES.md`; `python scripts/licenses/generate_notices.py --verify` fails when the two drift.
The folder is excluded from `tsconfig.json` (the minified bundles are not type-checked; their `.d.mts` files are).

| Folder | Package | Version | Licence | Source | SHA-256 of the npm tarball |
|---|---|---|---|---|---|
| `mp4-muxer/` | [mp4-muxer](https://github.com/Vanilagy/mp4-muxer) | 5.2.2 | MIT (Vanilagy) | `build/mp4-muxer.mjs`, `build/mp4-muxer.d.ts` (renamed `.d.mts`), `LICENSE` of `mp4-muxer-5.2.2.tgz` | `5f6c7bae9ad005b32f7fab99268326fe767d7da23e0bb03798c9139861317162` |
| `webm-muxer/` | [webm-muxer](https://github.com/Vanilagy/webm-muxer) | 5.1.4 | MIT (Vanilagy) | `build/webm-muxer.mjs`, `build/webm-muxer.d.ts` (renamed `.d.mts`), `LICENSE` of `webm-muxer-5.1.4.tgz` | `36ece4765ce3e044fbf5b70fbf798b399892ff21f6b001348302f1c45e1b3183` |

Used by `src/lib/utils/clipEncoder.ts`: the film of `rxd animate` / the Movie Studio is encoded inside the headless
Chrome that draws the frames (WebCodecs `VideoEncoder` / `AudioEncoder`, the browser's own codecs) and muxed to
MP4 (H.264 + AAC) or, where the browser has no H.264 encoder, to WebM (VP9 + Opus) by these two files. To update
one: `npm pack <name>@<version>`, verify the tarball's SHA-256 and LICENSE, copy the three files, update this
table and `static.toml`, regenerate the notices.
