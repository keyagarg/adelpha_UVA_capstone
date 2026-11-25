import type { MarcosBoard, RxRole } from "../mri/api";

/** Board list editing for Settings > Imaging Console > MaRCoS boards. Pure, so it is easy to test. */

export const RX_ROLE_OPTIONS: { value: RxRole; label: string }[] = [
  { value: "mri", label: "MRI (scans)" },
  { value: "emi", label: "EMI tool" },
  { value: "off", label: "Off" },
];

/** The original console: one board, RX0 records scans, TX0 plays the RF pulse. */
export function defaultBoards(): MarcosBoard[] {
  return [{ ip: "", port: 11111, rx0: "mri", rx1: "off", tx0: true, tx1: false, set_cic_shift: false }];
}

/** A board added with "Add board": both receivers record scans, it never transmits. */
export function newFollower(): MarcosBoard {
  return { ip: "", port: 11111, rx0: "mri", rx1: "mri", tx0: false, tx1: false, set_cic_shift: false };
}

/** The saved list, or the original single board when nothing is saved yet. */
export function boardsOrDefault(boards: MarcosBoard[] | undefined): MarcosBoard[] {
  return boards && boards.length ? boards : defaultBoards();
}

export function addBoard(boards: MarcosBoard[]): MarcosBoard[] {
  return [...boardsOrDefault(boards), newFollower()];
}

/** Board 0 is the controller and always stays. */
export function removeBoard(boards: MarcosBoard[], index: number): MarcosBoard[] {
  if (index <= 0 || index >= boards.length) return boards;
  return boards.filter((_, i) => i !== index);
}

export function updateBoard(boards: MarcosBoard[], index: number, change: Partial<MarcosBoard>): MarcosBoard[] {
  return boards.map((board, i) => (i === index ? { ...board, ...change } : board));
}

/** Turn one of board 0's TX outputs on or off. The last one on stays on. */
export function setBoard0Tx(boards: MarcosBoard[], channel: 0 | 1, on: boolean): MarcosBoard[] {
  const next: MarcosBoard = channel === 0 ? { ...boards[0], tx0: on } : { ...boards[0], tx1: on };
  if (!next.tx0 && !next.tx1) return boards;
  return [next, ...boards.slice(1)];
}

function receivers(boards: MarcosBoard[], role: RxRole): string[] {
  const found: string[] = [];
  boards.forEach((board, id) => {
    if (board.rx0 === role) found.push(`board ${id} RX0`);
    if (board.rx1 === role) found.push(`board ${id} RX1`);
  });
  return found;
}

/** One sentence each for what scans and the EMI tool will record, and where the RF pulse plays. */
export function boardSummary(boards: MarcosBoard[]): string {
  const mri = receivers(boards, "mri");
  const emi = receivers(boards, "emi");
  const board0 = boards[0];
  const tx = [board0?.tx0 ? "TX0" : "", board0?.tx1 ? "TX1" : ""].filter(Boolean).join(" and ") || "TX0";
  return (
    `Scans record on ${mri.length ? mri.join(", ") : "nothing"}. ` +
    `The RF pulse plays on board 0 ${tx}. ` +
    `The EMI tool records on ${emi.length ? emi.join(", ") : "nothing"}.`
  );
}

/** Settings a scan or EMI capture would trip over, as sentences. Empty when all is well. */
export function boardProblems(boards: MarcosBoard[], scannerIp: string, scannerPort: number): string[] {
  const problems: string[] = [];
  if (!receivers(boards, "mri").length) problems.push("No receiver is marked MRI, so scans have nothing to record.");
  const board0 = boards[0];
  if (board0 && board0.rx0 !== "mri" && board0.rx1 !== "mri") {
    problems.push("Board 0 has no MRI receiver. Sequences other than FID record on board 0 and will not run.");
  }
  const seen = new Map<string, number>();
  if (scannerIp.trim()) seen.set(`${scannerIp.trim()}:${scannerPort}`, 0);
  boards.forEach((board, id) => {
    if (id === 0) return;
    const ip = board.ip.trim();
    if (!ip) {
      problems.push(`Board ${id} has no address, so it is left out.`);
      return;
    }
    const key = `${ip}:${board.port}`;
    const other = seen.get(key);
    if (other !== undefined) problems.push(`Boards ${other} and ${id} use the same address, ${key}.`);
    else seen.set(key, id);
  });
  return problems;
}
