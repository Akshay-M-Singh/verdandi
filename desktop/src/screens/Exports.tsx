import ExportBuild from "@/components/ExportBuild";
import ExportProfiles from "@/screens/ExportProfiles";
import { Button } from "@/components/ui/button";

export type ExportsView = "build" | "profiles";

export default function Exports({
  view,
  onView,
  jobId,
  onSelectJob,
}: {
  view: ExportsView;
  onView: (view: ExportsView) => void;
  jobId: string | null;
  onSelectJob: (id: string | null) => void;
}) {
  return (
    <>
      <div className="flex items-center gap-2">
        <Button
          variant={view === "build" ? "default" : "outline"}
          size="sm"
          onClick={() => onView("build")}
        >
          Build workbook
        </Button>
        <Button
          variant={view === "profiles" ? "default" : "outline"}
          size="sm"
          onClick={() => onView("profiles")}
        >
          Profiles
        </Button>
      </div>
      {view === "build" ? (
        <ExportBuild jobId={jobId} onSelectJob={onSelectJob} />
      ) : (
        <ExportProfiles />
      )}
    </>
  );
}
