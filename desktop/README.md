# Verdandi Desktop (D66)

The downloadable application: a **Tauri 2** window (Rust) + a **React/TypeScript**
interface + the existing Python engine, bundled as a local sidecar. It is the
same pipeline the CLI runs — capture (`text_layer.run_batch`) → extraction
(`extract.run_batch`) → verification → flags — with a purpose-built operator
UI. No core module imports anything here (guard test in
`tests/test_desktop_service.py`); the Streamlit UI was retired (D67), and
`ui/lib.py` + `ui/render.py` remain as shared pure helpers.

## Layout

```
desktop/
  sidecar.py              engine service entry (dev + PyInstaller)
  build_sidecar.py        PyInstaller build -> src-tauri/binaries/engine-<triple>
  requirements-desktop.txt
  engine_service/         FastAPI helper (localhost only)
    app.py                routes: /health /defaults /jobs ...
    jobs.py               job queue + disk-derived progress
    runner.py             the engine glue (same run_batch entry points as the CLI)
    workspace.py          ~/.verdandi/runs/<date>/<n>/ layout + input staging
  src/                    React UI (Vite)
  src-tauri/              Tauri shell (spawns/kills the sidecar), icons, config
  .github/workflows/desktop.yml   CI: sidecar + app on macOS arm64 + Windows
```

## Phase 0 scope (current)

- Path intake (paste one PDF path per line) → live per-file progress →
  document list → raw extraction JSON viewer.
- One profile + model from the shipped config (`/defaults`); jobs run
  sequentially in the background; runs land under `~/.verdandi/runs/`.
- `VERDANDI_WORKSPACE` overrides the workspace; `VERDANDI_SIDECAR_PORT`
  overrides the sidecar port (default 8787).

Not yet (Phase 1+): native file/folder pickers (Tauri dialog plugin), the
review screen with evidence crops, Excel export from the app, key storage in
the OS keychain, signing/notarization, auto-updates.

## Two surfaces (why the browser loop asks for paths)

- **Browser dev loop** (`make desktop-dev` / `npm run dev`): a browser tab cannot
  hand the engine real file paths, so intake accepts **pasted paths**; drag &
  drop and the OS pickers are intentionally absent.
- **App window** (`make desktop-window`, or any installed build): **native file
  and folder pickers + drag & drop** are enabled (Tauri dialog plugin).

## Continuous checking (the daily loop)

`make desktop-dev` is the desktop equivalent of `make ui`: leave it running,
watch features land at http://localhost:5173. Interface changes hot-reload on
save; engine changes restart themselves (uvicorn reloader). Ctrl-C stops both.

```sh
# once
.venv/bin/python -m pip install -r requirements.txt -r desktop/requirements-desktop.txt
cd desktop && npm install && cd ..

make desktop-dev            # engine (auto-reload) + interface, one command
make desktop-check          # red/green: tests + UI build + live engine smoke
```

Visual pass (headless browser — screenshots every screen and reports console
errors; needs the dev loop running):

```sh
cd desktop && npm run shots /tmp/uishots
```

The halves, when you want them separately:

```sh
make desktop-service        # engine only (reload) on 127.0.0.1:8787
make desktop-ui             # Vite dev server on http://localhost:5173
make desktop-window         # the native window (needs Rust); starts its own
                            # engine via a source-run launcher (no PyInstaller)
```

Levels of "is it working": browser loop (fast, daily) → native window
(`desktop-window`; it starts its own engine and Vite — don't also run
desktop-service or desktop-dev) → installers via the on-demand CI checkpoint
(`gh workflow run desktop`) or `desktop/build_sidecar.py` + `npx tauri build`
locally.

## Phase 1 progress (operator flow)

- [x] Intake: native file/folder picker (Tauri dialog plugin), drag & drop,
      capture-only (free preview) mode, profile + parallelism controls
- [x] Run detail: live progress, per-file status, errors, retry unfinished files
      (cancel mid-run not supported yet — engine runs to completion)
- [x] Results: documents table + per-document header fields/line items with
      confidence and verification level (multi-document entries included)
- [x] Review: flags list with status chips; evidence crop (D36 located box) or
      full page, located text, claimed-page mismatch callout; confirm/edit or
      mark-issue with write-through to review.json + the run's JSON (D39)
- [x] Export: profile picker (rules/exports.yaml) → build → download .xlsx
- [x] Runs history: disk-backed list (survives restarts; interrupted runs are
      surfaced as failed), click to open
- [x] Settings: profile/model/workers defaults (workspace settings.json) and the
      gateway key in the OS keychain (keyring; env fallback)
- [ ] Native window polish: menu, app icon final art, crash/error reporting
- [ ] Settings: folder defaults (watch folders / default intake), cancel jobs

Phase 0 (done): path intake → live job table → raw JSON viewer; engine service + job
queue; Tauri shell + sidecar build; on-demand CI packaging checkpoint.
Phase 1 engine surface: jobs (disk history, retry, capture mode), evidence crops,
review write-through, export build/download, settings — 22 offline desktop tests.

## Phase 4 progress (owner tools in-app)

- [x] **Batch 1 — Templates editor**: profile list (with workflow-reference
      badges), guided editor (identity/keywords/min-score, fields with
      type/threshold/labels, line-item columns + roles, row rules + document
      rule, table note), duplicate/delete (D52 warnings), expert YAML with
      validate + save. Saves go through the real config validator; packaged
      apps edit the user config layer. Core fix: `ui.lib` config paths now
      resolve at CALL time (D10) instead of import time.
- [x] **Batch 2 — Workflows editor**: workflow list, ordered documents /
      expected lists (order = prompt order), cross-check rule builder
      (template + field pickers with profile-field suggestions), duplicate /
      delete, expert YAML with validate + save. D52 dangling references are
      warnings (surfaced by `validate_workflows` now, like exports); packaged
      apps edit the user config layer.
- [x] **Batch 3 — Export builder**: friendly source catalog (file info,
      workflow documents, line items, cross-checks), per-column cards (header,
      "Data to show", type override, reorder/duplicate/delete), workflow
      seeding, live preview on the newest extracted run with review-status
      colours, profile CRUD + expert YAML. **Core fix:** template columns were
      silently blank for every single-document run (only `documents[]` entries
      were consulted) — `export._found_entries` now binds the file's own
      document when `_meta.document` matches.

## Phase 3 progress (partner-ready release)

- [x] **Batch 1 — run lifecycle & disk**: per-run storage breakdown, "Free up
      space" (prune page renders, keep JSON/flags/reviews/exports), auto-prune
      after export (setting, default on), delete runs (refuses busy ones),
      workspace usage in Settings, evidence falls back to located text after a
      prune
- [x] **Batch 2 — large-batch UX**: pre-run summary (`POST /paths/summary`:
      file count, size, ≈model calls + large-batch hint), run ETA + queue
      position, per-file failure detail (error text from the failed JSON),
      "Results so far" while running, Runs multi-select + bulk delete.
      Live-validated: 100-file capture in 45 s (3 workers), 151 MB of renders,
      prune freed 150.7 MB keeping 11.4 MB of metadata
- [x] **Batch 3 — releases + auto-update**: tag-driven `release.yml` builds
      both platforms with updater-signed artifacts and publishes installers +
      `latest.json` to this repository's Releases; in-app updater (quiet
      check on launch,
      Settings check, install & restart with progress); `scripts/version.mjs`
      keeps the four version carriers in sync; OS signing activates
      automatically when certificate secrets exist. Runbook:
      `RELEASING.md`. Owner action: `RELEASES_TOKEN` secret (fine-grained PAT)
- [x] **Batch 4 — live paid E2E** (2026-10-02): one benchmark PDF through the
      real app path — capture (free) → catalog (paid, detected "invoice p1")
      → pin pages → extract → results (6 fields, 4 line items) → review
      write-through → export workbook → auto-prune. Total spend **$0.0036**
      (8.4k prompt + 1.6k completion tokens, hint rates)

## Phase 2 progress (operator-complete)

- [x] **Batch 0 — UI kit**: Tailwind v4 + shadcn/ui (black & gold theme), sidebar
      shell, all screens migrated, toasts, favicon, `npm run shots` visual pass
- [x] **Batch 1 — user config + model picker**: packaged apps seed
      `~/.verdandi/{config,rules}` from the bundle and the engine reads the user
      copies (dev checkouts keep using the repo configs); Settings model picker
      (live catalog + vision-verified, drift warning, candidates with explicit
      paid verify, probe cost shown), reset-to-defaults, `VERDANDI_USER_CONFIG=1`
      forces the layer for testing
- [x] **Batch 2 — capture → inspect → pin → extract**: catalog sweep (one cheap
      call/file, skippable) with document cards + page thumbnails, pin
      pages/label (D26), `POST /jobs/{id}/extract` re-queues the same capture
      run, flags recomputed after extraction, retry carries pins
- [x] **Batch 3 — multi-profile & workflow runs**: Document set picker (one
      profile / several profiles / workflow bundle with documents + expected +
      cross-check summary), workflow list from rules/workflows.yaml, Results
      shows per-entry documents and a CROSS-CHECKS panel (PASS / FAIL / NOT
      CHECKED + pair values + unbound notes)
- [x] **Batch 4 — Results/Review depth**: page overlay with the D36 located box
      + model claim (click any field/line item to jump, page switcher, legend);
      Review filters (status/file/reason), hide-reviewed, counts, refresh flags
      and apply-all-to-JSON (D39); flags already recomputed after every
      extraction/retry (Batch 2)
- [x] **Batch 5 — worker-process jobs + cancel**: every job runs in its own
      worker process (`sidecar --run-job`) — Cancel kills the process group,
      a worker crash can't take the service down, output lands in
      `<run>/logs/worker.log` (View log dialog); queued cancels are skipped
- [x] **Batch 6 — onboarding + app frame**: first-run wizard (welcome → key
      into the OS keychain → what leaves the machine → finish), About dialog
      (version/model/workspace), Export diagnostics (zip: versions, sanitized
      settings, recent worker logs — never documents), error boundary around
      every screen, "Open folder" via the Tauri opener plugin
- [ ] **Batch 7** — Windows test pass: runbook + 10-point smoke checklist ready
      (`desktop/WINDOWS-TESTING.md`) — awaiting the owner's Windows run

## Release build

```sh
.venv/bin/python desktop/build_sidecar.py       # -> src-tauri/binaries/engine-<triple>
cd desktop && npm run build && npx tauri build  # -> src-tauri/target/release/bundle/
```

CI is **on demand** (owner decision 2026-10-02): dev pushes don't trigger it.
Run the packaging checkpoint when needed — `gh workflow run desktop -R
Akshay-M-Singh/verdandi` — and the cross-platform test matrix with `gh
workflow run check -R Akshay-M-Singh/verdandi`. Both build/upload on
`macos-14` + `windows-latest`. Apple Silicon + Windows x64 only (owner
decision). Releases are tag-driven (`RELEASING.md`).

## Tests

```sh
.venv/bin/python -m unittest tests.test_desktop_service -v
```

12 offline tests: isolation guards (core never imports `desktop/`; `desktop/`
never imports `benchmark/`), workspace/staging rules, job lifecycle with a
fake runner that writes the CLI's artifacts, and the HTTP API via
TestClient. No paid calls, no network.

## Decisions

Owner decisions and the full rationale live in `architecture.md` **D66**:
Apple Silicon + Windows; operator flow first; partners bring their own
gateway key for now (managed relay / other providers later); installers +
auto-updates via this repository's GitHub Releases; signing/notarization
and OS keychain storage are Phase 2. **The original invoices under
`invoices/` are local-only and never committed or pushed** (owner rule,
2026-09-30).
