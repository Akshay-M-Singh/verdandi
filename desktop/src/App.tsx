import { Component, type ReactNode, useEffect, useState } from "react";
import { listen } from "@tauri-apps/api/event";
import {
  FileCog,
  FileSearch,
  FileSpreadsheet,
  History,
  Info,
  PanelsTopLeft,
  Play,
  Settings2,
  Stethoscope,
  Workflow as WorkflowIcon,
} from "lucide-react";
import { api, DIAGNOSTICS_URL, IN_TAURI } from "./api";
import type { SettingsView } from "./api";
import { toast } from "sonner";
import { notifyError } from "@/lib/errors";
import { openExternal } from "@/lib/external";
import {
  checkForUpdate,
  installUpdate,
  type AvailableUpdate,
} from "./lib/updates";
import { Toaster } from "@/components/ui/sonner";
import logoLockup from "@/assets/logo-lockup.png";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import Onboarding from "@/components/Onboarding";
import RunWorkbench from "@/components/RunWorkbench";
import { cn } from "@/lib/utils";
import Intake from "./screens/Intake";
import Results from "./screens/Results";
import Review from "./screens/Review";
import Exports, { type ExportsView } from "./screens/Exports";
import Runs from "./screens/Runs";
import Settings from "./screens/Settings";
import Templates from "./screens/Templates";
import Workflows from "./screens/Workflows";

export type Tab =
  | "run"
  | "results"
  | "review"
  | "runs"
  | "templates"
  | "workflows"
  | "exports"
  | "settings";

// Legal documents live in the source repository (linked from About).
const SOURCE_BASE =
  "https://github.com/Akshay-M-Singh/verdandi/blob/main";
const LICENSE_URL = `${SOURCE_BASE}/LICENSE`;
const PRIVACY_URL = `${SOURCE_BASE}/legal/PRIVACY.md`;

const TABS: { key: Tab; label: string; icon: typeof Play }[] = [
  { key: "run", label: "Run", icon: Play },
  { key: "results", label: "Results", icon: FileSearch },
  { key: "review", label: "Review", icon: PanelsTopLeft },
  { key: "runs", label: "Console", icon: History },
  { key: "templates", label: "Templates", icon: FileCog },
  { key: "workflows", label: "Workflows", icon: WorkflowIcon },
  { key: "exports", label: "Exports", icon: FileSpreadsheet },
  { key: "settings", label: "Settings", icon: Settings2 },
];

class ErrorBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <div className="rounded-lg border border-destructive/60 bg-card p-5">
          <h2 className="text-sm font-semibold text-destructive">
            This screen hit an error
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Try switching screens; if it persists, export diagnostics from the
            sidebar and restart the app.
          </p>
          <pre className="mt-3 max-h-48 overflow-auto text-xs text-muted-foreground">
            {String(this.state.error)}
          </pre>
        </div>
      );
    }
    return this.props.children;
  }
}

export default function App() {
  const [engineUp, setEngineUp] = useState<boolean | null>(null);
  const [tab, setTab] = useState<Tab>("run");
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobName, setJobName] = useState("");
  const [exportsView, setExportsView] = useState<ExportsView>("profiles");
  const [onboarded, setOnboarded] = useState<boolean | null>(null);
  const [about, setAbout] = useState<{
    version: string;
    settings: SettingsView;
  } | null>(null);
  const [update, setUpdate] = useState<AvailableUpdate | null>(null);
  const [updateOpen, setUpdateOpen] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);

  useEffect(() => {
    let alive = true;
    const check = () =>
      api
        .health()
        .then(() => alive && setEngineUp(true))
        .catch(() => alive && setEngineUp(false));
    check();
    const timer = window.setInterval(check, 5000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    // The shell's download handler reports where a download landed (macOS
    // saves silently to ~/Downloads; without this the UI gave no feedback).
    if (!IN_TAURI) return;
    const unlisten = listen<{ success: boolean; path: string | null }>(
      "download-finished",
      (event) => {
        if (event.payload.success) {
          toast.success("Saved to Downloads");
        } else {
          toast.error("Download failed");
        }
      },
    );
    return () => {
      void unlisten.then((off) => off());
    };
  }, []);

  useEffect(() => {
    api
      .settings()
      .then((s) => setOnboarded(s.onboarded))
      .catch(() => setOnboarded(true)); // engine down: don't trap the user
  }, []);

  useEffect(() => {
    if (!jobId) {
      setJobName("");
      return;
    }
    let alive = true;
    api
      .job(jobId)
      .then((j) => {
        if (alive) setJobName(j.name || "");
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [jobId]);

  useEffect(() => {
    // Quiet update check on launch (no-op in the browser dev loop).
    checkForUpdate()
      .then((u) => {
        if (u) {
          setUpdate(u);
          setUpdateOpen(true);
        }
      })
      .catch(() => undefined);
  }, []);

  async function runUpdate() {
    setInstalling(true);
    try {
      await installUpdate(setProgress);
      // installUpdate relaunches the app; this line is unreachable on success
    } catch (e) {
      setInstalling(false);
      setProgress(null);
      window.alert(`Update failed: ${e}`);
    }
  }

  const openJob = (id: string, target: Tab) => {
    setJobId(id);
    if (target === "exports") setExportsView("build");
    setTab(target);
  };

  async function showAbout() {
    try {
      const [health, settings] = await Promise.all([
        api.health(),
        api.settings(),
      ]);
      setAbout({ version: health.version, settings });
    } catch (e) {
      notifyError(e, "open About");
    }
  }

  async function downloadDiagnostics() {
    try {
      const res = await fetch(DIAGNOSTICS_URL);
      if (!res.ok) throw new Error(res.statusText);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "verdandi-diagnostics.zip";
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      notifyError(e, "export diagnostics");
    }
  }

  const needsJob = ![
    "run",
    "runs",
    "review",
    "templates",
    "workflows",
    "exports",
    "settings",
  ].includes(tab);

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-56 shrink-0 flex-col border-r border-border bg-sidebar">
        <div className="px-5 pt-6 pb-4">
          <img
            src={logoLockup}
            alt="Verdandi"
            className="w-36 select-none"
            draggable={false}
          />
          <div className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
            <span
              className={cn(
                "size-2 rounded-full",
                engineUp === null
                  ? "bg-muted-foreground"
                  : engineUp
                    ? "bg-emerald-500"
                    : "bg-destructive",
              )}
            />
            {engineUp === null
              ? "connecting…"
              : engineUp
                ? "engine ready"
                : "engine offline"}
          </div>
        </div>
        <nav className="flex flex-col gap-1 px-3">
          {TABS.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              onClick={() => setTab(key)}
              aria-current={tab === key ? "page" : undefined}
              className={cn(
                "flex items-center gap-2.5 rounded-md px-3 py-2 text-sm transition-colors",
                tab === key
                  ? "bg-sidebar-accent font-medium text-primary"
                  : "text-muted-foreground hover:bg-sidebar-accent/60 hover:text-sidebar-foreground",
              )}
            >
              <Icon className="size-4" />
              {label}
            </button>
          ))}
        </nav>
        <div className="mt-auto flex flex-col gap-1 px-3 pb-4">
          <div className="px-2 pb-1 text-[11px] text-muted-foreground">
            {jobId ? (
              <span title={jobName ? `${jobName} · ${jobId}` : jobId}>
                run{" "}
                <span className={jobName ? "" : "font-mono"}>
                  {jobName || jobId}
                </span>
              </span>
            ) : (
              "no run selected"
            )}
          </div>
          <button
            onClick={showAbout}
            className="flex items-center gap-2 rounded-md px-3 py-1.5 text-xs text-muted-foreground hover:bg-sidebar-accent/60 hover:text-sidebar-foreground"
          >
            <Info className="size-3.5" /> About
          </button>
          <button
            onClick={downloadDiagnostics}
            className="flex items-center gap-2 rounded-md px-3 py-1.5 text-xs text-muted-foreground hover:bg-sidebar-accent/60 hover:text-sidebar-foreground"
          >
            <Stethoscope className="size-3.5" /> Export diagnostics
          </button>
        </div>
      </aside>

      <main className="min-w-0 flex-1 px-8 py-7">
        <div className="mx-auto flex max-w-6xl flex-col gap-5">
          <ErrorBoundary>
            {needsJob && !jobId ? (
              <section className="rounded-lg border border-border bg-card p-5">
                <h2 className="text-sm font-semibold">No run selected</h2>
                <p className="mt-1 text-sm text-muted-foreground">
                  Open <strong>Console</strong> and pick one, or start a new
                  run from <strong>Run</strong>.
                </p>
              </section>
            ) : (
              <>
                {tab === "run" &&
                  (jobId ? (
                    <RunWorkbench
                      key={jobId}
                      jobId={jobId}
                      onOpenJob={openJob}
                      onNewRun={() => setJobId(null)}
                    />
                  ) : (
                    <Intake
                      onOpenJob={(id) => {
                        setJobId(id);
                        setTab("run");
                      }}
                    />
                  ))}
                {tab === "results" && jobId && (
                  <Results key={jobId} jobId={jobId} />
                )}
                {tab === "review" && (
                  <Review
                    key={jobId ?? "picker"}
                    jobId={jobId}
                    onSelectJob={setJobId}
                  />
                )}
                {tab === "runs" && (
                  <Runs
                    onOpenJob={openJob}
                    onStartRun={() => setTab("run")}
                    selectedJobId={jobId}
                    onSelectJob={setJobId}
                    onJobRenamed={setJobName}
                  />
                )}
                {tab === "templates" && <Templates />}
                {tab === "workflows" && <Workflows />}
                {tab === "exports" && (
                  <Exports
                    view={exportsView}
                    onView={setExportsView}
                    jobId={jobId}
                    onSelectJob={setJobId}
                  />
                )}
                {tab === "settings" && (
                  <Settings
                    onUpdateFound={(u) => {
                      setUpdate(u);
                      setUpdateOpen(true);
                    }}
                  />
                )}
              </>
            )}
          </ErrorBoundary>
        </div>
      </main>

      <Onboarding
        open={onboarded === false}
        onDone={() => setOnboarded(true)}
      />

      <Dialog open={about !== null} onOpenChange={(o) => !o && setAbout(null)}>
        <DialogContent className="max-w-md">
          <DialogTitle className="flex justify-center">
            <img src={logoLockup} alt="Verdandi" className="w-40 select-none" />
          </DialogTitle>
          <DialogDescription>Version {about?.version ?? "—"}</DialogDescription>
          {about && (
            <div className="flex flex-col gap-1 text-sm">
              <p>
                Model: <span className="text-primary">{about.settings.model}</span>
              </p>
              <p>Profile: {about.settings.profile}</p>
              <p className="text-muted-foreground">
                Gateway key: {about.settings.key_present ? "set" : "not set"}
              </p>
              <p className="break-all font-mono text-xs text-muted-foreground">
                {about.settings.workspace}
              </p>
              <div className="pt-2">
                <Button variant="outline" size="sm" onClick={downloadDiagnostics}>
                  Export diagnostics
                </Button>
              </div>

              <div className="mt-3 flex gap-3 border-t border-border pt-3 text-xs">
                <button
                  className="text-muted-foreground underline hover:text-foreground"
                  onClick={() =>
                    void openExternal(LICENSE_URL).catch((e) =>
                      notifyError(e, "open the license"),
                    )
                  }
                >
                  License
                </button>
                <button
                  className="text-muted-foreground underline hover:text-foreground"
                  onClick={() =>
                    void openExternal(PRIVACY_URL).catch((e) =>
                      notifyError(e, "open the privacy policy"),
                    )
                  }
                >
                  Privacy policy
                </button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      <Dialog
        open={updateOpen && update !== null}
        onOpenChange={(o) => {
          if (!installing) setUpdateOpen(o);
        }}
      >
        <DialogContent className="max-w-md">
          <DialogTitle>Update available</DialogTitle>
          <DialogDescription>
            Verdandi {update?.version} is ready (you have{" "}
            {update?.currentVersion}).
            {update?.body ? ` ${update.body}` : ""}
          </DialogDescription>
          <div className="flex justify-end gap-2 pt-2">
            <Button
              variant="ghost"
              disabled={installing}
              onClick={() => setUpdateOpen(false)}
            >
              Later
            </Button>
            <Button onClick={runUpdate} disabled={installing}>
              {installing
                ? progress != null
                  ? `Installing… ${progress}%`
                  : "Installing…"
                : "Install and restart"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Toaster position="bottom-right" />
    </div>
  );
}
