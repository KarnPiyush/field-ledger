import { encodeQueue, type QueueCommand } from "@fieldledger/offline-queue";

import type {
  DocumentFingerprint,
  SerializedQueue,
  WordBridge,
  WordInstruction,
} from "../office/bridge";
import { fingerprintSnapshot } from "../office/fingerprint";
import type { ProposalGateway, ProposalStatus } from "../data/replay";

const STATE_KEY = "fieldledger.word.demo.document.v1";
const APPLIED_KEY = "fieldledger.word.demo.applied.v1";
const RESULTS_KEY = "fieldledger.word.demo.results.v1";
const EVENT = "fieldledger:demo-state";

interface DemoDocumentState {
  documentId: string;
  bodyText: string;
  selectionText: string;
  selectionContext: string;
  queue: SerializedQueue;
  online: boolean;
  loseNextResponse: boolean;
}

const initialState: DemoDocumentState = {
  documentId: "browser-demo-document-1",
  bodyText:
    "North Annex field report. Concrete quantity: 42 cubic metres. Inspection is due Friday.",
  selectionText: "Concrete quantity: 42 cubic metres.",
  selectionContext:
    "North Annex field report. Concrete quantity: 42 cubic metres. Inspection is due Friday.",
  queue: encodeQueue([]),
  online: true,
  loseNextResponse: false,
};

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}

export class LocalWordBridge implements WordBridge {
  public async ready(): Promise<void> {}

  public async getFingerprint(): Promise<DocumentFingerprint> {
    const state = this.state();
    return fingerprintSnapshot({
      documentId: state.documentId,
      bodyText: state.bodyText,
      selectionText: state.selectionText,
      selectionContext: state.selectionContext,
    });
  }

  public async loadQueue(): Promise<SerializedQueue> {
    return this.state().queue;
  }

  public async saveQueue(queue: SerializedQueue): Promise<void> {
    this.update({ queue });
  }

  public async openReview(url: string): Promise<void> {
    window.open(url, "_blank", "noopener,noreferrer");
  }

  public isOnline(): boolean {
    return this.state().online;
  }

  public setOnline(online: boolean): void {
    this.update({ online });
    window.dispatchEvent(new Event(online ? "online" : "offline"));
  }

  public moveDocument(): void {
    const state = this.state();
    this.update({
      bodyText: `${state.bodyText} Document moved after the instruction was reviewed.`,
      selectionContext: `Moved section. ${state.selectionText}`,
    });
  }

  public loseNextResponse(): void {
    this.update({ loseNextResponse: true });
  }

  public consumeLostResponse(): boolean {
    const state = this.state();
    if (!state.loseNextResponse) return false;
    this.update({ loseNextResponse: false });
    return true;
  }

  public subscribe(listener: () => void): () => void {
    window.addEventListener(EVENT, listener);
    window.addEventListener("storage", listener);
    return () => {
      window.removeEventListener(EVENT, listener);
      window.removeEventListener("storage", listener);
    };
  }

  private state(): DemoDocumentState {
    const stored = readJson<Partial<DemoDocumentState>>(STATE_KEY, {});
    return { ...initialState, ...stored };
  }

  private update(change: Partial<DemoDocumentState>): void {
    localStorage.setItem(STATE_KEY, JSON.stringify({ ...this.state(), ...change }));
    window.dispatchEvent(new Event(EVENT));
  }
}

export class BrowserDemoProposalGateway implements ProposalGateway {
  public constructor(
    private readonly bridge: LocalWordBridge,
    private readonly inner: ProposalGateway,
  ) {}

  public async start(command: QueueCommand<WordInstruction>): Promise<ProposalStatus> {
    const status = await this.inner.start(command);
    if (this.bridge.consumeLostResponse()) {
      throw new Error("Browser demo response lost after dispatch");
    }
    return status;
  }

  public reconcile(command: QueueCommand<WordInstruction>): Promise<ProposalStatus> {
    return this.inner.reconcile(command);
  }

  public poll(jobId: string): Promise<ProposalStatus> {
    return this.inner.poll(jobId);
  }
}
