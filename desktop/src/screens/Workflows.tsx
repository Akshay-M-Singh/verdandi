import { useEffect, useState } from "react";
import { toast } from "sonner";
import { notifyError } from "@/lib/errors";
import { plural } from "@/lib/format";
import { ChevronDown, ChevronUp, Code2, Copy, FilePlus2, Plus, Trash2, X } from "lucide-react";
import { api } from "@/api";
import type {
  WarningInfo,
  Workflow,
  WorkflowCrossCheck,
  WorkflowProfile,
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
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

function newWorkflow(): Workflow {
  return {
    id: "new_workflow",
    label: "New workflow",
    description: "",
    documents: [],
    expected: [],
    cross_checks: [],
  };
}

/** Canonical rule id from its sides (2026-10-04): the editor keeps ids in
 *  sync so a renamed field can never leave a stale id that misdescribes the
 *  comparison (the forwarder_chain invoice_number→boe_number drift). */
function derivedCheckId(c: WorkflowCrossCheck): string {
  const { template: lt, field: lf } = c.left;
  const { template: rt, field: rf } = c.right;
  if (!lt || !lf || !rt || !rf) return "";
  return `${lt}.${lf}__${rt}.${rf}`;
}

function OrderedList({
  items,
  options,
  onChange,
}: {
  items: string[];
  options: WorkflowProfile[];
  onChange: (next: string[]) => void;
}) {
  const [pick, setPick] = useState("");
  const remaining = options.filter((o) => !items.includes(o.id));
  const move = (i: number, dir: -1 | 1) => {
    const next = [...items];
    const j = i + dir;
    [next[i], next[j]] = [next[j], next[i]];
    onChange(next);
  };
  return (
    <div className="flex flex-col gap-2">
      {items.map((id, i) => (
        <div
          key={id}
          className="flex items-center gap-1 rounded-md border border-border px-2 py-1"
        >
          <span className="flex-1 text-sm">{id}</span>
          <Button
            variant="ghost"
            size="icon"
            className="size-6"
            disabled={i === 0}
            onClick={() => move(i, -1)}
          >
            <ChevronUp />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="size-6"
            disabled={i === items.length - 1}
            onClick={() => move(i, 1)}
          >
            <ChevronDown />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="size-6"
            onClick={() => onChange(items.filter((x) => x !== id))}
          >
            <X />
          </Button>
        </div>
      ))}
      {remaining.length > 0 && (
        <Select
          value={pick}
          onValueChange={(v) => {
            onChange([...items, v]);
            setPick("");
          }}
        >
          <SelectTrigger className="w-64">
            <SelectValue placeholder="add…" />
          </SelectTrigger>
          <SelectContent>
            {remaining.map((o) => (
              <SelectItem key={o.id} value={o.id}>
                {o.id}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
    </div>
  );
}

export default function Workflows() {
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [profiles, setProfiles] = useState<WorkflowProfile[]>([]);
  const [warnings, setWarnings] = useState<WarningInfo[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [originalId, setOriginalId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Workflow | null>(null);
  const [busy, setBusy] = useState(false);
  const [yamlOpen, setYamlOpen] = useState(false);
  const [yamlText, setYamlText] = useState("");
  const [yamlMsg, setYamlMsg] = useState<string | null>(null);

  async function load(select?: string) {
    try {
      const data = await api.workflows();
      setWorkflows(data.workflows);
      setProfiles(data.profiles);
      setWarnings(data.warnings);
      const id = select ?? selectedId ?? data.workflows[0]?.id ?? null;
      const wf = data.workflows.find((w) => w.id === id) ?? null;
      setSelectedId(wf?.id ?? null);
      setOriginalId(wf?.id ?? null);
      setDraft(wf ? structuredClone(wf) : null);
    } catch (e) {
      notifyError(e);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function select(w: Workflow) {
    setSelectedId(w.id);
    setOriginalId(w.id);
    setDraft(structuredClone(w));
  }

  const checks = draft?.cross_checks ?? [];

  const setCheck = (i: number, patch: Partial<WorkflowCrossCheck>) =>
    setDraft((d) => {
      if (!d) return d;
      return {
        ...d,
        cross_checks: d.cross_checks.map((c, j) => {
          if (j !== i) return c;
          const next = { ...c, ...patch };
          // Auto-id: fill when empty, follow field/template edits while the
          // id is still the previous derived value; never clobber a custom id.
          if (!c.id || c.id === derivedCheckId(c)) {
            const derived = derivedCheckId(next);
            if (derived) next.id = derived;
          }
          return next;
        }),
      };
    });

  function fieldOptions(templateId: string): string[] {
    return profiles.find((p) => p.id === templateId)?.fields ?? [];
  }

  async function save() {
    if (!draft) return;
    setBusy(true);
    try {
      const res = await api.saveWorkflow(draft, originalId);
      toast.success(res.message);
      if ((res.warnings ?? []).length) {
        toast.warning(
          `Saved with ${plural(res.warnings.length, "warning")}`,
          { description: res.warnings.map((w) => w.title).join(" · ") },
        );
      }
      await load(res.workflow.id);
    } catch (e) {
      notifyError(e, "save the workflow");
    } finally {
      setBusy(false);
    }
  }

  async function duplicate() {
    if (!selectedId) return;
    try {
      const res = await api.duplicateWorkflow(selectedId);
      toast.success("Duplicated");
      await load(res.workflow.id);
    } catch (e) {
      notifyError(e, "duplicate the workflow");
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
      await api.deleteWorkflow(selectedId);
      toast.success("Deleted");
      await load();
    } catch (e) {
      notifyError(e, "delete the workflow");
    }
  }

  async function openYaml() {
    try {
      const res = await api.workflowsYaml();
      setYamlText(res.text);
      setYamlMsg(null);
      setYamlOpen(true);
    } catch (e) {
      notifyError(e);
    }
  }

  async function validateYaml() {
    const res = await api.validateWorkflowsYaml(yamlText);
    setYamlMsg(`${res.ok ? "OK" : "INVALID"} — ${res.message}`);
  }

  async function saveYaml() {
    try {
      const res = await api.saveWorkflowsYaml(yamlText);
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
          <CardTitle>Workflows</CardTitle>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setSelectedId(null);
              setOriginalId(null);
              setDraft(newWorkflow());
            }}
          >
            <FilePlus2 /> New
          </Button>
        </CardHeader>
        <CardContent className="flex flex-col gap-1">
          {workflows.map((w) => (
            <button
              key={w.id}
              onClick={() => select(w)}
              className={cn(
                "flex flex-col items-start rounded-md px-3 py-2 text-left text-sm",
                w.id === selectedId
                  ? "bg-accent/70 text-primary"
                  : "text-muted-foreground hover:bg-accent/40",
              )}
            >
              <span className="font-medium">{w.label}</span>
              <span className="font-mono text-[11px]">{w.id}</span>
              <span className="text-[10px] text-muted-foreground">
                {w.documents.length} doc(s) · {w.cross_checks?.length ?? 0}{" "}
                check(s)
              </span>
            </button>
          ))}
          {!workflows.length && (
            <p className="text-sm text-muted-foreground">No workflows yet.</p>
          )}
          {warnings.length > 0 && (
            <div className="flex flex-col gap-2 px-1 pt-2">
              {warnings.map((w, i) => (
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

      <div className="flex min-w-0 flex-col gap-5">
        {!draft ? (
          <Card>
            <CardHeader>
              <CardTitle>No workflow selected</CardTitle>
              <CardDescription>
                Pick one on the left, or create a new workflow.
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
                  <div className="flex w-52 flex-col gap-1.5">
                    <Label className="text-xs text-muted-foreground">Id</Label>
                    <Input
                      value={draft.id}
                      onChange={(e) => setDraft({ ...draft, id: e.target.value })}
                    />
                  </div>
                  <div className="flex w-64 flex-col gap-1.5">
                    <Label className="text-xs text-muted-foreground">Label</Label>
                    <Input
                      value={draft.label}
                      onChange={(e) =>
                        setDraft({ ...draft, label: e.target.value })
                      }
                    />
                  </div>
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label className="text-xs text-muted-foreground">
                    Description
                  </Label>
                  <Input
                    value={draft.description ?? ""}
                    onChange={(e) =>
                      setDraft({ ...draft, description: e.target.value })
                    }
                  />
                </div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Documents</CardTitle>
                <CardDescription>
                  Extracted per file in ONE call — order is prompt order.
                </CardDescription>
              </CardHeader>
              <CardContent>
                <OrderedList
                  items={draft.documents}
                  options={profiles}
                  onChange={(next) => setDraft({ ...draft, documents: next })}
                />
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Expected documents</CardTitle>
                <CardDescription>
                  Completeness contract — may exceed Documents. Missing members
                  are flagged per file (never fabricated).
                </CardDescription>
              </CardHeader>
              <CardContent>
                <OrderedList
                  items={draft.expected}
                  options={profiles}
                  onChange={(next) => setDraft({ ...draft, expected: next })}
                />
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="flex-row items-center justify-between gap-2">
                <CardTitle>Cross-checks</CardTitle>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() =>
                    setDraft({
                      ...draft,
                      cross_checks: [
                        ...checks,
                        {
                          id: "",
                          label: "",
                          left: {
                            template: draft.documents[0] ?? "",
                            field: "",
                          },
                          right: {
                            template: draft.documents[1] ?? "",
                            field: "",
                          },
                        },
                      ],
                    })
                  }
                >
                  <Plus /> Add check
                </Button>
              </CardHeader>
              <CardContent className="flex flex-col gap-4">
                {checks.map((c, i) => (
                  <div
                    key={i}
                    className="flex flex-col gap-2 rounded-md border border-border p-3"
                  >
                    <div className="flex flex-wrap gap-2">
                      <Input
                        className="w-56"
                        placeholder="id"
                        value={c.id ?? ""}
                        onChange={(e) => setCheck(i, { id: e.target.value })}
                      />
                      <Input
                        className="w-56"
                        placeholder="label"
                        value={c.label ?? ""}
                        onChange={(e) => setCheck(i, { label: e.target.value })}
                      />
                      <Button
                        variant="ghost"
                        size="icon"
                        className="ml-auto"
                        onClick={() =>
                          setDraft({
                            ...draft,
                            cross_checks: checks.filter((_, j) => j !== i),
                          })
                        }
                      >
                        <Trash2 />
                      </Button>
                    </div>
                    {(() => {
                      const derived = derivedCheckId(c);
                      if (!derived || c.id === derived) return null;
                      return (
                        <p className="text-[11px] text-primary">
                          id doesn't match the fields —{" "}
                          <button
                            type="button"
                            className="underline"
                            onClick={() => setCheck(i, { id: derived })}
                          >
                            regenerate
                          </button>
                        </p>
                      );
                    })()}
                    <div className="flex flex-wrap items-end gap-2">
                      <div className="flex w-44 flex-col gap-1">
                        <Label className="text-[11px] text-muted-foreground">
                          left template
                        </Label>
                        <Select
                          value={c.left.template}
                          onValueChange={(v) =>
                            setCheck(i, { left: { ...c.left, template: v } })
                          }
                        >
                          <SelectTrigger>
                            <SelectValue placeholder="template" />
                          </SelectTrigger>
                          <SelectContent>
                            {draft.documents.map((d) => (
                              <SelectItem key={d} value={d}>
                                {d}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </div>
                      <div className="flex w-40 flex-col gap-1">
                        <Label className="text-[11px] text-muted-foreground">
                          left field
                        </Label>
                        <Input
                          list={`left-fields-${i}`}
                          value={c.left.field}
                          onChange={(e) =>
                            setCheck(i, { left: { ...c.left, field: e.target.value } })
                          }
                        />
                        <datalist id={`left-fields-${i}`}>
                          {fieldOptions(c.left.template).map((f) => (
                            <option key={f} value={f} />
                          ))}
                        </datalist>
                      </div>
                      <span className="pb-2 text-sm text-muted-foreground">
                        =
                      </span>
                      <div className="flex w-44 flex-col gap-1">
                        <Label className="text-[11px] text-muted-foreground">
                          right template
                        </Label>
                        <Select
                          value={c.right.template}
                          onValueChange={(v) =>
                            setCheck(i, { right: { ...c.right, template: v } })
                          }
                        >
                          <SelectTrigger>
                            <SelectValue placeholder="template" />
                          </SelectTrigger>
                          <SelectContent>
                            {draft.documents.map((d) => (
                              <SelectItem key={d} value={d}>
                                {d}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </div>
                      <div className="flex w-40 flex-col gap-1">
                        <Label className="text-[11px] text-muted-foreground">
                          right field
                        </Label>
                        <Input
                          list={`right-fields-${i}`}
                          value={c.right.field}
                          onChange={(e) =>
                            setCheck(i, {
                              right: { ...c.right, field: e.target.value },
                            })
                          }
                        />
                        <datalist id={`right-fields-${i}`}>
                          {fieldOptions(c.right.template).map((f) => (
                            <option key={f} value={f} />
                          ))}
                        </datalist>
                      </div>
                    </div>
                  </div>
                ))}
                <p className="text-xs text-muted-foreground">
                  A rule fires only when both sides are found; both templates
                  must be in Documents.
                </p>
              </CardContent>
            </Card>

            <div className="flex flex-wrap gap-2">
              <Button onClick={save} disabled={busy}>
                {busy ? "Saving…" : "Save workflow"}
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
          <DialogTitle>workflows.yaml</DialogTitle>
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
