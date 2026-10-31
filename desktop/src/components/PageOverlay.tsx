import { SIDECAR_URL } from "@/api";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export interface OverlayBox {
  bbox: number[] | null | undefined;
  kind: "located" | "claimed" | "row";
  label?: string;
}

const STYLES: Record<OverlayBox["kind"], string> = {
  located: "border-emerald-400",
  claimed: "border-sky-400/80 border-dashed",
  row: "border-primary",
};

const LEGEND: Record<OverlayBox["kind"], string> = {
  located: "located in text",
  claimed: "model claim",
  row: "line item row",
};

export default function PageOverlay({
  jobId,
  stem,
  page,
  pages,
  boxes,
  onPage,
  available = true,
}: {
  jobId: string;
  stem: string;
  page: number;
  pages: number;
  boxes: OverlayBox[];
  onPage?: (page: number) => void;
  available?: boolean;
}) {
  if (!available) {
    return (
      <div className="flex h-40 items-center justify-center rounded-md border border-dashed border-border px-6 text-center text-xs text-muted-foreground">
        Page renders were pruned to free disk — located text only.
      </div>
    );
  }
  const url = `${SIDECAR_URL}/jobs/${jobId}/documents/${encodeURIComponent(stem)}/page/${page}`;
  const shown = boxes.filter(
    (b) => Array.isArray(b.bbox) && b.bbox.length === 4,
  );
  return (
    <div className="flex flex-col gap-2">
      <div className="relative overflow-hidden rounded-md border border-border bg-white">
        <img src={url} alt={`${stem} page ${page}`} className="w-full" />
        {shown.map((b, i) => {
          const [x0, y0, x1, y1] = b.bbox as number[];
          return (
            <div
              key={i}
              title={b.label ?? LEGEND[b.kind]}
              className={cn("absolute border-2", STYLES[b.kind])}
              style={{
                left: `${x0 * 100}%`,
                top: `${y0 * 100}%`,
                width: `${(x1 - x0) * 100}%`,
                height: `${(y1 - y0) * 100}%`,
              }}
            />
          );
        })}
      </div>
      <div className="flex items-center gap-3 text-[11px] text-muted-foreground">
        {pages > 1 && onPage && (
          <span className="flex items-center gap-1">
            <Button
              size="sm"
              variant="outline"
              className="h-6 px-2"
              disabled={page <= 1}
              onClick={() => onPage(page - 1)}
            >
              ‹
            </Button>
            page {page} / {pages}
            <Button
              size="sm"
              variant="outline"
              className="h-6 px-2"
              disabled={page >= pages}
              onClick={() => onPage(page + 1)}
            >
              ›
            </Button>
          </span>
        )}
        {[...new Set(shown.map((b) => b.kind))].map((kind) => (
          <span key={kind} className="flex items-center gap-1">
            <span
              className={cn("inline-block size-2 border-2", STYLES[kind])}
            />
            {LEGEND[kind]}
          </span>
        ))}
      </div>
    </div>
  );
}
