import { ClipboardList, History, Inbox, ListOrdered, Wrench } from "lucide-react";
import { NavLink } from "react-router-dom";

const links = [
  { to: "/inbox", label: "Inbox", Icon: Inbox, phone: true },
  { to: "/queue", label: "Queue", Icon: ListOrdered, phone: true },
  { to: "/audit", label: "Audit", Icon: History, phone: true },
  { to: "/ops", label: "Operations", Icon: Wrench, phone: false },
] as const;

export function Navigation() {
  return (
    <nav className="primary-nav" aria-label="Primary navigation">
      <div className="desktop-brand"><span className="brand-mark" aria-hidden="true" /><span className="wordmark">FieldLedger</span></div>
      <p className="nav-section"><ClipboardList aria-hidden="true" size={16} /> Review workspace</p>
      {links.map(({ to, label, Icon, phone }) => (
        <NavLink key={to} to={to} className={({ isActive }) => `nav-link${isActive ? " nav-link--active" : ""}${phone ? "" : " nav-link--desktop"}`}>
          <Icon aria-hidden="true" /><span>{label}</span>
        </NavLink>
      ))}
    </nav>
  );
}
