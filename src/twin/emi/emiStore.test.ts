import { beforeEach, expect, it, vi } from "vitest";

import { fetchEmiStatus, measureEmi, type EmiCapture } from "../mri/api";
import { useEmiStore } from "./emiStore";

vi.mock("../mri/api", () => ({ fetchEmiStatus: vi.fn(), measureEmi: vi.fn() }));

function captureFixture(peak = 0.004, id = "emi-1"): EmiCapture {
  return {
    capture_id: id,
    timestamp: "2026-11-16T17:22:42.404Z",
    source: "simulated",
    center_frequency_hz: 15.52e6,
    sample_period_s: 1.5625e-6,
    bandwidth_hz: 640_000,
    n_samples: 2,
    averages: 1,
    units: "a.u.",
    frequency_hz: [15.2e6, 15.84e6],
    traces: [
      {
        board: 0,
        channel: "rx1",
        label: "Board 0 RX1",
        role: "emi",
        magnitude: [0.001, peak],
        time_magnitude: [0.01, 0.01],
        rms: 0.01,
        peak_frequency_hz: 15.84e6,
        peak_magnitude: peak,
      },
    ],
  };
}

const status = { available: true, busy: false, simulation: true, channels: [], reason: "" };

beforeEach(() => {
  vi.clearAllMocks();
  useEmiStore.getState().reset();
  vi.mocked(fetchEmiStatus).mockResolvedValue(status);
});

it("keeps the capture in the store after a single measurement", async () => {
  vi.mocked(measureEmi).mockResolvedValue(captureFixture());
  await useEmiStore.getState().measureOnce();
  const state = useEmiStore.getState();
  expect(state.capture?.capture_id).toBe("emi-1");
  expect(state.measuring).toBe(false);
  expect(state.peaks["0-rx1"]).toBe(0.004);
  expect(measureEmi).toHaveBeenCalledWith({ include_reference: false });
});

it("passes the MRI reference choice to the next capture", async () => {
  vi.mocked(measureEmi).mockResolvedValue(captureFixture());
  useEmiStore.getState().setIncludeReference(true);
  await useEmiStore.getState().measureOnce();
  expect(measureEmi).toHaveBeenCalledWith({ include_reference: true });
});

it("reports a failed measurement and keeps the previous capture", async () => {
  vi.mocked(measureEmi).mockResolvedValueOnce(captureFixture());
  await useEmiStore.getState().measureOnce();
  vi.mocked(measureEmi).mockRejectedValueOnce(new Error("A scan is running."));
  await useEmiStore.getState().measureOnce();
  const state = useEmiStore.getState();
  expect(state.error).toBe("A scan is running.");
  expect(state.capture?.capture_id).toBe("emi-1");
  expect(fetchEmiStatus).toHaveBeenCalled();
});

it("runs until stopped, keeps the last capture, and holds the largest peak", async () => {
  vi.mocked(measureEmi)
    .mockResolvedValueOnce(captureFixture(0.009, "emi-1"))
    .mockResolvedValue(captureFixture(0.004, "emi-2"));
  useEmiStore.getState().start();
  expect(useEmiStore.getState().running).toBe(true);
  await vi.waitFor(() => expect(useEmiStore.getState().capture?.capture_id).toBe("emi-2"), { timeout: 3000 });
  useEmiStore.getState().stop();
  const calls = vi.mocked(measureEmi).mock.calls.length;
  const state = useEmiStore.getState();
  expect(state.running).toBe(false);
  expect(state.capture?.capture_id).toBe("emi-2");
  expect(state.peaks["0-rx1"]).toBe(0.009);
  await new Promise((resolve) => setTimeout(resolve, 400));
  expect(vi.mocked(measureEmi).mock.calls.length).toBe(calls);
});

it("ends the run when a capture fails", async () => {
  vi.mocked(measureEmi).mockRejectedValue(new Error("A scan is running."));
  useEmiStore.getState().start();
  await vi.waitFor(() => expect(useEmiStore.getState().running).toBe(false));
  expect(useEmiStore.getState().error).toBe("A scan is running.");
  expect(measureEmi).toHaveBeenCalledTimes(1);
});

it("toggles a trace's visibility", () => {
  useEmiStore.getState().toggleTrace("1-rx0");
  expect(useEmiStore.getState().hidden["1-rx0"]).toBe(true);
  useEmiStore.getState().toggleTrace("1-rx0");
  expect(useEmiStore.getState().hidden["1-rx0"]).toBe(false);
});
