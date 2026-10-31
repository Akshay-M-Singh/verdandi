## What changed

<!-- One or two sentences. Link the issue if there is one. -->

## Why

<!-- The problem, not just the implementation. If this changes a design
     decision, link the entry in architecture.md §6. -->

## Verification

<!-- Paste the commands you ran and their result. -->

- [ ] `make check`
- [ ] `make desktop-check` (if the desktop app changed)
- [ ] `cd desktop && npm run build` (if TypeScript/UI changed)

## Checklist

- [ ] No real invoices, customer documents or extracted values are included.
- [ ] Tests cover the change (offline; no paid calls).
- [ ] `architecture.md` updated for a design change.
- [ ] `THIRD_PARTY_NOTICES.md` updated for a new dependency.
- [ ] Commits are signed off (`git commit -s`).
