import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/600.css";
import "@fontsource/ibm-plex-serif/600.css";
import "./styles.css";

import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

import {
  BrowserDemoProposalGateway,
  LocalWordBridge,
} from "./demo/localBridge";
import {
  FieldLedgerProposalGateway,
  WordQueueSender,
} from "./data/replay";
import { bootstrapWordBackend } from "./data/runtime";
import {
  createOfficeWordBridge,
  type WordBridge,
} from "./office/bridge";
import { Taskpane, type DemoControls } from "./taskpane/Taskpane";

interface Runtime {
  bridge: WordBridge;
  sender: WordQueueSender;
  hostLabel: string;
  initialOnline: boolean;
  subscribe: (listener: (online: boolean) => void) => () => void;
  reviewUrlForBatch: (batchId: string) => string;
  demoControls?: DemoControls;
}

function officeAvailable(): boolean {
  const scope = globalThis as typeof globalThis & { Office?: unknown; Word?: unknown };
  return Boolean(scope.Office && scope.Word);
}

function browserConnection(listener: (online: boolean) => void): () => void {
  const update = () => listener(navigator.onLine);
  window.addEventListener("online", update);
  window.addEventListener("offline", update);
  return () => {
    window.removeEventListener("online", update);
    window.removeEventListener("offline", update);
  };
}

function reviewBaseUrl(): string {
  const reviewUrl = import.meta.env.VITE_FIELDLEDGER_REVIEW_URL;
  if (!reviewUrl) throw new Error("VITE_FIELDLEDGER_REVIEW_URL is required for Word handoff");
  return reviewUrl;
}

async function fieldLedgerGateway(reviewUrl: string): Promise<FieldLedgerProposalGateway> {
  const apiUrl = new URL(import.meta.env.VITE_FIELDLEDGER_API_URL ?? "/", window.location.origin).toString();
  const backend = await bootstrapWordBackend({
    baseUrl: apiUrl,
    fetcher: window.fetch.bind(window),
  });
  return new FieldLedgerProposalGateway(
    backend.client,
    {
      documentId: backend.documentId,
      workflowRunId: backend.workflowRunId,
      reviewBaseUrl: reviewUrl,
    },
  );
}

async function nativeRuntime(): Promise<Runtime> {
  const bridge = createOfficeWordBridge();
  const reviewUrl = reviewBaseUrl();
  const gateway = await fieldLedgerGateway(reviewUrl);
  return {
    bridge,
    sender: new WordQueueSender(bridge, gateway),
    hostLabel: "Microsoft Word",
    initialOnline: navigator.onLine,
    subscribe: (listener) => browserConnection(listener),
    reviewUrlForBatch: (batchId) =>
      new URL(`/review/${encodeURIComponent(batchId)}/1`, reviewUrl).toString(),
  };
}

async function demoRuntime(): Promise<Runtime> {
  const bridge = new LocalWordBridge();
  const reviewUrl = reviewBaseUrl();
  const gateway = new BrowserDemoProposalGateway(
    bridge,
    await fieldLedgerGateway(reviewUrl),
  );
  return {
    bridge,
    sender: new WordQueueSender(bridge, gateway, { isOnline: () => bridge.isOnline() }),
    hostLabel: "Browser demo",
    initialOnline: bridge.isOnline(),
    subscribe: (listener) => bridge.subscribe(() => listener(bridge.isOnline())),
    reviewUrlForBatch: (batchId) =>
      new URL(`/review/${encodeURIComponent(batchId)}/1`, reviewUrl).toString(),
    demoControls: {
      setOnline: (online) => bridge.setOnline(online),
      moveDocument: () => bridge.moveDocument(),
      loseNextResponse: () => bridge.loseNextResponse(),
    },
  };
}

function App() {
  const [native] = useState(officeAvailable);
  const [runtime, setRuntime] = useState<Runtime | null>(null);
  const [error, setError] = useState<string>();
  const [bootstrapAttempt, setBootstrapAttempt] = useState(0);
  const [online, setOnline] = useState(runtime?.initialOnline ?? navigator.onLine);

  useEffect(() => {
    let cancelled = false;
    setError(undefined);
    void (native ? nativeRuntime() : demoRuntime()).then(
      (initialized) => {
        if (cancelled) return;
        setRuntime(initialized);
        setOnline(initialized.initialOnline);
      },
      (failure: unknown) => {
        console.error("Word runtime bootstrap failed", {
          error: failure instanceof Error ? failure.message : String(failure),
        });
        if (!cancelled) setError("Could not connect Word to FieldLedger. Retry after checking the connection.");
      },
    );
    return () => {
      cancelled = true;
    };
  }, [bootstrapAttempt, native]);

  useEffect(() => runtime?.subscribe(setOnline), [runtime]);

  if (runtime === null) {
    return error ? (
      <main className="taskpane-shell" role="alert">
        <p>{error}</p>
        <button type="button" className="primary-command" onClick={() => setBootstrapAttempt((value) => value + 1)}>
          Retry connection
        </button>
      </main>
    ) : (
      <main className="taskpane-shell" role="status">Connecting to FieldLedger…</main>
    );
  }

  return (
    <Taskpane
      bridge={runtime.bridge}
      sender={runtime.sender}
      online={online}
      hostLabel={runtime.hostLabel}
      reviewUrlForBatch={runtime.reviewUrlForBatch}
      {...(runtime.demoControls === undefined ? {} : { demoControls: runtime.demoControls })}
    />
  );
}

const root = document.getElementById("root");
if (!root) throw new Error("Task pane root element is missing");
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
