import {
  Download,
  FileWarning,
  RotateCcw,
  Unplug,
  Wifi,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type {
  QueueCommand,
  Sender,
  SendResult,
} from "@fieldledger/offline-queue";

import { DocumentQueueStore, WordQueueDataError } from "../data/queueStore";
import { replayQueue } from "../data/replay";
import type {
  DocumentFingerprint,
  WordBridge,
  WordInstruction,
} from "../office/bridge";
import { Composer } from "./Composer";
import { ConflictPanel } from "./ConflictPanel";
import { QueueList } from "./QueueList";
import { StatusBar } from "./StatusBar";

export interface DemoControls {
  setOnline: (online: boolean) => void;
  moveDocument: () => void;
  loseNextResponse: () => void;
}

export interface TaskpaneProps {
  bridge: WordBridge;
  sender: Sender<WordInstruction>;
  online: boolean;
  hostLabel: string;
  reviewUrlForBatch?: (batchId: string) => string;
  demoControls?: DemoControls;
}

function defaultReviewUrl(batchId: string): string {
  return new URL(`/review.html?batch=${encodeURIComponent(batchId)}`, window.location.href).toString();
}

function statusMessage(result: Awaited<ReturnType<typeof replayQueue>>): string | undefined {
  switch (result.status) {
    case "offline":
      return "Waiting to sync";
    case "stale":
      return "Needs refresh";
    case "failed":
      return "Nothing was applied";
    case "reconciliation_required":
      return "Needs reconciliation";
    case "contended":
      return "Queue changed; retrying is safe";
    case "drained":
      return result.applied > 0 ? "Proposal ready" : undefined;
  }
}

export function Taskpane({
  bridge,
  sender,
  online,
  hostLabel,
  reviewUrlForBatch = defaultReviewUrl,
  demoControls,
}: TaskpaneProps) {
  const store = useMemo(() => new DocumentQueueStore(bridge), [bridge]);
  const [commands, setCommands] = useState<readonly QueueCommand<WordInstruction>[]>([]);
  const [fingerprint, setFingerprint] = useState<DocumentFingerprint>();
  const [text, setText] = useState("");
  const [target, setTarget] = useState<"document" | "selection">("document");
  const [busy, setBusy] = useState(true);
  const [message, setMessage] = useState<string>();
  const [error, setError] = useState<string>();
  const [loadError, setLoadError] = useState<WordQueueDataError>();
  const [selectedId, setSelectedId] = useState<string>();
  const [reviewUrl, setReviewUrl] = useState<string>();
  const submission = useRef<Promise<void> | undefined>(undefined);
  const draftKey = useRef(crypto.randomUUID());
  const mounted = useRef(true);

  const reloadState = useCallback(async () => {
    try {
      const [nextCommands, nextFingerprint] = await Promise.all([
        store.list(),
        bridge.getFingerprint(),
      ]);
      if (!mounted.current) return;
      setCommands(nextCommands);
      setFingerprint(nextFingerprint);
      setLoadError(undefined);
      const blocked = nextCommands.find(
        (command) =>
          command.state === "stale" ||
          command.state === "failed" ||
          command.state === "reconciliation_required",
      );
      if (blocked) setSelectedId(blocked.id);
    } catch (caught) {
      if (!mounted.current) return;
      if (caught instanceof WordQueueDataError) {
        setLoadError(caught);
        setError("The saved queue in this document cannot be read.");
      } else {
        console.error("Word queue refresh failed", caught);
        setError("The task pane could not read this document. Retry when Word is available.");
      }
    }
  }, [bridge, store]);

  const runReplay = useCallback(async () => {
    if (!online || loadError) return;
    setBusy(true);
    setMessage(undefined);
    setError(undefined);
    const observe = async (
      operation: (command: QueueCommand<WordInstruction>) => Promise<SendResult>,
      command: QueueCommand<WordInstruction>,
    ) => {
      const result = await operation(command);
      if (result.kind === "applied" && result.response.status === "preview_ready") {
        setReviewUrl(reviewUrlForBatch(result.response.batch_id));
      }
      return result;
    };
    const observingSender: Sender<WordInstruction> = {
      send: (command) => observe((value) => sender.send(value), command),
      reconcile: (command) => observe((value) => sender.reconcile(value), command),
    };
    try {
      const summary = await replayQueue(store, observingSender);
      if (!mounted.current) return;
      setMessage(statusMessage(summary));
      await reloadState();
    } catch (caught) {
      console.error("Word queue replay failed", caught);
      if (mounted.current) setError("Sync could not finish. The instruction remains saved.");
    } finally {
      if (mounted.current) setBusy(false);
    }
  }, [loadError, online, reloadState, reviewUrlForBatch, sender, store]);

  useEffect(() => {
    mounted.current = true;
    let cancelled = false;
    void (async () => {
      setBusy(true);
      setMessage(undefined);
      setError(undefined);
      try {
        await bridge.ready();
        await reloadState();
        if (!cancelled && online) await runReplay();
      } catch (caught) {
        console.error("Word task pane startup failed", caught);
        if (!cancelled) setError("The task pane could not start. Your document was not changed.");
      } finally {
        if (!cancelled) setBusy(false);
      }
    })();
    return () => {
      cancelled = true;
      mounted.current = false;
    };
  }, [bridge, online, reloadState, runReplay]);

  useEffect(() => {
    const resume = () => {
      if (document.visibilityState === "visible" && online) void runReplay();
    };
    window.addEventListener("focus", resume);
    document.addEventListener("visibilitychange", resume);
    return () => {
      window.removeEventListener("focus", resume);
      document.removeEventListener("visibilitychange", resume);
    };
  }, [online, runReplay]);

  const submit = useCallback(() => {
    if (submission.current || !fingerprint || !text.trim() || loadError) return;
    const operation = (async () => {
      setBusy(true);
      setMessage(undefined);
      setError(undefined);
      try {
        await store.enqueue(
          {
            text,
            base: fingerprint,
            target: {
              kind: target,
              preview:
                target === "selection"
                  ? fingerprint.selectionPreview || "Current selection"
                  : "Current document",
            },
          },
          {
            id: crypto.randomUUID(),
            deduplicationKey: draftKey.current,
            createdAt: new Date().toISOString(),
          },
        );
        if (!mounted.current) return;
        setText("");
        draftKey.current = crypto.randomUUID();
        setMessage("Saved in this document");
        await reloadState();
        if (online) await runReplay();
      } catch (caught) {
        console.error("Word instruction save failed", caught);
        if (mounted.current) {
          setError("This instruction was not saved. The text remains available to retry.");
        }
      } finally {
        submission.current = undefined;
        if (mounted.current) setBusy(false);
      }
    })();
    submission.current = operation;
  }, [fingerprint, loadError, online, reloadState, runReplay, store, target, text]);

  const selected = commands.find((command) => command.id === selectedId);

  const resetQueue = async () => {
    setBusy(true);
    setMessage(undefined);
    setError(undefined);
    try {
      await store.reset();
      setLoadError(undefined);
      setMessage("Queue reset in this document");
      await reloadState();
    } catch (caught) {
      console.error("Word queue reset failed", caught);
      setError("The queue could not be reset in this document.");
    } finally {
      setBusy(false);
    }
  };

  const exportQueue = async () => {
    const raw = await store.exportRaw();
    const href = URL.createObjectURL(new Blob([raw], { type: "application/json" }));
    const anchor = document.createElement("a");
    anchor.href = href;
    anchor.download = "fieldledger-word-queue.json";
    anchor.click();
    URL.revokeObjectURL(href);
  };

  const refreshInstruction = async () => {
    if (!selected) return;
    setBusy(true);
    setMessage(undefined);
    setError(undefined);
    try {
      const current = await bridge.getFingerprint();
      await store.refreshStale(selected.id, current);
      setSelectedId(undefined);
      await reloadState();
      if (online) await runReplay();
    } catch (caught) {
      console.error("Stale Word instruction refresh failed", caught);
      setError("The instruction could not be refreshed. Nothing was applied.");
    } finally {
      setBusy(false);
    }
  };

  const retryFailed = async () => {
    if (!selected) return;
    setBusy(true);
    setMessage(undefined);
    setError(undefined);
    try {
      await store.retryFailed(selected.id);
      await runReplay();
    } catch (caught) {
      console.error("Word instruction retry failed", caught);
      setError("Retry could not start. The instruction remains saved.");
    } finally {
      setBusy(false);
    }
  };

  const reconcile = async () => {
    if (!selected) return;
    setBusy(true);
    setMessage(undefined);
    setError(undefined);
    try {
      await store.prepareReconciliation(selected.id);
      await runReplay();
    } catch (caught) {
      console.error("Word instruction reconciliation failed", caught);
      setError("Reconciliation could not finish. The instruction remains saved.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="taskpane-shell">
      <header className="app-bar">
        <span className="brand-mark" aria-hidden="true" />
        <div>
          <h1>FieldLedger</h1>
          <p>{hostLabel}</p>
        </div>
        <code title={fingerprint?.documentId ?? "Loading document identity"}>
          {fingerprint?.documentId.slice(0, 8) ?? "--------"}
        </code>
      </header>
      <StatusBar online={online} busy={busy} message={error ?? message} error={Boolean(error)} />

      {loadError ? (
        <section className="blocking-error" role="alert">
          <FileWarning aria-hidden="true" />
          <h2>Saved queue cannot be read</h2>
          <p>
            The saved queue in this document cannot be read. Export it before resetting;
            FieldLedger will not discard it silently.
          </p>
          <div className="panel-actions">
            <button type="button" className="secondary-command" onClick={() => void exportQueue()}>
              <Download aria-hidden="true" /> Export queue
            </button>
            <button type="button" className="primary-command" onClick={() => void resetQueue()}>
              <RotateCcw aria-hidden="true" /> Reset queue
            </button>
          </div>
        </section>
      ) : null}

      <Composer
        text={text}
        target={target}
        disabled={busy || Boolean(loadError)}
        onTextChange={(value) => {
          setText(value);
          draftKey.current = crypto.randomUUID();
        }}
        onTargetChange={(value) => {
          setTarget(value);
          draftKey.current = crypto.randomUUID();
        }}
        onSubmit={submit}
      />

      <QueueList commands={commands} selectedId={selectedId} onSelect={setSelectedId} />

      {selected ? (
        <ConflictPanel
          command={selected}
          current={fingerprint}
          busy={busy}
          onRefresh={() => void refreshInstruction()}
          onLater={() => setSelectedId(undefined)}
          onRetry={() => void retryFailed()}
          onReconcile={() => void reconcile()}
        />
      ) : null}

      {reviewUrl ? (
        <section className="review-handoff">
          <p>Proposal is ready for human review. Word content was not changed.</p>
          <button
            type="button"
            className="primary-command"
            onClick={() => void bridge.openReview(reviewUrl)}
          >
            Open proposal review
          </button>
        </section>
      ) : null}

      <div className="retry-band">
        <button type="button" className="icon-command" onClick={() => void runReplay()} disabled={busy || !online} title="Retry queue">
          <RotateCcw aria-hidden="true" /> <span>Retry queue</span>
        </button>
      </div>

      {demoControls ? (
        <section className="demo-controls" aria-labelledby="demo-heading">
          <div className="section-heading">
            <h2 id="demo-heading">Demo controls</h2>
            <span>Browser only</span>
          </div>
          <div>
            <button type="button" onClick={() => demoControls.setOnline(!online)}>
              {online ? <Unplug aria-hidden="true" /> : <Wifi aria-hidden="true" />}
              {online ? "Go offline" : "Go online"}
            </button>
            <button type="button" onClick={demoControls.moveDocument}>Move document</button>
            <button type="button" onClick={demoControls.loseNextResponse}>Lose next response</button>
          </div>
        </section>
      ) : null}
    </main>
  );
}
