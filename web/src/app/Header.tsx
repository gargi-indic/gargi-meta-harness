import { useState } from 'react';
import { NavLink } from 'react-router-dom';
import { useHealth } from '../api/queries';
import { StatusTag } from '../ui/StatusTag';

export function Header() {
  const [open, setOpen] = useState(false);
  const { data: health } = useHealth();

  return (
    <header className="header">
      <div className="header-inner">
        <div className="logo">GARGI HARNESS</div>
        {health?.demo && <StatusTag tone="muted">DEMO DATA</StatusTag>}
        <button
          className="menu-btn"
          aria-expanded={open}
          aria-controls="main-nav"
          onClick={() => setOpen(!open)}
        >
          Menu
        </button>
        <nav id="main-nav" aria-label="Main" data-open={open}>
          <NavLink to="/campaigns" onClick={() => setOpen(false)}>Campaigns</NavLink>
          <NavLink to="/board" onClick={() => setOpen(false)}>Board</NavLink>
          <NavLink to="/inbox" onClick={() => setOpen(false)}>Inbox</NavLink>
          <NavLink to="/runs" onClick={() => setOpen(false)}>Runs</NavLink>
          <NavLink to="/playbooks" onClick={() => setOpen(false)}>Playbooks</NavLink>
          <NavLink to="/planner" onClick={() => setOpen(false)}>Planner</NavLink>
        </nav>
      </div>
    </header>
  );
}
