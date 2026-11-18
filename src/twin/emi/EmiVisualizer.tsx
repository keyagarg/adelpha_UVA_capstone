import { useEffect, useMemo } from "react";
import { Activity, Play, Save, Square } from "lucide-react";

import { lightThemePalette, useConsoleTheme } from "../consoleTheme";
import type { EmiCapture, EmiTrace, PlotAxes } from "../mri/api";
import { ScientificPlot } from "../ScientificPlot";
import { traceKey, useEmiStore } from "./emiStore";
import "./emiVisualizer.css";

type ModeColor = { dark: string; light: string };

/**
 * One fixed colour per receive channel, keyed by board and channel so a trace
 * keeps its colour whichever other traces are present or hidden.
 */
const TRACE_COLORS: Record<string, ModeColor> = {
  "0-rx1": { dark: "#3987e5", light: "#2a78d6" },
  "1-rx0": { dark: "#d95926", light: "#eb6834" },
  "1-rx1": { dark: "#199e70", light: "#1baf7a" },
};
/** The MRI reference channel and any unexpected channel stay neutral. */
const NEUTRAL_COLOR: ModeColor = { dark: "#9ca3af", light: "#6b7280" };

function traceColor(trace: EmiTrace, light: boolean): string {
  const color =
    trace.role === "mri_reference" ? NEUTRAL_COLOR : TRACE_COLORS[traceKey(trace)] ?? NEUTRAL_COLOR;
  return light ? color.light : color.dark;
}

function formatMHz(hz: number, digits = 3): string {
  return `${(hz / 1e6).toFixed(digits)} MHz`;
}

/** One overlaid spectrum plot: x is the offset from the centre frequency in kHz. */
function captureToAxes(
  capture: EmiCapture,
  light: boolean,
  hidden: Record<string, boolean>,
  peaks: Record<string, number>,
): PlotAxes[] {
  const center = capture.center_frequency_hz;
  const x = capture.frequency_hz.map((hz) => (hz - center) / 1e3);
  const halfSpanKHz = capture.bandwidth_hz / 2e3;
  const visible = capture.traces.filter((trace) => !hidden[traceKey(trace)]);
  const peak = Math.max(0, ...visible.map((trace) => peaks[traceKey(trace)] ?? trace.peak_magnitude));
  return [
    {
      title: "",
      xlabel: `Offset from ${formatMHz(center)} (kHz)`,
      ylabel: `Magnitude (${capture.units})`,
      xmin: -halfSpanKHz,
      xmax: halfSpanKHz,
      ymin: 0,
      ymax: peak > 0 ? peak * 1.1 : 1,
      series: visible.map((trace) => ({
        name: trace.label,
        x,
        y: trace.magnitude,
        color: traceColor(trace, light),
      })),
    },
  ];
}

/**
 * EMI stage of the Digital Twin viewport. Records noise bursts on the EMI
 * receive channels through the Imaging Console service and plots one spectrum
 * per channel. `active` is false while another workspace covers the viewport.
 */
export function EmiVisualizer({ active = true }: { active?: boolean }) {
  const [theme] = useConsoleTheme();
  const light = Boolean(lightThemePalette(theme));
  const status = useEmiStore((s) => s.status);
  const capture = useEmiStore((s) => s.capture);
  const measuring = useEmiStore((s) => s.measuring);
  const running = useEmiStore((s) => s.running);
  const error = useEmiStore((s) => s.error);
  const includeReference = useEmiStore((s) => s.includeReference);
  const hidden = useEmiStore((s) => s.hidden);
  const peaks = useEmiStore((s) => s.peaks);
  const refreshStatus = useEmiStore((s) => s.refreshStatus);
  const measureOnce = useEmiStore((s) => s.measureOnce);
  const start = useEmiStore((s) => s.start);
  const stop = useEmiStore((s) => s.stop);
  const setIncludeReference = useEmiStore((s) => s.setIncludeReference);
  const toggleTrace = useEmiStore((s) => s.toggleTrace);
  const saving = useEmiStore((s) => s.saving);
  const savedFolder = useEmiStore((s) => s.savedFolder);
  const save = useEmiStore((s) => s.save);

  // Check availability on open; never leave the boards acquiring unwatched.
  useEffect(() => {
    void refreshStatus();
    return () => stop();
  }, [refreshStatus, stop]);

  useEffect(() => {
    if (!active) stop();
  }, [active, stop]);

  const axes = useMemo(
    () => (capture ? captureToAxes(capture, light, hidden, peaks) : null),
    [capture, light, hidden, peaks],
  );

  const unavailable = status !== null && !status.available;
  const notice = error ?? (unavailable ? status?.reason || "EMI measurement is unavailable." : null);
  const simulated = capture ? capture.source === "simulated" : status?.simulation ?? false;
  const showSource = capture !== null || status !== null;
  const referenceOffered = status?.channels.some((channel) => channel.role === "mri_reference") ?? false;
  const allHidden = capture !== null && axes !== null && axes[0].series.length === 0;

  return (
    <div className="emi-view">
      <header className="emi-view-head">
        <div className="emi-view-heading">
          <h2>
            EMI spectrum
            {showSource ? (
              <span className={`emi-source${simulated ? " is-simulated" : ""}`}>
                {simulated ? "Simulated" : "Measured"}
              </span>
            ) : null}
            {running ? <span className="emi-source emi-live">Running</span> : null}
          </h2>
          <p>
            {capture
              ? `Centre ${formatMHz(capture.center_frequency_hz)} · span ${(capture.bandwidth_hz / 1e3).toFixed(0)} kHz · ${capture.n_samples} samples · ${new Date(capture.timestamp).toLocaleTimeString()}`
              : "In-band noise on the EMI receive channels."}
          </p>
        </div>
        <div className="emi-view-actions">
          <label className="emi-check">
            <input
              type="checkbox"
              checked={includeReference}
              disabled={!referenceOffered}
              onChange={(event) => setIncludeReference(event.target.checked)}
            />
            MRI reference channel
          </label>
          <button
            type="button"
            className="emi-btn"
            onClick={() => void measureOnce()}
            disabled={measuring || running || unavailable}
          >
            <Activity size={15} strokeWidth={1.75} aria-hidden />
            {measuring ? "Measuring…" : "Measure once"}
          </button>
          <button
            type="button"
            className="emi-btn"
            onClick={() => void save()}
            disabled={!capture || saving || running || measuring || savedFolder !== null}
            title={running ? "Stop the run to save the capture on screen" : undefined}
          >
            <Save size={15} strokeWidth={1.75} aria-hidden />
            {saving ? "Saving…" : savedFolder !== null ? "Saved" : "Save"}
          </button>
          {running ? (
            <button type="button" className="emi-btn emi-btn-primary" onClick={stop}>
              <Square size={14} strokeWidth={1.75} aria-hidden />
              Stop
            </button>
          ) : (
            <button
              type="button"
              className="emi-btn emi-btn-primary"
              onClick={start}
              disabled={measuring || unavailable}
            >
              <Play size={14} strokeWidth={1.75} aria-hidden />
              Run
            </button>
          )}
        </div>
      </header>

      {notice ? (
        <p className="emi-view-notice" role="alert">
          {notice}
        </p>
      ) : null}

      {savedFolder !== null ? (
        <p className="emi-view-saved" role="status">
          Saved to <code>{savedFolder}</code>
        </p>
      ) : null}

      <div className="emi-view-plot">
        {axes && !allHidden ? (
          <ScientificPlot axes={axes} />
        ) : (
          <p className="emi-view-empty">
            {allHidden
              ? "Every channel is hidden. Tick a channel in the table to show it."
              : "No capture yet. Select Measure once for a single noise burst, or Run to keep measuring."}
          </p>
        )}
      </div>

      {capture ? (
        <table className="emi-view-readout">
          <thead>
            <tr>
              <th scope="col">Channel</th>
              <th scope="col">RMS ({capture.units})</th>
              <th scope="col">Peak frequency</th>
              <th scope="col">Peak ({capture.units})</th>
            </tr>
          </thead>
          <tbody>
            {capture.traces.map((trace) => {
              const key = traceKey(trace);
              return (
                <tr key={key} className={hidden[key] ? "is-hidden" : undefined}>
                  <th scope="row">
                    <label className="emi-check">
                      <input type="checkbox" checked={!hidden[key]} onChange={() => toggleTrace(key)} />
                      <span className="emi-swatch" style={{ background: traceColor(trace, light) }} aria-hidden />
                      {trace.label}
                      {trace.role === "mri_reference" ? " (MRI reference)" : ""}
                    </label>
                  </th>
                  <td>{trace.rms.toExponential(2)}</td>
                  <td>{formatMHz(trace.peak_frequency_hz, 4)}</td>
                  <td>{trace.peak_magnitude.toExponential(2)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}
