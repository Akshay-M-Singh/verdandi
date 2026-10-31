// Free end-to-end check of the Inspect & extract panel (no paid calls).
//
//   VERDANDI_WORKSPACE=/tmp/verdandi-inspect make desktop-service   # terminal 1
//   make desktop-ui                                                 # terminal 2
//   cd desktop && node scripts/inspect_flow.mjs /tmp/inspect-shots [pdf...]
//
// Queues a capture-only job for the given PDFs (default: two benchmark docs),
// waits for capture, then drives the UI: Console selects the run, the Run tab
// shows the workbench with the inspect panel (thumbnails visible, a page pins,
// the extract button label updates, the full-page dialog opens). Exits
// non-zero on failure. Uses the dev loop only — no window, no paid extraction.
import path from "node:path";
import { chromium } from "playwright";

const args = process.argv.slice(2);
const out = args[0] || "/tmp/verdandi-inspect-shots";
const pdfs = args.slice(1);
const docs = (
  pdfs.length
    ? pdfs
    : ["../benchmark/data/pdfs/cord_23.pdf", "../benchmark/data/pdfs/cord_22.pdf"]
).map((p) => path.resolve(p));

const api = "http://127.0.0.1:8787";
const health = await fetch(`${api}/health`).catch(() => null);
if (!health?.ok) {
  console.error("engine not reachable on :8787 — start `make desktop-service`");
  process.exit(2);
}

const created = await fetch(`${api}/jobs`, {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ paths: docs, mode: "capture", doc_id: "broker_invoice" }),
}).then((r) => r.json());
console.log("capture job:", created.id, created.stems.join(", "));

let state = created.state;
for (let i = 0; i < 40 && state !== "done" && state !== "failed"; i++) {
  await new Promise((r) => setTimeout(r, 1500));
  state = (await fetch(`${api}/jobs/${created.id}`).then((r) => r.json())).state;
}
if (state !== "done") {
  console.error("capture job did not finish:", state);
  process.exit(1);
}

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
const errors = [];
page.on("console", (m) => {
  if (m.type() === "error") errors.push("CONSOLE: " + m.text());
});
page.on("pageerror", (e) => errors.push("PAGE: " + e.message));

await page.goto("http://localhost:5173");
await page.waitForTimeout(2000);
const getStarted = page.getByRole("button", { name: "Get started" });
if (await getStarted.count()) {
  await getStarted.click();
  await page.getByRole("button", { name: "Skip for now" }).click();
  await page.getByRole("button", { name: "Start using Verdandi" }).click();
  await page.waitForTimeout(800);
}

const nav = page.getByRole("navigation");
await nav.getByRole("button", { name: "Console", exact: true }).click();
await page.waitForTimeout(1500);
await page.locator("tbody tr").first().click();
// The capture → inspect → pin → extract workflow lives in the Run tab.
await nav.getByRole("button", { name: "Run", exact: true }).click();
await page.getByText("Inspect & extract").waitFor({ timeout: 15000 });
await page.waitForTimeout(1200);

const thumbs = page.locator('img[alt*="page"]');
const thumbCount = await thumbs.count();
await page.screenshot({ path: `${out}/inspect-1-grid.png`, fullPage: true });

await page.locator('button[title^="pin page"]').first().click();
await page.waitForTimeout(400);
const pinned = await page
  .getByRole("button", { name: /Extract selected pages \(1\)/ })
  .count();
await page.screenshot({ path: `${out}/inspect-2-pinned.png`, fullPage: true });

await thumbs.first().click();
await page.waitForTimeout(600);
const dialog = await page.getByText(/— page 1/).count();
await page.screenshot({ path: `${out}/inspect-3-dialog.png` });

const ok = thumbCount > 0 && pinned > 0 && dialog > 0;
console.log(
  JSON.stringify({ thumbCount, pinnedLabel: pinned > 0, dialog: dialog > 0, ok }),
);
if (errors.length) console.log("errors:", errors);
await browser.close();
process.exit(ok ? 0 : 1);
