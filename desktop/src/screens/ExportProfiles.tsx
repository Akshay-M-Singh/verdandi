import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { notifyError } from "@/lib/errors";
import { plural } from "@/lib/format";
import {
  ChevronLeft,
  ChevronRight,
  Code2,
  Copy,
  FilePlus2,
  Plus,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import { api } from "@/api";
import type {
  ExportCatalog,
  ExportColumn,
  ExportPreview,
  ExportProfileFull,
  ExportSource,
} from "@/api";
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
import {
  Dialog,
  DialogContent,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
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

function newProfile(): ExportProfileFull {
  return {
    id: "new_export",
    label: "New export",
    grain: "file",
    options: { date_format: "dd/mm/yyyy" },
    columns: [{ header: "File", source: { meta: "original" } }],
  };
}

function sourceValue(source: Record<string, string>): string {
  return JSON.stringify(source);
}

function groupOf(source: Record<string, string>): string {
  if ("meta" in source) return "File info";
  if ("template" in source) return "Documents";
  if ("line_item" in source) return "Line items";
  return "Checks";
}

const STATUS_CLASS: Record<string, string> = {
  pending: "text-primary",
  issue: "text-destructive",
  mismatch: "text-destructive",
  not_checked: "text-muted-foreground",
};

export default function ExportProfiles() {
  const [profiles, setProfiles] = useState<ExportProfileFull[]>([]);
  const [catalog, setCatalog] = useState<ExportCatalog | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [originalId, setOriginalId] = useState<string | null>(null);
  const [draft, setDraft] = useState<ExportProfileFull | null>(null);
  const [busy, setBusy] = useState(false);
  const [seedWorkflow, setSeedWorkflow] = useState("");
  const [preview, setPreview] = useState<ExportPreview | null>(null);
  const [yamlOpen, setYamlOpen] = useState(false);
  const [yamlText, setYamlText] = useState("");
  const [yamlMsg, setYamlMsg] = useState<string | null>(null);

  async function load(select?: string) {
    try {
      const rows = await api.exportProfiles();
      setProfiles(rows);
      const id = select ?? selectedId ?? rows[0]?.id ?? null;
      const profile = rows.find((p) => p.id === id) ?? null;
      setSelectedId(profile?.id ?? null);
      setOriginalId(profile?.id ?? null);
      setDraft(profile ? structuredClone(profile) : null);
      setPreview(null);
    } catch (e) {
      notifyError(e);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!draft) return;
    const timer = window.setTimeout(() => {
      api
        .exportsCatalog(draft)
        .then(setCatalog)
        .catch(() => setCatalog(null));
    }, 250);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft?.workflow, draft?.grain, draft?.line_item_template]);

  const grouped = useMemo(() => {
    const out: Record<string, ExportSource[]> = {};
    for (const s of catalog?.sources ?? []) {
      const g = groupOf(s.source);
      (out[g] ??= []).push(s);
    }
    return out;
  }, [catalog]);

  function select(p: ExportProfileFull) {
    setSelectedId(p.id);
    setOriginalId(p.id);
    setDraft(structuredClone(p));
    setPreview(null);
  }

  const columns = draft?.columns ?? [];
  const setColumns = (next: ExportColumn[]) =>
    setDraft((d) => (d ? { ...d, columns: next } : d));
  const setColumn = (i: number, patch: Partial<ExportColumn>) =>
    setColumns(columns.map((c, j) => (j === i ? { ...c, ...patch } : c)));
  const move = (i: number, dir: -1 | 1) => {
    const next = [...columns];
    const j = i + dir;
    [next[i], next[j]] = [next[j], next[i]];
    setColumns(next);
  };

  function changeSource(i: number, value: string) {
    const source = JSON.parse(value) as Record<string, string>;
    const found = (catalog?.sources ?? []).find(
      (s) => sourceValue(s.source) === value,
    );
    const current = columns[i];
    const prevDefault = (catalog?.sources ?? []).find(
      (s) => sourceValue(s.source) === sourceValue(current.source),
    )?.header;
    const header =
      !current.header || current.header === prevDefault
        ? (found?.header ?? current.header)
        : current.header;
    setColumn(i, { source, header });
  }

  async function save() {
    if (!draft) return;
    setBusy(true);
    try {
      const res = await api.saveExportProfile(draft, originalId);
      toast.success(res.message);
      await load(res.profile.id);
    } catch (e) {
      notifyError(e, "save the export profile");
    } finally {
      setBusy(false);
    }
  }

  async function duplicate() {
    if (!selectedId) return;
    try {
      const res = await api.duplicateExportProfile(selectedId);
      toast.success("Duplicated");
      await load(res.profile.id);
    } catch (e) {
      notifyError(e, "duplicate the export profile");
    }
  }

  async function remove() {
    if (!selectedId) return;
    if (
      !window.confirm(
        `Delete “${draft?.label ?? selectedId}”? This can't be undone.`,
      )
    ) {
      return;
    }
    try {
      await api.deleteExportProfile(selectedId);
      toast.success("Deleted");
      await load();
    } catch (e) {
      notifyError(e, "delete the export profile");
    }
  }

  async function seed() {
    if (!seedWorkflow) return;
    try {
      const res = await api.exportsSeed(seedWorkflow);
      setSelectedId(null);
      setOriginalId(null);
      setDraft(res.profile);
      setPreview(null);
      toast.success(`Seeded ${plural(res.profile.columns.length, "column")}`);
    } catch (e) {
      notifyError(e, "seed columns");
    }
  }

  async function runPreview() {
    if (!draft) return;
    setBusy(true);
    try {
      setPreview(await api.exportsPreview(draft));
    } catch (e) {
      notifyError(e, "build the preview");
    } finally {
      setBusy(false);
    }
  }

  async function openYaml() {
    try {
      const res = await api.exportsYaml();
      setYamlText(res.text);
      setYamlMsg(null);
      setYamlOpen(true);
    } catch (e) {
      notifyError(e);
    }
  }

  async function validateYaml() {
    const res = await api.validateExportsYaml(yamlText);
    setYamlMsg(`${res.ok ? "OK" : "INVALID"} — ${res.message}`);
  }

  async function saveYaml() {
    try {
      const res = await api.saveExportsYaml(yamlText);
      toast.success(res.message);
      setYamlOpen(false);
      await load();
    } catch (e) {
      notifyError(e, "save the YAML");
    }
  }

  return (
    <div className="grid gap-5 lg:grid-cols-[250px_1fr]">
      <Card className="h-fit">
        <CardHeader className="flex-row items-center justify-between gap-2">
          <CardTitle>Export profiles</CardTitle>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setSelectedId(null);
              setOriginalId(null);
              setDraft(newProfile());
              setPreview(null);
            }}
          >
            <FilePlus2 /> New
          </Button>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          {profiles.map((p) => (
            <button
              key={p.id}
              onClick={() => select(p)}
              className={cn(
                "flex flex-col items-start rounded-md px-3 py-2 text-left text-sm",
                p.id === selectedId
                  ? "bg-accent/70 text-primary"
                  : "text-muted-foreground hover:bg-accent/40",
              )}
            >
              <span className="font-medium">{p.label}</span>
              <span className="font-mono text-[11px]">{p.id}</span>
              <span className="text-[10px] text-muted-foreground">
                {p.grain ?? "file"} · {p.columns?.length ?? 0} column(s)
              </span>
            </button>
          ))}
          {!profiles.length && (
            <p className="text-sm text-muted-foreground">No profiles yet.</p>
          )}
          {(catalog?.workflows.length ?? 0) > 0 && (
            <div className="mt-2 flex flex-col gap-2 border-t border-border pt-3">
              <Label className="text-xs text-muted-foreground">
                Seed from workflow
              </Label>
              <Select value={seedWorkflow} onValueChange={setSeedWorkflow}>
                <SelectTrigger>
                  <SelectValue placeholder="workflow" />
                </SelectTrigger>
                <SelectContent>
                  {catalog!.workflows.map((w) => (
                    <SelectItem key={w.id} value={w.id}>
                      {w.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button
                variant="outline"
                size="sm"
                onClick={seed}
                disabled={!seedWorkflow}
              >
                <Sparkles /> Seed columns
              </Button>
            </div>
          )}
        </CardContent>
      </Card>

      <div className="flex min-w-0 flex-col gap-5">
        {!draft ? (
          <Card>
            <CardHeader>
              <CardTitle>No export profile selected</CardTitle>
              <CardDescription>
                Pick one on the left, create a new one, or seed from a
                workflow.
              </CardDescription>
            </CardHeader>
          </Card>
        ) : (
          <>
            <Card>
              <CardHeader>
                <CardTitle>Identity</CardTitle>
              </CardHeader>
              <CardContent className="flex flex-col gap-4">
                <div className="flex flex-wrap gap-4">
                  <div className="flex w-44 flex-col gap-1.5">
                    <Label className="text-xs text-muted-foreground">Id</Label>
                    <Input
                      value={draft.id}
                      onChange={(e) =>
                        setDraft({ ...draft, id: e.target.value })
                      }
                    />
                  </div>
                  <div className="flex w-56 flex-col gap-1.5">
                    <Label className="text-xs text-muted-foreground">Label</Label>
                    <Input
                      value={draft.label}
                      onChange={(e) =>
                        setDraft({ ...draft, label: e.target.value })
                      }
                    />
                  </div>
                  <div className="flex w-52 flex-col gap-1.5">
                    <Label className="text-xs text-muted-foreground">
                      Workflow (scopes sources)
                    </Label>
                    <Select
                      value={draft.workflow ?? "none"}
                      onValueChange={(v) =>
                        setDraft({
                          ...draft,
                          workflow: v === "none" ? undefined : v,
                        })
                      }
                    >
                      <SelectTrigger>
                        <SelectValue placeholder="none" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="none">no workflow</SelectItem>
                        {(catalog?.workflows ?? []).map((w) => (
                          <SelectItem key={w.id} value={w.id}>
                            {w.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="flex w-36 flex-col gap-1.5">
                    <Label className="text-xs text-muted-foreground">Grain</Label>
                    <Select
                      value={draft.grain ?? "file"}
                      onValueChange={(v) =>
                        setDraft({ ...draft, grain: v as "file" | "line_item" })
                      }
                    >
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="file">one row per file</SelectItem>
                        <SelectItem value="line_item">
                          one row per line item
                        </SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  {draft.grain === "line_item" && (
                    <div className="flex w-52 flex-col gap-1.5">
                      <Label className="text-xs text-muted-foreground">
                        Line-item template
                      </Label>
                      <Select
                        value={draft.line_item_template ?? ""}
                        onValueChange={(v) =>
                          setDraft({ ...draft, line_item_template: v })
                        }
                      >
                        <SelectTrigger>
                          <SelectValue placeholder="template" />
                        </SelectTrigger>
                        <SelectContent>
                          {(catalog?.line_item_templates ?? []).map((t) => (
                            <SelectItem key={t} value={t}>
                              {t}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                  )}
                  <div className="flex w-36 flex-col gap-1.5">
                    <Label className="text-xs text-muted-foreground">
                      Date format
                    </Label>
                    <Input
                      value={draft.options?.date_format ?? "dd/mm/yyyy"}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          options: {
                            ...(draft.options ?? {}),
                            date_format: e.target.value,
                          },
                        })
                      }
                    />
                  </div>
                  <label className="flex items-center gap-2 pb-2 text-sm">
                    <Checkbox
                      checked={Boolean(draft.options?.require_reviewed)}
                      onCheckedChange={(v) =>
                        setDraft({
                          ...draft,
                          options: {
                            ...(draft.options ?? {}),
                            require_reviewed: Boolean(v),
                          },
                        })
                      }
                    />
                    Require reviewed
                  </label>
                  <label className="flex items-center gap-2 pb-2 text-sm">
                    <Checkbox
                      checked={Boolean(
                        draft.options?.include_line_items_sheet,
                      )}
                      onCheckedChange={(v) =>
                        setDraft({
                          ...draft,
                          options: {
                            ...(draft.options ?? {}),
                            include_line_items_sheet: Boolean(v),
                          },
                        })
                      }
                    />
                    Include Line items sheet
                  </label>
                </div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="flex-row items-center justify-between gap-2">
                <CardTitle>Columns</CardTitle>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() =>
                    setColumns([
                      ...columns,
                      { header: "", source: { meta: "original" } },
                    ])
                  }
                >
                  <Plus /> Add column
                </Button>
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                {columns.map((col, i) => {
                  const selectedLabel = catalog?.sources.find(
                    (s) =>
                      sourceValue(s.source) === sourceValue(col.source),
                  )?.label;
                  return (
                  <div
                    key={i}
                    className="flex flex-wrap items-end gap-2 rounded-md border border-border p-3"
                  >
                    <div className="flex w-40 flex-col gap-1">
                      <Label className="text-[11px] text-muted-foreground">
                        Header
                      </Label>
                      <Input
                        value={col.header}
                        onChange={(e) => setColumn(i, { header: e.target.value })}
                      />
                    </div>
                    <div className="flex min-w-64 flex-1 flex-col gap-1">
                      <Label className="text-[11px] text-muted-foreground">
                        Data to show
                      </Label>
                      <Select
                        value={sourceValue(col.source)}
                        onValueChange={(v) => changeSource(i, v)}
                      >
                        <SelectTrigger
                          className="w-full min-w-0 *:data-[slot=select-value]:min-w-0"
                          title={selectedLabel}
                        >
                          <SelectValue placeholder="pick a source">
                            <span className="block truncate">
                              {selectedLabel}
                            </span>
                          </SelectValue>
                        </SelectTrigger>
                        <SelectContent>
                          {Object.entries(grouped).map(([group, sources]) => (
                            <SelectGroup key={group}>
                              <SelectLabel>{group}</SelectLabel>
                              {sources.map((s) => (
                                <SelectItem
                                  key={sourceValue(s.source)}
                                  value={sourceValue(s.source)}
                                >
                                  {s.label}
                                </SelectItem>
                              ))}
                            </SelectGroup>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                    <div className="flex w-28 flex-col gap-1">
                      <Label className="text-[11px] text-muted-foreground">
                        Type
                      </Label>
                      <Select
                        value={col.type ?? "auto"}
                        onValueChange={(v) =>
                          setColumn(i, {
                            type: v === "auto" ? undefined : v,
                          })
                        }
                      >
                        <SelectTrigger>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="auto">auto</SelectItem>
                          <SelectItem value="text">text</SelectItem>
                          <SelectItem value="number">number</SelectItem>
                          <SelectItem value="date">date</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                    <div className="flex gap-1 pb-0.5">
                      <Button
                        variant="ghost"
                        size="icon"
                        disabled={i === 0}
                        onClick={() => move(i, -1)}
                      >
                        <ChevronLeft />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        disabled={i === columns.length - 1}
                        onClick={() => move(i, 1)}
                      >
                        <ChevronRight />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        title="duplicate"
                        onClick={() =>
                          setColumns([
                            ...columns.slice(0, i + 1),
                            structuredClone(col),
                            ...columns.slice(i + 1),
                          ])
                        }
                      >
                        <Copy />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() =>
                          setColumns(columns.filter((_, j) => j !== i))
                        }
                      >
                        <X />
                      </Button>
                    </div>
                  </div>
                  );
                })}
                {(catalog?.warnings.length ?? 0) > 0 && (
                  <div className="flex flex-col gap-2">
                    {catalog!.warnings.map((w, i) => (
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
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="flex-row items-center justify-between gap-2">
                <CardTitle>Preview</CardTitle>
                <Button variant="outline" size="sm" onClick={runPreview} disabled={busy}>
                  Preview on latest run
                </Button>
              </CardHeader>
              {preview && (
                <CardContent className="flex flex-col gap-2 overflow-x-auto">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        {preview.headers.map((h, i) => (
                          <TableHead key={i}>{h}</TableHead>
                        ))}
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {preview.rows.map((row, i) => (
                        <TableRow key={i}>
                          {row.cells.map((cell, j) => (
                            <TableCell
                              key={j}
                              title={cell.comment ?? undefined}
                              className={cn(STATUS_CLASS[cell.status])}
                            >
                              {cell.value == null ? "—" : String(cell.value)}
                            </TableCell>
                          ))}
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                  <p className="text-xs text-muted-foreground">
                    Showing {preview.rows.length} of {preview.total_rows} row(s)
                    from run {preview.run}. Amber = pending review, red =
                    issue/mismatch, grey = not checked.
                  </p>
                </CardContent>
              )}
            </Card>

            <div className="flex flex-wrap gap-2">
              <Button onClick={save} disabled={busy}>
                {busy ? "Saving…" : "Save profile"}
              </Button>
              <Button variant="outline" onClick={duplicate} disabled={!selectedId}>
                <Copy /> Duplicate
              </Button>
              <Button
                variant="outline"
                className="text-destructive"
                onClick={remove}
                disabled={!selectedId}
              >
                <Trash2 /> Delete
              </Button>
              <Button variant="ghost" onClick={openYaml}>
                <Code2 /> Expert YAML
              </Button>
            </div>
          </>
        )}
      </div>

      <Dialog open={yamlOpen} onOpenChange={setYamlOpen}>
        <DialogContent className="max-w-4xl">
          <DialogTitle>exports.yaml</DialogTitle>
          <Textarea
            className="min-h-[50vh] font-mono text-xs"
            value={yamlText}
            onChange={(e) => setYamlText(e.target.value)}
          />
          {yamlMsg && <p className="text-xs text-muted-foreground">{yamlMsg}</p>}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={validateYaml}>
              Validate
            </Button>
            <Button onClick={saveYaml}>Save file</Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
