# Windows test pass (D66 Phase 2, Batch 7)

The app has never run on Windows. This checklist is the owner-run pass; CI
(`.github/workflows/desktop.yml`) is the compile gate once a GitHub remote
exists, but real behavior needs a real machine.

## 1. Get the source onto the Windows machine

Transfer the repo (GitHub is intentionally not used yet). Everything tracked
by git is enough; the transfer archive excludes the local-only invoices by
construction:

```
# on the Mac (from the repo root)
git archive --format=zip -o /tmp/verdandi-src.zip HEAD -- . ':(exclude)benchmark'
```

Unzip somewhere like `C:\dev\verdandi`.

## 2. Prerequisites (one-time)

| Tool | Version | Notes |
|---|---|---|
| Python | 3.12+ | python.org installer; tick "Add to PATH" |
| Node.js | 22+ | nodejs.org LTS |
| Rust | stable | `winget install Rustlang.Rustup` then `rustup default stable-msvc` |
| Visual Studio Build Tools | 2022 | "Desktop development with C++" workload (Rust MSVC + PyInstaller) |
| WebView2 | — | preinstalled on Windows 10/11 |

## 3. Setup + tests

```powershell
cd C:\dev\verdandi
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt -r desktop\requirements-desktop.txt -r requirements-crossplatform.txt
.venv\Scripts\python -m unittest tests.test_desktop_service -v
cd desktop
npm install
npm run build
```

## 4. The dev window (fast loop)

```powershell
# terminal A — engine (source)
cd C:\dev\verdandi
.venv\Scripts\python -m desktop.sidecar

# terminal B — window
cd C:\dev\verdandi\desktop
npm run tauri dev
```

Expected: window opens, sidebar shows "engine ready". (The Apple Vision OCR
engine is macOS-only — on Windows capture uses pypdfium2 + RapidOCR, whose
models download to `out\cache\rapidocr` on first use.)

## 5. Release build (installer)

```powershell
cd C:\dev\verdandi
.venv\Scripts\python desktop\build_sidecar.py        # PyInstaller sidecar
cd desktop
npx tauri build                                       # NSIS + MSI under src-tauri\target\release\bundle
```

The installer is unsigned — Windows SmartScreen will warn ("unknown
publisher"); that is expected until the signing certificate lands.

## 6. Smoke checklist (tick each)

- [ ] First launch shows the onboarding wizard; key saves (Windows Credential
      Manager via `keyring`) and Settings shows "key set"
- [ ] Run tab: choose a PDF (native picker), **Capture only** → run finishes,
      page thumbnails render
- [ ] Inspect files (catalog) → document cards + pages appear
- [ ] Run extraction (paid, small) → Results shows fields/line items; clicking
      a field highlights it on the page (overlay boxes)
- [ ] Review: flags list, evidence crop renders, Confirm writes through
      (status chip changes; "Apply reviews to JSON" reports entries)
- [ ] Export: build workbook + download opens a valid .xlsx
- [ ] Cancel a running job → state becomes "cancelled"; Retry works
- [ ] View log shows worker output; Open folder opens the run directory
- [ ] Export diagnostics downloads a zip (info.json + logs only — no documents)
- [ ] Runs list survives an app restart (disk-backed history)

## 7. Report back

Note anything that breaks with: the screen, what you did, the error text, and
the diagnostics zip (it contains worker logs, no documents). Known things to
watch: path separators in custom flows, PyInstaller collecting onnxruntime
DLLs, RapidOCR model download behind a proxy, and window scaling.
