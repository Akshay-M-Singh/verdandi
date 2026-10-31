import { useEffect, useState } from "react";
import { toast } from "sonner";
import Notice from "@/components/Notice";
import { notifyError } from "@/lib/errors";
import { openExternal } from "@/lib/external";
import { FolderOpen, RefreshCw } from "lucide-react";
import { api } from "@/api";
import type { ModelsView, SettingsView } from "@/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { fmtBytes, plural } from "@/lib/format";
import { checkForUpdate, type AvailableUpdate } from "@/lib/updates";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
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

// Radix Select forbids empty item values; map the stored "" (same as primary)
// to this sentinel in the UI.
const VM_PRIMARY = "__primary__";

function listPreview(ids: string[], cap = 6): string {
  if (ids.length <= cap) return ids.join(", ");
  return `${ids.slice(0, cap).join(", ")} +${ids.length - cap} more`;
}

function ago(ts: number | null | undefined): string {
  if (!ts) return "never";
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  return h < 48 ? `${h} h ago` : `${Math.round(h / 24)} days ago`;
}

export default function Settings({
  onUpdateFound,
}: {
  onUpdateFound: (u: AvailableUpdate) => void;
}) {
  const [view, setView] = useState<SettingsView | null>(null);
  const [checkingUpdate, setCheckingUpdate] = useState(false);
  const [models, setModels] = useState<ModelsView | null>(null);
  const [profile, setProfile] = useState("");
  const [model, setModel] = useState("");
  const [verificationModel, setVerificationModel] = useState(VM_PRIMARY);
  const [workers, setWorkers] = useState(2);
  const [apiKey, setApiKey] = useState("");
  const [verifying, setVerifying] = useState<string | null>(null);
  const [verifyingAll, setVerifyingAll] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [testingKey, setTestingKey] = useState(false);
  const [catalogNote, setCatalogNote] = useState<{
    title: string;
    body?: string;
  } | null>(null);
  const [autoPrune, setAutoPrune] = useState(true);
  const [usage, setUsage] = useState<{ runs: number; total_bytes: number } | null>(
    null,
  );

  const apply = (v: SettingsView) => {
    setView(v);
    setProfile(v.profile ?? "");
    setModel(v.model ?? "");
    setVerificationModel(v.verification_model || VM_PRIMARY);
    setWorkers(v.workers || 2);
    setAutoPrune(v.auto_prune_renders !== false);
  };

  const loadUsage = () =>
    api
      .workspaceStorage()
      .then(setUsage)
      .catch(() => undefined);

  const loadModels = async (force = false, quiet = false) => {
    if (force) setRefreshing(true);
    try {
      const res = await api.models(force);
      setModels(res);
      if (res.fallback) {
        setModel(res.fallback.to);
        setCatalogNote({
          title: `Switched to ${res.fallback.to}`,
          body: `${res.fallback.from} is no longer offered by GO, so runs now use the first still-available verified model.`,
        });
      } else if (res.pruned?.length) {
        setCatalogNote({
          title: `Removed ${plural(res.pruned.length, "model")} GO no longer offers`,
          body: `Your verified list was updated (${res.pruned.join(", ")}).`,
        });
      } else if (force && !quiet) {
        toast.success(
          res.catalog_new.length || res.catalog_removed.length
            ? `Catalog refreshed — ${res.catalog_new.length} new, ${res.catalog_removed.length} removed`
            : "Catalog is up to date",
        );
      }
    } catch (e) {
      if (!quiet) notifyError(e, "load the model catalog");
    } finally {
      if (force) setRefreshing(false);
    }
  };

  async function verifyAll() {
    if (!models || !models.unverified.length) return;
    const ids = models.unverified.map((c) => c.id);
    const cost = (models.probe_cost * ids.length).toFixed(4);
    if (
      !window.confirm(
        `Verify ${plural(ids.length, "model")}? One small paid probe each ` +
          `(~$${cost} total).`,
      )
    ) {
      return;
    }
    setVerifyingAll(true);
    let verified = 0;
    try {
      for (const id of ids) {
        setVerifying(id);
        try {
          const res = await api.verifyModel(id);
          if (res.message.includes("verified")) verified += 1;
        } catch {
          /* keep probing the rest */
        }
      }
      toast.success(`Verified ${verified}/${ids.length}`);
      await loadModels();
    } finally {
      setVerifying(null);
      setVerifyingAll(false);
    }
  }

  useEffect(() => {
    api
      .settings()
      .then(apply)
      .catch((e) => notifyError(e));
    // Quiet stale refresh (2026-10-05): the GO catalog moves; if the cached
    // snapshot is older than 5 minutes, refresh in the background. The
    // models.dev hints keep their own 24h TTL.
    api
      .models()
      .then((res) => {
        setModels(res);
        const age = res.catalog_fetched_at
          ? Date.now() / 1000 - res.catalog_fetched_at
          : Number.POSITIVE_INFINITY;
        if (res.catalog_from_cache && age > 300) void loadModels(true, true);
      })
      .catch((e) => notifyError(e, "load the model catalog"));
    loadUsage();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function save() {
    try {
      apply(
        await api.saveSettings({
          profile,
          model,
          workers,
          auto_prune_renders: autoPrune,
          verification_model:
            verificationModel === VM_PRIMARY ? "" : verificationModel,
        }),
      );
      toast.success("Saved");
      loadUsage();
    } catch (e) {
      notifyError(e, "save the settings");
    }
  }

  async function checkUpdates() {
    setCheckingUpdate(true);
    try {
      const u = await checkForUpdate();
      if (u) {
        onUpdateFound(u);
      } else {
        toast.success("You're up to date");
      }
    } catch (e) {
      notifyError(e);
    } finally {
      setCheckingUpdate(false);
    }
  }

  async function saveKey() {
    if (!apiKey.trim()) return;
    try {
      const res = await api.saveSettings({ api_key: apiKey.trim() });
      apply(res);
      setApiKey("");
      toast.success(
        res.key_stored
          ? "Key stored in the OS keychain"
          : "Key works for this session — no keychain available to save it",
      );
    } catch (e) {
      notifyError(e, "store the key");
    }
  }

  async function removeKey() {
    if (
      !window.confirm(
        "Remove the gateway key from this computer? Runs will stop working " +
          "until a new key is added.",
      )
    ) {
      return;
    }
    try {
      apply(await api.saveSettings({ remove_key: true }));
      toast.success("Key removed");
    } catch (e) {
      notifyError(e, "remove the key");
    }
  }

  async function testKey() {
    if (!model) {
      toast.error("Pick a model first");
      return;
    }
    if (
      !window.confirm(
        `Test the key with one tiny paid probe on ${model}? ` +
          `(~100–300 tokens, well under a cent)`,
      )
    ) {
      return;
    }
    setTestingKey(true);
    try {
      const res = await api.verifyModel(model);
      toast.success(res.message);
    } catch (e) {
      notifyError(e, "test the key");
    } finally {
      setTestingKey(false);
    }
  }

  async function verify(id: string) {
    const cost = models ? `~$${models.probe_cost.toFixed(4)}` : "a tiny paid call";
    if (!window.confirm(`Verify “${id}”? This makes one small paid probe (${cost}).`)) {
      return;
    }
    setVerifying(id);
    try {
      const res = await api.verifyModel(id);
      toast.success(res.message);
      await loadModels();
    } catch (e) {
      notifyError(e, "verify the model");
    } finally {
      setVerifying(null);
    }
  }

  async function openWorkspace() {
    try {
      const { openPath } = await import("@tauri-apps/plugin-opener");
      await openPath(view?.workspace ?? "");
    } catch (e) {
      notifyError(e, "open the workspace folder");
    }
  }

  async function resetConfig() {
    if (
      !window.confirm(
        "Reset templates, workflows, exports and settings to the factory " +
          "defaults? Your runs and results are not affected.",
      )
    ) {
      return;
    }
    try {
      const res = await api.resetDefaults();
      apply(res.settings);
      toast.success(`Reset ${plural(res.reset.length, "config file")}`);
      await loadModels();
    } catch (e) {
      notifyError(e, "reset the config");
    }
  }

  if (!view) {
    return (
      <Card>
        <CardHeader>
          <Skeleton className="h-5 w-32" />
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          <Skeleton className="h-4 w-64" />
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-9 w-full" />
        </CardContent>
      </Card>
    );
  }

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle>Defaults</CardTitle>
          <CardDescription>
              Primary model from config:{" "}
              <span className="text-primary">{view.primary_model}</span>
              {models?.offline && (
                <span className="text-muted-foreground">
                  {" "}
                  · catalog offline — showing configured models
                </span>
              )}
              {models && (
                <span className="text-muted-foreground">
                  {" "}
                  · checked {ago(models.catalog_fetched_at)}
                  {models.catalog_new.length || models.catalog_removed.length
                    ? ` · ${models.catalog_new.length} new, ${models.catalog_removed.length} removed`
                    : ""}
                </span>
              )}
          </CardDescription>
          <CardAction>
            <Button
              variant="outline"
              size="sm"
              disabled={refreshing}
              onClick={() => loadModels(true)}
            >
              <RefreshCw className={refreshing ? "animate-spin" : ""} />
              {refreshing ? "Refreshing…" : "Refresh catalog"}
            </Button>
          </CardAction>
        </CardHeader>
        <CardContent className="flex flex-wrap items-end gap-4">
          {catalogNote && (
            <div className="w-full">
              <Notice
                tone="warning"
                title={catalogNote.title}
                body={catalogNote.body}
                action={
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setCatalogNote(null)}
                  >
                    Dismiss
                  </Button>
                }
              />
            </div>
          )}
          {models &&
            !catalogNote &&
            (models.catalog_new.length > 0 || models.catalog_removed.length > 0) && (
              <div className="w-full">
                <Notice
                  tone="info"
                  title="Catalog changed"
                  body={[
                    models.catalog_new.length
                      ? `${plural(models.catalog_new.length, "new model")}: ${listPreview(models.catalog_new)}`
                      : "",
                    models.catalog_removed.length
                      ? `${plural(models.catalog_removed.length, "model")} removed: ${listPreview(models.catalog_removed)}`
                      : "",
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                  hint="New models appear under Vision candidates — verify one to use it."
                  details={`new: ${models.catalog_new.join(", ") || "—"}\nremoved: ${models.catalog_removed.join(", ") || "—"}`}
                />
              </div>
            )}
          <div className="flex w-52 flex-col gap-1.5">
            <Label className="text-xs text-muted-foreground">Profile</Label>
            <Select value={profile} onValueChange={setProfile}>
              <SelectTrigger>
                <SelectValue placeholder="profile" />
              </SelectTrigger>
              <SelectContent>
                {view.profiles.map((p) => (
                  <SelectItem key={p.id} value={p.id}>
                    {p.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex w-72 flex-col gap-1.5">
            <Label className="text-xs text-muted-foreground">Model</Label>
            <Select value={model} onValueChange={setModel}>
              <SelectTrigger className="w-full">
                <SelectValue placeholder="model" />
              </SelectTrigger>
              <SelectContent>
                {(models?.choices ?? []).map((c) => (
                  <SelectItem key={c.id} value={c.id}>
                    {c.id}
                    {c.label ? `  ·  ${c.label}` : ""}
                  </SelectItem>
                ))}
                {models && !models.choices.some((c) => c.id === model) && model && (
                  <SelectItem value={model}>{model}</SelectItem>
                )}
              </SelectContent>
            </Select>
          </div>
          <div className="flex w-72 flex-col gap-1.5">
            <Label className="text-xs text-muted-foreground">
              Backup model (rechecks)
            </Label>
            <Select
              value={verificationModel}
              onValueChange={setVerificationModel}
            >
              <SelectTrigger className="w-full">
                <SelectValue placeholder="same as primary" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={VM_PRIMARY}>Same as primary</SelectItem>
                <SelectItem value="off">Off — no rechecks</SelectItem>
                {(models?.choices ?? []).map((c) => (
                  <SelectItem key={c.id} value={c.id}>
                    {c.id}
                    {c.label ? `  ·  ${c.label}` : ""}
                  </SelectItem>
                ))}
                {verificationModel !== VM_PRIMARY &&
                  verificationModel !== "off" &&
                  !(models?.choices ?? []).some(
                    (c) => c.id === verificationModel,
                  ) && (
                    <SelectItem value={verificationModel}>
                      {verificationModel}
                    </SelectItem>
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
          <label className="flex items-center gap-2 pb-2 text-sm">
            <Checkbox
              checked={autoPrune}
              onCheckedChange={(v) => setAutoPrune(Boolean(v))}
            />
            Free disk after export (delete page renders)
          </label>
          <Button onClick={save}>Save</Button>
        </CardContent>
      </Card>

      {(models?.unverified.length ?? 0) > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Vision candidates</CardTitle>
            <CardDescription>
              Not verified yet — a probe makes one small paid call (
              {models ? `~$${models.probe_cost.toFixed(4)}` : "tiny"} each).
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center gap-2 pb-1">
              <Button
                variant="outline"
                size="sm"
                disabled={verifyingAll || verifying !== null}
                onClick={verifyAll}
              >
                Verify all ({models!.unverified.length}) ~$
                {(models!.probe_cost * models!.unverified.length).toFixed(4)}
              </Button>
              <span className="text-xs text-muted-foreground">
                A probe makes one small paid call each.
              </span>
            </div>
            {models!.unverified.map((c) => (
              <div key={c.id} className="flex items-center justify-between gap-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 truncate text-sm">
                    {c.id}
                    {models!.catalog_new.includes(c.id) && (
                      <Badge
                        variant="outline"
                        className="border-primary/60 text-primary"
                      >
                        new
                      </Badge>
                    )}
                  </div>
                  <div className="truncate text-xs text-muted-foreground">
                    {c.label}
                  </div>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={verifying !== null}
                  onClick={() => verify(c.id)}
                >
                  {verifying === c.id ? "Probing…" : "Verify"}
                </Button>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-3">
            Gateway key
            <Badge
              variant="outline"
              className={
                view.key_present
                  ? "border-emerald-500/60 text-emerald-500"
                  : "border-primary/60 text-primary"
              }
            >
              {view.key_present ? "key set" : "no key"}
            </Badge>
          </CardTitle>
          <CardDescription>
            Stored in the operating system's keychain and sent only to
            opencode.ai — new runs use it automatically.{" "}
            <button
              type="button"
              className="underline"
              onClick={() => void openExternal("https://opencode.ai/auth")}
            >
              Get a key at opencode.ai/auth
            </button>
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-3">
          <Input
            className="max-w-sm"
            type="password"
            placeholder="sk-…"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
          />
          <Button variant="outline" onClick={saveKey} disabled={!apiKey.trim()}>
            Save key
          </Button>
          <Button
            variant="outline"
            onClick={testKey}
            disabled={testingKey || !model}
          >
            {testingKey ? "Testing…" : "Test key"}
          </Button>
          {view.key_present && (
            <Button
              variant="ghost"
              className="text-destructive"
              onClick={removeKey}
            >
              Remove key
            </Button>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Updates</CardTitle>
          <CardDescription>
            Checks the public release channel; installs in place and restarts.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button
            variant="outline"
            onClick={checkUpdates}
            disabled={checkingUpdate}
          >
            {checkingUpdate ? "Checking…" : "Check for updates"}
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Workspace</CardTitle>
          <CardDescription>
            Runs, results, templates and settings live in this folder; reset
            restores the shipped starter templates.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex items-center justify-between gap-4">
          <div className="flex min-w-0 flex-col gap-1">
            <p className="font-mono text-xs text-muted-foreground">
              {view.workspace}
            </p>
            {usage && (
              <p className="text-xs text-muted-foreground">
                {usage.runs} run(s) · {fmtBytes(usage.total_bytes)} used
              </p>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Button variant="outline" size="sm" onClick={openWorkspace}>
              <FolderOpen /> Open folder
            </Button>
            <Button variant="outline" size="sm" onClick={resetConfig}>
              <RefreshCw /> Reset to shipped defaults
            </Button>
          </div>
        </CardContent>
      </Card>
    </>
  );
}
