#!/usr/bin/env node
// Single-source version bump (D66): keeps the four version carriers in sync.
//
//   node scripts/version.mjs show
//   node scripts/version.mjs set 0.2.0
//
// Carriers: package.json, src-tauri/tauri.conf.json, src-tauri/Cargo.toml
// [package], and the engine's desktop/__init__.py.
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const FILES = {
  pkg: path.join(root, "package.json"),
  conf: path.join(root, "src-tauri", "tauri.conf.json"),
  cargo: path.join(root, "src-tauri", "Cargo.toml"),
  py: path.join(root, "__init__.py"),
};

function show() {
  console.log(JSON.parse(readFileSync(FILES.conf, "utf8")).version);
}

function set(version) {
  const pkg = JSON.parse(readFileSync(FILES.pkg, "utf8"));
  pkg.version = version;
  writeFileSync(FILES.pkg, JSON.stringify(pkg, null, 2) + "\n");

  const conf = JSON.parse(readFileSync(FILES.conf, "utf8"));
  conf.version = version;
  writeFileSync(FILES.conf, JSON.stringify(conf, null, 2) + "\n");

  const cargo = readFileSync(FILES.cargo, "utf8").replace(
    /^version = "[^"]+"/m,
    `version = "${version}"`,
  );
  writeFileSync(FILES.cargo, cargo);

  const py = readFileSync(FILES.py, "utf8").replace(
    /__version__ = "[^"]+"/,
    `__version__ = "${version}"`,
  );
  writeFileSync(FILES.py, py);

  console.log(
    `version -> ${version} (package.json, tauri.conf.json, Cargo.toml, __init__.py)`,
  );
}

const [cmd, arg] = process.argv.slice(2);
if (cmd === "show") {
  show();
} else if (cmd === "set" && /^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/.test(arg ?? "")) {
  set(arg);
} else {
  console.error("usage: node scripts/version.mjs show | set X.Y.Z");
  process.exit(2);
}
