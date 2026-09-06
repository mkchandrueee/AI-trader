"use client";

interface StatCardProps {
  label: string;
  value: string | number;
  sub?: string;
  color?: "green" | "red" | "yellow" | "blue" | "purple" | "default";
  pulse?: boolean;
}

const colorMap: Record<string, string> = {
  green:   "#00e87b",
  red:     "#ff3e3e",
  yellow:  "#e8c300",
  blue:    "#4da6ff",
  purple:  "#a78bfa",
  default: "#c8cdd5",
};

export default function StatCard({ label, value, sub, color = "default", pulse }: StatCardProps) {
  return (
    <div className="t-panel p-3">
      <p className="text-[9px] uppercase tracking-[1.5px] mb-1" style={{ color: '#5a6270' }}>{label}</p>
      <div className="text-xl font-bold flex items-center gap-2" style={{ color: colorMap[color] }}>
        {pulse && (
          <span className="w-2 h-2 t-pulse" style={{ background: '#00e87b' }} />
        )}
        {value}
      </div>
      {sub && <p className="text-[10px] mt-1" style={{ color: '#3d4450' }}>{sub}</p>}
    </div>
  );
}
