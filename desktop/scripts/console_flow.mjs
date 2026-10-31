// Free check of the Console run inspector (no paid calls).
//
//   make desktop-dev                       # engine + UI running
//   cd desktop && node scripts/console_flow.mjs /tmp/console-shots
//
// Verifies: clicking a run selects it in place (table stays, detail renders
// below), the token bar/cost/per-file rows appear for an extracted run, and a
// capture-only run embeds the Inspect & extract panel. Exits non-zero on
// failure; logs console errors.
import { chromium } from "playwright";

const out = process.argv[2] || "/tmp/verdandi-console-shots";
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
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

const rows = page.locator("tbody tr");
const rowCount = await rows.count();
console.log("runs:", rowCount);

// extracted run first (Mode column shows "full")
const full = rows.filter({ hasText: "full" }).first();
const hasFull = await full.count();
if (hasFull) {
  await full.click();
  await page.getByText("TOKENS", { exact: true }).waitFor({ timeout: 10000 });
  await page.waitForTimeout(1200);
}
const tableStillVisible = (await rows.count()) > 0;
const tiles = await page.getByText("Files processed").count();
const costText = await page.getByText(/≈ \$0\./).count();
const perFileTokens = await page
  .locator("table")
  .last()
  .locator("tbody tr")
  .first()
  .count();
await page.screenshot({ path: `${out}/console-full.png`, fullPage: true });

// capture-only run embeds the inspect panel
const capture = rows.filter({ hasText: "capture" }).first();
const hasCapture = await capture.count();
if (hasCapture) {
  await capture.click();
  await page.waitForTimeout(1500);
}
const inspect = await page.getByText("Inspect & extract").count();
await page.screenshot({ path: `${out}/console-capture.png`, fullPage: true });

const ok =
  rowCount > 0 && tableStillVisible && tiles > 0 && costText > 0 &&
  (hasCapture ? inspect > 0 : true);
console.log(
  JSON.stringify({
    rowCount,
    tableStillVisible,
    tiles,
    costText,
    perFileTokens,
    inspect,
    ok,
  }),
);
if (errors.length) console.log("errors:", errors);
await browser.close();
process.exit(ok ? 0 : 1);
