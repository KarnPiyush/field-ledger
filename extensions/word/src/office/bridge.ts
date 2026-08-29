import { fingerprintSnapshot } from "./fingerprint";
import {
  OfficeSettingsStorage,
  type OfficeSettings,
} from "./storage";

export interface DocumentFingerprint {
  documentId: string;
  contentDigest: string;
  selectionDigest: string;
  selectionPreview: string;
}

export interface WordInstruction {
  text: string;
  base: DocumentFingerprint;
  target: { kind: "document" | "selection"; preview: string };
}

export type SerializedQueue = string;

export interface WordBridge {
  ready(): Promise<void>;
  getFingerprint(): Promise<DocumentFingerprint>;
  loadQueue(): Promise<SerializedQueue>;
  saveQueue(queue: SerializedQueue): Promise<void>;
  openReview(url: string): Promise<void>;
}

export interface WordReadSnapshot {
  bodyText: string;
  selectionText: string;
  selectionContext: string;
}

export interface OfficeWordBridgeOptions {
  ready: () => Promise<void>;
  settings: OfficeSettings;
  readSnapshot: () => Promise<WordReadSnapshot>;
  openBrowserWindow: (url: string) => void;
  createId?: () => string;
  saveTimeoutMs?: number;
}

export class OfficeWordBridge implements WordBridge {
  private readonly storage: OfficeSettingsStorage;

  public constructor(private readonly options: OfficeWordBridgeOptions) {
    this.storage = new OfficeSettingsStorage(options.settings, {
      ...(options.createId === undefined ? {} : { createId: options.createId }),
      ...(options.saveTimeoutMs === undefined ? {} : { timeoutMs: options.saveTimeoutMs }),
    });
  }

  public ready(): Promise<void> {
    return this.options.ready();
  }

  public async getFingerprint(): Promise<DocumentFingerprint> {
    const [documentId, snapshot] = await Promise.all([
      this.storage.getOrCreateDocumentId(),
      this.options.readSnapshot(),
    ]);
    return fingerprintSnapshot({ documentId, ...snapshot });
  }

  public loadQueue(): Promise<SerializedQueue> {
    return this.storage.loadQueue();
  }

  public saveQueue(queue: SerializedQueue): Promise<void> {
    return this.storage.saveQueue(queue);
  }

  public async openReview(url: string): Promise<void> {
    this.options.openBrowserWindow(url);
  }
}

interface WordProxy {
  text: string;
  load(property: string): void;
}

interface WordParagraphCollection {
  items: WordProxy[];
  load(property: string): void;
}

interface WordSelection extends WordProxy {
  paragraphs: WordParagraphCollection;
}

interface WordContext {
  document: {
    body: WordProxy;
    getSelection(): WordSelection;
  };
  sync(): Promise<void>;
}

interface OfficeGlobal {
  onReady(): Promise<unknown>;
  context: {
    document: { settings: OfficeSettings };
    ui?: { openBrowserWindow?: (url: string) => void };
  };
}

interface WordGlobal {
  run<T>(callback: (context: WordContext) => Promise<T>): Promise<T>;
}

function globals(): { office: OfficeGlobal; word: WordGlobal } {
  const scope = globalThis as typeof globalThis & { Office?: OfficeGlobal; Word?: WordGlobal };
  if (!scope.Office || !scope.Word) throw new Error("Microsoft Word host is unavailable");
  return { office: scope.Office, word: scope.Word };
}

export function createOfficeWordBridge(): OfficeWordBridge {
  const { office, word } = globals();
  return new OfficeWordBridge({
    ready: async () => {
      await office.onReady();
    },
    settings: office.context.document.settings,
    readSnapshot: () =>
      word.run(async (context) => {
        const body = context.document.body;
        const selection = context.document.getSelection();
        body.load("text");
        selection.load("text");
        selection.paragraphs.load("items/text");
        await context.sync();
        return {
          bodyText: body.text,
          selectionText: selection.text,
          selectionContext: selection.paragraphs.items.map((item) => item.text).join("\n"),
        };
      }),
    openBrowserWindow: (url) => {
      const open = office.context.ui?.openBrowserWindow;
      if (open) open(url);
      else window.open(url, "_blank", "noopener,noreferrer");
    },
  });
}
