import { useEffect, useId, useState } from "react";
import { toast } from "sonner";
import { notifyError } from "@/lib/errors";
import { openExternal } from "@/lib/external";
import { api } from "@/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import logoLockup from "@/assets/logo-lockup.png";

export default function Onboarding({
  open,
  onDone,
}: {
  open: boolean;
  onDone: () => void;
}) {
  const [step, setStep] = useState(0);
  const [key, setKey] = useState("");
  const [keySet, setKeySet] = useState(false);
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);
  const [model, setModel] = useState("");
  const keyId = useId();

  useEffect(() => {
    api
      .settings()
      .then((s) => setModel(s.model ?? s.primary_model ?? ""))
      .catch(() => undefined);
  }, []);

  async function testKey() {
    if (!model) {
      toast.error("No model configured to test with");
      return;
    }
    if (
      !window.confirm(
        `Test the key with one tiny paid probe on ${model}? (~100–300 tokens, well under a cent)`,
      )
    ) {
      return;
    }
    setTesting(true);
    try {
      const res = await api.verifyModel(model);
      toast.success(res.message);
      setKeySet(true);
    } catch (e) {
      notifyError(e);
    } finally {
      setTesting(false);
    }
  }

  async function saveKey() {
    if (!key.trim()) return;
    setBusy(true);
    try {
      const res = await api.saveSettings({ api_key: key.trim() });
      setKeySet(true);
      setKey("");
      toast.success(
        res.key_stored
          ? "Key stored in the OS keychain"
          : "Key works for this session — no keychain available to save it",
      );
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  }

  async function finish() {
    setBusy(true);
    try {
      await api.saveSettings({ onboarded: true });
      onDone();
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open}>
      <DialogContent
        className="max-w-lg"
        onInteractOutside={(e) => e.preventDefault()}
        onEscapeKeyDown={(e) => e.preventDefault()}
      >
        {step === 0 && (
          <>
            <DialogTitle className="flex justify-center">
              <img
                src={logoLockup}
                alt="Verdandi"
                className="w-44 select-none"
              />
            </DialogTitle>
            <DialogDescription>
              Read broker invoices, verify every value, review what's doubtful,
              and export a clean workbook.
            </DialogDescription>
            <div className="flex justify-end">
              <Button onClick={() => setStep(1)}>Get started</Button>
            </div>
          </>
        )}

        {step === 1 && (
          <>
            <DialogTitle>Connect the model gateway</DialogTitle>
            <DialogDescription>
              Paste your gateway key — it is stored in the operating system's
              keychain, never in a file we ship.
            </DialogDescription>
            <div className="flex flex-col gap-2">
              <Label htmlFor={keyId} className="text-xs text-muted-foreground">
                API key
              </Label>
              <div className="flex gap-2">
                <Input
                  id={keyId}
                  type="password"
                  placeholder="sk-…"
                  value={key}
                  onChange={(e) => setKey(e.target.value)}
                />
                <Button onClick={saveKey} disabled={busy || !key.trim()}>
                  Save
                </Button>
              </div>
              {keySet && (
                <p className="text-xs text-emerald-500">
                  Key saved — you're connected.
                </p>
              )}
              <button
                type="button"
                className="self-start text-xs text-muted-foreground underline"
                onClick={() =>
                  void openExternal("https://opencode.ai/auth").catch((e) =>
                    notifyError(e, "open the link"),
                  )
                }
              >
                Get a key at opencode.ai/auth
              </button>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  onClick={testKey}
                  disabled={testing || !model}
                >
                  {testing ? "Testing…" : "Test key"}
                </Button>
                <span className="text-xs text-muted-foreground">
                  One tiny paid probe on{" "}
                  <span className="font-mono">{model || "—"}</span>; optional.
                </span>
              </div>
            </div>
            <div className="flex justify-between">
              <Button variant="ghost" onClick={() => setStep(0)}>
                Back
              </Button>
              <Button variant="outline" onClick={() => setStep(2)}>
                {keySet ? "Next" : "Skip for now"}
              </Button>
            </div>
          </>
        )}

        {step === 2 && (
          <>
            <DialogTitle>What leaves this machine</DialogTitle>
            <DialogDescription asChild>
              <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
                <li>
                  <strong className="text-foreground">Page images</strong> go to
                  the AI gateway for reading — that's how extraction works.
                </li>
                <li>
                  <strong className="text-foreground">Everything else</strong>{" "}
                  (runs, crops, exports, reviews) stays in your workspace
                  folder.
                </li>
                <li>
                  OCR, verification and export are local and free — one paid
                  model call per document, plus small optional extras you
                  confirm.
                </li>
              </ul>
            </DialogDescription>
            <div className="flex justify-between">
              <Button variant="ghost" onClick={() => setStep(1)}>
                Back
              </Button>
              <Button onClick={finish} disabled={busy}>
                {busy ? "Saving…" : "Finish"}
              </Button>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
