import { useEffect, useState } from "react";
import Explain from "@/components/Explain";
import { notifyError } from "@/lib/errors";
import { valueText } from "@/lib/format";
import { levelBody, levelLabel } from "@/lib/messages";
import { FileSearch } from "lucide-react";
import { api } from "@/api";
import type {
  DocumentData,
  DocumentRow,
  HeaderField,
  Job,
  LineItem,
} from "@/api";
import CrossDocPanel from "@/components/CrossDocPanel";
import EmptyState from "@/components/EmptyState";
import PageOverlay, { type OverlayBox } from "@/components/PageOverlay";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";

interface Selection {
  page: number;
  boxes: OverlayBox[];
}

function fieldSelection(f: HeaderField): Selection {
  const boxes: OverlayBox[] = [];
  const match = f.verification?.checks?.find((c) => c.check === "grounding")
    ?.match;
  if (f.source_region) {
    boxes.push({ bbox: f.source_region.bbox, kind: "claimed", label: "model claim" });
  }
  if (match?.line_bbox) {
    boxes.push({ bbox: match.line_bbox, kind: "located", label: match.text });
  }
  const page =
    match?.page ?? f.source_region?.page ?? 1;
  return { page, boxes };
}

function itemSelection(li: LineItem): Selection {
  return {
    page: li.source_region?.page ?? 1,
    boxes: [{ bbox: li.source_region?.bbox, kind: "row", label: "row region" }],
  };
}

function FieldTable({
  fields,
  onSelect,
}: {
  fields: HeaderField[];
  onSelect: (f: HeaderField) => void;
}) {
  if (!fields.length) {
    return <p className="text-sm text-muted-foreground">No header fields.</p>;
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Field</TableHead>
          <TableHead>Value</TableHead>
          <TableHead className="text-right">Conf.</TableHead>
          <TableHead>Verification</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {fields.map((f) => (
          <TableRow
            key={f.name}
            className="cursor-pointer"
            onClick={() => onSelect(f)}
          >
            <TableCell>{f.name}</TableCell>
            <TableCell>{valueText(f.value) || "—"}</TableCell>
            <TableCell className="text-right tabular-nums">
              {f.confidence != null ? f.confidence.toFixed(2) : "—"}
            </TableCell>
            <TableCell>
              <span className="flex items-center gap-1.5">
                {levelLabel(f.verification?.level)}
                <Explain
                  title={levelLabel(f.verification?.level)}
                  body={levelBody(f.verification?.level)}
                  code={f.verification?.level}
                />
              </span>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function ItemTable({
  items,
  onSelect,
}: {
  items: LineItem[];
  onSelect: (li: LineItem) => void;
}) {
  if (!items.length) {
    return <p className="text-sm text-muted-foreground">No line items.</p>;
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>#</TableHead>
          <TableHead>Description</TableHead>
          <TableHead className="text-right">Qty</TableHead>
          <TableHead className="text-right">Unit price</TableHead>
          <TableHead className="text-right">Amount</TableHead>
          <TableHead className="text-right">Conf.</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {items.map((r, i) => (
          <TableRow
            key={i}
            className="cursor-pointer"
            onClick={() => onSelect(r)}
          >
            <TableCell>{i + 1}</TableCell>
            <TableCell>{r.description ?? ""}</TableCell>
            <TableCell className="text-right tabular-nums">
              {r.quantity ?? ""}
            </TableCell>
            <TableCell className="text-right tabular-nums">
              {r.unit_price ?? ""}
            </TableCell>
            <TableCell className="text-right tabular-nums">
              {r.amount ?? "—"}
            </TableCell>
            <TableCell className="text-right tabular-nums">
              {r.confidence != null ? r.confidence.toFixed(2) : ""}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export default function Results({ jobId }: { jobId: string }) {
  const [docs, setDocs] = useState<DocumentRow[]>([]);
  const [stem, setStem] = useState<string | null>(null);
  const [data, setData] = useState<DocumentData | null>(null);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [rendersAvailable, setRendersAvailable] = useState(true);
  const [job, setJob] = useState<Job | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [rawOpen, setRawOpen] = useState(false);
  const [docsLoaded, setDocsLoaded] = useState(false);
  const [summary, setSummary] = useState<Record<string, string>[]>([]);

  useEffect(() => {
    api
      .job(jobId)
      .then((j) => {
        setJob(j);
        setRendersAvailable(!j.renders_pruned);
        if (j.workflow) {
          api
            .jobSummary(jobId)
            .then(setSummary)
            .catch(() => setSummary([]));
        }
      })
      .catch(() => undefined);
  }, [jobId]);

  useEffect(() => {
    api
      .documents(jobId)
      .then((rows) => {
        setDocs(rows);
        const first = rows.find((r) => r.status === "extracted") ?? rows[0];
        if (first) setStem(first.stem);
      })
      .catch((e) => notifyError(e))
      .finally(() => setDocsLoaded(true));
  }, [jobId]);

  const selected = docs.find((d) => d.stem === stem);

  useEffect(() => {
    if (!stem || selected?.status !== "extracted") {
      setData(null);
      return;
    }
    setData(null);
    setSelection(null);
    api
      .document(jobId, stem)
      .then(setData)
      .catch((e) => notifyError(e));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, stem, selected?.status]);

  const pages = selected?.pages || 1;

  function downloadJson() {
    if (!data || !stem) return;
    const blob = new Blob([JSON.stringify(data, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${stem}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  function allBoxes(page: number): OverlayBox[] {
    if (!data) return [];
    const sources: HeaderField[] = data.documents?.length
      ? data.documents.flatMap((e) => e.header_fields ?? [])
      : (data.header_fields ?? []);
    const boxes: OverlayBox[] = [];
    for (const f of sources) {
      const label = `${f.name}: ${valueText(f.value)}`;
      if (f.source_region?.page === page) {
        boxes.push({ bbox: f.source_region.bbox, kind: "claimed", label });
      }
      const match = f.verification?.checks?.find(
        (c) => c.check === "grounding",
      )?.match;
      if (match?.page === page && match.line_bbox) {
        boxes.push({ bbox: match.line_bbox, kind: "located", label });
      }
    }
    return boxes;
  }

  if (!docsLoaded) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Results</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-9 w-full" />
          ))}
        </CardContent>
      </Card>
    );
  }

  if (!docs.length) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Results</CardTitle>
        </CardHeader>
        <CardContent>
          <EmptyState
            icon={FileSearch}
            title="No documents in this run yet"
            description="Capture a PDF first (Run screen), then extract to see fields and line items here."
          />
        </CardContent>
      </Card>
    );
  }

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle>Documents</CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>File</TableHead>
                <TableHead>Issuer</TableHead>
                <TableHead className="text-right">Fields</TableHead>
                <TableHead className="text-right">Line items</TableHead>
                <TableHead className="text-right">Pages</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {docs.map((d) => (
                <TableRow
                  key={d.stem}
                  onClick={() => setStem(d.stem)}
                  className={cn(
                    "cursor-pointer",
                    d.stem === stem && "bg-accent/60",
                  )}
                >
                  <TableCell
                    className="max-w-64 truncate font-medium"
                    title={d.stem}
                  >
                    {d.stem}
                  </TableCell>
                  <TableCell
                    className="max-w-56 truncate"
                    title={d.selected_name ?? undefined}
                  >
                    {d.selected_name ?? "—"}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {d.fields}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {d.line_items}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {d.pages || "—"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      {stem && !data && selected?.status === "captured" && (
        <Card>
          <CardHeader>
            <CardTitle>{stem}</CardTitle>
            <CardDescription>
              Capture only — no extraction in this run yet. Start an extraction
              run to see fields and line items.
            </CardDescription>
          </CardHeader>
        </Card>
      )}

      {stem && data && (
        <Card>
          <CardHeader>
            <CardTitle>{stem}</CardTitle>
            {data.selected_document?.name && (
              <CardDescription>
                Issuer{" "}
                <span className="font-medium text-foreground">
                  {data.selected_document.name}
                </span>
                {data.selected_document.confidence != null &&
                  ` · confidence ${data.selected_document.confidence.toFixed(2)}`}
                {" · click a field or row to see it on the page"}
              </CardDescription>
            )}
          </CardHeader>
          <CardContent className="grid gap-6 lg:grid-cols-[1.1fr_0.9fr]">
            <div className="flex min-w-0 flex-col gap-5">
              {data.documents?.length
                ? data.documents.map((entry, i) => (
                    <div
                      key={i}
                      className="border-t border-border pt-4 first:border-t-0 first:pt-0"
                    >
                      <h3 className="mb-2 text-xs font-semibold tracking-wider text-primary">
                        {entry.template_id ?? `entry ${i}`}
                        {entry.found === false ? " (not found)" : ""}
                      </h3>
                      <FieldTable
                        fields={entry.header_fields ?? []}
                        onSelect={(f) => setSelection(fieldSelection(f))}
                      />
                      <div className="h-3" />
                      <ItemTable
                        items={entry.line_items ?? []}
                        onSelect={(li) => setSelection(itemSelection(li))}
                      />
                    </div>
                  ))
                : (
                  <>
                    <FieldTable
                      fields={data.header_fields ?? []}
                      onSelect={(f) => setSelection(fieldSelection(f))}
                    />
                    <ItemTable
                      items={data.line_items ?? []}
                      onSelect={(li) => setSelection(itemSelection(li))}
                    />
                  </>
                )}
              {(data.notes?.length ?? 0) > 0 && (
                <p className="text-sm text-muted-foreground">
                  Notes: {data.notes!.join(" · ")}
                </p>
              )}
            </div>

            <div className="min-w-0 lg:sticky lg:top-6 lg:self-start">
              <label className="mb-2 flex items-center gap-2 text-xs text-muted-foreground">
                <Checkbox
                  checked={showAll}
                  onCheckedChange={(v) => setShowAll(Boolean(v))}
                />
                Show all fields on the page
              </label>
              <PageOverlay
                jobId={jobId}
                stem={stem}
                page={selection?.page ?? 1}
                pages={pages}
                boxes={
                  showAll
                    ? allBoxes(selection?.page ?? 1)
                    : (selection?.boxes ?? [])
                }
                available={rendersAvailable}
                onPage={(p) =>
                  setSelection((prev) => ({
                    page: p,
                    boxes: prev?.boxes ?? [],
                  }))
                }
              />
              {!selection && !showAll && (
                <p className="mt-2 text-xs text-muted-foreground">
                  Select a field or line item to highlight it on the page.
                </p>
              )}
            </div>
          </CardContent>
        </Card>
      )}

      {stem && data && (
        <CrossDocPanel
          data={data}
          jobId={jobId}
          stem={stem}
          pages={pages}
          rendersAvailable={rendersAvailable}
        />
      )}

      {stem && data && (
        <Card>
          <CardHeader className="flex-row items-center justify-between gap-2">
            <CardTitle>Raw extraction JSON</CardTitle>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setRawOpen((v) => !v)}
              >
                {rawOpen ? "Hide" : "Show"}
              </Button>
              <Button variant="outline" size="sm" onClick={downloadJson}>
                Download JSON
              </Button>
            </div>
          </CardHeader>
          {rawOpen && (
            <CardContent>
              <pre className="max-h-96 overflow-auto rounded-md border border-border bg-background p-3 font-mono text-xs">
                {JSON.stringify(data, null, 2)}
              </pre>
            </CardContent>
          )}
        </Card>
      )}

      {job?.workflow && summary.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Workflow summary</CardTitle>
            <CardDescription>
              Per file × template (summary.csv from the workflow run).
            </CardDescription>
          </CardHeader>
          <CardContent className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  {Object.keys(summary[0]).map((h) => (
                    <TableHead key={h}>{h}</TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {summary.map((row, i) => (
                  <TableRow key={i}>
                    {Object.keys(summary[0]).map((h) => (
                      <TableCell key={h}>{valueText(row[h])}</TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
    </>
  );
}
