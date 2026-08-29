import type { AsyncPhase } from "../state/asyncPhase";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { DecisionBar } from "../components/DecisionBar";
import { EvidenceLink } from "../components/EvidenceLink";
import { StackedDiff } from "../components/StackedDiff";

export interface ReviewItemModel {
  id: string;
  category: string;
  title: string;
  summary: string;
  reason: string;
  source: string;
  coordinate: string;
  before: string;
  after: string;
  removed: string;
  added: string;
  evidenceImage: string;
}

export interface ReviewPageProps {
  projectName: string;
  batchTitle: string;
  progress: { current: number; total: number };
  item: ReviewItemModel;
  phase: AsyncPhase;
  locked: boolean;
  draft?: { decision: "approve" | "reject"; note?: string };
  onDecision: (decision: "approve" | "reject", note?: string) => Promise<void>;
  onBack: () => void;
  onPrevious?: () => void;
  onNext?: () => void;
}

export function ReviewPage({ item, progress, phase, locked, draft, onDecision, onPrevious, onNext }: ReviewPageProps) {
  return (
    <div className="page review-page">
      <progress className="review-progress" aria-label={`Item ${progress.current} of ${progress.total}`} value={progress.current} max={progress.total} />
      <nav className="review-pagination" aria-label="Review item navigation">
        <button type="button" className="button button--quiet" disabled={!onPrevious} onClick={onPrevious}>
          <ChevronLeft aria-hidden="true" size={18} /> Previous
        </button>
        <button type="button" className="button button--quiet" disabled={!onNext} onClick={onNext}>
          Next <ChevronRight aria-hidden="true" size={18} />
        </button>
      </nav>
      <header className="review-heading">
        <span className="eyebrow">{item.category}</span>
        <h1>{item.title}</h1>
        <p>{item.summary}</p>
      </header>
      <div className="review-content">
        <section className="reason-panel"><h2>Why this changed</h2><p>{item.reason}</p></section>
        <EvidenceLink source={item.source} coordinate={item.coordinate} href="#evidence" />
        <figure className="evidence-preview" id="evidence">
          <img src={item.evidenceImage} alt={`${item.source}, ${item.coordinate}`} />
          <figcaption>Synthetic source page rendered from the tracked Site Report fixture.</figcaption>
        </figure>
        <StackedDiff before={item.before} after={item.after} removed={item.removed} added={item.added} note="Conflict retained: Site Report 31 lists the reviewed value." />
      </div>
      <DecisionBar busy={phase.busy} locked={locked} {...(draft ? { draft } : {})} onDecision={onDecision} />
    </div>
  );
}
