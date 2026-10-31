import { useEffect, useState } from "react";
import { notifyError } from "@/lib/errors";
import { History } from "lucide-react";
import { api } from "@/api";
import type { Job, JobMetrics } from "@/api";
import EmptyState from "@/components/EmptyState";
import { stateBadge } from "@/components/RunInspector";
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
import { cn } from "@/lib/utils";

export default function RunPicker({
  selectedId = null,
  onPick,
  title,
  description,
  actionLabel = "Select",
}: {
  selectedId?: string | null;
  onPick: (id: string) => void;
  title: string;
  description?: string;
  actionLabel?: string;
}) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [metrics, setMetrics] = useState<Record<string, JobMetrics>>({});
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    api
      .jobs()
      .then(setJobs)
      .catch((e) => notifyError(e))
      .finally(() => setLoaded(true));
  }, []);

  const jobKey = jobs.map((j) => j.id).join(",");
  useEffect(() => {
    const ids = jobKey ? jobKey.split(",") : [];
    if (!ids.length) return;
    api
      .jobsMetrics(ids)
      .then(setMetrics)
      .catch(() => undefined);
  }, [jobKey]);

  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        {description && <CardDescription>{description}</CardDescription>}
      </CardHeader>
      <CardContent>
        {!loaded ? (
          <div className="flex flex-col gap-2">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-9 w-full" />
            ))}
          </div>
        ) : jobs.length === 0 ? (
          <EmptyState
            icon={History}
            title="No runs yet"
            description="Start a run from the New run screen first."
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Run</TableHead>
                <TableHead>Created</TableHead>
                <TableHead>Mode</TableHead>
                <TableHead className="text-right">Files</TableHead>
                <TableHead className="text-right">Flags</TableHead>
                <TableHead>State</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {jobs.map((j) => {
                const done = j.files.filter(
                  (f) => f.status === "extracted" || f.status === "captured",
                ).length;
                const m = metrics[j.id];
                return (
                  <TableRow
                    key={j.id}
                    className={cn(
                      "cursor-pointer",
                      selectedId === j.id && "bg-accent/60",
                    )}
                    onClick={() => onPick(j.id)}
                  >
                    <TableCell
                      className="max-w-64"
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
                    <TableCell className="text-xs text-muted-foreground">
                      {j.created_at}
                    </TableCell>
                    <TableCell>{j.mode}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      {done}/{j.stems.length}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {m ? `${m.pending}/${m.flags}` : "—"}
                    </TableCell>
                    <TableCell>{stateBadge(j.state)}</TableCell>
                    <TableCell className="text-right">
                      <Button variant="outline" size="sm">
                        {actionLabel}
                      </Button>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
