import { toast } from "sonner";

/** Human sentences for the engine's error text — no jargon leads.
 *
 * Known causes are mapped to plain language; everything else is prettified
 * (codes → words, paths stripped). The raw text is always available behind
 * the toast's "Copy details" action.
 */

const CAUSES: [RegExp, (m: RegExpMatchArray) => string][] = [
  [/unknown export profile '([^']+)'/i,
   (m) => `The export profile “${m[1]}” no longer exists — pick another one.`],
  [/unknown job/i, () => "That run no longer exists."],
  [/choose either/i,
   () => "Choose either a workflow or profile(s) — not both."],
  [/no profile\/model configured/i,
   () => "Pick a profile and a model in Settings first."],
  [/nothing to retry/i, () => "Every file in this run already finished."],
  [/still unreviewed/i,
   () => "Some items are still unreviewed — finish Review first, or turn off the review gate for this export."],
  [/no pages captured/i, () => "No pages were captured for this file."],
  [/no page render/i, () => "That page image isn't available anymore (renders may have been freed)."],
  [/not in the vision-verified/i,
   () => "That model isn't verified for vision yet — verify it in Settings."],
  [/rate.?limit|429/i,
   () => "The model provider is rate-limiting right now — wait a moment and try again."],
  [/api.?key|401|403/i,
   () => "The gateway key was rejected — check it in Settings."],
  [/offline|ENOTFOUND|ECONNREFUSED|timed? ?out/i,
   () => "Couldn't reach the model gateway — check your connection and try again."],
];

function prettify(text: string): string {
  return text
    .replace(/^[a-z ]+failed:\s*/i, "")
    .replace(/\s*[—-]\s*(rules|config)\/[\w./-]+/g, "")
    .replace(/\b([a-z]+)_([a-z_]+)\b/g, "$1 $2")
    .replace(/\s+/g, " ")
    .trim();
}

export function friendlyError(error: unknown): { message: string; raw: string } {
  const raw = String(error instanceof Error ? error.message : error);
  for (const [pattern, build] of CAUSES) {
    const match = raw.match(pattern);
    if (match) return { message: build(match), raw };
  }
  const text = prettify(raw);
  if (text) {
    return { message: text.charAt(0).toUpperCase() + text.slice(1), raw };
  }
  return { message: "Something went wrong.", raw };
}

/** Friendly error toast with a copy-details action; deduped by message. */
const _recent = new Set<string>();

export function notifyError(error: unknown, action?: string): void {
  const { message, raw } = friendlyError(error);
  const key = `${action ?? ""}|${message}`;
  if (_recent.has(key)) return;
  _recent.add(key);
  window.setTimeout(() => _recent.delete(key), 5000);
  toast.error(action ? `Couldn't ${action}` : message, {
    description: action ? message : undefined,
    duration: 9000,
    action: raw
      ? {
          label: "Copy details",
          onClick: () => {
            void navigator.clipboard?.writeText(raw);
            toast.success("Details copied");
          },
        }
      : undefined,
  });
}
