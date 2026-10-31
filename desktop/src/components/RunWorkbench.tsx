import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { notifyError } from "@/lib/errors";
import { plural } from "@/lib/format";
import { Plus } from "lucide-react";
import { api } from "@/api";
import type { Job } from "@/api";
import type { Tab } from "@/App";
import InspectPanel from "@/components/InspectPanel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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

const ACTIVE = new Set(["queued", "running"]);

function stateBadge(state: Job["state"]) {
  const cls =
    state === "done"
      ? "border-emerald-500/60 text-emerald-500"
      : state === "failed"
        ? "border-destructive/60 text-destructive"
        : state === "cancelled"
          ? "text-muted-foreground"
          : "border-primary/60 text-primary";
  return (
    <Badge variant="outline" className={cls}>
      {state}
    </Badge>
  );
}

function modeLabel(mode: Job["mode"]) {
  if (mode === "capture") return "capture only";
  if (mode === "extract") return "extraction";
  return "full";
}

/** The Run tab's workbench (2026-10-06): everything that MOVES a run forward.
 *
 * Capture → inspect → pin → extract all happen here (InspectPanel appears when
 * a capture-only run finishes); Cancel and Retry unfinished files live here
 * too. Console keeps the history/metrics/logs (data + forensics) and links
 * back with "Continue in Run". */
export default function RunWorkbench({
  jobId,
  onOpenJob,
  onNewRun,
}: {
  jobId: string;
  onOpenJob: (id: string, tab: Tab) => void;
  onNewRun: () => void;
}) {
  const [job, setJob] = useState<Job | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const jobRef = useRef<Job | null>(null);

  useEffect(() => {
    jobRef.current = job;
  }, [job]);

  useEffect(() => {
    setJob(null);
    let alive = true;
    const load = async () => {
      try {
        const j = await api.job(jobId);
        if (alive) setJob(j);
      } catch (e) {
        if (alive) notifyError(e);
      }
    };
    load();
    const timer = window.setInterval(() => {
      const current = jobRef.current;
      if (!current || ACTIVE.has(current.state)) load();
    }, 1000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [jobId]);

  async function cancel() {
    if (!window.confirm("Stop this run? Finished files keep their results.")) {
      return;
    }
    setCancelling(true);
    try {
      setJob(await api.cancel(jobId));
      toast.success("Run cancelled");
    } catch (e) {
      notifyError(e, "stop the run");
    } finally {
      setCancelling(false);
    }
  }

  async function retry() {
    setRetrying(true);
    try {
      const next = await api.retry(jobId);
      toast.success("Retry queued");
      onOpenJob(next.id, "run");
    } catch (e) {
      notifyError(e, "retry the run");
    } finally {
      setRetrying(false);
    }
  }

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

  const active = ACTIVE.has(job.state);
  const extracted = job.files.some((f) => f.status === "extracted");
  const processed = job.files.filter(
    (f) => f.status === "extracted" || f.status === "captured",
  ).length;
  const failed = job.files.filter(
    (f) => f.status === "failed" || f.status === "error",
  ).length;
  const progressPct = job.stems.length
    ? Math.round((processed / job.stems.length) * 100)
    : 0;

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center gap-3">
            <span className={job.name ? "text-sm" : "font-mono text-sm"}>
              {job.name || job.id}
            </span>
            {job.name && (
              <span className="font-mono text-[11px] text-muted-foreground">
                {job.id}
              </span>
            )}
            {stateBadge(job.state)}
            <Badge variant="outline">{modeLabel(job.mode)}</Badge>
            {job.renders_pruned && (
              <Badge variant="outline" className="text-muted-foreground">
                renders pruned
              </Badge>
            )}
            <Button
              variant="ghost"
              size="sm"
              className="ml-auto"
              onClick={onNewRun}
            >
              <Plus /> New run
            </Button>
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
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
            <span>
              {processed}/{job.stems.length} files done
            </span>
            {failed > 0 && (
              <span className="text-destructive">{failed} failed</span>
            )}
            {active && (
              <span className="text-muted-foreground">
                {job.state === "queued" ? "waiting in the queue…" : "working…"}
              </span>
            )}
            {!active && !extracted && job.mode === "capture" && (
              <span className="text-muted-foreground">
                Capture finished — inspect the pages and extract below.
              </span>
            )}
          </div>

          {job.stems.length > 1 && (
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-border">
              <div
                className="h-full bg-primary transition-all"
                style={{ width: `${progressPct}%` }}
              />
            </div>
          )}

          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>File</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Detail</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {job.files.map((f) => (
                <TableRow key={f.stem}>
                  <TableCell
                    className="max-w-64 truncate font-mono text-xs"
                    title={f.stem}
                  >
                    {f.stem}
                  </TableCell>
                  <TableCell>{f.status}</TableCell>
                  <TableCell
                    className="max-w-72 truncate text-xs text-destructive"
                    title={f.error ?? undefined}
                  >
                    {f.error ?? ""}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>

          {job.error && <p className="text-sm text-destructive">{job.error}</p>}

          <div className="flex flex-wrap items-center gap-2">
            {active && (
              <Button variant="outline" onClick={cancel} disabled={cancelling}>
                {cancelling ? "Stopping…" : "Cancel run"}
              </Button>
            )}
            {job.state === "failed" && (
              <Button variant="outline" onClick={retry} disabled={retrying}>
                {retrying ? "Retrying…" : "Retry unfinished files"}
              </Button>
            )}
            {extracted && (
              <>
                <Button
                  variant="outline"
                  onClick={() => onOpenJob(job.id, "results")}
                >
                  {active ? "Results so far" : "View results"}
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
          </div>

          <p
            className="truncate font-mono text-xs text-muted-foreground"
            title={job.run_dir}
          >
            {job.run_dir}
          </p>
        </CardContent>
      </Card>

      {job.mode === "capture" && job.state === "done" && (
        <InspectPanel job={job} onStarted={setJob} />
      )}
    </>
  );
}
