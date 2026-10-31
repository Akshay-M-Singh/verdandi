import { Info } from "lucide-react";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

/** The "Explain this" ⓘ: plain title/body/hint with the raw code last. */
export default function Explain({
  title,
  body,
  hint,
  code,
  className,
}: {
  title: string;
  body?: string;
  hint?: string;
  code?: string;
  className?: string;
}) {
  if (!title) return null;
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            aria-label="Explain this"
            className={className ?? "text-muted-foreground hover:text-foreground"}
          >
            <Info className="size-3.5" />
          </button>
        </TooltipTrigger>
        <TooltipContent
          side="top"
          className="max-w-sm flex-col items-start gap-1 whitespace-normal bg-popover text-popover-foreground"
        >
          <p className="text-xs font-semibold">{title}</p>
          {body && <p className="text-xs text-muted-foreground">{body}</p>}
          {hint && <p className="text-xs text-foreground/80">{hint}</p>}
          {code && (
            <p className="font-mono text-[10px] text-muted-foreground">
              {code}
            </p>
          )}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
