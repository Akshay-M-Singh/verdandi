# Releasing Verdandi (D66)

Tag-driven: push a `vX.Y.Z` tag and `.github/workflows/release.yml` builds
both platforms with updater-signed artifacts and publishes installers +
`latest.json` to the **public** releases repo
(`Akshay-M-Singh/verdandi-app`). The source repo is public
(`Akshay-M-Singh/verdandi`).

## One-time setup

1. **Updater signing keypair** (done 2026-10-02):
   - Private key + password: `~/.verdandi/keys/updater.key[.password]` —
     **back these up somewhere safe**. Losing them means existing installs
     can never auto-update again (a new key requires a manual reinstall).
   - Repo secrets set: `TAURI_SIGNING_PRIVATE_KEY`,
     `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`.
   - Public key is in `desktop/src-tauri/tauri.conf.json` (`plugins.updater`).
2. **`RELEASES_TOKEN` secret** (owner action): fine-grained PAT with
   `contents: write` on `Akshay-M-Singh/verdandi-app`, added as a
   repository secret on the source repo. Without it the publish job
   fails with a clear message.
3. **Certificates (later)**: add any of these secrets and the release
   workflow activates OS signing/notarization automatically —
   `APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`,
   `APPLE_SIGNING_IDENTITY`, `APPLE_ID`, `APPLE_PASSWORD`, `APPLE_TEAM_ID`,
   `WINDOWS_CERTIFICATE`, `WINDOWS_CERTIFICATE_PASSWORD`.

## Cut a release

```sh
cd desktop
node scripts/version.mjs set 0.2.0      # syncs package.json, tauri.conf.json,
                                        # Cargo.toml, desktop/__init__.py
cd ..
git add -A && git commit -m "Release v0.2.0"
git tag v0.2.0
git push && git push origin v0.2.0      # tag push triggers the release workflow
```

Watch it: `gh run list -R Akshay-M-Singh/verdandi --workflow release`.
Verify: `gh release view v0.2.0 -R Akshay-M-Singh/verdandi-app` — it must
contain the DMG + NSIS installers, the updater artifacts (`*.app.tar.gz`
for macOS, the `-setup.exe` + `.sig` for Windows), and `latest.json`.

**Verify the macOS signature before announcing anything** (v0.1.2 shipped an
unsigned bundle and macOS showed "Verdandi is damaged… move to bin"):

```sh
codesign --verify --deep --strict desktop/src-tauri/target/release/bundle/macos/Verdandi.app
# must print "valid on disk" + "satisfies its Designated Requirement"
ls desktop/src-tauri/target/release/bundle/macos/Verdandi.app/Contents/_CodeSignature/CodeResources
```

The bundle is ad-hoc signed via `bundle.macOS.signingIdentity: "-"`. Ad-hoc
signing still requires the user's one-time Privacy & Security approval; only
Developer ID + notarization removes that dialog.

**And smoke the packaged engine — codesign alone is NOT enough** (v0.1.3
passed every signature check but its engine died at exec: Tauri's hardened
runtime + PyInstaller's extracted Python.framework = "different Team IDs";
`bundle.macOS.hardenedRuntime` is therefore `false`):

```sh
desktop/src-tauri/target/release/bundle/macos/Verdandi.app/Contents/MacOS/engine &
sleep 20 && curl -s http://127.0.0.1:8787/health   # {"status":"ok",...}
kill %1
```

The version in the health reply must match the release.

## Update test (after any release)

1. Install the PREVIOUS version (e.g. the artifact from the prior release).
2. Launch it — the update dialog should offer the new version.
3. Install and restart; Settings → About shows the new engine version.

## Notes

- **Ad-hoc signed (owner decision, 2026-10-09):** `bundle.macOS.signingIdentity: "-"`.
  No Apple Developer account; macOS still asks each user for a one-time
  Privacy & Security approval. The public download page carries the exact
  steps (`verdandi-app/README.md`; source copy
  `legal/RELEASES-README.md`).
- **Legal docs:** the Apache-2.0 LICENSE and the Privacy Policy live in the
  source repo (`LICENSE`, `legal/PRIVACY.md`) and are what the app's About
  dialog links to. `legal/RELEASES-README.md` is a copy for the public
  download page; it is not the source of truth.
- **Both platforms ship:** macOS (Apple Silicon) and Windows (x64) installers
  are built and smoke-tested by `release.yml`; `latest.json` carries both
  targets. Actions minutes are free on a public repository, so the normal
  path is a tag push. If Actions is unavailable, build locally:
  `python desktop/build_sidecar.py` then
  `TAURI_SIGNING_PRIVATE_KEY=$(cat ~/.verdandi/keys/updater.key)
  TAURI_SIGNING_PRIVATE_KEY_PASSWORD=$(cat ~/.verdandi/keys/updater.key.password)
  npx tauri build` (in `desktop/`), stage the DMG/updater artifacts,
  `node desktop/scripts/make_latest_json.mjs`, and `gh release
  create/upload` into `verdandi-app`.
- The updater endpoint is
  `https://github.com/Akshay-M-Singh/verdandi-app/releases/latest/download/latest.json`;
  `desktop/scripts/make_latest_json.mjs` composes the manifest from the
  build artifacts (darwin-aarch64 from `*.app.tar.gz`, windows-x86_64 from
  the `-setup.exe` installer + `.sig`; an older `*.nsis.zip` is also
  accepted).
- Installers are unsigned: macOS Gatekeeper and Windows SmartScreen will
  warn; auto-updates still work.
- Version must match the tag (`release.yml` publishes whatever
  `make_latest_json.mjs` is given from the tag).
- **Backlog (owner-agreed):** Developer ID signing + notarization (removes the
  one-time Privacy & Security / SmartScreen prompts); the `content_mismatch`
  flag-noise fix (doc-level witnessing + currency symbols + honest wording);
  a managed relay / additional model providers (D66).
