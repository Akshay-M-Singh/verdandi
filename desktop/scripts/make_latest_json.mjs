#!/usr/bin/env node
// Compose the Tauri updater manifest (latest.json) from a flat directory of
// release assets (installers + updater archives + their .sig files).
//
//   node scripts/make_latest_json.mjs <dir> <version> <owner/repo> <tag>
//
// The manifest is served from the PUBLIC releases repo
// (https://github.com/<owner/repo>/releases/latest/download/latest.json).
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

const [dir, version, repo, tag] = process.argv.slice(2);
if (!dir || !version || !repo || !tag) {
  console.error(
    "usage: node scripts/make_latest_json.mjs <dir> <version> <owner/repo> <tag>",
  );
  process.exit(2);
}

const files = readdirSync(dir);
const platforms = {};

function add(platform, artifact) {
  if (!artifact) return;
  const sigName = `${artifact}.sig`;
  if (!files.includes(sigName)) {
    console.warn(`skip ${platform}: no signature for ${artifact}`);
    return;
  }
  platforms[platform] = {
    signature: readFileSync(path.join(dir, sigName), "utf8").trim(),
    url: `https://github.com/${repo}/releases/download/${tag}/${encodeURIComponent(artifact)}`,
  };
}

// macOS: the updater consumes the .app.tar.gz; only arm64 is built (D66).
add("darwin-aarch64", files.find((f) => f.endsWith(".app.tar.gz")));
// Windows: the updater consumes the NSIS installer. This Tauri version signs
// the installer itself (`-setup.exe` + .sig, verified on the v0.1.0 Windows
// build log); older Tauri produced a `.nsis.zip` — accept either, zip first.
add(
  "windows-x86_64",
  files.find((f) => f.endsWith(".nsis.zip")) ??
    files.find((f) => f.endsWith("-setup.exe")),
);

if (!Object.keys(platforms).length) {
  console.error("no updater artifacts found (need *.app.tar.gz / -setup.exe or *.nsis.zip + .sig)");
  process.exit(1);
}

const manifest = {
  version,
  notes: process.env.RELEASE_NOTES || `Verdandi ${version}`,
  pub_date: new Date().toISOString(),
  platforms,
};
const out = path.join(dir, "latest.json");
writeFileSync(out, JSON.stringify(manifest, null, 2) + "\n");
console.log(`wrote ${out}: ${Object.keys(platforms).join(", ")}`);
