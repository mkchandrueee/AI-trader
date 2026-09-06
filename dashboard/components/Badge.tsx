"use client";

type Variant = "green" | "red" | "yellow" | "blue" | "purple" | "gray";

const colors: Record<Variant, { border: string; text: string; bg: string }> = {
  green:  { border: '#1a5c3a', text: '#00e87b', bg: '#0a2a18' },
  red:    { border: '#5c1a1a', text: '#ff3e3e', bg: '#2a0a0a' },
  yellow: { border: '#5c4a0a', text: '#e8c300', bg: '#2a2200' },
  blue:   { border: '#1a3a5c', text: '#4da6ff', bg: '#0a1a2a' },
  purple: { border: '#3a1a5c', text: '#a78bfa', bg: '#1a0a2a' },
  gray:   { border: '#333a45', text: '#5a6270', bg: '#1e222c' },
};

export default function Badge({ label, variant = "gray" }: { label: string; variant?: Variant }) {
  const c = colors[variant];
  return (
    <span
      className="t-badge"
      style={{ borderColor: c.border, color: c.text, background: c.bg }}
    >
      {label}
    </span>
  );
}
