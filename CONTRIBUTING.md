# Contributing to Verdandi

Thanks for helping improve Verdandi. This project is a local-first document
pipeline; the most useful contributions are bug reports with a minimal
reproduction, verification-stack improvements, and support for new document
layouts (as data, not code).

## Ground rules

- **Never commit real invoices, scans or extracted values.** The `invoices/`
  directory and `tests/*truth.json` files are gitignored for a reason. Use
  synthetic PDFs (`tests/fixtures/pdfbuild.py`) or public datasets.
- **No regex or template field extraction.** Values come from the vision
  model; the deterministic layer only verifies them (see decision D5/D30 in
  [`architecture.md`](architecture.md)). A PR that adds a value-pattern
  extractor will be declined.
- **Config is data.** Document profiles live in `rules/documents.yaml`,
  workflows in `rules/workflows.yaml`, thresholds in
  `config/thresholds.yaml`. Prefer a data change over a code change.
- **Read the decision log.** `architecture.md` §6 records why the pipeline is
  shaped the way it is. Do not silently reverse a decision; propose an
  amendment.

## Development setup

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -r desktop/requirements-desktop.txt   # desktop work

cd desktop && npm install && cd ..
```

Copy `.env.example` to `.env` and add `OPENCODE_API_KEY` if you want to run
paid extraction. The test suite makes no network or paid calls.

## Checks before opening a PR

```sh
make check
make desktop-check
cd desktop && npm run build
```

`make check` runs the unit suite, a CLI smoke test, and asserts that private
data (`.env`, `invoices/`, truth files) stays ignored. `make desktop-check`
runs the offline desktop tests, builds the UI and smokes the engine.

## Contribution license

By submitting a pull request you agree that your contribution is licensed
under the Apache License, Version 2.0 (inbound = outbound). Sign off your
commits:

```sh
git commit -s -m "your message"
```

## Pull requests

- Keep the diff focused; one concern per PR.
- Add or update offline tests for behaviour changes.
- Update `architecture.md` when a design decision changes, and
  `THIRD_PARTY_NOTICES.md` when adding a dependency.
- Describe the problem and the verification you ran. Screenshots help for UI
  changes (`cd desktop && npm run shots /tmp/shots`).

## Reporting bugs

Use the issue templates. Include the app version, OS, the exact command or
screen, and the smallest PDF that reproduces the problem — but never a real
customer document. Redact or synthesize.
