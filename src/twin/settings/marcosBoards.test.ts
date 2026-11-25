import { expect, it } from "vitest";

import type { MarcosBoard } from "../mri/api";
import {
  addBoard,
  boardProblems,
  boardSummary,
  boardsOrDefault,
  defaultBoards,
  removeBoard,
  setBoard0Tx,
  updateBoard,
} from "./marcosBoards";

const follower = (ip: string, change: Partial<MarcosBoard> = {}): MarcosBoard => ({
  ip,
  port: 11111,
  rx0: "mri",
  rx1: "mri",
  tx0: false,
  tx1: false,
  set_cic_shift: false,
  ...change,
});

it("defaults to the original single board: RX0 for scans, TX0 for the pulse", () => {
  expect(boardsOrDefault(undefined)).toEqual(defaultBoards());
  expect(boardsOrDefault([])).toEqual(defaultBoards());
  expect(boardSummary(defaultBoards())).toBe(
    "Scans record on board 0 RX0. The RF pulse plays on board 0 TX0. The EMI tool records on nothing.",
  );
});

it("adds followers that record scans on both receivers, and never removes board 0", () => {
  const two = addBoard(defaultBoards());
  expect(two).toHaveLength(2);
  expect(two[1]).toMatchObject({ rx0: "mri", rx1: "mri", tx0: false, tx1: false });
  expect(removeBoard(two, 0)).toBe(two);
  expect(removeBoard(two, 1)).toEqual(defaultBoards());
});

it("keeps at least one of board 0's TX outputs on", () => {
  const both = setBoard0Tx(defaultBoards(), 1, true);
  expect([both[0].tx0, both[0].tx1]).toEqual([true, true]);
  const tx1Only = setBoard0Tx(both, 0, false);
  expect([tx1Only[0].tx0, tx1Only[0].tx1]).toEqual([false, true]);
  expect(setBoard0Tx(tx1Only, 1, false)).toBe(tx1Only);
});

it("summarises MRI and EMI receivers across boards", () => {
  const boards = [
    { ...defaultBoards()[0], rx1: "emi" as const },
    follower("169.254.93.61", { rx0: "emi", rx1: "emi" }),
  ];
  expect(boardSummary(boards)).toBe(
    "Scans record on board 0 RX0. The RF pulse plays on board 0 TX0. " +
      "The EMI tool records on board 0 RX1, board 1 RX0, board 1 RX1.",
  );
});

it("reports missing addresses, duplicate addresses and missing MRI receivers", () => {
  const boards = [
    { ...defaultBoards()[0], rx0: "emi" as const },
    follower(""),
    follower("169.254.44.172"),
  ];
  expect(boardProblems(boards, "169.254.44.172", 11111)).toEqual([
    "Board 0 has no MRI receiver. Sequences other than FID record on board 0 and will not run.",
    "Board 1 has no address, so it is left out.",
    "Boards 0 and 2 use the same address, 169.254.44.172:11111.",
  ]);
  const silent = updateBoard(defaultBoards(), 0, { rx0: "off" });
  expect(boardProblems(silent, "10.42.0.251", 11111)[0]).toBe(
    "No receiver is marked MRI, so scans have nothing to record.",
  );
  expect(boardProblems(defaultBoards(), "10.42.0.251", 11111)).toEqual([]);
});
