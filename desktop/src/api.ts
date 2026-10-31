// Local engine service client (D66). The sidecar binds 127.0.0.1 only.
export const SIDECAR_URL =
  (import.meta.env.VITE_SIDECAR_URL as string | undefined) ??
  "http://127.0.0.1:8787";

export type JobState = "queued" | "running" | "done" | "failed" | "cancelled";

export interface WarningInfo {
  code?: string;
  title: string;
  body?: string;
  hint?: string;
  where?: string;
}

export interface JobFileRow {
  stem: string;
  status: "pending" | "captured" | "extracted" | "failed" | "error";
  found: boolean | null;
  error?: string | null;
}

export interface Job {
  id: string;
  state: JobState;
  created_at: string;
  name: string;
  paths: string[];
  stems: string[];
  run_dir: string;
  doc_id: string;
  model: string;
  verification_model: string;
  workers: number;
  mode: "full" | "capture" | "extract";
  doc_ids: string[];
  workflow: string | null;
  pinned_pages: string | null;
  pinned_label: string | null;
  renders_pruned: boolean;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  files: JobFileRow[];
  queue_position?: number | null;
}

export interface CatalogDocument {
  type: string | null;
  label: string | null;
  pages: number[];
}

export interface CatalogCard {
  stem: string;
  status: string;
  error?: string | null;
  documents: CatalogDocument[];
}

export interface DocumentRow {
  stem: string;
  status: string;
  found: boolean | null;
  selected_name: string | null;
  fields: number;
  line_items: number;
  pages: number;
}

export interface Evidence {
  page: number;
  bbox: number[] | null;
  text: string | null;
  claimed_bbox: number[] | null;
  claimed_page: number | null;
  value: string | null;
}

export interface ReviewState {
  status: "confirmed" | "issue";
  note?: string;
  value?: string;
}

export interface FlagRow {
  file: string;
  template?: string;
  entry?: string;
  rule?: string;
  field: string;
  value?: string;
  confidence?: string;
  reason: string;
  reason_label?: string;
  reason_help?: string;
  reason_hint?: string;
  detail?: string;
  sources?: string;
  review?: ReviewState | null;
  evidence?: Evidence;
}

export interface Profile {
  id: string;
  label: string;
}

export interface SettingsView {
  profiles: Profile[];
  profile: string | null;
  model: string | null;
  primary_model: string | null;
  verification_model: string;
  workers: number;
  key_present: boolean;
  key_stored?: boolean;
  workspace: string;
  onboarded: boolean;
  auto_prune_renders: boolean;
}

export interface JobStorage {
  renders_bytes: number;
  renders_files: number;
  total_bytes: number;
  renders_pruned: boolean;
}

export interface ExportProfile {
  id: string;
  label: string;
  grain: string;
}

export interface ExportColumn {
  header: string;
  source: Record<string, string>;
  type?: string;
}

export interface ExportProfileFull {
  id: string;
  label: string;
  workflow?: string;
  grain?: "file" | "line_item";
  line_item_template?: string;
  options?: {
    date_format?: string;
    require_reviewed?: boolean;
    include_line_items_sheet?: boolean;
  };
  columns: ExportColumn[];
  [key: string]: unknown;
}

export interface ExportSource {
  source: Record<string, string>;
  label: string;
  header: string;
  help: string;
}

export interface ExportCatalog {
  sources: ExportSource[];
  warnings: WarningInfo[];
  workflows: { id: string; label: string }[];
  line_item_templates: string[];
}

export interface ExportPreview {
  run: string | null;
  headers: string[];
  total_rows: number;
  rows: {
    stem: string;
    cells: { value: unknown; status: string; type: string; comment: string | null }[];
  }[];
}

export interface ModelChoice {
  id: string;
  label: string;
  verified: boolean;
}

export interface ModelsView {
  offline: boolean;
  catalog_error?: string | null;
  catalog_new: string[];
  catalog_removed: string[];
  catalog_ids: string[];
  catalog_fetched_at: number | null;
  catalog_from_cache: boolean;
  catalog_stale: boolean;
  pruned: string[];
  fallback?: { from: string; to: string } | null;
  primary: string | null;
  current: string | null;
  choices: ModelChoice[];
  unverified: { id: string; label: string }[];
  text_only: string[];
  probe_cost: number;
  config_root: string;
}

export interface JobMetrics {
  flags: number;
  pending: number;
  confirmed: number;
  issues: number;
  reasons: Record<string, number>;
  review_summary?: string;
  prompt_tokens: number;
  completion_tokens: number;
  recheck_prompt_tokens: number;
  recheck_completion_tokens: number;
  rechecks: number;
  per_file: Record<
    string,
    {
      prompt_tokens: number;
      completion_tokens: number;
      recheck_prompt_tokens: number;
      recheck_completion_tokens: number;
      rechecks: number;
    }
  >;
}

export interface ModelRates {
  models: Record<
    string,
    { input_per_1m: number | null; output_per_1m: number | null }
  >;
  primary_model: string;
  verification_model: string;
  source: string;
  fetched_at: number | null;
  from_cache: boolean;
  error: string | null;
}

export interface Region {
  page: number;
  bbox: number[];
}

export interface GroundingMatch {
  page: number;
  line_bbox?: number[];
  fragment_bbox?: number[];
  text?: string;
}

export interface HeaderField {
  name: string;
  value: string | null;
  confidence?: number;
  source?: string | null;
  source_region?: Region | null;
  verification?: {
    level?: string;
    score?: number;
    checks?: { check: string; match?: GroundingMatch | null }[];
    recheck?: { agree: boolean };
  };
}

export interface LineItem {
  description?: string;
  quantity?: string;
  unit_price?: string;
  amount?: string | null;
  confidence?: number;
  source_region?: Region | null;
}

export interface DocumentData {
  status?: string;
  selected_document?: { name?: string; confidence?: number; reason?: string };
  header_fields?: HeaderField[];
  line_items?: LineItem[];
  notes?: string[];
  documents?: {
    template_id?: string;
    found?: boolean;
    selected_document?: { name?: string; confidence?: number };
    header_fields?: HeaderField[];
    line_items?: LineItem[];
  }[];
  _meta?: {
    verification?: {
      cross_checks?: Record<string, CrossCheck>;
    };
  };
}

export interface CrossPair {
  left: string | null;
  right: string | null;
  left_entry: number;
  right_entry: number;
  equal: boolean;
  left_source?: string;
  right_source?: string;
}

export interface CrossCheck {
  label: string;
  left: { template: string; field: string };
  right: { template: string; field: string };
  evaluable: boolean;
  passed: boolean | null;
  skip: string | null;
  kind?: string;
  pairs?: CrossPair[];
  unbound?: {
    left: { entry: number; value: string | null; source?: string }[];
    right: { entry: number; value: string | null; source?: string }[];
  };
}

export interface WorkflowCrossCheck {
  id?: string;
  label?: string;
  left: { template: string; field: string };
  right: { template: string; field: string };
}

export interface Workflow {
  id: string;
  label: string;
  description?: string;
  documents: string[];
  expected: string[];
  cross_checks: WorkflowCrossCheck[];
  [key: string]: unknown;
}

export interface WorkflowProfile {
  id: string;
  label: string;
  fields: string[];
}

export interface WorkflowsView {
  workflows: Workflow[];
  warnings: WarningInfo[];
  profiles: WorkflowProfile[];
}

export interface TemplateField {
  name: string;
  type: string;
  description: string;
  threshold?: number;
  labels?: string[];
  anchor?: unknown;
}

export interface TemplateColumn {
  name: string;
  description: string;
  type: string;
  role?: string;
}

export interface RowRule {
  rule: string;
  fields: string[];
  target: string;
  tolerance?: number;
  missing?: string;
}

export interface DocRule {
  rule: string;
  target: string;
  tolerance?: number;
}

export interface Template {
  id: string;
  label: string;
  description: string;
  signals?: { keywords?: string[]; min_score?: number };
  extraction?: {
    fields?: TemplateField[];
    line_items?: boolean;
    line_items_columns?: TemplateColumn[];
    line_items_note?: string;
    row_arithmetic?: RowRule[];
    arithmetic?: DocRule[];
  };
  allow_no_match?: boolean;
  [key: string]: unknown;
}

export interface TemplatesView {
  templates: Template[];
  workflow_refs: Record<string, string[]>;
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${SIDECAR_URL}${path}`, init);
  if (!res.ok) {
    let detail: unknown = res.statusText;
    try {
      detail = ((await res.json()) as { detail?: unknown }).detail ?? detail;
    } catch {
      /* keep statusText */
    }
    if (detail && typeof detail === "object") {
      const d = detail as { message?: string; details?: string; code?: string };
      const err = new Error(d.message ?? d.details ?? res.statusText) as Error & {
        code?: string;
      };
      if (d.code) err.code = d.code;
      throw err;
    }
    throw new Error(String(detail));
  }
  return (await res.json()) as T;
}

export const api = {
  health: () => req<{ status: string; version: string }>("/health"),

  settings: () => req<SettingsView>("/settings"),
  saveSettings: (patch: {
    api_key?: string;
    remove_key?: boolean;
    profile?: string;
    model?: string;
    workers?: number;
    onboarded?: boolean;
    auto_prune_renders?: boolean;
    verification_model?: string;
  }) =>
    req<SettingsView>("/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),

  submit: (body: {
    paths: string[];
    doc_id?: string;
    doc_ids?: string[];
    workflow?: string;
    model?: string;
    workers?: number;
    mode?: "full" | "capture";
    name?: string;
  }) =>
    req<Job>("/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  jobs: () => req<Job[]>("/jobs"),
  job: (id: string) => req<Job>(`/jobs/${id}`),
  renameJob: (id: string, name: string) =>
    req<Job>(`/jobs/${id}/rename`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  retry: (id: string) =>
    req<Job>(`/jobs/${id}/retry`, { method: "POST" }),
  cancel: (id: string) => req<Job>(`/jobs/${id}/cancel`, { method: "POST" }),
  logs: (id: string) => req<{ log: string }>(`/jobs/${id}/logs`),
  storage: (id: string) => req<JobStorage>(`/jobs/${id}/storage`),
  prune: (id: string) =>
    req<{ files: number; freed_bytes: number }>(`/jobs/${id}/prune`, {
      method: "POST",
    }),
  deleteJob: (id: string) =>
    req<{ deleted: string }>(`/jobs/${id}`, { method: "DELETE" }),
  workspaceStorage: () =>
    req<{ runs: number; total_bytes: number; workspace: string }>("/storage"),
  pathsSummary: (paths: string[]) =>
    req<{ files: number; bytes: number; calls_estimate: number }>(
      "/paths/summary",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths }),
      },
    ),
  extract: (
    id: string,
    pins: { pinned_pages?: string; pinned_label?: string; stems?: string[] },
  ) =>
    req<Job>(`/jobs/${id}/extract`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(pins),
    }),
  catalog: (id: string, stem?: string) =>
    req<{ exit_code: number; cards: CatalogCard[] }>(`/jobs/${id}/catalog`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stem: stem ?? null }),
    }),
  catalogCards: (id: string) => req<CatalogCard[]>(`/jobs/${id}/catalog`),
  jobSummary: (id: string) => req<Record<string, string>[]>(`/jobs/${id}/summary`),
  jobMetrics: (id: string) => req<JobMetrics>(`/jobs/${id}/metrics`),
  jobsMetrics: (ids: string[]) =>
    req<Record<string, JobMetrics>>("/jobs/metrics", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    }),
  documents: (id: string) => req<DocumentRow[]>(`/jobs/${id}/documents`),
  document: (id: string, stem: string) =>
    req<DocumentData>(`/jobs/${id}/documents/${stem}`),
  flags: (id: string) => req<FlagRow[]>(`/jobs/${id}/flags`),
  recomputeFlags: (id: string) =>
    req<{ flags: number }>(`/jobs/${id}/flags/recompute`, { method: "POST" }),
  applyAllReviews: (id: string) =>
    req<{ stems: number; entries: number }>(`/jobs/${id}/review/apply-all`, {
      method: "POST",
    }),
  review: (
    id: string,
    row: Record<string, unknown>,
    status: "confirmed" | "issue",
    note: string,
    value?: string,
  ) =>
    req<{ message: string }>(`/jobs/${id}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ row, status, note, value }),
    }),

  workflows: () => req<WorkflowsView>("/workflows"),
  saveWorkflow: (workflow: Workflow, originalId: string | null) =>
    req<{ workflow: Workflow; message: string; warnings: WarningInfo[] }>(
      "/workflows",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workflow, original_id: originalId }),
      },
    ),
  deleteWorkflow: (id: string) =>
    req<{ deleted: string; message: string }>(
      `/workflows/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    ),
  duplicateWorkflow: (id: string) =>
    req<{ workflow: Workflow; message: string }>("/workflows/duplicate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    }),
  workflowsYaml: () => req<{ text: string }>("/workflows/yaml"),
  validateWorkflowsYaml: (text: string) =>
    req<{ ok: boolean; message: string; ids: string[] }>("/workflows/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    }),
  saveWorkflowsYaml: (text: string) =>
    req<{ path: string; message: string }>("/workflows/yaml", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    }),

  templates: () => req<TemplatesView>("/templates"),
  saveTemplate: (profile: Template, originalId: string | null) =>
    req<{ profile: Template; message: string; workflow_refs: Record<string, string[]> }>(
      "/templates",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile, original_id: originalId }),
      },
    ),
  deleteTemplate: (id: string) =>
    req<{ deleted: string; warnings: WarningInfo[]; message: string }>(
      `/templates/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    ),
  duplicateTemplate: (id: string) =>
    req<{ profile: Template; message: string }>("/templates/duplicate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    }),
  templatesYaml: () => req<{ text: string }>("/templates/yaml"),
  validateTemplatesYaml: (text: string) =>
    req<{ ok: boolean; message: string; ids: string[] }>("/templates/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    }),
  saveTemplatesYaml: (text: string) =>
    req<{ path: string; message: string }>("/templates/yaml", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    }),
  models: (force = false) =>
    req<ModelsView>(force ? "/models?force=1" : "/models"),
  rates: () => req<ModelRates>("/models/rates"),
  verifyModel: (model: string) =>
    req<{ message: string }>("/models/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model }),
    }),
  setDefaultModel: (model: string) =>
    req<SettingsView>("/models/default", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model }),
    }),
  resetDefaults: () =>
    req<{ reset: string[]; settings: SettingsView }>("/settings/reset", {
      method: "POST",
    }),

  exportProfiles: () => req<ExportProfileFull[]>("/exports"),
  exportsCatalog: (profile: ExportProfileFull | null) =>
    req<ExportCatalog>("/exports/catalog", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile }),
    }),
  exportsSeed: (workflowId: string) =>
    req<{ profile: ExportProfileFull }>("/exports/seed", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workflow_id: workflowId }),
    }),
  exportsPreview: (profile: ExportProfileFull, runId?: string) =>
    req<ExportPreview>("/exports/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile, run_id: runId ?? null }),
    }),
  saveExportProfile: (profile: ExportProfileFull, originalId: string | null) =>
    req<{ profile: ExportProfileFull; message: string }>("/exports", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile, original_id: originalId }),
    }),
  deleteExportProfile: (id: string) =>
    req<{ deleted: string; message: string }>(
      `/exports/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    ),
  duplicateExportProfile: (id: string) =>
    req<{ profile: ExportProfileFull; message: string }>("/exports/duplicate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    }),
  exportsYaml: () => req<{ text: string }>("/exports/yaml"),
  validateExportsYaml: (text: string) =>
    req<{ ok: boolean; message: string; ids: string[] }>("/exports/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    }),
  saveExportsYaml: (text: string) =>
    req<{ path: string; message: string }>("/exports/yaml", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    }),
  buildExport: (id: string, profile: string) =>
    req<{
      file: string;
      path: string;
      pruned: { files: number; freed_bytes: number } | null;
    }>(`/jobs/${id}/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile }),
    }),

  cropUrl: (id: string, stem: string, page: number, bbox?: number[] | null) => {
    const base = `${SIDECAR_URL}/jobs/${id}/documents/${encodeURIComponent(stem)}/crop`;
    const params = new URLSearchParams({ page: String(page) });
    if (bbox && bbox.length === 4) params.set("bbox", bbox.join(","));
    return `${base}?${params.toString()}`;
  },
};

export const IN_TAURI =
  typeof window !== "undefined" && "__TAURI_INTERNALS__" in (window as object);

export const DIAGNOSTICS_URL = `${SIDECAR_URL}/diagnostics`;
