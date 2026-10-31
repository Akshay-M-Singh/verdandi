// README screenshots (durable) — drives the dev loop and saves the images the
// public README embeds.
//
// PRIVACY: uses ONLY benchmark/synthetic PDFs and the SHIPPED STARTER config
// (VERDANDI_USER_CONFIG=1 + a throwaway workspace) — your own templates,
// workflows, exports and runs can never appear in a screenshot.
//
//   VERDANDI_WORKSPACE=/tmp/vd-readme-shots VERDANDI_USER_CONFIG=1 \
//     make desktop-service VERDANDI_SIDECAR_PORT=8812   # terminal 1 (engine)
//   VITE_SIDECAR_URL=http://127.0.0.1:8812 make desktop-ui   # terminal 2
//   cd desktop && node scripts/readme_shots.mjs /tmp/vd-shots
//
// Prepares a capture-only run (free) and one small paid extraction
// (~fractions of a cent), then captures: run, inspect, results, review,
// export, console.
import path from "node:path";
import { chromium } from "playwright";

const ROOT = path.resolve(import.meta.dirname, "..", "..");
const PDFS = path.join(ROOT, "benchmark", "data", "pdfs");
const out = process.argv[2] || "/tmp/verdandi-readme-shots";
const api = process.env.VERDANDI_SIDECAR_URL || "http://127.0.0.1:8787";
const ui = process.env.VERDANDI_UI_URL || "http://localhost:5173";
// The extraction doc must MATCH one of the shipped STARTER profiles and be
// privacy-safe (synthetic or public dataset). A generated broker invoice with
// one row-math error gives values + line items + a genuine review flag.
const extractPdf = process.env.READMESHOTS_EXTRACT_PDF
  || path.join(PDFS, "fatura_07.pdf");
const extractDoc = process.env.READMESHOTS_DOC_ID || "invoice_starter";

async function call(pathname, body) {
  const res = await fetch(api + pathname, {
    method: body === undefined ? "GET" : "POST",
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${pathname} -> ${res.status}`);
  return res.json();
}

async function waitJob(id) {
  for (let i = 0; i < 180; i += 1) {
    const job = await call(`/jobs/${id}`);
    if (job.state === "done" || job.state === "failed") return job;
    await new Promise((r) => setTimeout(r, 2000));
  }
  throw new Error(`job ${id} did not finish`);
}

// --- prepare runs (benchmark PDFs only) -------------------------------------
await call("/settings", { onboarded: true });
const capture = await call("/jobs", {
  paths: [path.join(PDFS, "cord_22.pdf"), path.join(PDFS, "cord_23.pdf")],
  doc_id: "any_document", model: "gpt-5.6-luna", mode: "capture",
  name: "Capture preview",
});
await waitJob(capture.id);
const extract = await call("/jobs", {
  paths: [extractPdf],
  doc_id: extractDoc, model: "gpt-5.6-luna",
  name: "Invoice batch",
});
await waitJob(extract.id);
console.log("runs ready:", capture.id, extract.id);

// --- capture screens --------------------------------------------- ----------
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
const errors = [];
page.on("console", (m) => {
  if (m.type() === "error") errors.push(m.text());
});
await page.goto(ui);
await page.waitForTimeout(2500);
const nav = page.getByRole("navigation");
const shot = (name) => page.screenshot({ path: path.join(out, `${name}.png`) });

// 1. Run tab — intake form
await page.waitForTimeout(600);
await shot("run");

// 2. Console — the extraction run selected (metrics/forensics panel)
await nav.getByRole("button", { name: "Console", exact: true }).click();
await page.waitForTimeout(1800);
await page.locator("tbody tr").filter({ hasText: "Invoice batch" }).first().click();
await page.waitForTimeout(1500);
await shot("console");

// 3. Results — a field row with a value clicked, evidence overlay visible
await page
  .locator("main")
  .getByRole("button", { name: "Results", exact: true })
  .click();
await page.waitForTimeout(2000);
const fieldRows = page.locator("main table").nth(1).locator("tbody tr");
let clicked = false;
for (let i = 0; i < (await fieldRows.count()); i += 1) {
  const value = await fieldRows.nth(i).locator("td").nth(1).innerText();
  if (value.trim() && value.trim() !== "—") {
    await fieldRows.nth(i).click();
    clicked = true;
    break;
  }
}
if (!clicked && (await fieldRows.count())) await fieldRows.first().click();
await page.waitForTimeout(1200);
await shot("results");

// 4. Review — first flag selected, evidence pane visible
await nav.getByRole("button", { name: "Review", exact: true }).click();
await page.waitForTimeout(2000);
const flagRow = page.locator("main table").first().locator("tbody tr").first();
if (await flagRow.count()) {
  await flagRow.click();
  await page.waitForTimeout(1500);
}
await shot("review");

// 5. Exports — the profile/column builder view. The starter config ships no
// export profiles, so seed a generic one (the shot must never show a
// workspace-specific profile).
await call("/exports", {
  profile: {
    id: "invoice_summary",
    label: "Invoice summary",
    grain: "file",
    options: { date_format: "dd/mm/yyyy" },
    columns: [
      { header: "File", source: { meta: "original" } },
      {
        header: "Invoice Number",
        source: { template: "invoice_starter", field: "invoice_number" },
      },
      {
        header: "Invoice Date",
        source: { template: "invoice_starter", field: "invoice_date" },
      },
      {
        header: "Vendor",
        source: { template: "invoice_starter", field: "vendor_name" },
      },
      {
        header: "Total Amount",
        source: { template: "invoice_starter", field: "total_amount" },
      },
    ],
  },
});
await nav.getByRole("button", { name: "Exports", exact: true }).click();
await page.waitForTimeout(1200);
const profilesToggle = page.getByRole("button", { name: "Profiles", exact: true });
if (await profilesToggle.count()) {
  await profilesToggle.click();
  await page.waitForTimeout(1500);
}
await shot("export");

// 6. Run workbench — the capture run continued, Inspect & extract panel
await nav.getByRole("button", { name: "Console", exact: true }).click();
await page.waitForTimeout(1500);
await page.locator("tbody tr").filter({ hasText: "Capture preview" }).first().click();
await page.waitForTimeout(1200);
const cont = page.getByRole("button", { name: /Continue — inspect/ });
if (await cont.count()) {
  await cont.click();
} else {
  await nav.getByRole("button", { name: "Run", exact: true }).click();
}
await page.getByText("Inspect & extract").waitFor({ timeout: 20000 });
await page.waitForTimeout(1500);
await shot("inspect");

console.log(JSON.stringify({ out, consoleErrors: errors.slice(0, 5) }));
await browser.close();
