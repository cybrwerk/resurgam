import type { SVGProps } from "react";

/* Minimal hand-authored icon set — avoids pulling in an icon library for a
   handful of status glyphs. Every icon is used alongside text, never alone
   (manifest §26.6/§26.9: status is never color-only). */

type IconProps = SVGProps<SVGSVGElement>;

const base = {
  width: 14,
  height: 14,
  viewBox: "0 0 16 16",
  fill: "none",
  "aria-hidden": true,
};

export function CheckCircleIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeWidth="1.5" />
      <path d="M5.2 8.2l1.8 1.8 3.8-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function WarningTriangleIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M8 2.5l6.2 11H1.8L8 2.5z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      <path d="M8 6.5v3.2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <circle cx="8" cy="11.8" r="0.8" fill="currentColor" />
    </svg>
  );
}

export function InfoCircleIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <circle cx="8" cy="8" r="6.2" stroke="currentColor" strokeWidth="1.5" />
      <path d="M8 7.2v4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <circle cx="8" cy="4.9" r="0.8" fill="currentColor" />
    </svg>
  );
}

export function XCircleIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeWidth="1.5" />
      <path d="M5.8 5.8l4.4 4.4M10.2 5.8l-4.4 4.4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

export function QuestionCircleIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeWidth="1.5" />
      <path d="M6.2 6.3a1.8 1.8 0 113.2 1.1c-.5.5-1 .8-1 1.6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <circle cx="8" cy="11.3" r="0.75" fill="currentColor" />
    </svg>
  );
}

export function ClockIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeWidth="1.5" />
      <path d="M8 4.5V8l2.6 1.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function ChevronDownIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M4 6l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function ServerIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <rect x="2" y="3" width="12" height="4" rx="1" stroke="currentColor" strokeWidth="1.5" />
      <rect x="2" y="9" width="12" height="4" rx="1" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="4.5" cy="5" r="0.6" fill="currentColor" />
      <circle cx="4.5" cy="11" r="0.6" fill="currentColor" />
    </svg>
  );
}

export function DatabaseIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <ellipse cx="8" cy="4" rx="5.5" ry="2" stroke="currentColor" strokeWidth="1.5" />
      <path d="M2.5 4v8c0 1.1 2.46 2 5.5 2s5.5-.9 5.5-2V4" stroke="currentColor" strokeWidth="1.5" />
      <path d="M2.5 8c0 1.1 2.46 2 5.5 2s5.5-.9 5.5-2" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

/** Filled gold/amber, not `currentColor` -- a folder reads as a folder
 * at a glance (Windows Explorer convention) regardless of the
 * surrounding text color or theme, raised directly by the user. */
export function FolderIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path
        d="M2 4.5c0-.55.45-1 1-1h3.5l1.5 1.5H13c.55 0 1 .45 1 1v6c0 .55-.45 1-1 1H3c-.55 0-1-.45-1-1v-7.5z"
        fill="#FFC94D"
        stroke="#B3831A"
        strokeWidth="1"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Container (CT) backup groups -- a crate/box, distinct from `VmIcon`'s
 * monitor and `HostIcon`'s tower. Raised directly by the user: the
 * Datastores tree used this same isometric-box glyph for every backup
 * group regardless of type, so a VM and a CT row looked identical apart
 * from the small text tag next to it. */
export function BoxIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M2.5 5.2L8 2.5l5.5 2.7v6.1L8 14 2.5 11.3V5.2z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      <path d="M2.5 5.2L8 7.9l5.5-2.7M8 7.9V14" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

/** Virtual machine (VM) backup groups -- a monitor + stand, the
 * conventional "VM" glyph, kept distinct from `BoxIcon` (CT) and
 * `HostIcon` (bare-metal host) so the three read apart at a glance in
 * the Datastores tree.
 */
export function VmIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <rect x="2" y="3" width="12" height="8" rx="1" stroke="currentColor" strokeWidth="1.5" />
      <path d="M6 13.5h4M8 11v2.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

/** Bare-metal host backup groups -- a tower, distinct from `ServerIcon`
 * (the PBS instance row further up the same tree, two horizontal
 * rack units) and from `VmIcon`/`BoxIcon`.
 */
export function HostIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <rect x="4" y="1.5" width="8" height="13" rx="1" stroke="currentColor" strokeWidth="1.5" />
      <path d="M6 4.2h4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <circle cx="6.2" cy="11.5" r="0.7" fill="currentColor" />
    </svg>
  );
}

export function FileIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M4 2h5l3 3v9H4V2z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      <path d="M9 2v3h3" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

/** Filled gold/amber, same reasoning as FolderIcon -- the back panel a
 * touch darker than the front flap for a little depth. */
export function FolderOpenIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path
        d="M2 5.2c0-.55.45-1 1-1h3l1.5 1.5H13c.55 0 1 .45 1 1v.3H4.3c-.4 0-.76.24-.92.6L2 11V5.2z"
        fill="#F2B33D"
        stroke="#B3831A"
        strokeWidth="1"
        strokeLinejoin="round"
      />
      <path
        d="M2 11l1.6-4.1c.16-.4.54-.65.97-.65H14l-1.7 4.6c-.15.4-.53.65-.96.65H2.6c-.5 0-.85-.5-.6-.95z"
        fill="#FFD873"
        stroke="#B3831A"
        strokeWidth="1"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function DownloadIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M8 2.5v7.2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <path d="M4.8 6.8L8 10l3.2-3.2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M2.5 12.2v.8c0 .55.45 1 1 1h9c.55 0 1-.45 1-1v-.8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function SearchIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <circle cx="7" cy="7" r="4.5" stroke="currentColor" strokeWidth="1.5" />
      <path d="M10.3 10.3L14 14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

/* Navigation section icons (OPNsense-style sidebar, 2026-10-04) -- same
   16px grid and 1.5 stroke as the status glyphs above. In the collapsed
   icon rail they are the only visible cue, so each section's flyout
   carries its full label. */

export function HomeIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M2.5 7.2L8 2.5l5.5 4.7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M4 6.2v7.3h3V10h2v3.5h3V6.2" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

export function ArchiveIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <rect x="1.75" y="2.5" width="12.5" height="3.5" rx="1" stroke="currentColor" strokeWidth="1.5" />
      <path d="M2.75 6v6.5a1 1 0 001 1h8.5a1 1 0 001-1V6" stroke="currentColor" strokeWidth="1.5" />
      <path d="M6.5 8.75h3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

export function CogIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <circle cx="8" cy="8" r="2.2" stroke="currentColor" strokeWidth="1.5" />
      <path
        d="M8 1.75v1.8M8 12.45v1.8M14.25 8h-1.8M3.55 8h-1.8M12.42 3.58l-1.27 1.27M4.85 11.15l-1.27 1.27M12.42 12.42l-1.27-1.27M4.85 4.85L3.58 3.58"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function HistoryIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M2.6 8a5.4 5.4 0 101.6-3.85" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <path d="M2.3 2.6v2.6h2.6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M8 5.2V8l1.9 1.4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function LifebuoyIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <circle cx="8" cy="8" r="6.25" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="8" cy="8" r="2.5" stroke="currentColor" strokeWidth="1.5" />
      <path d="M3.6 3.6l2.6 2.6M9.8 9.8l2.6 2.6M12.4 3.6L9.8 6.2M6.2 9.8l-2.6 2.6" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

export function ChartIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M2.25 2v11.75H14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M5 10.5V8M8 10.5V5.5M11 10.5V7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

export function ShieldIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M8 1.75l5.25 2v4c0 3.2-2.3 5.6-5.25 6.5C5.05 13.35 2.75 10.95 2.75 7.75v-4L8 1.75z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      <path d="M5.8 8l1.5 1.5 2.9-3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function ChevronRightIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M6 3.5L10.5 8 6 12.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function SidebarToggleIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <rect x="1.75" y="2.5" width="12.5" height="11" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
      <path d="M6 2.5v11" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

/* Top-bar controls (Petrol & Brass alignment, 2026-10-04): 16px grid,
   1.4 stroke, per that system's iconography rules. */

export function MenuIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M2 4h12M2 8h12M2 12h12" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}

export function SunIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <circle cx="8" cy="8" r="2.8" stroke="currentColor" strokeWidth="1.4" />
      <path
        d="M8 1.5v1.4M8 13.1v1.4M1.5 8h1.4M13.1 8h1.4M3.4 3.4l1 1M11.6 11.6l1 1M12.6 3.4l-1 1M4.4 11.6l-1 1"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function MoonIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M13.5 9.6A5.6 5.6 0 016.4 2.5a5.6 5.6 0 107.1 7.1z" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
    </svg>
  );
}

export function MonitorIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <rect x="1.75" y="2.5" width="12.5" height="8.5" rx="1" stroke="currentColor" strokeWidth="1.4" />
      <path d="M6 14h4M8 11v3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}
