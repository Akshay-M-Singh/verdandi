import { IN_TAURI } from "@/api";

export interface AvailableUpdate {
  version: string;
  currentVersion: string;
  body?: string;
}

/** Quiet update check (null when up to date, offline, or in a browser). */
export async function checkForUpdate(): Promise<AvailableUpdate | null> {
  if (!IN_TAURI) return null;
  const { check } = await import("@tauri-apps/plugin-updater");
  const update = await check();
  if (!update) return null;
  return {
    version: update.version,
    currentVersion: update.currentVersion,
    body: update.body ?? undefined,
  };
}

/** Download + install the pending update, then relaunch the app. */
export async function installUpdate(
  onProgress?: (percent: number | null) => void,
): Promise<void> {
  const { check } = await import("@tauri-apps/plugin-updater");
  const { relaunch } = await import("@tauri-apps/plugin-process");
  const update = await check();
  if (!update) return;
  let total: number | null = null;
  let done = 0;
  await update.downloadAndInstall((event) => {
    if (event.event === "Started") {
      total = event.data.contentLength ?? null;
    } else if (event.event === "Progress") {
      done += event.data.chunkLength;
      onProgress?.(total ? Math.round((done / total) * 100) : null);
    }
  });
  await relaunch();
}
