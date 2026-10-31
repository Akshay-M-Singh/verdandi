import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import Explain from "@/components/Explain";
import { notifyError } from "@/lib/errors";
import { prettyRule, reasonTitle } from "@/lib/messages";
import { plural, valueText } from "@/lib/format";
import { Copy, RefreshCw, ShieldCheck } from "lucide-react";
import { api } from "@/api";
import type { DocumentData, FlagRow } from "@/api";
import CrossDocPanel from "@/components/CrossDocPanel";
import EmptyState from "@/components/EmptyState";
import RunPicker from "@/components/RunPicker";
import PageOverlay, { type OverlayBox } from "@/components/PageOverlay";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

function sameFlag(a: FlagRow, b: FlagRow): boolean {
  return (
    a.file === b.file &&
    a.entry === b.entry &&
    a.template === b.template &&
    a.field === b.field &&
    a.reason === b.reason &&
    a.detail === b.detail
  );
}

function statusBadge(f: FlagRow) {
  if (f.review?.status === "confirmed") {
    return (
      <Badge variant="outline" className="border-emerald-500/60 text-emerald-500">
        confirmed
      </Badge>
    );
  }
  if (f.review?.status === "issue") {
    return (
      <Badge variant="outline" className="border-destructive/60 text-destructive">
        issue
      </Badge>
    );
  }
  return (
    <Badge variant="outline" className="border-primary/60 text-primary">
      pending
    </Badge>
  );
}

/** Cross-document rule id for a flag row (the `rule` column; legacy rows
 *  carry it inside the detail text). */
function ruleOf(f: FlagRow): string | null {
  if (f.rule) return f.rule;
  const m = /rule '([^']+)'/.exec(f.detail ?? "");
  return m ? m[1] : null;
}

export default function Review({
  jobId,
  onSelectJob,
}: {
  jobId: string | null;
  onSelectJob: (id: string | null) => void;
}) {
  const [picking, setPicking] = useState(!jobId);
  const [flags, setFlags] = useState<FlagRow[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [selected, setSelected] = useState<FlagRow | null>(null);
  const [value, setValue] = useState("");
  const [note, setNote] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [fileFilter, setFileFilter] = useState("all");
  const [fieldFilter, setFieldFilter] = useState("all");
  const [reasonFilter, setReasonFilter] = useState("all");
  const [hideReviewed, setHideReviewed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [rendersAvailable, setRendersAvailable] = useState(true);
  const [docsPages, setDocsPages] = useState<Record<string, number>>({});
  const [crossData, setCrossData] = useState<DocumentData | null>(null);

  useEffect(() => {
    if (!jobId) return;
    api
      .job(jobId)
      .then((j) => setRendersAvailable(!j.renders_pruned))
      .catch(() => undefined);
    api
      .documents(jobId)
      .then((rows) =>
        setDocsPages(
          Object.fromEntries(rows.map((r) => [r.stem, r.pages || 1])),
        ),
      )
      .catch(() => undefined);
  }, [jobId]);

  const load = async (keep?: FlagRow | null) => {
    if (!jobId) return;
    try {
      const rows = await api.flags(jobId);
      setFlags(rows);
      const target = keep ?? selected;
      const match = target ? rows.find((r) => sameFlag(r, target)) : null;
      setSelected(match ?? rows[0] ?? null);
    } catch (e) {
      notifyError(e);
    } finally {
      setLoaded(true);
    }
  };

  useEffect(() => {
    if (jobId) load(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);

  useEffect(() => {
    if (selected) {
      setValue(valueText(selected.evidence?.value ?? selected.value));
      setNote(selected.review?.note ?? "");
    }
  }, [selected]);

  const files = useMemo(
    () => [...new Set(flags.map((f) => f.file))].sort(),
    [flags],
  );
  const reasons = useMemo(
    () => [...new Set(flags.map((f) => f.reason))].sort(),
    [flags],
  );
  const fields = useMemo(
    () => [...new Set(flags.map((f) => f.field))].sort(),
    [flags],
  );
  const counts = useMemo(
    () => ({
      total: flags.length,
      pending: flags.filter((f) => !f.review).length,
      confirmed: flags.filter((f) => f.review?.status === "confirmed").length,
      issues: flags.filter((f) => f.review?.status === "issue").length,
    }),
    [flags],
  );
  const filtered = useMemo(
    () =>
      flags.filter((f) => {
        const status = f.review?.status ?? "pending";
        if (statusFilter !== "all" && status !== statusFilter) return false;
        if (fileFilter !== "all" && f.file !== fileFilter) return false;
        if (fieldFilter !== "all" && f.field !== fieldFilter) return false;
        if (reasonFilter !== "all" && f.reason !== reasonFilter) return false;
        if (hideReviewed && f.review) return false;
        return true;
      }),
    [flags, statusFilter, fileFilter, fieldFilter, reasonFilter, hideReviewed],
  );

  async function act(status: "confirmed" | "issue") {
    if (!selected || !jobId) return;
    setBusy(true);
    try {
      const row = { ...selected } as Record<string, unknown>;
      delete row.review;
      delete row.evidence;
      await api.review(jobId, row, status, note, value);
      toast.success(status === "confirmed" ? "Confirmed" : "Marked as issue");
      await load(selected);
    } catch (e) {
      notifyError(e, "save the review");
    } finally {
      setBusy(false);
    }
  }

  const pending = useMemo(() => filtered.filter((f) => !f.review), [filtered]);

  function nextPending() {
    if (!pending.length) return;
    const idx = selected ? pending.findIndex((f) => sameFlag(f, selected)) : -1;
    setSelected(pending[(idx + 1) % pending.length]);
  }

  async function copyEvidence(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      toast.success("Copied");
    } catch {
      toast.error("Could not copy");
    }
  }

  const selectedRule = selected ? ruleOf(selected) : null;
  const pairRows = useMemo(
    () =>
      selected && selectedRule
        ? flags.filter(
            (f) => f.file === selected.file && ruleOf(f) === selectedRule,
          )
        : [],
    [flags, selected, selectedRule],
  );

  useEffect(() => {
    if (!jobId || !selected || !selectedRule) {
      setCrossData(null);
      return;
    }
    let alive = true;
    api
      .document(jobId, selected.file)
      .then((d) => {
        if (alive) setCrossData(d);
      })
      .catch(() => {
        if (alive) setCrossData(null);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, selected?.file, selectedRule]);

  async function actPair(status: "confirmed" | "issue") {
    if (!jobId || !pairRows.length) return;
    setBusy(true);
    try {
      for (const row of pairRows) {
        const clean = { ...row } as Record<string, unknown>;
        delete clean.review;
        delete clean.evidence;
        await api.review(jobId, clean, status, note);
      }
      toast.success(
        status === "confirmed"
          ? "Cross-check confirmed"
          : "Cross-check marked as issue",
      );
      await load(selected);
    } catch (e) {
      notifyError(e, "save the review");
    } finally {
      setBusy(false);
    }
  }

  if (!jobId || picking) {
    return (
      <RunPicker
        selectedId={jobId}
        onPick={(id) => {
          onSelectJob(id);
          setPicking(false);
        }}
        title="Choose a run to review"
        description="Reviews are per run — pick the run first; flags, evidence and review state never mix across runs."
        actionLabel="Review"
      />
    );
  }

  async function refreshFlags() {
    if (!jobId) return;
    setBusy(true);
    try {
      const res = await api.recomputeFlags(jobId);
      toast.success(`Flags rebuilt — ${plural(res.flags, "row")}`);
      await load(selected);
    } catch (e) {
      notifyError(e, "refresh flags");
    } finally {
      setBusy(false);
    }
  }

  async function applyAll() {
    if (!jobId) return;
    setBusy(true);
    try {
      const res = await api.applyAllReviews(jobId);
      toast.success(
        `Applied ${plural(res.entries, "review")} across ${plural(res.stems, "file")}`,
      );
      await load(selected);
    } catch (e) {
      notifyError(e, "apply the reviews");
    } finally {
      setBusy(false);
    }
  }

  if (!loaded) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Review</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-9 w-full" />
          ))}
        </CardContent>
      </Card>
    );
  }

  if (!flags.length) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Review</CardTitle>
        </CardHeader>
        <CardContent>
          <EmptyState
            icon={ShieldCheck}
            title="No flags in this run"
            description="Every extracted value passed the verification stack — nothing needs review."
            action={
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={refreshFlags}
                  disabled={busy}
                >
                  <RefreshCw /> Refresh flags
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setPicking(true)}
                >
                  Change run
                </Button>
              </div>
            }
          />
        </CardContent>
      </Card>
    );
  }

  const boxes: OverlayBox[] = selected?.evidence
    ? [
        { bbox: selected.evidence.bbox, kind: "located" },
        ...(selected.evidence.claimed_page === selected.evidence.page
          ? [{ bbox: selected.evidence.claimed_bbox, kind: "claimed" as const }]
          : []),
      ]
    : [];

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center gap-3">
            Flags
            <Badge variant="outline">{counts.total} total</Badge>
            <Badge variant="outline" className="border-primary/60 text-primary">
              {counts.pending} pending
            </Badge>
            <Badge
              variant="outline"
              className="border-emerald-500/60 text-emerald-500"
            >
              {counts.confirmed} confirmed
            </Badge>
            <Badge
              variant="outline"
              className="border-destructive/60 text-destructive"
            >
              {counts.issues} issues
            </Badge>
            <Button
              variant="ghost"
              size="sm"
              className="ml-auto"
              onClick={() => setPicking(true)}
            >
              Change run
            </Button>
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex w-36 flex-col gap-1.5">
              <Label className="text-xs text-muted-foreground">Status</Label>
              <Select value={statusFilter} onValueChange={setStatusFilter}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All</SelectItem>
                  <SelectItem value="pending">Pending</SelectItem>
                  <SelectItem value="confirmed">Confirmed</SelectItem>
                  <SelectItem value="issue">Issues</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="flex w-44 flex-col gap-1.5">
              <Label className="text-xs text-muted-foreground">File</Label>
              <Select value={fileFilter} onValueChange={setFileFilter}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All files</SelectItem>
                  {files.map((f) => (
                    <SelectItem key={f} value={f}>
                      {f}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex w-44 flex-col gap-1.5">
              <Label className="text-xs text-muted-foreground">Field</Label>
              <Select value={fieldFilter} onValueChange={setFieldFilter}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All fields</SelectItem>
                  {fields.map((f) => (
                    <SelectItem key={f} value={f}>
                      {f}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex w-52 flex-col gap-1.5">
              <Label className="text-xs text-muted-foreground">Reason</Label>
              <Select value={reasonFilter} onValueChange={setReasonFilter}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All reasons</SelectItem>
                  {reasons.map((r) => (
                    <SelectItem key={r} value={r}>
                      {r}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <label className="flex items-center gap-2 pb-2 text-sm">
              <Checkbox
                checked={hideReviewed}
                onCheckedChange={(v) => setHideReviewed(Boolean(v))}
              />
              Hide reviewed
            </label>
            <div className="ml-auto flex gap-2 pb-0.5">
              <Button variant="outline" size="sm" onClick={refreshFlags} disabled={busy}>
                <RefreshCw /> Refresh flags
              </Button>
              <Button variant="outline" size="sm" onClick={applyAll} disabled={busy}>
                Apply reviews to JSON
              </Button>
            </div>
          </div>

          <Table>
            <TableHeader className="sticky top-0 z-10 [&_th]:bg-card">
              <TableRow>
                <TableHead>File</TableHead>
                <TableHead>Field</TableHead>
                <TableHead>Reason</TableHead>
                <TableHead>Rule</TableHead>
                <TableHead className="text-right">Conf.</TableHead>
                <TableHead>Status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filtered.map((f, i) => (
                <TableRow
                  key={i}
                  onClick={() => setSelected(f)}
                  title={f.detail}
                  className={cn(
                    "cursor-pointer",
                    selected && sameFlag(f, selected) && "bg-accent/60",
                  )}
                >
                  <TableCell
                    className="max-w-56 truncate"
                    title={`${f.file}${f.entry ? ` · #${Number(f.entry) + 1}` : ""}`}
                  >
                    {f.file}
                    {f.entry ? ` · #${Number(f.entry) + 1}` : ""}
                  </TableCell>
                  <TableCell className="max-w-44 truncate" title={f.field}>
                    {f.field}
                  </TableCell>
                  <TableCell
                    className="max-w-56 truncate"
                    title={`${f.reason}${f.detail ? ` — ${f.detail}` : ""}`}
                  >
                    {reasonTitle(f.reason, f.reason_label)}
                  </TableCell>
                  <TableCell
                    className="max-w-48 truncate text-xs text-muted-foreground"
                    title={f.rule ?? undefined}
                  >
                    {f.rule ? prettyRule(f.rule) : "—"}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {f.confidence
                      ? Number(f.confidence).toFixed(2)
                      : "—"}
                  </TableCell>
                  <TableCell>{statusBadge(f)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {filtered.length === 0 && (
            <p className="text-sm text-muted-foreground">
              No flags match the filters.
            </p>
          )}
        </CardContent>
      </Card>

      {selected && selectedRule && crossData && (
        <CrossDocPanel
          data={crossData}
          jobId={jobId}
          stem={selected.file}
          pages={docsPages[selected.file] ?? 1}
          rendersAvailable={rendersAvailable}
          focusRule={selectedRule}
          description="Both sides of the cross-document check — review the pair together, or use the per-side actions below."
          actions={() => (
            <>
              <Button
                size="sm"
                onClick={() => actPair("confirmed")}
                disabled={busy}
              >
                Confirm {pairRows.length > 1 ? "both sides" : "side"}
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => actPair("issue")}
                disabled={busy}
              >
                Mark pair as issue
              </Button>
            </>
          )}
        />
      )}

      {selected && (
        <Card>
          <CardHeader>
            <CardTitle className="flex flex-wrap items-center gap-3">
              {selected.field}
              {statusBadge(selected)}
              {selected.confidence != null && (
                <span className="text-xs font-normal text-muted-foreground tabular-nums">
                  confidence {Number(selected.confidence).toFixed(2)}
                </span>
              )}
              <span className="flex items-center gap-1.5 text-sm font-normal text-muted-foreground">
                {reasonTitle(selected.reason, selected.reason_label)}
                {selected.template ? ` · ${selected.template}` : ""}
                <Explain
                  title={reasonTitle(selected.reason, selected.reason_label)}
                  body={selected.reason_help}
                  hint={selected.reason_hint}
                  code={selected.reason}
                />
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="grid gap-6 md:grid-cols-[1.05fr_0.95fr]">
            <div className="flex min-w-0 flex-col gap-3">
              {selected.evidence ? (
                <>
                  <PageOverlay
                    jobId={jobId}
                    stem={selected.file}
                    page={selected.evidence.page}
                    pages={1}
                    boxes={boxes}
                    available={rendersAvailable}
                  />
                  {selected.evidence.text && (
                    <div className="flex items-start gap-2 rounded-md border border-border bg-background p-3">
                      <p className="min-w-0 flex-1 font-mono text-xs leading-relaxed text-muted-foreground">
                        {selected.evidence.text}
                      </p>
                      <button
                        title="copy located text"
                        className="shrink-0 text-muted-foreground hover:text-foreground"
                        onClick={() => copyEvidence(selected.evidence!.text!)}
                      >
                        <Copy className="size-3.5" />
                      </button>
                    </div>
                  )}
                  {selected.evidence.claimed_page != null &&
                    selected.evidence.claimed_page !== selected.evidence.page && (
                      <p className="text-sm text-destructive">
                        model claimed page {selected.evidence.claimed_page}; found
                        on {selected.evidence.page}
                      </p>
                    )}
                </>
              ) : (
                <EmptyState
                  title="No page evidence for this flag"
                  description="This check has no located region — review the value against the document manually."
                  className="py-8"
                />
              )}
            </div>
            <div className="flex flex-col gap-4">
              <div className="flex flex-col gap-1.5">
                <Label className="text-xs text-muted-foreground">Value</Label>
                <Input value={value} onChange={(e) => setValue(e.target.value)} />
                <p className="text-[11px] text-muted-foreground">
                  Confirm saves this value into the run JSON; Mark issue keeps
                  the extracted value unchanged.
                </p>
              </div>
              <div className="flex flex-col gap-1.5">
                <Label className="text-xs text-muted-foreground">Note</Label>
                <Textarea
                  rows={3}
                  placeholder="Why did you confirm or flag this?"
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                />
              </div>
              {selected.sources && (
                <div className="flex flex-col gap-1.5">
                  <Label className="text-xs text-muted-foreground">
                    Sources
                  </Label>
                  <p className="rounded-md border border-border bg-background p-3 font-mono text-xs text-muted-foreground">
                    {selected.sources}
                  </p>
                </div>
              )}
              <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
                <Button onClick={() => act("confirmed")} disabled={busy}>
                  Confirm
                </Button>
                <Button
                  variant="outline"
                  onClick={() => act("issue")}
                  disabled={busy}
                >
                  Mark issue
                </Button>
                <Button
                  variant="ghost"
                  className="ml-auto"
                  onClick={nextPending}
                  disabled={!pending.length}
                  title={`${pending.length} pending flag(s)`}
                >
                  Next pending ({pending.length})
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      )}
    </>
  );
}
