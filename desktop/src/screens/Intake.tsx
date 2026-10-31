import { useEffect, useState } from "react";
import { toast } from "sonner";
import { notifyError } from "@/lib/errors";
import { FolderOpen, FilePlus2, X } from "lucide-react";
import { api, IN_TAURI } from "@/api";
import type {
  ModelsView,
  SettingsView,
  WarningInfo,
  Workflow,
} from "@/api";
import { fmtBytes, plural } from "@/lib/format";
import type { Tab } from "@/App";
import Notice from "@/components/Notice";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
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

async function pickPaths(kind: "file" | "folder"): Promise<string[]> {
  if (!IN_TAURI) return [];
  const { open } = await import("@tauri-apps/plugin-dialog");
  const result =
    kind === "file"
      ? await open({
          multiple: true,
          filters: [{ name: "PDF", extensions: ["pdf"] }],
        })
      : await open({ directory: true, multiple: true });
  if (!result) return [];
  return Array.isArray(result) ? result : [result];
}

export default function Intake({
  onOpenJob,
}: {
  onOpenJob: (id: string, tab: Tab) => void;
}) {
  const [view, setView] = useState<SettingsView | null>(null);
  const [paths, setPaths] = useState<string[]>([]);
  const [profile, setProfile] = useState("");
  const [setMode, setSetMode] = useState<"single" | "multiple" | "workflow">(
    "single",
  );
  const [docIds, setDocIds] = useState<string[]>([]);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [workflowWarnings, setWorkflowWarnings] =
    useState<WarningInfo[]>([]);
  const [workflow, setWorkflow] = useState("");
  const [models, setModels] = useState<ModelsView | null>(null);
  const [model, setModel] = useState("");
  const [workers, setWorkers] = useState(2);
  const [runName, setRunName] = useState("");
  const [captureOnly, setCaptureOnly] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [summary, setSummary] = useState<{
    files: number;
    bytes: number;
    calls_estimate: number;
  } | null>(null);

  useEffect(() => {
    api
      .settings()
      .then((v) => {
        setView(v);
        setProfile(v.profile ?? "");
        setModel(v.model ?? "");
        setWorkers(v.workers || 2);
      })
      .catch((e) => notifyError(e));
    api
      .models()
      .then(setModels)
      .catch(() => undefined); // offline: fall back to the settings default
    api
      .workflows()
      .then((res) => {
        setWorkflows(res.workflows);
        setWorkflowWarnings(res.warnings);
        if (res.workflows.length) setWorkflow(res.workflows[0].id);
      })
      .catch(() => undefined); // workflows are optional
  }, []);

  useEffect(() => {
    if (!IN_TAURI) return;
    let unlisten: (() => void) | undefined;
    import("@tauri-apps/api/webviewWindow")
      .then(({ getCurrentWebviewWindow }) =>
        getCurrentWebviewWindow()
          .onDragDropEvent((event) => {
            if (event.payload.type === "drop") {
              addPaths(event.payload.paths);
            }
          })
          .then((fn) => {
            unlisten = fn;
          }),
      )
      .catch((e) => toast.error(`Drag & drop unavailable: ${e}`));
    return () => unlisten?.();
  }, []);

  function addPaths(next: string[]) {
    setPaths((prev) => Array.from(new Set([...prev, ...next])));
  }

  async function choose(kind: "file" | "folder") {
    try {
      addPaths(await pickPaths(kind));
    } catch (e) {
      toast.error(`Could not open the file picker: ${e}`);
    }
  }

  useEffect(() => {
    if (!paths.length) {
      setSummary(null);
      return;
    }
    const timer = window.setTimeout(() => {
      api
        .pathsSummary(paths)
        .then(setSummary)
        .catch(() => setSummary(null));
    }, 400);
    return () => window.clearTimeout(timer);
  }, [paths]);

  async function run() {
    if (!view) return;
    setBusy(true);
    try {
      const target =
        setMode === "single"
          ? { doc_id: profile || undefined }
          : setMode === "multiple"
            ? { doc_ids: docIds }
            : { workflow: workflow || undefined };
      const job = await api.submit({
        paths,
        model: model || undefined,
        workers,
        mode: captureOnly ? "capture" : "full",
        name: runName.trim() || undefined,
        ...target,
      });
      setPaths([]);
      setRunName("");
      onOpenJob(job.id, "run");
    } catch (e) {
      notifyError(e, "start the run");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Run extraction</CardTitle>
        <CardDescription>
          Pick PDFs or a folder{IN_TAURI ? " — or drag them onto the window" : ""}.
          {view ? (
            <>
              {" "}
              Model <span className="text-primary">{view.model}</span> (change it
              in Settings).
            </>
          ) : null}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2">
          {IN_TAURI ? (
            <>
              <Button variant="outline" onClick={() => choose("file")}>
                <FilePlus2 /> Choose files…
              </Button>
              <Button variant="outline" onClick={() => choose("folder")}>
                <FolderOpen /> Choose folder…
              </Button>
            </>
          ) : (
            <>
              <Input
                className="max-w-md flex-1"
                placeholder="/path/to/invoice.pdf (dev loop: paste paths)"
                value={text}
                onChange={(e) => setText(e.target.value)}
              />
              <Button
                variant="outline"
                onClick={() => {
                  addPaths(text.split("\n").map((s) => s.trim()).filter(Boolean));
                  setText("");
                }}
              >
                Add
              </Button>
            </>
          )}
        </div>

        {!IN_TAURI && (
          <p className="text-xs text-muted-foreground">
            Native file/folder pickers and drag &amp; drop live in the app
            window (<span className="font-mono">make desktop-window</span> or an
            installed build) — this browser dev loop accepts pasted paths.
          </p>
        )}

        {summary && (
          <p className="text-xs text-muted-foreground">
            {plural(summary.files, "PDF")} · {fmtBytes(summary.bytes)} · ≈
            {plural(summary.calls_estimate, "model call")} + small rechecks
            {summary.files > 200 && (
              <span className="text-primary">
                {" "}
                — large batch: capture-only first is cheaper to validate
              </span>
            )}
          </p>
        )}

        {paths.length > 0 && (
          <ul className="max-h-44 overflow-auto rounded-md border border-border">
            {paths.map((p) => (
              <li
                key={p}
                className="flex items-center justify-between gap-3 border-b border-border/60 px-3 py-1.5 text-xs last:border-b-0"
              >
                <span className="truncate font-mono">{p}</span>
                <button
                  className="text-muted-foreground hover:text-foreground"
                  title="remove"
                  onClick={() => setPaths((prev) => prev.filter((x) => x !== p))}
                >
                  <X className="size-3.5" />
                </button>
              </li>
            ))}
          </ul>
        )}

        <div className="flex flex-wrap items-end gap-5">
          <div className="flex w-44 flex-col gap-1.5">
            <Label className="text-xs text-muted-foreground">Document set</Label>
            <Select
              value={setMode}
              onValueChange={(v) =>
                setSetMode(v as "single" | "multiple" | "workflow")
              }
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="single">One profile</SelectItem>
                <SelectItem value="multiple">Several profiles</SelectItem>
                <SelectItem value="workflow">Workflow bundle</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {setMode === "single" && (
            <div className="flex w-52 flex-col gap-1.5">
              <Label className="text-xs text-muted-foreground">Profile</Label>
              <Select value={profile} onValueChange={setProfile}>
                <SelectTrigger>
                  <SelectValue placeholder="profile" />
                </SelectTrigger>
                <SelectContent>
                  {(view?.profiles ?? []).map((p) => (
                    <SelectItem key={p.id} value={p.id}>
                      {p.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}

          {setMode === "multiple" && (
            <div className="flex w-64 flex-col gap-1.5">
              <Label className="text-xs text-muted-foreground">
                Profiles to extract
              </Label>
              <div className="max-h-32 overflow-auto rounded-md border border-border p-2">
                {(view?.profiles ?? []).map((p) => (
                  <label
                    key={p.id}
                    className="flex items-center gap-2 py-0.5 text-sm"
                  >
                    <Checkbox
                      checked={docIds.includes(p.id)}
                      onCheckedChange={(v) =>
                        setDocIds((prev) =>
                          v ? [...prev, p.id] : prev.filter((x) => x !== p.id),
                        )
                      }
                    />
                    {p.label}
                  </label>
                ))}
              </div>
            </div>
          )}

          {setMode === "workflow" && (
            <div className="flex w-80 flex-col gap-1.5">
              <Label className="text-xs text-muted-foreground">Workflow</Label>
              <Select value={workflow} onValueChange={setWorkflow}>
                <SelectTrigger>
                  <SelectValue placeholder="workflow" />
                </SelectTrigger>
                <SelectContent>
                  {workflows.map((w) => (
                    <SelectItem key={w.id} value={w.id}>
                      {w.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {(() => {
                const w = workflows.find((x) => x.id === workflow);
                if (!w) return null;
                return (
                  <p className="text-xs text-muted-foreground">
                    {w.documents.join(" + ")}
                    {w.expected.length
                      ? ` · expects ${w.expected.length}`
                      : ""}
                    {w.cross_checks.length
                      ? ` · ${plural(w.cross_checks.length, "cross-check")}`
                      : ""}
                  </p>
                );
              })()}
              {workflowWarnings.length > 0 && (
                <div className="flex flex-col gap-2">
                  {workflowWarnings.map((w, i) => (
                    <Notice
                      key={i}
                      title={w.title}
                      body={w.body}
                      hint={w.hint}
                      details={w.where}
                    />
                  ))}
                </div>
              )}
            </div>
          )}
          <div className="flex w-60 flex-col gap-1.5">
            <Label className="text-xs text-muted-foreground">
              Model (this run)
            </Label>
            <Select value={model} onValueChange={setModel}>
              <SelectTrigger>
                <SelectValue placeholder="model" />
              </SelectTrigger>
              <SelectContent>
                {(models?.choices ?? []).map((c) => (
                  <SelectItem key={c.id} value={c.id}>
                    {c.id}
                  </SelectItem>
                ))}
                {model &&
                  !(models?.choices ?? []).some((c) => c.id === model) && (
                    <SelectItem value={model}>{model}</SelectItem>
                  )}
              </SelectContent>
            </Select>
          </div>
          <div className="flex w-28 flex-col gap-1.5">
            <Label className="text-xs text-muted-foreground">Parallel files</Label>
            <Input
              type="number"
              min={1}
              max={8}
              value={workers}
              onChange={(e) => setWorkers(Number(e.target.value) || 1)}
            />
          </div>
          <div className="flex w-64 flex-col gap-1.5">
            <Label className="text-xs text-muted-foreground">
              Run name (optional)
            </Label>
            <Input
              placeholder="e.g. July invoices"
              value={runName}
              onChange={(e) => setRunName(e.target.value)}
            />
          </div>
          <label className="flex items-center gap-2 pb-2 text-sm">
            <Checkbox
              checked={captureOnly}
              onCheckedChange={(v) => setCaptureOnly(Boolean(v))}
            />
            Capture only (free preview)
          </label>
          <Button
            className="ml-auto"
            onClick={run}
            disabled={
              busy ||
              paths.length === 0 ||
              !view ||
              (setMode === "multiple" && docIds.length === 0) ||
              (setMode === "workflow" && !workflow)
            }
          >
            {busy ? "Starting…" : captureOnly ? "Run capture" : "Run extraction"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
