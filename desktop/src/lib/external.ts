import { IN_TAURI } from "@/api";

/** Open an external URL in the user's browser (opener plugin in the app,
 *  a new tab in the browser dev loop). */
export async function openExternal(url: string): Promise<void> {
  if (IN_TAURI) {
    const { openUrl } = await import("@tauri-apps/plugin-opener");
    await openUrl(url);
    return;
  }
  window.open(url, "_blank", "noopener,noreferrer");
}
