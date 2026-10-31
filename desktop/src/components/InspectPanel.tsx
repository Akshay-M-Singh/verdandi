import { useEffect, useState } from "react";
import { toast } from "sonner";
import { notifyError } from "@/lib/errors";
import { Check, Play, ScanSearch } from "lucide-react";
import { api, SIDECAR_URL } from "@/api";
import type { CatalogCard, CatalogDocument, Job } from "@/api";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { plural } from "@/lib/format";
import { cn } from "@/lib/utils";

export default function InspectPanel({
  job,
  onStarted,
}: {
  job: Job;
  onStarted: (job: Job) => void;
}) {
  const [cards, setCards] = useState<CatalogCard[]>([]);
  const [pageCounts, setPageCounts] = useState<Record<string, number>>({});
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [extracting, setExtracting] = useState(false);
  const [selected, setSelected] = useState<Record<string, number[]>>({});
  const [labels, setLabels] = useState<Record<string, string>>({});
  const [pages, setPages] = useState("");
  const [label, setLabel] = useState("");

  useEffect(() => {
    api
      .documents(job.id)
      .then((rows) =>
        setPageCounts(
          Object.fromEntries(rows.map((r) => [r.stem, r.pages || 0])),
        ),
      )
      .catch(() => setPageCounts({}))
      .finally(() => setLoaded(true));
    api
      .catalogCards(job.id)
      .then(setCards)
      .catch(() => setCards([]));
  }, [job.id]);

  const pageUrl = (stem: string, n: number) =>
    `${SIDECAR_URL}/jobs/${job.id}/documents/${encodeURIComponent(stem)}/page/${n}`;

  const cardFor = (stem: string) => cards.find((c) => c.stem === stem);
  const selectedPages = (stem: string) => selected[stem] ?? [];

  function togglePage(stem: string, page: number) {
    setSelected((prev) => {
      const cur = new Set(prev[stem] ?? []);
      if (cur.has(page)) cur.delete(page);
      else cur.add(page);
      return { ...prev, [stem]: [...cur].sort((a, b) => a - b) };
    });
  }

  function selectAll(stem: string, count: number) {
    setSelected((prev) => ({
      ...prev,
      [stem]:
        (prev[stem] ?? []).length === count
          ? []
          : Array.from({ length: count }, (_, i) => i + 1),
    }));
  }

  async function inspect(stem?: string) {
    const scope = stem ? `file ${stem}` : plural(job.stems.length, "file");
    if (
      !window.confirm(
        `Look inside ${scope}? This makes one small paid call per file. ` +
          `It's optional — extraction works without it.`,
      )
    ) {
      return;
    }
    setBusy(true);
    try {
      const res = await api.catalog(job.id, stem);
      setCards(res.cards);
      toast.success("Catalog ready");
    } catch (e) {
      notifyError(e, "run the catalog sweep");
    } finally {
      setBusy(false);
    }
  }

  async function extractFile(stem: string) {
    const pins = selectedPages(stem);
    const pinPages = pins.length ? pins.join(",") : undefined;
    const pinLabel = labels[stem]?.trim() || undefined;
    const what = pinPages ? `pages ${pinPages}` : "the whole document";
    if (
      !window.confirm(
        `Extract ${what} from ${stem}? This uses one paid model call.`,
      )
    ) {
      return;
    }
    setExtracting(true);
    try {
      const next = await api.extract(job.id, {
        stems: [stem],
        pinned_pages: pinPages,
        pinned_label: pinLabel,
      });
      toast.success("Extraction queued");
      onStarted(next);
    } catch (e) {
      notifyError(e, "start the extraction");
    } finally {
      setExtracting(false);
    }
  }

  async function extractDoc(stem: string, doc: CatalogDocument) {
    const docPages = (doc.pages ?? []).filter((p) => p > 0);
    const pinPages = docPages.length ? docPages.join(",") : undefined;
    const pinLabel = doc.label ?? doc.type ?? undefined;
    if (
      !window.confirm(
        `Extract ${stem}${pinPages ? ` (pages ${pinPages})` : ""}? ` +
          `This uses one paid model call.`,
      )
    ) {
      return;
    }
    setExtracting(true);
    try {
      const next = await api.extract(job.id, {
        stems: [stem],
        pinned_pages: pinPages,
        pinned_label: pinLabel,
      });
      toast.success("Extraction queued");
      onStarted(next);
    } catch (e) {
      notifyError(e, "start the extraction");
    } finally {
      setExtracting(false);
    }
  }

  async function startAll() {
    setExtracting(true);
    try {
      const next = await api.extract(job.id, {
        pinned_pages: pages,
        pinned_label: label,
      });
      toast.success("Extraction queued");
      onStarted(next);
    } catch (e) {
      notifyError(e, "start the extraction");
    } finally {
      setExtracting(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Inspect &amp; extract</CardTitle>
        <CardDescription>
          Capture is done (free). Preview the pages and tick the ones to
          extract — no ticks means the whole document. The catalog sweep
          ("What's in this file?") is optional and paid.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        {!loaded ? (
          <div className="flex flex-col gap-3">
            {job.stems.map((stem) => (
              <div
                key={stem}
                className="h-40 animate-pulse rounded-lg border border-border bg-accent/20"
              />
            ))}
          </div>
        ) : (
          <>
            {job.stems.map((stem) => {
              const count = pageCounts[stem] ?? 0;
              const pins = selectedPages(stem);
              const card = cardFor(stem);
              return (
                <div
                  key={stem}
                  className="flex flex-col gap-3 rounded-lg border border-border p-4"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium">{stem}</span>
                    <span className="text-xs text-muted-foreground">
                      {count === 1 ? "1 page" : `${count} pages`}
                    </span>
                    {card?.documents.map((d, i) => (
                      <span
                        key={i}
                        className="rounded-full border border-border px-2.5 py-0.5 text-xs text-muted-foreground"
                      >
                        {d.type ?? "document"}
                        {d.label ? ` · ${d.label}` : ""}
                        {(d.pages ?? []).length
                          ? ` · p${d.pages.join(", ")}`
                          : ""}
                      </span>
                    ))}
                    {card?.status === "failed" && (
                      <span className="text-xs text-destructive">
                        catalog failed: {card.error ?? "unknown"}
                      </span>
                    )}
                    <Button
                      variant="ghost"
                      size="sm"
                      className="ml-auto"
                      disabled={busy}
                      onClick={() => inspect(stem)}
                    >
                      <ScanSearch />
                      {card ? "Re-inspect" : "What's in this file? · 1 cheap call"}
                    </Button>
                  </div>

                  {count === 0 ? (
                    <p className="text-xs text-destructive">
                      No pages captured for this file — check the run log.
                    </p>
                  ) : (
                    <>
                      <div className="flex flex-wrap gap-2">
                        {Array.from({ length: count }, (_, i) => i + 1).map(
                          (n) => {
                            const isSel = pins.includes(n);
                            return (
                              <div key={n} className="relative">
                                <Dialog>
                                  <DialogTrigger asChild>
                                    <button
                                      title={`page ${n} — click to view`}
                                      className={cn(
                                        "rounded-md border transition-all",
                                        isSel
                                          ? "border-primary ring-2 ring-primary/60"
                                          : "border-border hover:opacity-80",
                                      )}
                                    >
                                      <img
                                        src={pageUrl(stem, n)}
                                        alt={`${stem} page ${n}`}
                                        className="h-28 rounded-md bg-white"
                                      />
                                    </button>
                                  </DialogTrigger>
                                  <DialogContent className="max-w-3xl">
                                    <DialogTitle className="text-sm">
                                      {stem} — page {n}
                                    </DialogTitle>
                                    <img
                                      src={pageUrl(stem, n)}
                                      alt={`${stem} page ${n}`}
                                      className="max-h-[75vh] w-full rounded-md bg-white object-contain"
                                    />
                                  </DialogContent>
                                </Dialog>
                                <button
                                  title={
                                    isSel
                                      ? `unpin page ${n}`
                                      : `pin page ${n} for extraction`
                                  }
                                  onClick={() => togglePage(stem, n)}
                                  className={cn(
                                    "absolute left-1.5 top-1.5 z-10 flex size-5 items-center justify-center rounded border",
                                    isSel
                                      ? "border-primary bg-primary text-primary-foreground"
                                      : "border-border bg-background/85 hover:border-primary",
                                  )}
                                >
                                  {isSel ? <Check className="size-3.5" /> : null}
                                </button>
                                <span className="absolute bottom-1 right-1 rounded bg-background/85 px-1 text-[10px] text-muted-foreground">
                                  p{n}
                                </span>
                              </div>
                            );
                          },
                        )}
                      </div>

                      <div className="flex flex-wrap items-center gap-2">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => selectAll(stem, count)}
                        >
                          {pins.length === count ? "Clear pages" : "All pages"}
                        </Button>
                        <Input
                          className="w-56"
                          placeholder="pin label (optional)"
                          value={labels[stem] ?? ""}
                          onChange={(e) =>
                            setLabels((prev) => ({
                              ...prev,
                              [stem]: e.target.value,
                            }))
                          }
                        />
                        <Button
                          size="sm"
                          disabled={extracting}
                          onClick={() => extractFile(stem)}
                        >
                          <Play />
                          {pins.length
                            ? `Extract selected pages (${pins.length})`
                            : "Extract this file"}
                        </Button>
                      </div>

                      {(card?.documents.length ?? 0) > 0 && (
                        <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
                          <span className="text-xs text-muted-foreground">
                            Quick extract:
                          </span>
                          {card!.documents.map((d, i) => (
                            <Button
                              key={i}
                              variant="outline"
                              size="sm"
                              disabled={extracting}
                              onClick={() => extractDoc(stem, d)}
                            >
                              <Play />
                              {d.label ?? d.type ?? "document"}
                              {(d.pages ?? []).length
                                ? ` · p${d.pages.join(", ")}`
                                : ""}
                            </Button>
                          ))}
                        </div>
                      )}
                    </>
                  )}
                </div>
              );
            })}

            {job.stems.length > 1 && (
              <div className="flex flex-wrap items-end gap-4 border-t border-border pt-4">
                <div className="flex w-44 flex-col gap-1.5">
                  <Label className="text-xs text-muted-foreground">
                    All files · pages
                  </Label>
                  <Input
                    placeholder="1-3,5 (all if empty)"
                    value={pages}
                    onChange={(e) => setPages(e.target.value)}
                  />
                </div>
                <div className="flex w-56 flex-col gap-1.5">
                  <Label className="text-xs text-muted-foreground">
                    All files · pin label
                  </Label>
                  <Input
                    placeholder="e.g. broker invoice"
                    value={label}
                    onChange={(e) => setLabel(e.target.value)}
                  />
                </div>
                <Button onClick={startAll} disabled={extracting}>
                  {extracting ? "Starting…" : "Extract all files"}
                </Button>
                <Button
                  variant="outline"
                  onClick={() => inspect()}
                  disabled={busy}
                >
                  <ScanSearch /> Inspect all {job.stems.length} files
                </Button>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
