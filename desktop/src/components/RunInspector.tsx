import { useEffect, useRef, useState, type ReactNode } from "react";
import { toast } from "sonner";
import { notifyError } from "@/lib/errors";
import { Check, Pencil, X } from "lucide-react";
import { api, IN_TAURI } from "@/api";
import type {
  DocumentRow,
  Job,
  JobMetrics,
  JobStorage,
  ModelRates,
  Workflow,
} from "@/api";
import type { Tab } from "@/App";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { fmtBytes, fmtDuration, plural, valueText } from "@/lib/format";
import { cn } from "@/lib/utils";

const ACTIVE = new Set(["queued", "running"]);

export function stateBadge(state: Job["state"]) {
  const variant =
    state === "done"
      ? "text-emerald-500 border-emerald-500/60"
      : state === "failed"
        ? "text-destructive border-destructive/60"
        : state === "cancelled"
          ? "text-muted-foreground"
          : "text-primary border-primary/60";
  return (
    <Badge variant="outline" className={variant}>
      {state}
    </Badge>
  );
}

function fmtCost(v: number): string {
  return `$${v.toFixed(4)}`;
}

function fmtRate(v: number | null | undefined): string {
  return v == null ? "—" : `$${Number(v)}`;
}

function TokenBar({
  prompt,
  completion,
  recheck,
  className,
}: {
  prompt: number;
  completion: number;
  recheck: number;
  className?: string;
}) {
  const total = prompt + completion + recheck;
  const pct = (v: number) => (total ? `${(v / total) * 100}%` : "0%");
  return (
    <span
      className={cn(
        "flex h-2 w-full overflow-hidden rounded-full bg-border",
        className,
      )}
    >
      <span className="bg-primary" style={{ width: pct(prompt) }} />
      <span className="bg-emerald-500" style={{ width: pct(completion) }} />
      <span
        className="bg-muted-foreground"
        style={{ width: pct(recheck) }}
      />
    </span>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <span className="min-w-0 truncate text-sm">{children}</span>
    </div>
  );
}

function Tile({
  label,
  value,
  sub,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-0.5 rounded-lg border border-border bg-card/60 px-3 py-2.5">
      <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <span className="text-lg font-semibold tabular-nums">{value}</span>
      {sub && <span className="text-xs text-muted-foreground">{sub}</span>}
    </div>
  );
}

export default function RunInspector({
  jobId,
  initialJob = null,
  onOpenJob,
  onDeleted,
  onClose,
  onRenamed,
}: {
  jobId: string;
  initialJob?: Job | null;
  onOpenJob: (id: string, tab: Tab) => void;
  onDeleted: () => void;
  onClose?: () => void;
  onRenamed?: (name: string) => void;
}) {
  const [job, setJob] = useState<Job | null>(initialJob);
  const [metrics, setMetrics] = useState<JobMetrics | null>(null);
  const [storage, setStorage] = useState<JobStorage | null>(null);
  const [docs, setDocs] = useState<DocumentRow[]>([]);
  const [summary, setSummary] = useState<Record<string, string>[]>([]);
  const [templates, setTemplates] = useState<Record<string, string>>({});
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [rates, setRates] = useState<ModelRates | null>(null);
  const [logOpen, setLogOpen] = useState(false);
  const [logText, setLogText] = useState("");
  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const [savingName, setSavingName] = useState(false);
  const [pruning, setPruning] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const jobRef = useRef<Job | null>(job);

  useEffect(() => {
    jobRef.current = job;
  }, [job]);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const j = await api.job(jobId);
        if (alive) setJob(j);
      } catch (e) {
        if (alive) notifyError(e);
      }
    };
    if (!jobRef.current) load();
    const timer = window.setInterval(() => {
      const current = jobRef.current;
      if (!current || ACTIVE.has(current.state)) load();
    }, 1000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [jobId]);

  const progressKey = job
    ? `${job.state}:${job.files.map((f) => f.status).join(",")}`
    : "";

  useEffect(() => {
    if (!job) return;
    api
      .storage(jobId)
      .then(setStorage)
      .catch(() => undefined);
    api
      .jobMetrics(jobId)
      .then(setMetrics)
      .catch(() => undefined);
    api
      .documents(jobId)
      .then(setDocs)
      .catch(() => undefined);
    if (job.workflow) {
      api
        .jobSummary(jobId)
        .then(setSummary)
        .catch(() => setSummary([]));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, progressKey, job?.renders_pruned]);

  useEffect(() => {
    api
      .templates()
      .then((v) =>
        setTemplates(
          Object.fromEntries(v.templates.map((t) => [t.id, t.label])),
        ),
      )
      .catch(() => undefined);
    api
      .workflows()
      .then((v) => setWorkflows(v.workflows))
      .catch(() => undefined);
    api
      .rates()
      .then(setRates)
      .catch(() => setRates(null));
  }, []);

  if (!job) {
    return (
      <Card>
        <CardHeader>
          <Skeleton className="h-5 w-48" />
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          <Skeleton className="h-4 w-72" />
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-9 w-full" />
        </CardContent>
      </Card>
    );
  }

  const extracted = job.files.some((f) => f.status === "extracted");
  const captured = job.files.some(
    (f) => f.status === "captured" || f.status === "extracted",
  );
  const processed = job.files.filter(
    (f) => f.status === "extracted" || f.status === "captured",
  ).length;
  const failed = job.files.filter(
    (f) => f.status === "failed" || f.status === "error",
  ).length;
  const pages = docs.reduce((a, d) => a + (d.pages || 0), 0);
  const fields = docs.reduce((a, d) => a + d.fields, 0);
  const items = docs.reduce((a, d) => a + d.line_items, 0);
  const workflow = workflows.find((w) => w.id === job.workflow) ?? null;
  const profiles = job.doc_ids?.length ? job.doc_ids : [job.doc_id];

  let eta: string | null = null;
  if (job.state === "running" && job.started_at) {
    const doneCount = job.files.filter((f) => f.status !== "pending").length;
    const total = job.stems.length;
    const elapsed = (Date.now() - new Date(job.started_at).getTime()) / 1000;
    if (doneCount > 0 && doneCount < total) {
      eta = fmtDuration((elapsed / doneCount) * (total - doneCount));
    }
  }
  let duration: string | null = null;
  if (job.started_at) {
    const end = job.finished_at
      ? new Date(job.finished_at).getTime()
      : Date.now();
    duration = fmtDuration((end - new Date(job.started_at).getTime()) / 1000);
  }

  const rateFor = (model: string) => {
    const r = rates?.models?.[model];
    if (!r || (r.input_per_1m == null && r.output_per_1m == null)) return null;
    return { ci: r.input_per_1m ?? 0, co: r.output_per_1m ?? 0 };
  };
  const costOf = (prompt: number, completion: number, model: string) => {
    const r = rateFor(model);
    if (!r) return null;
    return (prompt / 1e6) * r.ci + (completion / 1e6) * r.co;
  };
  const vsetting = (job.verification_model || "").trim();
  const recheckModel =
    vsetting === "off" ? "" : vsetting || job.model;
  const primaryCost = metrics
    ? costOf(metrics.prompt_tokens, metrics.completion_tokens, job.model)
    : null;
  const recheckCost = metrics
    ? costOf(
        metrics.recheck_prompt_tokens,
        metrics.recheck_completion_tokens,
        recheckModel,
      )
    : null;
  const totalCost =
    primaryCost != null || recheckCost != null
      ? (primaryCost ?? 0) + (recheckCost ?? 0)
      : null;
  const primaryRate = rateFor(job.model);
  const recheckRate = rateFor(recheckModel);

  const totalTokens = metrics
    ? metrics.prompt_tokens +
      metrics.completion_tokens +
      metrics.recheck_prompt_tokens +
      metrics.recheck_completion_tokens
    : 0;
  const perFileAvg =
    metrics && processed ? Math.round(totalTokens / processed) : 0;

  async function openLog() {
    try {
      const res = await api.logs(jobId);
      setLogText(res.log || "(no worker log yet)");
      setLogOpen(true);
    } catch (e) {
      notifyError(e, "open the log");
    }
  }

  async function saveName() {
    if (savingName) return;
    setSavingName(true);
    try {
      const next = await api.renameJob(jobId, nameDraft.trim());
      setJob(next);
      setEditingName(false);
      onRenamed?.(next.name);
      toast.success(next.name ? "Run renamed" : "Run name cleared");
    } catch (e) {
      notifyError(e, "rename the run");
    } finally {
      setSavingName(false);
    }
  }

  async function openFolder() {
    try {
      const { openPath } = await import("@tauri-apps/plugin-opener");
      await openPath(job?.run_dir ?? "");
    } catch (e) {
      notifyError(e, "open the folder");
    }
  }

  async function prune() {
    if (
      !window.confirm(
        "Free up space? Page images will be deleted; results, reviews and " +
          "exports stay. Evidence crops will stop showing.",
      )
    ) {
      return;
    }
    setPruning(true);
    try {
      const res = await api.prune(jobId);
      toast.success(
        res.files
          ? `Freed ${fmtBytes(res.freed_bytes)} (${plural(res.files, "file")})`
          : "Nothing to prune",
      );
      setJob(await api.job(jobId));
      setStorage(await api.storage(jobId));
    } catch (e) {
      notifyError(e, "free up space");
    } finally {
      setPruning(false);
    }
  }

  async function deleteRun() {
    if (
      !window.confirm(
        `Delete “${job?.name || jobId}”? Its results, reviews and page ` +
          `images will be removed from this computer. This can't be undone.`,
      )
    ) {
      return;
    }
    setDeleting(true);
    try {
      await api.deleteJob(jobId);
      toast.success("Run deleted");
      onDeleted();
    } catch (e) {
      notifyError(e, "delete the run");
      setDeleting(false);
    }
  }

  const fileRows = job.files.length
    ? job.files
    : job.stems.map((s) => ({
        stem: s,
        status: "pending" as const,
        found: null,
        error: null,
      }));

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center gap-3">
            Run{" "}
            {editingName ? (
              <span className="flex items-center gap-1.5">
                <Input
                  autoFocus
                  className="h-7 w-56 text-sm"
                  placeholder="name this run"
                  value={nameDraft}
                  onChange={(e) => setNameDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") saveName();
                    if (e.key === "Escape") setEditingName(false);
                  }}
                />
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={saveName}
                  disabled={savingName}
                >
                  <Check className="size-3.5" /> Save
                </Button>
              </span>
            ) : (
              <span className="flex items-center gap-1.5">
                <span className={job.name ? "text-sm" : "font-mono text-sm"}>
                  {job.name || job.id}
                </span>
                {job.name && (
                  <span className="font-mono text-[11px] text-muted-foreground">
                    {job.id}
                  </span>
                )}
                <button
                  title={job.name ? "rename run" : "name this run"}
                  className="text-muted-foreground hover:text-foreground"
                  onClick={() => {
                    setNameDraft(job.name);
                    setEditingName(true);
                  }}
                >
                  <Pencil className="size-3.5" />
                </button>
              </span>
            )}
            {stateBadge(job.state)}
            <Badge variant="outline">
              {job.mode === "capture" ? "capture only" : "full"}
            </Badge>
            {job.renders_pruned && (
              <Badge variant="outline" className="text-muted-foreground">
                renders pruned
              </Badge>
            )}
            {onClose && (
              <Button
                variant="ghost"
                size="sm"
                className="ml-auto"
                onClick={onClose}
              >
                <X /> Close
              </Button>
            )}
          </CardTitle>
          <CardDescription>
            {plural(job.stems.length, "file")} · profile{" "}
            <span className="text-primary">{job.doc_id}</span> · model{" "}
            <span className="text-primary">{job.model}</span>
            {job.pinned_pages ? ` · pinned p${job.pinned_pages}` : ""}
            {job.pinned_label ? ` · ${job.pinned_label}` : ""}
            {job.state === "queued" && job.queue_position != null
              ? ` · queued #${job.queue_position}`
              : ""}
            {duration ? ` · ${duration}` : ""}
            {eta ? ` · ≈${eta} left` : ""}
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
            <Tile
              label="Files processed"
              value={`${processed}/${job.stems.length}`}
              sub={failed ? `${failed} failed` : "no failures"}
            />
            <Tile label="Pages" value={docs.length ? pages : "—"} />
            <Tile label="Fields" value={docs.length ? fields : "—"} />
            <Tile label="Line items" value={docs.length ? items : "—"} />
            <Tile
              label="Flags"
              value={metrics ? metrics.flags : "—"}
              sub={
                metrics
                  ? `${metrics.pending} pending · ${metrics.issues} issue`
                  : undefined
              }
            />
            <Tile
              label="Model calls"
              value={metrics ? processed + metrics.rechecks : "—"}
              sub={metrics ? `${processed} extract · ${metrics.rechecks} recheck` : undefined}
            />
            <Tile label="Duration" value={duration ?? "—"} />
            <Tile
              label="Est. cost"
              value={totalCost != null ? `≈ ${fmtCost(totalCost)}` : "—"}
              sub={
                totalCost != null
                  ? `models.dev rates · ${fmtRate(primaryRate?.ci)}/${fmtRate(primaryRate?.co)} per 1M`
                  : rates?.error
                    ? "rates unavailable (offline)"
                    : "no rates for this model"
              }
            />
          </div>

          {metrics && (
            <p className="text-sm text-muted-foreground">
              {metrics.review_summary ?? "Review summary unavailable"}
            </p>
          )}

          <div className="grid gap-x-6 gap-y-3 border-t border-border pt-4 sm:grid-cols-2 lg:grid-cols-3">
            <Field label="Model">
              {job.model}
              {primaryRate
                ? ` · ${fmtRate(primaryRate.ci)}/${fmtRate(primaryRate.co)} per 1M`
                : ""}
            </Field>
            <Field label="Backup (rechecks)">
              {vsetting === "off"
                ? "off"
                : vsetting
                  ? `${vsetting}${recheckRate ? ` · ${fmtRate(recheckRate.ci)}/${fmtRate(recheckRate.co)} per 1M` : ""}`
                  : `same as primary (${job.model})`}
            </Field>
            <Field label="Profiles">
              {profiles
                .map((p) => templates[p] ?? p)
                .join(" + ")}
            </Field>
            <Field label="Workflow">
              {job.workflow
                ? workflow
                  ? `${workflow.label} (${workflow.documents.length} doc, expects ${workflow.expected.length})`
                  : job.workflow
                : "—"}
            </Field>
            <Field label="Pins">
              {job.pinned_pages
                ? `pages ${job.pinned_pages}${job.pinned_label ? ` · ${job.pinned_label}` : ""}`
                : "—"}
            </Field>
            <Field label="Workers / mode">
              {job.workers} · {job.mode}
            </Field>
            <Field label="Created">{job.created_at}</Field>
            <Field label="Started">{job.started_at ?? "—"}</Field>
            <Field label="Finished">
              {job.finished_at ??
                (ACTIVE.has(job.state) ? "running…" : "—")}
            </Field>
          </div>

          {metrics && (
            <div className="flex flex-col gap-3 border-t border-border pt-4">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <h3 className="text-xs font-semibold tracking-wider text-primary">
                  TOKENS
                </h3>
                <span className="text-xs text-muted-foreground">
                  {totalTokens.toLocaleString()} total
                  {processed
                    ? ` · ≈${perFileAvg.toLocaleString()}/file`
                    : ""}
                </span>
              </div>
              <TokenBar
                prompt={metrics.prompt_tokens}
                completion={metrics.completion_tokens}
                recheck={
                  metrics.recheck_prompt_tokens +
                  metrics.recheck_completion_tokens
                }
                className="h-3"
              />
              <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
                <span className="flex items-center gap-1.5">
                  <span className="size-2 rounded-full bg-primary" />
                  prompt {metrics.prompt_tokens.toLocaleString()}
                  {primaryRate
                    ? ` · ${fmtCost((metrics.prompt_tokens / 1e6) * primaryRate.ci)}`
                    : ""}
                </span>
                <span className="flex items-center gap-1.5">
                  <span className="size-2 rounded-full bg-emerald-500" />
                  completion {metrics.completion_tokens.toLocaleString()}
                  {primaryRate
                    ? ` · ${fmtCost((metrics.completion_tokens / 1e6) * primaryRate.co)}`
                    : ""}
                </span>
                <span className="flex items-center gap-1.5">
                  <span className="size-2 rounded-full bg-muted-foreground" />
                  recheck{" "}
                  {(
                    metrics.recheck_prompt_tokens +
                    metrics.recheck_completion_tokens
                  ).toLocaleString()}{" "}
                  · {plural(metrics.rechecks, "call")}
                  {recheckRate
                    ? ` · ${fmtCost(
                        (metrics.recheck_prompt_tokens / 1e6) *
                          recheckRate.ci +
                          (metrics.recheck_completion_tokens / 1e6) *
                            recheckRate.co,
                      )}`
                    : ""}
                </span>
              </div>
              <p className="text-[11px] text-muted-foreground">
                Cost is an estimate at models.dev community rates
                {recheckModel && recheckModel !== job.model
                  ? ` (rechecks at ${recheckModel})`
                  : ""}
                {vsetting === "off" ? " (rechecks off)" : ""}
                — the GO subscription is quota-based, not per-token billing.
              </p>
            </div>
          )}

          <div className="flex flex-col gap-2 border-t border-border pt-4">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>File</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Found</TableHead>
                  <TableHead>Template</TableHead>
                  <TableHead className="text-right">Pages</TableHead>
                  <TableHead className="text-right">Fields</TableHead>
                  <TableHead className="text-right">Items</TableHead>
                  <TableHead>Tokens</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {fileRows.map((f) => {
                  const d = docs.find((x) => x.stem === f.stem);
                  const usage = metrics?.per_file?.[f.stem];
                  const fileTotal = usage
                    ? usage.prompt_tokens +
                      usage.completion_tokens +
                      usage.recheck_prompt_tokens +
                      usage.recheck_completion_tokens
                    : 0;
                  return (
                    <TableRow key={f.stem}>
                      <TableCell
                        className="max-w-56 truncate"
                        title={f.stem}
                      >
                        {f.stem}
                      </TableCell>
                      <TableCell>
                        {f.status}
                        {f.error && (
                          <div
                            className="max-w-72 truncate text-xs text-destructive"
                            title={f.error}
                          >
                            {f.error}
                          </div>
                        )}
                      </TableCell>
                      <TableCell>
                        {f.found === null ? "—" : f.found ? "yes" : "no"}
                      </TableCell>
                      <TableCell
                        className="max-w-40 truncate"
                        title={d?.selected_name ?? undefined}
                      >
                        {d?.selected_name ?? "—"}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {d?.pages || "—"}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {d ? d.fields : "—"}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {d ? d.line_items : "—"}
                      </TableCell>
                      <TableCell>
                        {usage ? (
                          <div
                            className="flex flex-col gap-1"
                            title={`${usage.prompt_tokens.toLocaleString()} prompt + ${usage.completion_tokens.toLocaleString()} completion + ${(usage.recheck_prompt_tokens + usage.recheck_completion_tokens).toLocaleString()} recheck`}
                          >
                            <span className="text-xs tabular-nums">
                              {fileTotal.toLocaleString()}
                            </span>
                            <TokenBar
                              prompt={usage.prompt_tokens}
                              completion={usage.completion_tokens}
                              recheck={
                                usage.recheck_prompt_tokens +
                                usage.recheck_completion_tokens
                              }
                              className="h-1.5 w-24"
                            />
                          </div>
                        ) : (
                          "—"
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>

          {job.error && <p className="text-sm text-destructive">{job.error}</p>}

          <div className="flex flex-wrap items-center gap-2">
            <Button variant="outline" onClick={openLog}>
              View log
            </Button>
            {IN_TAURI && (
              <Button variant="outline" onClick={openFolder}>
                Open folder
              </Button>
            )}
            {storage && storage.renders_files > 0 && (
              <Button variant="outline" onClick={prune} disabled={pruning}>
                {pruning ? "Freeing…" : "Free up space"}
              </Button>
            )}
            {!ACTIVE.has(job.state) && (
              <Button
                variant="outline"
                className="text-destructive"
                onClick={deleteRun}
                disabled={deleting}
              >
                {deleting ? "Deleting…" : "Delete run"}
              </Button>
            )}
            {extracted && (
              <>
                <Button
                  variant="outline"
                  onClick={() => onOpenJob(job.id, "results")}
                >
                  {ACTIVE.has(job.state) ? "Results so far" : "Results"}
                </Button>
                <Button
                  variant="outline"
                  onClick={() => onOpenJob(job.id, "review")}
                >
                  Review flags
                </Button>
                <Button
                  variant="outline"
                  onClick={() => onOpenJob(job.id, "exports")}
                >
                  Export
                </Button>
              </>
            )}
            {/* Console is data + forensics — the actions that move a run
                forward (inspect/pin/extract, cancel, retry) live in Run. */}
            {job.state === "failed" && (
              <Button variant="outline" onClick={() => onOpenJob(job.id, "run")}>
                Retry in Run
              </Button>
            )}
            {job.state === "done" && !extracted && captured && (
              <Button variant="outline" onClick={() => onOpenJob(job.id, "run")}>
                Continue — inspect &amp; extract
              </Button>
            )}
            {ACTIVE.has(job.state) && (
              <Button variant="outline" onClick={() => onOpenJob(job.id, "run")}>
                Open in Run
              </Button>
            )}
          </div>

          <p
            className="truncate font-mono text-xs text-muted-foreground"
            title={job.run_dir}
          >
            {job.run_dir}
          </p>
          {storage && (
            <p className="text-xs text-muted-foreground">
              Renders {fmtBytes(storage.renders_bytes)} (
              {plural(storage.renders_files, "file")}) · Total{" "}
              {fmtBytes(storage.total_bytes)}
            </p>
          )}
        </CardContent>
      </Card>

      {job.workflow && summary.length > 0 && (
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

      <Dialog open={logOpen} onOpenChange={setLogOpen}>
        <DialogContent className="max-w-3xl">
          <DialogTitle className="text-sm">
            Worker log — run {job.id}
          </DialogTitle>
          <pre className="max-h-[60vh] overflow-auto rounded-md border border-border bg-background p-3 font-mono text-xs">
            {logText}
          </pre>
        </DialogContent>
      </Dialog>
    </>
  );
}
