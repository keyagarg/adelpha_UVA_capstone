import { useCallback, useEffect, useRef, useState } from "react";
import { SatelliteDish, Play, Plus, Trash2 } from "lucide-react";

import {
  fetchAcqConfig,
  fetchConfig,
  formatDevicePingStatus,
  pingDevice,
  saveAcqConfig,
  saveConfig,
  startMarcosServer,
  type AcqConfig,
  type MarcosBoard,
  type MriConfig,
  type RxRole,
} from "../mri/api";
import { Select, SettingsRow, SettingsSection, Switch, TextInput } from "./controls";
import {
  RX_ROLE_OPTIONS,
  addBoard,
  boardProblems,
  boardSummary,
  boardsOrDefault,
  removeBoard,
  setBoard0Tx,
  updateBoard,
} from "./marcosBoards";

function NumberField({
  label,
  value,
  onChange,
  step = "any",
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  step?: string;
}) {
  return (
    <TextInput
      type="number"
      step={step}
      label={label}
      value={Number.isFinite(value) ? String(value) : ""}
      onChange={(next) => {
        const parsed = Number(next);
        if (Number.isFinite(parsed)) onChange(parsed);
      }}
    />
  );
}

export function ScannerHardwareSection({ compact = false }: { compact?: boolean }) {
  const [operator, setOperator] = useState<MriConfig | null>(null);
  const [acq, setAcq] = useState<AcqConfig | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [starting, setStarting] = useState(false);
  // Ping result per board, keyed by board id; cleared when boards are added or removed.
  const [boardPings, setBoardPings] = useState<Record<number, string>>({});
  const operatorTimer = useRef<number | null>(null);
  const acqTimer = useRef<number | null>(null);

  const load = useCallback(async () => {
    try {
      const [nextOperator, nextAcq] = await Promise.all([fetchConfig(), fetchAcqConfig()]);
      setOperator(nextOperator);
      setAcq(nextAcq);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load scanner settings");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const queueOperator = (next: MriConfig) => {
    setOperator(next);
    if (operatorTimer.current) window.clearTimeout(operatorTimer.current);
    operatorTimer.current = window.setTimeout(() => {
      setSaving(true);
      void saveConfig(next)
        .then((saved) => {
          setOperator(saved);
          setError("");
        })
        .catch((err) => setError(err instanceof Error ? err.message : "Save failed"))
        .finally(() => setSaving(false));
    }, 400);
  };

  const queueAcq = (next: AcqConfig) => {
    setAcq(next);
    if (acqTimer.current) window.clearTimeout(acqTimer.current);
    acqTimer.current = window.setTimeout(() => {
      setSaving(true);
      void saveAcqConfig(next)
        .then((saved) => {
          setAcq(saved);
          setError("");
        })
        .catch((err) => setError(err instanceof Error ? err.message : "Save failed"))
        .finally(() => setSaving(false));
    }, 400);
  };

  const runPing = async () => {
    try {
      const result = await pingDevice();
      if (!(result.reachable ?? result.ok)) {
        setError(result.detail || "Scanner unreachable");
      } else {
        setError("");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ping failed");
    }
  };

  const runStart = async () => {
    setStarting(true);
    setError("");
    try {
      const result = await startMarcosServer();
      if (!result.ok) setError(result.detail || "MaRCoS start failed");
    } catch (err) {
      setError(err instanceof Error ? err.message : "MaRCoS start failed");
    } finally {
      setStarting(false);
    }
  };

  if (!operator || !acq) {
    return (
      <SettingsSection title="Scanner">
        <p className="settings-about">{error || "Loading scanner settings…"}</p>
      </SettingsSection>
    );
  }

  const simulation = operator.hardware_simulation === "True";
  const boards = boardsOrDefault(acq.marcos_parameters.boards);
  const problems = boardProblems(boards, operator.scanner_ip, acq.marcos_parameters.port);
  const saveBoards = (next: MarcosBoard[]) =>
    queueAcq({ ...acq, marcos_parameters: { ...acq.marcos_parameters, boards: next } });
  const changeBoardCount = (next: MarcosBoard[]) => {
    setBoardPings({});
    saveBoards(next);
  };

  const pingBoard = async (index: number) => {
    const board = boards[index];
    setBoardPings((prev) => ({ ...prev, [index]: "Pinging…" }));
    try {
      const result = await pingDevice({ ip: board.ip.trim(), port: board.port });
      setBoardPings((prev) => ({ ...prev, [index]: formatDevicePingStatus(result) }));
    } catch (err) {
      setBoardPings((prev) => ({ ...prev, [index]: err instanceof Error ? err.message : "Ping failed" }));
    }
  };

  return (
    <>
      <SettingsSection title="Scanner">
        {error ? <p className="settings-about">{error}</p> : null}
        {saving ? <p className="settings-about">Saving…</p> : null}
        <SettingsRow title="Scanner IP" description="Board 0's address in MaRCoS boards." layout="stack">
          <TextInput
            label="Scanner IP"
            value={operator.scanner_ip}
            placeholder="10.42.0.251"
            onChange={(scanner_ip) => queueOperator({ ...operator, scanner_ip })}
          />
        </SettingsRow>
        <SettingsRow title="Hardware simulation">
          <Switch
            label="Hardware simulation"
            checked={simulation}
            onChange={(on) => queueOperator({ ...operator, hardware_simulation: on ? "True" : "False" })}
          />
        </SettingsRow>
        <SettingsRow title="MaRCoS ping">
          <button type="button" className="settings-btn" onClick={() => void runPing()}>
            <SatelliteDish size={14} strokeWidth={1.8} aria-hidden />
            Ping
          </button>
        </SettingsRow>
        <SettingsRow title="Start MaRCoS">
          <button type="button" className="settings-btn settings-btn-accent" onClick={() => void runStart()} disabled={starting || simulation}>
            <Play size={14} strokeWidth={1.8} aria-hidden />
            {starting ? "Starting…" : "Start MaRCoS"}
          </button>
        </SettingsRow>
      </SettingsSection>
      {!compact ? (
        <>
          <SettingsSection
            title="MaRCoS boards"
            description="Board 0 is the scanner above: it plays the RF pulse and gradients and triggers the other boards. Mark each receiver MRI (recorded by scans), EMI (recorded by the EMI tool) or Off. Only FID records on more than one board; other sequences record on board 0's MRI receiver."
            actions={
              <button type="button" className="settings-btn" onClick={() => changeBoardCount(addBoard(boards))}>
                <Plus size={14} strokeWidth={1.8} aria-hidden />
                Add board
              </button>
            }
          >
            <p className="settings-about">{boardSummary(boards)}</p>
            {problems.map((problem) => (
              <p key={problem} className="settings-about">
                {problem}
              </p>
            ))}
          </SettingsSection>
          {boards.map((board, index) => (
            <SettingsSection
              key={index}
              title={index === 0 ? "Board 0 · controller" : `Board ${index} · follower`}
              description={
                index === 0
                  ? `Scanner IP ${operator.scanner_ip || "(not set)"}, port ${acq.marcos_parameters.port}.`
                  : "Receive only. Starts when board 0's trigger arrives."
              }
              actions={
                index === 0 ? undefined : (
                  <>
                    <button
                      type="button"
                      className="settings-btn"
                      onClick={() => void pingBoard(index)}
                      disabled={!board.ip.trim()}
                    >
                      <SatelliteDish size={14} strokeWidth={1.8} aria-hidden />
                      Ping
                    </button>
                    <button type="button" className="settings-btn" onClick={() => changeBoardCount(removeBoard(boards, index))}>
                      <Trash2 size={14} strokeWidth={1.8} aria-hidden />
                      Remove
                    </button>
                  </>
                )
              }
            >
              {index > 0 && boardPings[index] ? <p className="settings-about">{boardPings[index]}</p> : null}
              {index > 0 ? (
                <>
                  <SettingsRow title="Address" layout="stack">
                    <TextInput
                      label={`Board ${index} address`}
                      value={board.ip}
                      placeholder="169.254.93.61"
                      onChange={(ip) => saveBoards(updateBoard(boards, index, { ip }))}
                    />
                  </SettingsRow>
                  <SettingsRow title="Port" description="MaRCoS server port. Default 11111.">
                    <NumberField
                      label={`Board ${index} port`}
                      value={board.port}
                      step="1"
                      onChange={(port) => saveBoards(updateBoard(boards, index, { port: Math.round(port) }))}
                    />
                  </SettingsRow>
                </>
              ) : null}
              <SettingsRow title="RX0">
                <Select
                  compact
                  label={`Board ${index} RX0`}
                  value={board.rx0}
                  options={RX_ROLE_OPTIONS}
                  onChange={(rx0) => saveBoards(updateBoard(boards, index, { rx0: rx0 as RxRole }))}
                />
              </SettingsRow>
              <SettingsRow title="RX1">
                <Select
                  compact
                  label={`Board ${index} RX1`}
                  value={board.rx1}
                  options={RX_ROLE_OPTIONS}
                  onChange={(rx1) => saveBoards(updateBoard(boards, index, { rx1: rx1 as RxRole }))}
                />
              </SettingsRow>
              {index === 0 ? (
                <>
                  <SettingsRow title="TX0" description="Plays the RF pulse. At least one TX output stays on.">
                    <Switch
                      label="Board 0 TX0"
                      checked={board.tx0}
                      disabled={board.tx0 && !board.tx1}
                      onChange={(on) => saveBoards(setBoard0Tx(boards, 0, on))}
                    />
                  </SettingsRow>
                  <SettingsRow title="TX1" description="Plays the same RF pulse on the second TX output.">
                    <Switch
                      label="Board 0 TX1"
                      checked={board.tx1}
                      disabled={board.tx1 && !board.tx0}
                      onChange={(on) => saveBoards(setBoard0Tx(boards, 1, on))}
                    />
                  </SettingsRow>
                </>
              ) : null}
              <SettingsRow
                title="CIC gain shift"
                description="Programs the receive filter's gain shift (set_cic_shift). Turn on if this board's receivers read close to zero."
              >
                <Switch
                  label={`Board ${index} CIC gain shift`}
                  checked={board.set_cic_shift}
                  onChange={(set_cic_shift) => saveBoards(updateBoard(boards, index, { set_cic_shift }))}
                />
              </SettingsRow>
            </SettingsSection>
          ))}
        </>
      ) : null}
      <SettingsSection title="RF" description="Larmor is the magnet frequency. RF max is the Hz that maps to transmit DAC ±1 (MRI4ALL default 7661.29 Hz). Lowering RF max increases TX voltage for the same pulse.">
        <SettingsRow title="Larmor frequency" description="Center frequency in MHz. MRI4ALL Z1 default 15.58 MHz (~366 mT). Use your measured Larmor.">
          <NumberField
            label="Larmor frequency (MHz)"
            value={acq.rf_parameters.larmor_frequency_MHz}
            step="0.001"
            onChange={(larmor_frequency_MHz) =>
              queueAcq({ ...acq, rf_parameters: { ...acq.rf_parameters, larmor_frequency_MHz } })
            }
          />
        </SettingsRow>
        <SettingsRow title="RF max amplitude" description="Full-scale TX calibration in Hz, not a clip. MRI4ALL default 7661.29. Do not lower this to 'limit power'.">
          <NumberField
            label="RF max amplitude (Hz)"
            value={acq.rf_parameters.rf_maximum_amplitude_Hze}
            onChange={(rf_maximum_amplitude_Hze) =>
              queueAcq({ ...acq, rf_parameters: { ...acq.rf_parameters, rf_maximum_amplitude_Hze } })
            }
          />
        </SettingsRow>
        {!compact ? (
          <SettingsRow title="π/2 fraction" description="Fraction of RF max expected to produce a 90° pulse.">
            <NumberField
              label="RF pi/2 fraction"
              value={acq.rf_parameters.rf_pi2_fraction}
              step="0.0001"
              onChange={(rf_pi2_fraction) =>
                queueAcq({ ...acq, rf_parameters: { ...acq.rf_parameters, rf_pi2_fraction } })
              }
            />
          </SettingsRow>
        ) : null}
      </SettingsSection>
      <SettingsSection title="Gradients" description="Hz/m produced at amplifier DAC ±1. MRI4ALL Z1 defaults: Gx 8e6, Gy 9e6, Gz 1e7. Lowering these increases coil current for the same sequence. Flocra refuses any pulse above these values.">
        <SettingsRow title="Gx full scale" description="X-axis DAC ±1 in Hz/m. MRI4ALL default 8000000.">
          <NumberField
            label="Gx full scale (Hz/m)"
            value={acq.gradients_parameters.gx_maximum}
            onChange={(gx_maximum) =>
              queueAcq({ ...acq, gradients_parameters: { ...acq.gradients_parameters, gx_maximum } })
            }
          />
        </SettingsRow>
        <SettingsRow title="Gy full scale" description="Y-axis DAC ±1 in Hz/m. MRI4ALL default 9000000.">
          <NumberField
            label="Gy full scale (Hz/m)"
            value={acq.gradients_parameters.gy_maximum}
            onChange={(gy_maximum) =>
              queueAcq({ ...acq, gradients_parameters: { ...acq.gradients_parameters, gy_maximum } })
            }
          />
        </SettingsRow>
        <SettingsRow title="Gz full scale" description="Z-axis DAC ±1 in Hz/m. MRI4ALL default 10000000.">
          <NumberField
            label="Gz full scale (Hz/m)"
            value={acq.gradients_parameters.gz_maximum}
            onChange={(gz_maximum) =>
              queueAcq({ ...acq, gradients_parameters: { ...acq.gradients_parameters, gz_maximum } })
            }
          />
        </SettingsRow>
      </SettingsSection>
      <SettingsSection title="Shim" description="Offsets added to normalized gradient channels. Must stay within ±1. MRI4ALL defaults are 0.">
        <SettingsRow title="Shim X">
          <NumberField
            label="Shim X"
            value={acq.shim_parameters.shim_x}
            step="0.0001"
            onChange={(shim_x) => queueAcq({ ...acq, shim_parameters: { ...acq.shim_parameters, shim_x } })}
          />
        </SettingsRow>
        <SettingsRow title="Shim Y">
          <NumberField
            label="Shim Y"
            value={acq.shim_parameters.shim_y}
            step="0.0001"
            onChange={(shim_y) => queueAcq({ ...acq, shim_parameters: { ...acq.shim_parameters, shim_y } })}
          />
        </SettingsRow>
        <SettingsRow title="Shim Z">
          <NumberField
            label="Shim Z"
            value={acq.shim_parameters.shim_z}
            step="0.0001"
            onChange={(shim_z) => queueAcq({ ...acq, shim_parameters: { ...acq.shim_parameters, shim_z } })}
          />
        </SettingsRow>
      </SettingsSection>
      <SettingsSection title="MaRCoS" description="Red Pitaya control port, FPGA clock, and gradient board.">
        <SettingsRow title="Port" description="MaRCoS server port. Default 11111.">
          <NumberField
            label="MaRCoS port"
            value={acq.marcos_parameters.port}
            step="1"
            onChange={(port) =>
              queueAcq({ ...acq, marcos_parameters: { ...acq.marcos_parameters, port: Math.round(port) } })
            }
          />
        </SettingsRow>
        <SettingsRow title="FPGA clock" description="Red Pitaya 122-16 clock in MHz. MaRCoS only supports 122.88 on rp-122.">
          <NumberField
            label="FPGA clock (MHz)"
            value={acq.marcos_parameters.fpga_clock_frequency_MHz}
            step="0.01"
            onChange={(fpga_clock_frequency_MHz) =>
              queueAcq({ ...acq, marcos_parameters: { ...acq.marcos_parameters, fpga_clock_frequency_MHz } })
            }
          />
        </SettingsRow>
        <SettingsRow title="Gradient board">
          <Select
            compact
            label="Gradient board"
            value={acq.marcos_parameters.gradient_board_type}
            onChange={(gradient_board_type) =>
              queueAcq({ ...acq, marcos_parameters: { ...acq.marcos_parameters, gradient_board_type } })
            }
            options={[
              { value: "gpa-fhdo", label: "gpa-fhdo" },
              { value: "ocra1", label: "ocra1" },
            ]}
          />
        </SettingsRow>
        <SettingsRow
          title="Initialize GPA"
          description="Send GPA-FHDO SPI setup when a scan starts. Leave off unless a gradient board is attached; those commands can close the MaRCoS connection."
        >
          <Switch
            label="Initialize GPA"
            checked={Boolean(acq.marcos_parameters.initialize_gpa)}
            onChange={(initialize_gpa) =>
              queueAcq({ ...acq, marcos_parameters: { ...acq.marcos_parameters, initialize_gpa } })
            }
          />
        </SettingsRow>
        <SettingsRow title="GPA-FHDO current per volt" description="Set by the board resistors. Default 2.5 A/V.">
          <NumberField
            label="GPA-FHDO current per volt"
            value={acq.marcos_parameters.gpa_fhdo_current_per_volt}
            step="0.01"
            onChange={(gpa_fhdo_current_per_volt) =>
              queueAcq({ ...acq, marcos_parameters: { ...acq.marcos_parameters, gpa_fhdo_current_per_volt } })
            }
          />
        </SettingsRow>
      </SettingsSection>
    </>
  );
}
