"use client";

import { useState, useMemo, useRef, useEffect } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";

interface CalendarDate {
  day: string; // YYYY-MM-DD
  bars: number;
  ticks?: number;
}

interface RetroCalendarProps {
  dates: CalendarDate[];
  selectedDate: string;
  onSelect: (date: string) => void;
}

const WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];

export default function RetroCalendar({ dates, selectedDate, onSelect }: RetroCalendarProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // Build lookup maps: "YYYY-MM-DD" → bars / ticks
  const barMap = useMemo(() => {
    const m: Record<string, number> = {};
    for (const d of dates) m[d.day] = d.bars;
    return m;
  }, [dates]);
  const tickMap = useMemo(() => {
    const m: Record<string, number> = {};
    for (const d of dates) if (d.ticks) m[d.day] = d.ticks;
    return m;
  }, [dates]);

  // Current calendar month navigation
  const initialMonth = selectedDate
    ? new Date(selectedDate + "T00:00:00")
    : new Date();
  const [viewYear, setViewYear] = useState(initialMonth.getFullYear());
  const [viewMonth, setViewMonth] = useState(initialMonth.getMonth());

  // Close on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const prevMonth = () => {
    if (viewMonth === 0) { setViewYear(y => y - 1); setViewMonth(11); }
    else setViewMonth(m => m - 1);
  };
  const nextMonth = () => {
    if (viewMonth === 11) { setViewYear(y => y + 1); setViewMonth(0); }
    else setViewMonth(m => m + 1);
  };

  // Build calendar grid
  const calendarDays = useMemo(() => {
    const firstDay = new Date(viewYear, viewMonth, 1);
    // Monday = 0
    let startDow = firstDay.getDay() - 1;
    if (startDow < 0) startDow = 6;
    const daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate();

    const cells: (null | { date: string; day: number; bars: number | null; ticks: number | null })[] = [];
    // Padding
    for (let i = 0; i < startDow; i++) cells.push(null);
    for (let d = 1; d <= daysInMonth; d++) {
      const ds = `${viewYear}-${String(viewMonth + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
      const b = barMap[ds] ?? null;
      const t = tickMap[ds] ?? null;
      cells.push({ date: ds, day: d, bars: b, ticks: t });
    }
    return cells;
  }, [viewYear, viewMonth, barMap, tickMap]);

  const monthName = new Date(viewYear, viewMonth).toLocaleString("en-US", { month: "long" });

  // Formatted selected date for button
  const selectedLabel = selectedDate || "SELECT DATE";

  return (
    <div className="relative" ref={ref}>
      {/* Trigger button */}
      <button
        onClick={() => setOpen(o => !o)}
        className="flex items-center gap-2 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wider"
        style={{
          background: "#181c24",
          border: "1px solid #252a33",
          color: "#c8cdd5",
        }}
      >
        <span style={{ color: "#4da6ff" }}>{selectedLabel}</span>
        {selectedDate && barMap[selectedDate] !== undefined && (
          <span className="text-[9px]" style={{ color: "#5a6270" }}>
            ({barMap[selectedDate]} bars)
          </span>
        )}
      </button>

      {/* Dropdown calendar */}
      {open && (
        <div
          className="absolute right-0 top-full mt-1 z-50"
          style={{
            background: "#111318",
            border: "2px solid #252a33",
            boxShadow: "0 4px 20px rgba(0,0,0,0.6)",
            width: 320,
          }}
        >
          {/* Header */}
          <div
            className="flex items-center justify-between px-3 py-2"
            style={{ background: "#0e1117", borderBottom: "1px solid #252a33" }}
          >
            <button onClick={prevMonth} className="p-1" style={{ color: "#5a6270" }}>
              <ChevronLeft className="w-3.5 h-3.5" />
            </button>
            <span className="text-[11px] font-bold uppercase tracking-wider" style={{ color: "#c8cdd5" }}>
              {monthName} {viewYear}
            </span>
            <button onClick={nextMonth} className="p-1" style={{ color: "#5a6270" }}>
              <ChevronRight className="w-3.5 h-3.5" />
            </button>
          </div>

          {/* Weekday headers */}
          <div className="grid grid-cols-7 px-2 pt-2">
            {WEEKDAYS.map(w => (
              <div key={w} className="text-center text-[8px] font-bold uppercase tracking-wider py-1" style={{ color: "#3d4450" }}>
                {w}
              </div>
            ))}
          </div>

          {/* Day cells */}
          <div className="grid grid-cols-7 px-2 pb-3 gap-[1px]">
            {calendarDays.map((cell, i) => {
              if (!cell) return <div key={`e-${i}`} />;

              const hasData = cell.bars !== null || cell.ticks !== null;
              const hasTicks = cell.ticks !== null && cell.ticks > 0;
              const isSelected = cell.date === selectedDate;
              const isToday = cell.date === new Date().toISOString().slice(0, 10);

              return (
                <button
                  key={cell.date}
                  onClick={() => {
                    if (hasData) { onSelect(cell.date); setOpen(false); }
                  }}
                  disabled={!hasData}
                  className="flex flex-col items-center py-1.5 transition-all"
                  style={{
                    background: isSelected ? "#00e87b" : hasData ? "#181c24" : "transparent",
                    border: isToday && !isSelected ? "1px solid #4da6ff" : isSelected ? "1px solid #00e87b" : "1px solid transparent",
                    color: isSelected ? "#000" : hasData ? "#c8cdd5" : "#252a33",
                    cursor: hasData ? "pointer" : "default",
                    opacity: hasData ? 1 : 0.4,
                  }}
                >
                  <span className="text-[11px] font-semibold">{cell.day}</span>
                  {hasData && (
                    <div className="flex items-center gap-0.5 mt-0.5">
                      {cell.bars !== null && cell.bars > 0 && (
                        <span className="text-[7px] font-bold" style={{
                          color: isSelected ? "#0a3d26" : "#5a6270",
                        }}>
                          {cell.bars}
                        </span>
                      )}
                      {hasTicks && (
                        <span className="inline-block w-1 h-1 rounded-full" style={{ background: isSelected ? "#0a3d26" : "#e8c300" }} />
                      )}
                    </div>
                  )}
                </button>
              );
            })}
          </div>

          {/* Legend */}
          <div className="px-3 py-2 flex items-center gap-4 text-[8px]" style={{ borderTop: "1px solid #252a33", color: "#3d4450" }}>
            <span className="flex items-center gap-1">
              <span className="inline-block w-2 h-2" style={{ background: "#181c24", border: "1px solid #252a33" }} />
              HAS DATA
            </span>
            <span className="flex items-center gap-1">
              <span className="inline-block w-1.5 h-1.5 rounded-full" style={{ background: "#e8c300" }} />
              TICKS
            </span>
            <span className="flex items-center gap-1">
              <span className="inline-block w-2 h-2" style={{ background: "#00e87b" }} />
              SELECTED
            </span>
            <span className="flex items-center gap-1">
              <span className="inline-block w-2 h-2" style={{ border: "1px solid #4da6ff" }} />
              TODAY
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
