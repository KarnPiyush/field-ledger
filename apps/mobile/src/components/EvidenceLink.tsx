import { ExternalLink } from "lucide-react";

interface EvidenceLinkProps {
  source: string;
  coordinate: string;
  href?: string;
}

export function EvidenceLink({ source, coordinate, href = "#evidence" }: EvidenceLinkProps) {
  return (
    <a className="evidence-link" href={href}>
      <span>Source: {source}, {coordinate}</span>
      <ExternalLink aria-hidden="true" size={15} />
    </a>
  );
}
