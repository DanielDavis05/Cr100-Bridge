# Ruffle (vendored)

The web UI's controller preview runs the controller's own `MainUI.swf` in
[Ruffle](https://ruffle.rs), dual-licensed MIT / Apache-2.0 (`LICENSE_MIT`, `LICENSE_APACHE`).

- Release: `nightly-2026-11-07` (`0.8.0-nightly.2026.11.7`), the self-hosted web build
  `ruffle-nightly-2026_11_07-web-selfhosted.zip` from
  <https://github.com/ruffle-rs/ruffle/releases/tag/nightly-2026-11-07>.
- Zip SHA-256: `427125b393cf23c3c3b12f4384dcc8457ad10141b4927c9f10703297668d69ee`
  (matches the digest GitHub publishes for the asset).
- Copied unchanged from the zip: `ruffle.js`, both `.wasm` builds (Ruffle picks the MVP build in
  browsers without WebAssembly extensions), `package.json`, `README.md` and the licenses. The
  source map is left out.

| File | SHA-256 |
| --- | --- |
| `ruffle.js` | `3943e356dbad72e7f0141ea96e36ecc0a933bffe51cda2a752915b99f0a8f36b` |
| `ruffle_web_bg.KIXQ5A6C.wasm` | `6d4306572b98abf1aaea74f82139db280fbb0bfb3e1614885195fda5ceb3ec90` |
| `ruffle_web-wasm_mvp_bg.TOQYS3HB.wasm` | `51d976644fee4516a158644e8a6ab365f7cf4a4981c4073e7076a79f4d848586` |

To update: download a newer self-hosted web build, check its digest, replace these files, update
this table and the CSP hashes in `tools/webui.py` if Ruffle's inline styles changed, and re-test the
preview (`docs/features/CONTROLLER-PREVIEW.md`).
