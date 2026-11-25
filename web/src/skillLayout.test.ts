import { describe, expect, it } from "vitest";
import { layout, NODE_H, NODE_W } from "./skillLayout";

const stages = [
  { id: "s1", title: "One" },
  { id: "s2", title: "Two" },
];
const sk = (id: string, stage_id: string, prerequisites: string[] = []) => ({ id, stage_id, prerequisites });

describe("layout", () => {
  const skills = [sk("a", "s1"), sk("b", "s1", ["a"]), sk("c", "s2", ["b", "ghost"]), sk("d", "s2")];
  const out = layout(skills, stages);
  const at = (id: string) => out.nodes.find((n) => n.id === id)!;

  it("turns each stage into a column and keeps spec order down the rows", () => {
    expect(at("a").x).toBe(at("b").x);
    expect(at("c").x).toBe(at("d").x);
    expect(at("c").x).toBeGreaterThan(at("a").x);
    expect(at("b").y).toBeGreaterThan(at("a").y);
    expect(at("d").y).toBeGreaterThan(at("c").y);
  });

  it("draws an edge from a prerequisite to its dependent, only when both exist", () => {
    expect(out.edges).toEqual([
      { from: "a", to: "b" },
      { from: "b", to: "c" },
    ]);
  });

  it("never overlaps two nodes and sizes the canvas to hold them all", () => {
    for (const p of out.nodes) {
      expect(p.x + NODE_W).toBeLessThanOrEqual(out.width);
      expect(p.y + NODE_H).toBeLessThanOrEqual(out.height);
      for (const q of out.nodes) {
        if (p === q) continue;
        const apart = p.x + NODE_W <= q.x || q.x + NODE_W <= p.x || p.y + NODE_H <= q.y || q.y + NODE_H <= p.y;
        expect(apart).toBe(true);
      }
    }
  });

  it("copes with no skills", () => {
    expect(layout([], stages).nodes).toEqual([]);
  });
});
