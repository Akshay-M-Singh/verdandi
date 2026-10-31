import type { ReactNode } from "react";
import { AlertTriangle, Info, XCircle } from "lucide-react";
import { cn } from "@/lib/utils";

export interface WarningInfo {
  code?: string;
  title: string;
  body?: string;
  hint?: string;
  where?: string;
}

const TONES = {
  warning: {
    icon: AlertTriangle,
    cls: "border-primary/50 bg-primary/5",
    iconCls: "text-primary",
  },
  error: {
    icon: XCircle,
    cls: "border-destructive/50 bg-destructive/5",
    iconCls: "text-destructive",
  },
  info: {
    icon: Info,
    cls: "border-border bg-card",
    iconCls: "text-muted-foreground",
  },
} as const;

/** Human warning card: plain sentence + what to do + optional details. */
export default function Notice({
  tone = "warning",
  title,
  body,
  hint,
  details,
  action,
  className,
}: {
  tone?: keyof typeof TONES;
  title: string;
  body?: string;
  hint?: string;
  details?: string;
  action?: ReactNode;
  className?: string;
}) {
  const t = TONES[tone];
  const Icon = t.icon;
  return (
    <div
      role={tone === "error" ? "alert" : undefined}
      className={cn(
        "flex gap-2.5 rounded-md border px-3 py-2.5",
        t.cls,
        className,
      )}
    >
      <Icon className={cn("mt-0.5 size-4 shrink-0", t.iconCls)} />
      <div className="flex min-w-0 flex-col gap-0.5">
        <p className="text-sm font-medium">{title}</p>
        {body && <p className="text-xs text-muted-foreground">{body}</p>}
        {hint && <p className="text-xs text-foreground/80">{hint}</p>}
        {details && (
          <details className="mt-0.5">
            <summary className="cursor-pointer text-[11px] text-muted-foreground">
              Details
            </summary>
            <p className="mt-1 font-mono text-[11px] text-muted-foreground">
              {details}
            </p>
          </details>
        )}
        {action && <div className="mt-1">{action}</div>}
      </div>
    </div>
  );
}
