export interface MetricBandProps {
  pending: number;
  queued: number;
  stale: number;
}

export function MetricBand({ pending, queued, stale }: MetricBandProps) {
  const metrics = [[pending, "To review"], [queued, "Queued"], [stale, "Needs refresh"]] as const;
  return (
    <div className="metric-band" aria-label="Review metrics">
      {metrics.map(([value, label]) => (
        <div className="metric" key={label}>
          <strong>{value}</strong>
          <span>{label}</span>
        </div>
      ))}
    </div>
  );
}
