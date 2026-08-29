export interface QueueLifecycle {
  retry(): Promise<void>;
  dispose(): void;
}

export function registerQueueLifecycle(drain: () => Promise<unknown>): QueueLifecycle {
  let active: Promise<unknown> | null = null;

  const trigger = async () => {
    if (active !== null) return active.then(() => undefined);
    active = drain();
    try {
      await active;
    } finally {
      active = null;
    }
  };
  const onOnline = () => void trigger();
  const onVisibility = () => {
    if (document.visibilityState === "visible") void trigger();
  };

  window.addEventListener("online", onOnline);
  document.addEventListener("visibilitychange", onVisibility);
  void trigger();

  return {
    async retry() {
      if (active !== null) await active;
      await trigger();
    },
    dispose() {
      window.removeEventListener("online", onOnline);
      document.removeEventListener("visibilitychange", onVisibility);
    },
  };
}
