import { Check, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

interface DecisionBarProps {
  busy: boolean;
  locked: boolean;
  draft?: { decision: "approve" | "reject"; note?: string };
  onDecision: (decision: "approve" | "reject", note?: string) => Promise<void>;
}

export function DecisionBar({ busy, locked, draft, onDecision }: DecisionBarProps) {
  const [rejecting, setRejecting] = useState(draft?.decision === "reject");
  const [note, setNote] = useState(draft?.note ?? "");
  const [localBusy, setLocalBusy] = useState(false);
  const [localSaved, setLocalSaved] = useState(Boolean(draft));
  const [savedDecision, setSavedDecision] = useState(draft?.decision);
  const [savedNote, setSavedNote] = useState(draft?.note ?? "");
  const [error, setError] = useState<string | null>(null);
  const pendingRef = useRef(false);
  const disabled = busy || localBusy || locked;

  useEffect(() => {
    setRejecting(draft?.decision === "reject");
    setNote(draft?.note ?? "");
    setLocalSaved(Boolean(draft));
    setSavedDecision(draft?.decision);
    setSavedNote(draft?.note ?? "");
  }, [draft?.decision, draft?.note]);

  const submit = async (decision: "approve" | "reject") => {
    if (disabled || pendingRef.current) return;
    if (decision === "reject" && !note.trim()) {
      setError("Add a reason before saving");
      return;
    }
    if (
      localSaved &&
      savedDecision === decision &&
      (decision === "approve" || savedNote === note.trim())
    ) return;
    setError(null);
    pendingRef.current = true;
    setLocalBusy(true);
    try {
      await onDecision(decision, decision === "reject" ? note.trim() : undefined);
      setLocalSaved(true);
      setSavedDecision(decision);
      setSavedNote(decision === "reject" ? note.trim() : "");
    } catch {
      setError("Could not save on this phone. Check storage and try again.");
    } finally {
      pendingRef.current = false;
      setLocalBusy(false);
    }
  };

  return (
    <aside className="decision-bar" aria-label="Decision controls" data-no-overlap>
      {rejecting ? (
        <div className="reject-note">
          <label htmlFor="rejection-note">Reason for rejection</label>
          <textarea id="rejection-note" value={note} aria-describedby={error ? "decision-error" : undefined} onChange={(event) => setNote(event.target.value)} />
          <div className="reject-note__actions">
            <button type="button" className="button button--quiet" onClick={() => { setRejecting(false); setError(null); }}>Cancel</button>
            <button type="button" className="button button--danger" disabled={disabled} onClick={() => void submit("reject")}>
              <X aria-hidden="true" size={18} /> Save rejection
            </button>
          </div>
        </div>
      ) : (
        <div className="decision-segment">
          <button type="button" className="button button--quiet" disabled={disabled} aria-pressed={draft?.decision === "reject"} onClick={() => setRejecting(true)}>
            <X aria-hidden="true" size={18} /> Reject
          </button>
          <button type="button" className="button button--approve" disabled={disabled} aria-pressed={draft?.decision === "approve"} onClick={() => void submit("approve")}>
            <Check aria-hidden="true" size={18} /> Approve
          </button>
        </div>
      )}
      {error ? <p className="decision-message decision-message--error" id="decision-error" role="alert">{error}</p> : null}
      {locked ? <p className="decision-message" role="status">Batch saved in the ordered queue</p> : null}
      {!locked && localSaved ? <p className="decision-message" role="status">Saved on this phone</p> : null}
      {!locked && !localSaved && !error ? <p className="decision-message">Decision saves on this phone</p> : null}
    </aside>
  );
}
