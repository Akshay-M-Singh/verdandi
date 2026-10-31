import { useEffect, useState, type ReactNode } from "react";
import type {
  CrossCheck,
  CrossPair,
  DocumentData,
  HeaderField,
} from "@/api";
import PageOverlay, { type OverlayBox } from "@/components/PageOverlay";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { valueText } from "@/lib/format";

interface SideEvidence {
  value: string | null;
  page: number;
  boxes: OverlayBox[];
  text: string | null;
  source: string | null;
}

/** Located evidence for one side of a pair: the model's claimed region plus
 *  the text channel's grounding match, exactly like field selection. */
function sideEvidence(
  data: DocumentData,
  entryIndex: number | undefined,
  fieldName: string,
): SideEvidence {
  const entry =
    entryIndex != null ? data.documents?.[entryIndex] : undefined;
  const rec: HeaderField | undefined = entry?.header_fields?.find(
    (f) => f.name === fieldName,
  );
  const match = rec?.verification?.checks?.find(
    (c) => c.check === "grounding",
  )?.match;
  const region = rec?.source_region ?? null;
  const boxes: OverlayBox[] = [];
  if (region) {
    boxes.push({ bbox: region.bbox, kind: "claimed", label: "model claim" });
  }
  if (match?.line_bbox) {
    boxes.push({
      bbox: match.line_bbox,
      kind: "located",
      label: match.text ?? "located text",
    });
  }
  return {
    value: rec?.value ?? null,
    page: match?.page ?? region?.page ?? 1,
    boxes,
    text: match?.text ?? null,
    source: rec?.source ?? null,
  };
}

function outcomeBadge(passed: boolean | null, skip?: string | null) {
  if (passed === true) {
    return (
      <Badge variant="outline" className="border-emerald-500/60 text-emerald-500">
        PASS
      </Badge>
    );
  }
  if (passed === false) {
    return (
      <Badge
        variant="outline"
        className="border-destructive/60 text-destructive"
      >
        FAIL
      </Badge>
    );
  }
  return (
    <Badge variant="outline" className="text-muted-foreground">
      NOT CHECKED{skip ? ` (${skip})` : ""}
    </Badge>
  );
}

function skipText(skip: string | null | undefined): string {
  if (skip === "template_not_found")
    return "One of the documents is not in this file — the workflow's expected-documents contract owns presence.";
  if (skip === "no_value")
    return "Both documents are present but at least one side has no value for the compared field.";
  return skip ?? "Not evaluated.";
}

function SidePanel({
  label,
  template,
  entry,
  field,
  evidence,
  outcome,
  jobId,
  stem,
  pages,
  rendersAvailable,
}: {
  label: string;
  template: string;
  entry: number | undefined;
  field: string;
  evidence: SideEvidence;
  outcome: "equal" | "mismatch" | null;
  jobId: string;
  stem: string;
  pages: number;
  rendersAvailable: boolean;
}) {
  const [page, setPage] = useState(evidence.page);
  useEffect(() => setPage(evidence.page), [evidence.page]);
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold tracking-wider text-primary">
          {label}
        </span>
        <span className="text-sm font-medium">{template}</span>
        {entry != null && (
          <span className="text-xs text-muted-foreground">entry {entry}</span>
        )}
        {outcome && (
          <Badge
            variant="outline"
            className={cn(
              outcome === "equal"
                ? "border-emerald-500/60 text-emerald-500"
                : "border-destructive/60 text-destructive",
            )}
          >
            {outcome === "equal" ? "matches" : "mismatch"}
          </Badge>
        )}
      </div>
      <div className="flex flex-col gap-1 text-sm">
        <span className="font-mono text-xs text-muted-foreground">{field}</span>
        <span className="font-medium">
          {valueText(evidence.value) || "—"}
        </span>
        {evidence.source && (
          <span className="text-xs text-muted-foreground">
            {evidence.source}
          </span>
        )}
      </div>
      <PageOverlay
        jobId={jobId}
        stem={stem}
        page={page}
        pages={pages}
        boxes={evidence.boxes}
        available={rendersAvailable}
        onPage={setPage}
      />
      {evidence.text && (
        <p className="rounded-md border border-border bg-background p-2 font-mono text-xs text-muted-foreground">
          {evidence.text}
        </p>
      )}
    </div>
  );
}

export default function CrossDocPanel({
  data,
  jobId,
  stem,
  pages,
  rendersAvailable,
  focusRule = null,
  actions,
  description,
}: {
  data: DocumentData;
  jobId: string;
  stem: string;
  pages: number;
  rendersAvailable: boolean;
  focusRule?: string | null;
  actions?: (ruleId: string, rule: CrossCheck) => ReactNode;
  description?: string;
}) {
  const checks = data._meta?.verification?.cross_checks ?? {};
  const ruleIds = Object.keys(checks);
  const [ruleId, setRuleId] = useState<string | null>(
    focusRule ?? ruleIds[0] ?? null,
  );
  const [pairIdx, setPairIdx] = useState(0);

  useEffect(() => {
    if (focusRule && checks[focusRule]) setRuleId(focusRule);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusRule]);

  const activeRule = ruleId ? checks[ruleId] : undefined;
  const activePairs: CrossPair[] = activeRule?.pairs ?? [];
  const mismatchedIdx = activePairs.findIndex((p) => !p.equal);

  useEffect(() => {
    // On a failing rule, default to the first mismatched pair.
    setPairIdx(mismatchedIdx >= 0 ? mismatchedIdx : 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ruleId]);

  if (!ruleIds.length) return null;
  const rule = activeRule;
  const pairs = activePairs;
  const selectedPair: CrossPair | undefined = pairs[pairIdx] ?? pairs[0];

  const left = selectedPair
    ? sideEvidence(data, selectedPair.left_entry, rule!.left.field)
    : null;
  const right = selectedPair
    ? sideEvidence(data, selectedPair.right_entry, rule!.right.field)
    : null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-3">
          Cross-document checks
          {actions && rule && ruleId && (
            <span className="ml-auto flex flex-wrap gap-2">
              {actions(ruleId, rule)}
            </span>
          )}
        </CardTitle>
        <CardDescription>
          {description ??
            "Equality rules between fields on two documents of the same file (set up in the workflow)."}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Rule</TableHead>
              <TableHead>Left</TableHead>
              <TableHead>Right</TableHead>
              <TableHead>Outcome</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {ruleIds.map((id) => {
              const c = checks[id];
              return (
                <TableRow
                  key={id}
                  onClick={() => setRuleId(id)}
                  className={cn(
                    "cursor-pointer",
                    id === ruleId && "bg-accent/60",
                  )}
                  title={id}
                >
                  <TableCell className="max-w-56 truncate">
                    {c.label || id}
                  </TableCell>
                  <TableCell className="text-xs">
                    {c.left.template}.{c.left.field}
                  </TableCell>
                  <TableCell className="text-xs">
                    {c.right.template}.{c.right.field}
                  </TableCell>
                  <TableCell>
                    {outcomeBadge(c.passed, c.skip)}
                    {c.evaluable && c.pairs?.length ? (
                      <span className="ml-2 text-xs text-muted-foreground">
                        {c.pairs.filter((p) => p.equal).length}/
                        {c.pairs.length} pair(s) match
                      </span>
                    ) : null}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>

        {rule && !rule.evaluable && (
          <p className="rounded-md border border-dashed border-border px-4 py-3 text-sm text-muted-foreground">
            {skipText(rule.skip)}
          </p>
        )}

        {rule && rule.evaluable && selectedPair && left && right && (
          <>
            {pairs.length > 1 && (
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs text-muted-foreground">
                  Compared pair:
                </span>
                {pairs.map((p, i) => (
                  <Button
                    key={i}
                    variant={i === pairIdx ? "default" : "outline"}
                    size="sm"
                    onClick={() => setPairIdx(i)}
                    title={`entry ${p.left_entry} ↔ entry ${p.right_entry}`}
                  >
                    {p.equal ? "✓" : "✗"} {p.left ?? "—"} ↔ {p.right ?? "—"}
                  </Button>
                ))}
              </div>
            )}
            <div className="grid gap-5 border-t border-border pt-4 lg:grid-cols-2">
              <SidePanel
                label="LEFT"
                template={rule.left.template}
                entry={selectedPair.left_entry}
                field={rule.left.field}
                evidence={left}
                outcome={selectedPair.equal ? "equal" : "mismatch"}
                jobId={jobId}
                stem={stem}
                pages={pages}
                rendersAvailable={rendersAvailable}
              />
              <SidePanel
                label="RIGHT"
                template={rule.right.template}
                entry={selectedPair.right_entry}
                field={rule.right.field}
                evidence={right}
                outcome={selectedPair.equal ? "equal" : "mismatch"}
                jobId={jobId}
                stem={stem}
                pages={pages}
                rendersAvailable={rendersAvailable}
              />
            </div>
          </>
        )}

        {rule &&
          rule.passed === true &&
          ((rule.unbound?.left.length ?? 0) > 0 ||
            (rule.unbound?.right.length ?? 0) > 0) && (
            <div className="flex flex-col gap-1 border-t border-border pt-3">
              <span className="text-xs font-semibold tracking-wider text-primary">
                UNBOUND DOCUMENTS
              </span>
              <p className="text-xs text-muted-foreground">
                These documents match nothing on the other side — they may not
                pertain to the same shipment.
              </p>
              {(
                [
                  ["left", rule.left.template, rule.unbound?.left ?? []],
                  ["right", rule.right.template, rule.unbound?.right ?? []],
                ] as const
              ).flatMap(([side, tpl, items]) =>
                items.map((u, i) => (
                  <p key={`${side}-${i}`} className="text-xs">
                    <span className="font-mono text-muted-foreground">
                      {tpl} entry {u.entry}
                    </span>{" "}
                    {valueText(u.value) || "—"}
                    {u.source ? (
                      <span className="text-muted-foreground">
                        {" "}
                        · {u.source}
                      </span>
                    ) : null}
                  </p>
                )),
              )}
            </div>
          )}
      </CardContent>
    </Card>
  );
}
