import { ChevronRight } from "lucide-react";
import { Link } from "react-router-dom";

export interface ReviewRowModel {
  id: string;
  projectName: string;
  title: string;
  detail: string;
  count: number;
  status: "pending" | "stale" | "synced";
}

export function ReviewRow({ batch }: { batch: ReviewRowModel }) {
  const destination = batch.status === "stale" ? "/queue" : `/review/${batch.id}/1`;
  return (
    <Link className={`review-row review-row--${batch.status}`} to={destination} aria-label={`${batch.projectName}, ${batch.count} items`}>
      <span className="review-row__rail" aria-hidden="true" />
      <span className="review-row__content">
        <strong>{batch.projectName}</strong>
        <span>{batch.title}</span>
        <small>{batch.detail}</small>
      </span>
      <span className="review-row__count" aria-label={`${batch.count} items`}>{batch.count}</span>
      <ChevronRight aria-hidden="true" size={18} />
    </Link>
  );
}
