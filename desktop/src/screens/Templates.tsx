import { useEffect, useState } from "react";
import { toast } from "sonner";
import { notifyError } from "@/lib/errors";
import { plural } from "@/lib/format";
import { Code2, Copy, FilePlus2, Plus, Trash2, X } from "lucide-react";
import { api } from "@/api";
import type {
  DocRule,
  RowRule,
  Template,
  TemplateColumn,
  TemplateField,
} from "@/api";
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
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

const FIELD_TYPES = ["str", "date", "amount", "name", "object"];
const COLUMN_TYPES = ["str", "date", "amount", "name"];

function newTemplate(): Template {
  return {
    id: "new_template",
    label: "New template",
    description: "",
    signals: { keywords: [], min_score: 0.5 },
    extraction: {
      fields: [
        {
          name: "document_title",
          type: "str",
          description: "",
          threshold: 0.8,
          labels: [],
        },
      ],
      line_items: false,
    },
    allow_no_match: true,
  };
}

function csv(s: string): string[] {
  return s
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
}

/** Comma-list input that keeps a raw draft while typing and commits on
 *  blur/Enter. Parsing on every keystroke (the old behaviour) ate the comma
 *  the moment it was typed, so a second value could never be added. */
function CsvInput({
  value,
  onCommit,
  className,
  placeholder,
}: {
  value: string[];
  onCommit: (values: string[]) => void;
  className?: string;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState(value.join(", "));
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    if (!editing) setDraft(value.join(", "));
  }, [value, editing]);

  const commit = () => {
    setEditing(false);
    const parsed = csv(draft);
    setDraft(parsed.join(", "));
    if (parsed.join("\u0000") !== value.join("\u0000")) onCommit(parsed);
  };

  return (
    <Input
      className={className}
      placeholder={placeholder}
      value={draft}
      onFocus={() => setEditing(true)}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") e.currentTarget.blur();
      }}
    />
  );
}

export default function Templates() {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [refs, setRefs] = useState<Record<string, string[]>>({});
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [originalId, setOriginalId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Template | null>(null);
  const [busy, setBusy] = useState(false);
  const [yamlOpen, setYamlOpen] = useState(false);
  const [yamlText, setYamlText] = useState("");
  const [yamlMsg, setYamlMsg] = useState<string | null>(null);

  async function load(select?: string) {
    try {
      const data = await api.templates();
      setTemplates(data.templates);
      setRefs(data.workflow_refs);
      const id =
        select ?? selectedId ?? data.templates[0]?.id ?? null;
      const profile = data.templates.find((t) => t.id === id) ?? null;
      setSelectedId(profile?.id ?? null);
      setOriginalId(profile?.id ?? null);
      setDraft(profile ? structuredClone(profile) : null);
    } catch (e) {
      notifyError(e);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function select(t: Template) {
    setSelectedId(t.id);
    setOriginalId(t.id);
    setDraft(structuredClone(t));
  }

  function createNew() {
    setSelectedId(null);
    setOriginalId(null);
    setDraft(newTemplate());
  }

  const ext = draft?.extraction ?? {};
  const fields = ext.fields ?? [];
  const columns = ext.line_items_columns ?? [];
  const rowRules = ext.row_arithmetic ?? [];
  const docRules = ext.arithmetic ?? [];

  const setExt = (patch: Partial<Template["extraction"]>) =>
    setDraft((d) =>
      d ? { ...d, extraction: { ...(d.extraction ?? {}), ...patch } } : d,
    );
  const setField = (i: number, patch: Partial<TemplateField>) =>
    setExt({ fields: fields.map((f, j) => (j === i ? { ...f, ...patch } : f)) });
  const setColumn = (i: number, patch: Partial<TemplateColumn>) =>
    setExt({
      line_items_columns: columns.map((c, j) => (j === i ? { ...c, ...patch } : c)),
    });
  const setRowRule = (i: number, patch: Partial<RowRule>) =>
    setExt({
      row_arithmetic: rowRules.map((r, j) => (j === i ? { ...r, ...patch } : r)),
    });
  const setDocRule = (i: number, patch: Partial<DocRule>) =>
    setExt({ arithmetic: docRules.map((r, j) => (j === i ? { ...r, ...patch } : r)) });

  async function save() {
    if (!draft) return;
    setBusy(true);
    try {
      const res = await api.saveTemplate(draft, originalId);
      toast.success(res.message);
      await load(res.profile.id);
    } catch (e) {
      notifyError(e, "save the template");
    } finally {
      setBusy(false);
    }
  }

  async function duplicate() {
    if (!selectedId) return;
    try {
      const res = await api.duplicateTemplate(selectedId);
      toast.success("Duplicated");
      await load(res.profile.id);
    } catch (e) {
      notifyError(e, "duplicate the template");
    }
  }

  async function remove() {
    if (!selectedId) return;
    const used = refs[selectedId] ?? [];
    const extra = used.length
      ? `\n\nWorkflows using it: ${used.join(", ")} (they will skip it and flag missing).`
      : "";
    if (
      !window.confirm(
        `Delete “${draft?.label ?? selectedId}”? This can't be undone.${extra}`,
      )
    ) {
      return;
    }
    try {
      const res = await api.deleteTemplate(selectedId);
      toast.success(`Deleted ${res.deleted}`);
      if (res.warnings.length) {
        toast.warning(
          `Deleted with ${plural(res.warnings.length, "warning")}`,
          { description: res.warnings.map((w) => w.title).join(" · ") },
        );
      }
      await load();
    } catch (e) {
      notifyError(e, "delete the template");
    }
  }

  async function openYaml() {
    try {
      const res = await api.templatesYaml();
      setYamlText(res.text);
      setYamlMsg(null);
      setYamlOpen(true);
    } catch (e) {
      notifyError(e);
    }
  }

  async function validateYaml() {
    const res = await api.validateTemplatesYaml(yamlText);
    setYamlMsg(`${res.ok ? "OK" : "INVALID"} — ${res.message}`);
  }

  async function saveYaml() {
    try {
      const res = await api.saveTemplatesYaml(yamlText);
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
          <CardTitle>Templates</CardTitle>
          <Button variant="outline" size="sm" onClick={createNew}>
            <FilePlus2 /> New
          </Button>
        </CardHeader>
        <CardContent className="flex flex-col gap-1">
          {templates.map((t) => (
            <button
              key={t.id}
              onClick={() => select(t)}
              className={cn(
                "flex flex-col items-start rounded-md px-3 py-2 text-left text-sm",
                t.id === selectedId
                  ? "bg-accent/70 text-primary"
                  : "text-muted-foreground hover:bg-accent/40",
              )}
            >
              <span className="font-medium">{t.label}</span>
              <span className="font-mono text-[11px]">{t.id}</span>
              {(refs[t.id] ?? []).length > 0 && (
                <span className="text-[10px] text-muted-foreground">
                  used by {refs[t.id].join(", ")}
                </span>
              )}
            </button>
          ))}
          {!templates.length && (
            <p className="text-sm text-muted-foreground">No templates yet.</p>
          )}
        </CardContent>
      </Card>

      <div className="flex min-w-0 flex-col gap-5">
        {!draft ? (
          <Card>
            <CardHeader>
              <CardTitle>No template selected</CardTitle>
              <CardDescription>
                Pick one on the left, or create a new template.
              </CardDescription>
            </CardHeader>
          </Card>
        ) : (
          <>
            <Card>
              <CardHeader>
                <CardTitle>Identity</CardTitle>
                <CardDescription>
                  The vision model matches this description; keywords are
                  user-defined helper words.
                </CardDescription>
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
                  <div className="flex w-32 flex-col gap-1.5">
                    <Label className="text-xs text-muted-foreground">
                      Min score
                    </Label>
                    <Input
                      type="number"
                      step="0.05"
                      min={0}
                      max={1}
                      value={draft.signals?.min_score ?? 0.5}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          signals: {
                            ...(draft.signals ?? {}),
                            min_score: Number(e.target.value),
                          },
                        })
                      }
                    />
                  </div>
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label className="text-xs text-muted-foreground">
                    Description (what this document IS)
                  </Label>
                  <Textarea
                    rows={3}
                    value={draft.description}
                    onChange={(e) =>
                      setDraft({ ...draft, description: e.target.value })
                    }
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label className="text-xs text-muted-foreground">
                    Keywords (comma separated)
                  </Label>
                  <CsvInput
                    value={draft.signals?.keywords ?? []}
                    onCommit={(keywords) =>
                      setDraft({
                        ...draft,
                        signals: { ...(draft.signals ?? {}), keywords },
                      })
                    }
                  />
                </div>
                <label className="flex items-center gap-2 text-sm">
                  <Checkbox
                    checked={Boolean(draft.allow_no_match)}
                    onCheckedChange={(v) =>
                      setDraft({ ...draft, allow_no_match: Boolean(v) })
                    }
                  />
                  Allow no-match (no target document found is acceptable)
                </label>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="flex-row items-center justify-between gap-2">
                <CardTitle>Fields</CardTitle>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() =>
                    setExt({
                      fields: [
                        ...fields,
                        {
                          name: `field_${fields.length + 1}`,
                          type: "str",
                          description: "",
                          threshold: 0.8,
                          labels: [],
                        },
                      ],
                    })
                  }
                >
                  <Plus /> Add field
                </Button>
              </CardHeader>
              <CardContent className="flex flex-col gap-2">
                {fields.map((f, i) => (
                  <div key={i} className="flex flex-wrap items-end gap-2">
                    <Input
                      className="w-36"
                      placeholder="name"
                      value={f.name}
                      onChange={(e) => setField(i, { name: e.target.value })}
                    />
                    <Select
                      value={f.type}
                      onValueChange={(v) => setField(i, { type: v })}
                    >
                      <SelectTrigger className="w-28">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {FIELD_TYPES.map((t) => (
                          <SelectItem key={t} value={t}>
                            {t}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <Input
                      className="min-w-48 flex-1"
                      placeholder="description (guides the model)"
                      value={f.description}
                      onChange={(e) =>
                        setField(i, { description: e.target.value })
                      }
                    />
                    <Input
                      className="w-20"
                      type="number"
                      step="0.05"
                      min={0}
                      max={1}
                      title="confidence threshold"
                      value={f.threshold ?? 0.8}
                      onChange={(e) =>
                        setField(i, { threshold: Number(e.target.value) })
                      }
                    />
                    <CsvInput
                      className="w-48"
                      placeholder="labels (comma)"
                      value={f.labels ?? []}
                      onCommit={(labels) => setField(i, { labels })}
                    />
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() =>
                        setExt({ fields: fields.filter((_, j) => j !== i) })
                      }
                    >
                      <X />
                    </Button>
                  </div>
                ))}
                <p className="text-xs text-muted-foreground">
                  name · type · description · threshold · printed labels
                </p>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-3">
                  Line items
                  <Checkbox
                    checked={Boolean(ext.line_items)}
                    onCheckedChange={(v) => setExt({ line_items: Boolean(v) })}
                  />
                </CardTitle>
                <CardDescription>
                  Declared columns + row rules for charge tables.
                </CardDescription>
              </CardHeader>
              {ext.line_items && (
                <CardContent className="flex flex-col gap-5">
                  <div className="flex flex-col gap-2">
                    <div className="flex items-center justify-between">
                      <Label className="text-xs text-muted-foreground">
                        Columns
                      </Label>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() =>
                          setExt({
                            line_items_columns: [
                              ...columns,
                              {
                                name: `column_${columns.length + 1}`,
                                description: "",
                                type: "str",
                              },
                            ],
                          })
                        }
                      >
                        <Plus /> Add column
                      </Button>
                    </div>
                    {columns.map((c, i) => (
                      <div key={i} className="flex flex-wrap items-end gap-2">
                        <Input
                          className="w-32"
                          value={c.name}
                          onChange={(e) => setColumn(i, { name: e.target.value })}
                        />
                        <Select
                          value={c.type}
                          onValueChange={(v) => setColumn(i, { type: v })}
                        >
                          <SelectTrigger className="w-24">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            {COLUMN_TYPES.map((t) => (
                              <SelectItem key={t} value={t}>
                                {t}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                        <Select
                          value={c.role ?? "none"}
                          onValueChange={(v) =>
                            setColumn(i, {
                              role: v === "none" ? undefined : v,
                            })
                          }
                        >
                          <SelectTrigger className="w-32">
                            <SelectValue placeholder="role" />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="none">no role</SelectItem>
                            <SelectItem value="description">
                              description
                            </SelectItem>
                            <SelectItem value="amount">amount</SelectItem>
                          </SelectContent>
                        </Select>
                        <Input
                          className="min-w-44 flex-1"
                          placeholder="description"
                          value={c.description}
                          onChange={(e) =>
                            setColumn(i, { description: e.target.value })
                          }
                        />
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() =>
                            setExt({
                              line_items_columns: columns.filter(
                                (_, j) => j !== i,
                              ),
                            })
                          }
                        >
                          <X />
                        </Button>
                      </div>
                    ))}
                  </div>

                  <div className="flex flex-col gap-2">
                    <div className="flex items-center justify-between">
                      <Label className="text-xs text-muted-foreground">
                        Row rules (per-row math)
                      </Label>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() =>
                          setExt({
                            row_arithmetic: [
                              ...rowRules,
                              {
                                rule: "sum_equals",
                                fields: [],
                                target: "",
                                tolerance: 0.02,
                                missing: "zero",
                              },
                            ],
                          })
                        }
                      >
                        <Plus /> Add row rule
                      </Button>
                    </div>
                    {rowRules.map((r, i) => (
                      <div key={i} className="flex flex-wrap items-end gap-2">
                        <Input className="w-28" value={r.rule} disabled />
                        <CsvInput
                          className="w-48"
                          placeholder="fields (comma)"
                          value={r.fields}
                          onCommit={(fields) => setRowRule(i, { fields })}
                        />
                        <span className="pb-2 text-xs text-muted-foreground">
                          =
                        </span>
                        <Input
                          className="w-28"
                          placeholder="target"
                          value={r.target}
                          onChange={(e) =>
                            setRowRule(i, { target: e.target.value })
                          }
                        />
                        <Input
                          className="w-20"
                          type="number"
                          step="0.01"
                          title="tolerance"
                          value={r.tolerance ?? 0.02}
                          onChange={(e) =>
                            setRowRule(i, { tolerance: Number(e.target.value) })
                          }
                        />
                        <label className="flex items-center gap-1.5 pb-2 text-xs text-muted-foreground">
                          <Checkbox
                            checked={r.missing === "zero"}
                            onCheckedChange={(v) =>
                              setRowRule(i, {
                                missing: v ? "zero" : undefined,
                              })
                            }
                          />
                          missing = zero
                        </label>
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() =>
                            setExt({
                              row_arithmetic: rowRules.filter(
                                (_, j) => j !== i,
                              ),
                            })
                          }
                        >
                          <X />
                        </Button>
                      </div>
                    ))}
                  </div>

                  <div className="flex flex-col gap-2">
                    <div className="flex items-center justify-between">
                      <Label className="text-xs text-muted-foreground">
                        Document rule (rows sum to a header field)
                      </Label>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() =>
                          setExt({
                            arithmetic: [
                              ...docRules,
                              {
                                rule: "line_items_sum_to",
                                target: "",
                                tolerance: 0.02,
                              },
                            ],
                          })
                        }
                      >
                        <Plus /> Add rule
                      </Button>
                    </div>
                    {docRules.map((r, i) => (
                      <div key={i} className="flex flex-wrap items-end gap-2">
                        <Select
                          value={r.rule}
                          onValueChange={(v) => setDocRule(i, { rule: v })}
                        >
                          <SelectTrigger className="w-44">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="line_items_sum_to">
                              line_items_sum_to
                            </SelectItem>
                            <SelectItem value="sum_equals">sum_equals</SelectItem>
                          </SelectContent>
                        </Select>
                        <Input
                          className="w-48"
                          placeholder="header field (target)"
                          value={r.target}
                          onChange={(e) => setDocRule(i, { target: e.target.value })}
                        />
                        <Input
                          className="w-20"
                          type="number"
                          step="0.01"
                          value={r.tolerance ?? 0.02}
                          onChange={(e) =>
                            setDocRule(i, { tolerance: Number(e.target.value) })
                          }
                        />
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() =>
                            setExt({
                              arithmetic: docRules.filter((_, j) => j !== i),
                            })
                          }
                        >
                          <X />
                        </Button>
                      </div>
                    ))}
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <Label className="text-xs text-muted-foreground">
                      Table note (how to read the charge table)
                    </Label>
                    <Textarea
                      rows={3}
                      value={ext.line_items_note ?? ""}
                      onChange={(e) => setExt({ line_items_note: e.target.value })}
                    />
                  </div>
                </CardContent>
              )}
            </Card>

            <div className="flex flex-wrap gap-2">
              <Button onClick={save} disabled={busy}>
                {busy ? "Saving…" : "Save template"}
              </Button>
              <Button
                variant="outline"
                onClick={duplicate}
                disabled={!selectedId}
              >
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
          <DialogTitle>documents.yaml</DialogTitle>
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
