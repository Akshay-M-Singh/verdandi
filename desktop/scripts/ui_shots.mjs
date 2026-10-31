// Visual check helper (D66): screenshot every screen against the dev loop.
//
//   make desktop-dev               # or: engine + vite separately
//   node scripts/ui_shots.mjs /tmp/uishots
//
// Requires the Vite dev server on :5173 and the engine on :8787. Logs console
// and page errors — a blank or broken screen shows up as an error or a bad shot.
import { chromium } from "playwright";

const out = process.argv[2] || "/tmp/verdandi-shots";
const TABS = [
  "Console",
  "Results",
  "Review",
  "Templates",
  "Workflows",
  "Exports",
  "Settings",
];

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
page.on("console", (m) => {
  if (m.type() === "error") console.log("CONSOLE ERROR:", m.text());
});
page.on("pageerror", (e) => console.log("PAGE ERROR:", e.message));
page.on("response", (r) => {
  if (r.status() >= 400) console.log("HTTP", r.status(), r.url());
});

await page.goto("http://localhost:5173");
await page.waitForTimeout(2000);

// First-run wizard blocks interaction — walk through it if present.
const getStarted = page.getByRole("button", { name: "Get started" });
if (await getStarted.count()) {
  await getStarted.click();
  await page.getByRole("button", { name: "Skip for now" }).click();
  await page.getByRole("button", { name: "Start using Verdandi" }).click();
  await page.waitForTimeout(800);
}
await page.screenshot({ path: `${out}/intake.png` });

// Scope nav clicks to the sidebar (screens may have buttons with the same name).
const nav = page.getByRole("navigation");
await nav.getByRole("button", { name: "Console", exact: true }).click();
await page.waitForTimeout(1500);
await page.screenshot({ path: `${out}/runs.png` });

const row = page.locator("tbody tr").first();
if (await row.count()) {
  await row.click();
  await page.waitForTimeout(1800);
  await page.screenshot({ path: `${out}/run-detail.png` });
  // Run tab with the selected run — the workbench (capture/pin/extract).
  await nav.getByRole("button", { name: "Run", exact: true }).click();
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `${out}/run-workbench.png`, fullPage: true });
}

for (const name of TABS.filter((t) => t !== "Console")) {
  await nav.getByRole("button", { name, exact: true }).click();
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `${out}/${name.toLowerCase()}.png` });
  if (name === "Exports") {
    // best effort: click the preview button to capture the rendered preview
    const previewBtn = page.getByRole("button", { name: "Preview on latest run" });
    if (await previewBtn.count()) {
      await previewBtn.click();
      await page.waitForTimeout(1200);
      await page.screenshot({ path: `${out}/exports-preview.png` });
    }
  }
  if (name === "Results") {
    // best effort: click the first extracted-field row to show the page overlay
    const fieldRow = page.locator("main table").nth(1).locator("tbody tr").first();
    if (await fieldRow.count()) {
      await fieldRow.click();
      await page.waitForTimeout(800);
      await page.screenshot({ path: `${out}/results-overlay.png` });
    }
  }
  if (name === "Review") {
    // best effort: select the first flag to show the evidence pane
    const flagRow = page.locator("main table").first().locator("tbody tr").first();
    if (await flagRow.count()) {
      await flagRow.click();
      await page.waitForTimeout(1000);
      await page.screenshot({ path: `${out}/review-detail.png`, fullPage: true });
    }
  }
}

await browser.close();
console.log(`shots -> ${out}`);
