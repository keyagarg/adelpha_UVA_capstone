import { create } from "zustand";

import { fetchEmiStatus, measureEmi, type EmiCapture, type EmiStatus, type EmiTrace } from "../mri/api";

/** Pause between captures in Run mode, so the plot stays readable and the API is not flooded. */
export const RUN_INTERVAL_MS = 250;

export function traceKey(trace: Pick<EmiTrace, "board" | "channel">): string {
  return `${trace.board}-${trace.channel}`;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

type EmiState = {
  status: EmiStatus | null;
  /** Latest capture. Lives here, not in the view, so it survives switching tools. */
  capture: EmiCapture | null;
  /** A single capture is in flight (Measure once). */
  measuring: boolean;
  /** The Run loop is active. */
  running: boolean;
  error: string | null;
  /** Also record board 0 RX0 (the MRI channel) as a reference trace. */
  includeReference: boolean;
  /** Trace keys the user has hidden on the plot. */
  hidden: Record<string, boolean>;
  /** Largest magnitude seen per trace during the current run; keeps the y-axis steady. */
  peaks: Record<string, number>;
  refreshStatus: () => Promise<void>;
  measureOnce: () => Promise<void>;
  start: () => void;
  stop: () => void;
  setIncludeReference: (value: boolean) => void;
  toggleTrace: (key: string) => void;
  reset: () => void;
};

/** Bumped by stop() and start(); a loop exits as soon as its own number is stale. */
let runToken = 0;

function tracePeaks(capture: EmiCapture, previous: Record<string, number> | null): Record<string, number> {
  const peaks: Record<string, number> = {};
  for (const trace of capture.traces) {
    const key = traceKey(trace);
    peaks[key] = Math.max(trace.peak_magnitude, previous?.[key] ?? 0);
  }
  return peaks;
}

export const useEmiStore = create<EmiState>((set, get) => ({
  status: null,
  capture: null,
  measuring: false,
  running: false,
  error: null,
  includeReference: false,
  hidden: {},
  peaks: {},

  refreshStatus: async () => {
    try {
      set({ status: await fetchEmiStatus() });
    } catch (err) {
      set({ status: null, error: `Could not reach the Imaging Console service: ${errorMessage(err)}` });
    }
  },

  measureOnce: async () => {
    if (get().measuring || get().running) return;
    set({ measuring: true, error: null });
    try {
      const capture = await measureEmi({ include_reference: get().includeReference });
      set({ capture, peaks: tracePeaks(capture, null) });
    } catch (err) {
      set({ error: errorMessage(err) });
      void get().refreshStatus();
    } finally {
      set({ measuring: false });
    }
  },

  start: () => {
    if (get().running || get().measuring) return;
    const token = ++runToken;
    set({ running: true, error: null, peaks: {} });
    void (async () => {
      while (token === runToken) {
        try {
          const capture = await measureEmi({ include_reference: get().includeReference });
          if (token !== runToken) return;
          set({ capture, peaks: tracePeaks(capture, get().peaks) });
        } catch (err) {
          if (token !== runToken) return;
          // A failed capture ends the run: retrying against a busy scanner helps nobody.
          runToken += 1;
          set({ running: false, error: errorMessage(err) });
          void get().refreshStatus();
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, RUN_INTERVAL_MS));
      }
    })();
  },

  stop: () => {
    runToken += 1;
    if (get().running) set({ running: false });
  },

  setIncludeReference: (value) => set({ includeReference: value }),

  toggleTrace: (key) => set((state) => ({ hidden: { ...state.hidden, [key]: !state.hidden[key] } })),

  reset: () => {
    runToken += 1;
    set({
      status: null,
      capture: null,
      measuring: false,
      running: false,
      error: null,
      includeReference: false,
      hidden: {},
      peaks: {},
    });
  },
}));
