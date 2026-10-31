# Stage 0 verification: automates the roadmap Stage 0 exit criteria.
# Stage 2 adds extraction (`extract-corpus`) + eval matrix (`eval`).
#
# Runs: every corpus pass is a run under out/runs/<YYYY-MM-DD>/<N>/ (see
# process.py). `make new-run` creates one and points out/latest at it; the
# stage targets operate on out/latest. Snapshot rule: never overwrite a run —
# create a new one.
.PHONY: check new-run ocr-corpus extract-corpus eval eval-verify models bench audit-nonvlm \
	desktop-dev desktop-service desktop-ui desktop-window desktop-check

PYTHON := .venv/bin/python
ARGS ?=  # e.g. make models ARGS=--refresh

LATEST_GUARD := @test -L out/latest || { echo "no out/latest — run: make new-run"; exit 1; }

check:
	@test -x $(PYTHON) || { echo "no .venv — run: python3 -m venv .venv && $(PYTHON) -m pip install -r requirements.txt"; exit 1; }
	$(PYTHON) -m unittest discover -s tests -v
	$(PYTHON) process.py --help >/dev/null
	@for p in .env out/ inbox/ .venv/ invoices/ AGENTS.md node_modules/; do git check-ignore -q $$p || { echo "FAIL: $$p is not gitignored"; exit 1; }; done
	@test -z "$$(git ls-files node_modules)" || { echo "FAIL: node_modules is tracked"; exit 1; }
	@echo "OK: all Stage 0 exit criteria pass"

# Runs: create out/runs/<YYYY-MM-DD>/<N>/ (auto-incremented) + out/latest symlink.
new-run:
	$(PYTHON) process.py --new-run

# Stage 1: run the text-acquisition stage over the whole corpus (routing + OCR).
# VISION_CAP (2026-09-03 cost lever, D30): when > 0, the harness ALSO writes
# pageNNN.render-vision.png — the page render downscaled to that long edge for
# the vision pass only (image tokens scale with dimensions; OCR/grounding keep
# the full-resolution render). A/B on four corpus files: 1536 → −63% prompt
# tokens, matrix unchanged; 1152 → −78% but one token-split read
# (flagged-class). Adoption value pending the full-corpus confirmation run.
# 0 = off (legacy).
VISION_CAP ?= 1536
ocr-corpus:
	$(LATEST_GUARD)
	$(PYTHON) text_layer.py invoices out/latest/raw --custom-words --vision-cap $(VISION_CAP)

# Stage 2: vision-primary extraction over out/latest/raw -> out/latest/json
# (one paid vision call per attachment; also runs the verification stack
# (grounding/arithmetic/format + gated recheck, D30) and writes <run>/flags.csv).
extract-corpus:
	$(LATEST_GUARD)
	$(PYTHON) extract.py out/latest/raw out/latest/json

# Field x file accuracy matrix vs tests/ground_truth.json + source-pointer audit.
eval:
	$(LATEST_GUARD)
	$(PYTHON) eval.py

# Verification layer (2026-09-02): scores the CURRENT flagging decisions
# (flags.csv per run) against tests/verification_truth.json — precision/recall
# of flagging + the phantom/missed gap lists. Phase 2 adds the check stack.
eval-verify:
	$(PYTHON) eval_verification.py

# Non-VLM subsystem audit (2026-09-29): synthetic adversarial fixtures
# (routing / blank / quality / orientation) + corpus replay diff + grounding
# fuzz + the orientation labelling packet. Local, zero LLM calls.
# Output: out/audit/nonvlm-<date>/ (gitignored).
audit-nonvlm:
	$(PYTHON) tools/audit_nonvlm.py

# Model catalog status (D35): live catalog vs models.dev hints vs probed
# `vision_verified` (config/models.yaml). `--refresh` bypasses the TTL caches.
models:
	$(PYTHON) model_catalog.py $(ARGS)

# Extraction latency/payload benchmark (D42) — PAID (one vision call per run,
# no verification). Compare models / render formats / caps on one file.
# e.g. make bench ARGS="--stem <file> --models gpt-5.6-luna,qwen3.8-flash"
bench:
	$(PYTHON) bench.py $(ARGS)

# --- Desktop app (D66) — the app dev loop (Streamlit retired, D67) ------------
# `make desktop-dev` is the equivalent of `make ui`: engine (auto-reload) +
# interface, leave it running, watch changes land at http://localhost:5173.
# One engine at a time: don't run desktop-service AND a built sidecar together.
desktop-dev:
	$(PYTHON) desktop/dev.py

desktop-service:
	$(PYTHON) -m desktop.sidecar --reload

desktop-ui:
	cd desktop && npm run dev

# The real native window (needs Rust). Writes the source-run sidecar launcher
# (no PyInstaller needed for dev) so the window starts its own engine; do NOT
# also run desktop-service at the same time (port 8787).
desktop-window:
	$(PYTHON) desktop/build_sidecar.py --dev-placeholder
	cd desktop && npm run tauri dev

# Quick red/green: offline service tests + interface build + live engine smoke.
desktop-check:
	$(PYTHON) -m unittest tests.test_desktop_service
	cd desktop && npm run build
	$(PYTHON) desktop/smoke.py
