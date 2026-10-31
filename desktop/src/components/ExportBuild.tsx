import { useEffect, useState } from "react";
import { toast } from "sonner";
import { notifyError } from "@/lib/errors";
import { api, SIDECAR_URL } from "@/api";
import type { ExportProfileFull, Job } from "@/api";
import RunPicker from "@/components/RunPicker";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export default function ExportBuild({
  jobId,
  onSelectJob,
}: {
  jobId: string | null;
  onSelectJob: (id: string | null) => void;
}) {
  const [picking, setPicking] = useState(!jobId);
  const [job, setJob] = useState<Job | null>(null);
  const [profiles, setProfiles] = useState<ExportProfileFull[]>([]);
  const [profile, setProfile] = useState("");
  const [file, setFile] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .exportProfiles()
      .then((rows) => {
        setProfiles(rows);
        if (rows.length) setProfile(rows[0].id);
      })
      .catch((e) => notifyError(e));
  }, []);

  useEffect(() => {
    if (!jobId) {
      setJob(null);
      return;
    }
    api
      .job(jobId)
      .then(setJob)
      .catch(() => setJob(null));
  }, [jobId]);

  async function build() {
    if (!jobId) return;
    setBusy(true);
    setFile(null);
    try {
      const res = await api.buildExport(jobId, profile);
      setFile(res.file);
      toast.success(
        res.pruned
          ? `Workbook built · freed ${res.pruned.files} render file(s)`
          : "Workbook built",
      );
    } catch (e) {
      notifyError(e, "build the workbook");
    } finally {
      setBusy(false);
    }
  }

  async function download(name: string) {
    try {
      const res = await fetch(
        `${SIDECAR_URL}/jobs/${jobId}/export/${encodeURIComponent(name)}`,
      );
      if (!res.ok) throw new Error(res.statusText);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      notifyError(e);
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
        title="Choose a run to export"
        description="Builds the colour-coded Excel workbook from that run's reviewed data."
        actionLabel="Export"
      />
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-3">
          Export workbook
          <Button
            variant="ghost"
            size="sm"
            className="ml-auto"
            onClick={() => setPicking(true)}
          >
            Change run
          </Button>
        </CardTitle>
        <CardDescription>
          Run{" "}
          <span className="font-medium text-foreground">
            {job?.name || jobId}
          </span>
          {job?.name ? (
            <span className="font-mono text-[11px] text-muted-foreground">
              {" "}
              {jobId}
            </span>
          ) : null}{" "}
          · page renders are freed automatically after export (setting).
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-wrap items-end gap-4">
        <div className="flex w-64 flex-col gap-1.5">
          <Label className="text-xs text-muted-foreground">Export profile</Label>
          <Select value={profile} onValueChange={setProfile}>
            <SelectTrigger>
              <SelectValue placeholder="profile" />
            </SelectTrigger>
            <SelectContent>
              {profiles.map((p) => (
                <SelectItem key={p.id} value={p.id}>
                  {p.label} ({p.grain})
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <Button onClick={build} disabled={busy || !profile}>
          {busy ? "Building…" : "Build workbook"}
        </Button>
        {file && (
          <Button variant="outline" onClick={() => download(file)}>
            Download {file}
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
