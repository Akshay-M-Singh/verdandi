import { useEffect, useState, type MouseEvent } from "react";
import { toast } from "sonner";
import { notifyError } from "@/lib/errors";
import { plural } from "@/lib/format";
import { PlayCircle, Trash2 } from "lucide-react";
import { api } from "@/api";
import type { Job, JobMetrics } from "@/api";
import type { Tab } from "@/App";
import EmptyState from "@/components/EmptyState";
import RunInspector from "@/components/RunInspector";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Card,
  CardContent,
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

export default function Runs({
  onOpenJob,
  onStartRun,
  selectedJobId = null,
  onSelectJob,
  onJobRenamed,
}: {
  onOpenJob: (id: string, tab: Tab) => void;
  onStartRun?: () => void;
  selectedJobId?: string | null;
  onSelectJob?: (id: string | null) => void;
  onJobRenamed?: (name: string) => void;
}) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [metrics, setMetrics] = useState<Record<string, JobMetrics>>({});
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const jobKey = jobs.map((j) => j.id).join(",");

  const load = () =>
    api
      .jobs()
      .then(setJobs)
      .catch((e) => notifyError(e))
      .finally(() => setLoaded(true));

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 5000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const ids = jobKey ? jobKey.split(",") : [];
    if (!ids.length) return;
    const timer = window.setTimeout(() => {
      api
        .jobsMetrics(ids)
        .then(setMetrics)
        .catch(() => undefined);
    }, 300);
    return () => window.clearTimeout(timer);
  }, [jobKey]);

  const deletable = jobs.filter(
    (j) => j.state !== "running" && j.state !== "queued",
  );
  const allSelected =
    deletable.length > 0 && deletable.every((j) => selected.has(j.id));
  const selectedJob = jobs.find((j) => j.id === selectedJobId) ?? null;

  function toggle(id: string, on: boolean) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  async function remove(id: string, event: MouseEvent) {
    event.stopPropagation();
    const job = jobs.find((j) => j.id === id);
    if (
      !window.confirm(
        `Delete “${job?.name || id}”? Its results, reviews and page images ` +
          `will be removed from this computer. This can't be undone.`,
      )
    ) {
      return;
    }
    try {
      await api.deleteJob(id);
      setJobs((prev) => prev.filter((j) => j.id !== id));
      toast.success("Run deleted");
    } catch (e) {
      notifyError(e, "delete the run");
    }
  }

  async function removeSelected() {
    const ids = [...selected];
    if (!ids.length) return;
    if (
      !window.confirm(
        `Delete ${plural(ids.length, "run")} and everything in them? ` +
          `This can't be undone.`,
      )
    ) {
      return;
    }
    try {
      for (const id of ids) {
        await api.deleteJob(id);
      }
      toast.success(`Deleted ${plural(ids.length, "run")}`);
      setSelected(new Set());
      await load();
    } catch (e) {
      notifyError(e, "delete the runs");
    }
  }

  return (
    <>
    <Card>
      <CardHeader>
        <CardTitle>Console</CardTitle>
      </CardHeader>
      <CardContent>
        {selected.size > 0 && (
          <div className="mb-3 flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              className="text-destructive"
              onClick={removeSelected}
            >
              <Trash2 /> Delete selected ({selected.size})
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setSelected(new Set())}
            >
              Clear
            </Button>
          </div>
        )}
        {!loaded ? (
          <div className="flex flex-col gap-2">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-9 w-full" />
            ))}
          </div>
        ) : jobs.length === 0 ? (
          <EmptyState
            icon={PlayCircle}
            title="No runs yet"
            description="A run captures PDFs (free) and extracts the data you pick. Start one from the New run screen."
            action={
              onStartRun && (
                <Button size="sm" onClick={onStartRun}>
                  Start a run
                </Button>
              )
            }
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-8">
                  <Checkbox
                    checked={allSelected}
                    disabled={!deletable.length}
                    onCheckedChange={(v) => {
                      setSelected(
                        v ? new Set(deletable.map((j) => j.id)) : new Set(),
                      );
                    }}
                  />
                </TableHead>
                <TableHead>Run</TableHead>
                <TableHead>State</TableHead>
                <TableHead>Mode</TableHead>
                <TableHead className="text-right">Files</TableHead>
                <TableHead className="text-right">Flags</TableHead>
                <TableHead>Tokens</TableHead>
                <TableHead>Created</TableHead>
                <TableHead>Model</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {jobs.map((j) => {
                const done = j.files.filter(
                  (f) => f.status === "extracted" || f.status === "captured",
                ).length;
                const busy = j.state === "running" || j.state === "queued";
                return (
                  <TableRow
                    key={j.id}
                    className={cn(
                      "cursor-pointer",
                      selectedJobId === j.id && "bg-accent/60",
                    )}
                    onClick={() => onSelectJob?.(j.id)}
                  >
                    <TableCell onClick={(e) => e.stopPropagation()}>
                      <Checkbox
                        checked={selected.has(j.id)}
                        disabled={busy}
                        onCheckedChange={(v) => toggle(j.id, Boolean(v))}
                      />
                    </TableCell>
                    <TableCell
                      className="max-w-56"
                      title={j.name ? `${j.name} · ${j.id}` : j.id}
                    >
                      {j.name ? (
                        <div className="flex flex-col">
                          <span className="truncate font-medium">{j.name}</span>
                          <span className="font-mono text-[11px] text-muted-foreground">
                            {j.id}
                          </span>
                        </div>
                      ) : (
                        <span className="font-mono text-xs">{j.id}</span>
                      )}
                    </TableCell>
                    <TableCell>{stateBadge(j.state)}</TableCell>
                    <TableCell>
                      {j.mode === "capture" ? "capture" : "full"}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {done}/{j.stems.length}
                    </TableCell>
                    <TableCell className="text-right text-xs tabular-nums">
                      {metrics[j.id]
                        ? `${metrics[j.id].pending}/${metrics[j.id].flags}`
                        : "—"}
                    </TableCell>
                    <TableCell>
                      {(() => {
                        const m = metrics[j.id];
                        if (!m) return <span className="text-xs">—</span>;
                        const total = m.prompt_tokens + m.completion_tokens;
                        const recheck =
                          m.recheck_prompt_tokens +
                          m.recheck_completion_tokens;
                        const all = total + recheck;
                        const perFile = j.stems.length
                          ? Math.round(all / j.stems.length)
                          : all;
                        const pct = (v: number) =>
                          all ? `${(v / all) * 100}%` : "0%";
                        return (
                          <div
                            className="flex flex-col gap-1"
                            title={`${m.prompt_tokens.toLocaleString()} prompt + ${m.completion_tokens.toLocaleString()} completion + ${recheck.toLocaleString()} recheck · ≈${perFile.toLocaleString()}/file`}
                          >
                            <span className="text-xs">
                              {all.toLocaleString()}
                            </span>
                            <span className="flex h-1.5 w-20 overflow-hidden rounded-full bg-border">
                              <span
                                className="bg-primary"
                                style={{ width: pct(m.prompt_tokens) }}
                              />
                              <span
                                className="bg-emerald-500"
                                style={{ width: pct(m.completion_tokens) }}
                              />
                              <span
                                className="bg-muted-foreground"
                                style={{ width: pct(recheck) }}
                              />
                            </span>
                          </div>
                        );
                      })()}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {j.created_at}
                    </TableCell>
                    <TableCell
                      className="max-w-40 truncate text-xs"
                      title={j.model}
                    >
                      {j.model}
                    </TableCell>
                    <TableCell>
                      {!busy && (
                        <button
                          title="delete run"
                          className="text-muted-foreground hover:text-destructive"
                          onClick={(e) => remove(j.id, e)}
                        >
                          <Trash2 className="size-3.5" />
                        </button>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>

    {selectedJob && (
      <RunInspector
        key={selectedJob.id}
        jobId={selectedJob.id}
        initialJob={selectedJob}
        onOpenJob={onOpenJob}
        onDeleted={() => {
          onSelectJob?.(null);
          load();
        }}
        onClose={() => onSelectJob?.(null)}
        onRenamed={(name) => {
          onJobRenamed?.(name);
          load();
        }}
      />
    )}
    </>
  );
}
