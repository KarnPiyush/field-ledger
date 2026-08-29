import type { FieldLedgerDatabase } from "./database";

export class ReviewCache {
  public constructor(private readonly db: FieldLedgerDatabase) {}

  public listBatches(projectId: string) {
    return this.db.batches.where("projectId").equals(projectId).sortBy("cachedAt");
  }

  public getBatch(projectId: string, batchId: string) {
    return this.db.batches.get(batchId).then((batch) =>
      batch?.projectId === projectId ? batch : undefined,
    );
  }

  public listAudit(projectId: string) {
    return this.db.auditRows.where("projectId").equals(projectId).reverse().sortBy("createdAt");
  }

  public listJobs(projectId: string) {
    return this.db.providerJobs.where("projectId").equals(projectId).sortBy("updatedAt");
  }
}
