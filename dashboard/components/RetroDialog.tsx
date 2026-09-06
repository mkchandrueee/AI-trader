"use client";

interface RetroDialogProps {
  open: boolean;
  onClose: () => void;
  title: string;
  message: string;
  type?: "error" | "warning" | "info";
}

export default function RetroDialog({ open, onClose, title, message, type = "error" }: RetroDialogProps) {
  if (!open) return null;

  const borderColor = type === "error" ? "#ff3e3e" : type === "warning" ? "#e8c300" : "#4da6ff";
  const titleColor = borderColor;
  const iconChar = type === "error" ? "✖" : type === "warning" ? "⚠" : "ℹ";

  return (
    <div className="fixed inset-0 z-[9998] flex items-center justify-center" style={{ background: "rgba(0,0,0,0.7)" }}>
      <div
        className="w-full max-w-md"
        style={{
          background: "#111318",
          border: `2px solid ${borderColor}`,
          boxShadow: `0 0 20px ${borderColor}33, inset 0 0 40px rgba(0,0,0,0.5)`,
        }}
      >
        {/* Title bar */}
        <div
          className="flex items-center justify-between px-4 py-2"
          style={{ background: "#0e1117", borderBottom: `1px solid ${borderColor}` }}
        >
          <div className="flex items-center gap-2">
            <span style={{ color: borderColor, fontSize: 12 }}>{iconChar}</span>
            <span className="text-[11px] font-bold uppercase tracking-wider" style={{ color: titleColor }}>
              {title}
            </span>
          </div>
          <button
            onClick={onClose}
            className="text-[11px] font-bold px-2 py-0.5"
            style={{ color: "#5a6270", border: "1px solid #252a33", background: "#181c24" }}
          >
            [X]
          </button>
        </div>

        {/* Body */}
        <div className="px-4 py-5">
          <pre
            className="text-[11px] leading-relaxed whitespace-pre-wrap"
            style={{ color: "#c8cdd5", fontFamily: "'JetBrains Mono', monospace" }}
          >
            {message}
          </pre>
        </div>

        {/* Footer */}
        <div className="px-4 py-3 flex justify-end" style={{ borderTop: "1px solid #252a33" }}>
          <button
            onClick={onClose}
            className="text-[10px] font-bold uppercase tracking-wider px-4 py-1.5"
            style={{
              background: "#181c24",
              border: `1px solid ${borderColor}`,
              color: borderColor,
            }}
          >
            OK
          </button>
        </div>
      </div>
    </div>
  );
}
