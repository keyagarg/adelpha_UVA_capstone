import { useEffect, useMemo, useRef, useState } from "react";
import { Activity } from "lucide-react";

import { lightThemePalette, useConsoleTheme } from "../consoleTheme";
import {
  fetchEmiStatus,
  measureEmi,
  type EmiCapture,
  type EmiStatus,
  type EmiTrace,
  type PlotAxes,
} from "../mri/api";
import { ScientificPlot } from "../ScientificPlot";
import "./emiVisualizer.css";

type ModeColor = { dark: string; light: string };

/**
 * One fixed colour per receive channel, keyed by board and channel so a trace
 * keeps its colour whichever other traces are present.
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
    trace.role === "mri_reference"
      ? NEUTRAL_COLOR
      : TRACE_COLORS[`${trace.board}-${trace.channel}`] ?? NEUTRAL_COLOR;
  return light ? color.light : color.dark;
}

function formatMHz(hz: number, digits = 3): string {
  return `${(hz / 1e6).toFixed(digits)} MHz`;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** One overlaid spectrum plot: x is the offset from the centre frequency in kHz. */
function captureToAxes(capture: EmiCapture, light: boolean): PlotAxes[] {
  const center = capture.center_frequency_hz;
  const x = capture.frequency_hz.map((hz) => (hz - center) / 1e3);
  const halfSpanKHz = capture.bandwidth_hz / 2e3;
  let peak = 0;
  for (const trace of capture.traces) {
    for (const value of trace.magnitude) if (value > peak) peak = value;
  }
  return [
    {
      title: "",
      xlabel: `Offset from ${formatMHz(center)} (kHz)`,
      ylabel: `Magnitude (${capture.units})`,
      xmin: -halfSpanKHz,
      xmax: halfSpanKHz,
      ymin: 0,
      ymax: peak > 0 ? peak * 1.1 : 1,
      series: capture.traces.map((trace) => ({
        name: trace.label,
        x,
        y: trace.magnitude,
        color: traceColor(trace, light),
      })),
    },
  ];
}

/**
 * EMI stage of the Digital Twin viewport. Records a noise burst on the EMI
 * receive channels through the Imaging Console service and plots one spectrum
 * per channel.
 */
export function EmiVisualizer() {
  const [theme] = useConsoleTheme();
  const light = Boolean(lightThemePalette(theme));
  const [status, setStatus] = useState<EmiStatus | null>(null);
  const [capture, setCapture] = useState<EmiCapture | null>(null);
  const [measuring, setMeasuring] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    fetchEmiStatus()
      .then((next) => {
        if (alive.current) setStatus(next);
      })
      .catch((err) => {
        if (alive.current) setError(`Could not reach the Imaging Console service: ${errorMessage(err)}`);
      });
    return () => {
      alive.current = false;
    };
  }, []);

  async function measureOnce() {
    setMeasuring(true);
    setError(null);
    try {
      const next = await measureEmi();
      if (alive.current) setCapture(next);
    } catch (err) {
      if (alive.current) setError(errorMessage(err));
    } finally {
      if (alive.current) setMeasuring(false);
    }
  }

  const axes = useMemo(() => (capture ? captureToAxes(capture, light) : null), [capture, light]);

  const unavailable = status !== null && !status.available;
  const notice = error ?? (unavailable ? status?.reason || "EMI measurement is unavailable." : null);
  const simulated = capture ? capture.source === "simulated" : status?.simulation ?? false;
  const showSource = capture !== null || status !== null;

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
          </h2>
          <p>
            {capture
              ? `Centre ${formatMHz(capture.center_frequency_hz)} · span ${(capture.bandwidth_hz / 1e3).toFixed(0)} kHz · ${capture.n_samples} samples · ${new Date(capture.timestamp).toLocaleTimeString()}`
              : "In-band noise on the EMI receive channels."}
          </p>
        </div>
        <button
          type="button"
          className="emi-btn emi-btn-primary"
          onClick={measureOnce}
          disabled={measuring || unavailable}
        >
          <Activity size={15} strokeWidth={1.75} aria-hidden />
          {measuring ? "Measuring…" : "Measure once"}
        </button>
      </header>

      {notice ? (
        <p className="emi-view-notice" role="alert">
          {notice}
        </p>
      ) : null}

      <div className="emi-view-plot">
        {axes ? (
          <ScientificPlot axes={axes} />
        ) : (
          <p className="emi-view-empty">
            No capture yet. Select Measure once to record a noise burst on the EMI channels.
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
            {capture.traces.map((trace) => (
              <tr key={`${trace.board}-${trace.channel}`}>
                <th scope="row">
                  <span className="emi-swatch" style={{ background: traceColor(trace, light) }} aria-hidden />
                  {trace.label}
                  {trace.role === "mri_reference" ? " (MRI reference)" : ""}
                </th>
                <td>{trace.rms.toExponential(2)}</td>
                <td>{formatMHz(trace.peak_frequency_hz, 4)}</td>
                <td>{trace.peak_magnitude.toExponential(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}
