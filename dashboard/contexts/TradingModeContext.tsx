"use client";

import { createContext, useContext, useState, useCallback, type ReactNode } from "react";

export type TradingMode = "test" | "live";

interface TradingModeContextValue {
  mode: TradingMode;
  setMode: (mode: TradingMode) => void;
  dialogError: string | null;
  setDialogError: (msg: string | null) => void;
  showDialog: boolean;
  setShowDialog: (v: boolean) => void;
}

const TradingModeContext = createContext<TradingModeContextValue | null>(null);

export function TradingModeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeInternal] = useState<TradingMode>("test");
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [showDialog, setShowDialog] = useState(false);

  const setMode = useCallback((newMode: TradingMode) => {
    if (newMode === "live") {
      // Broker credentials never live in NEXT_PUBLIC_* vars (those ship to the
      // browser) — ask the Flask backend, which reads them from its own .env,
      // whether a live AngelOne session is actually connected.
      fetch("/api/broker/auth/status")
        .then((r) => r.json())
        .then((d) => {
          if (!d.connected) {
            setDialogError("ANGELONE NOT CONNECTED\n\nSet ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PASSWORD_OR_PIN and ANGEL_TOTP_SECRET in the backend's .env, then restart the backend, to enable live trading.");
            setShowDialog(true);
            return;
          }
          setModeInternal(newMode);
        })
        .catch(() => {
          setDialogError("COULD NOT REACH BACKEND\n\nStart backend/app.py before switching to live mode.");
          setShowDialog(true);
        });
      return;
    }
    setModeInternal(newMode);
  }, []);

  return (
    <TradingModeContext.Provider value={{ mode, setMode, dialogError, setDialogError, showDialog, setShowDialog }}>
      {children}
    </TradingModeContext.Provider>
  );
}

export function useTradingMode() {
  const ctx = useContext(TradingModeContext);
  if (!ctx) throw new Error("useTradingMode must be used within TradingModeProvider");
  return ctx;
}
