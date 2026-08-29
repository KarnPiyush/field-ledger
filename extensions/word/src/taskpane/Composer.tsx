import { FileText, ListPlus, TextSelect } from "lucide-react";

export interface ComposerProps {
  text: string;
  target: "document" | "selection";
  disabled: boolean;
  onTextChange: (value: string) => void;
  onTargetChange: (target: "document" | "selection") => void;
  onSubmit: () => void;
}

export function Composer({
  text,
  target,
  disabled,
  onTextChange,
  onTargetChange,
  onSubmit,
}: ComposerProps) {
  return (
    <section className="composer" aria-label="Instruction composer">
      <div className="section-heading">
        <h2>Instruction</h2>
        <span>Proposal only</span>
      </div>
      <label htmlFor="instruction">Instruction</label>
      <textarea
        id="instruction"
        value={text}
        onChange={(event) => onTextChange(event.target.value)}
        placeholder="Describe the change for human review"
        rows={5}
        disabled={disabled}
      />
      <span className="field-label" id="target-label">Instruction target</span>
      <div className="segmented" role="radiogroup" aria-labelledby="target-label">
        <button
          type="button"
          role="radio"
          aria-checked={target === "document"}
          className={target === "document" ? "is-selected" : undefined}
          onClick={() => onTargetChange("document")}
          disabled={disabled}
        >
          <FileText aria-hidden="true" /> Document
        </button>
        <button
          type="button"
          role="radio"
          aria-checked={target === "selection"}
          className={target === "selection" ? "is-selected" : undefined}
          onClick={() => onTargetChange("selection")}
          disabled={disabled}
        >
          <TextSelect aria-hidden="true" /> Selection
        </button>
      </div>
      <button
        type="button"
        className="primary-command"
        onClick={onSubmit}
        disabled={disabled || text.trim().length === 0}
      >
        <ListPlus aria-hidden="true" /> Queue command
      </button>
    </section>
  );
}
