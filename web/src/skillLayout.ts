// Pure layout for the skill map: one column per stage, one row per skill, edges from prerequisite to dependent.
export const NODE_W = 170;
export const NODE_H = 44;
export const COL_GAP = 60;
export const ROW_GAP = 20;

interface LaidSkill {
  id: string;
  stage_id: string;
  prerequisites: string[];
}

export interface Layout {
  nodes: { id: string; x: number; y: number }[];
  edges: { from: string; to: string }[];
  width: number;
  height: number;
}

export function layout(skills: LaidSkill[], stages: { id: string }[]): Layout {
  const column = new Map(stages.map((s, i) => [s.id, i]));
  const rows = stages.map(() => 0);
  const nodes: Layout["nodes"] = [];
  for (const sk of skills) {
    const col = column.get(sk.stage_id);
    if (col === undefined) continue;
    nodes.push({ id: sk.id, x: col * (NODE_W + COL_GAP), y: rows[col] * (NODE_H + ROW_GAP) });
    rows[col] += 1;
  }
  const placed = new Set(nodes.map((n) => n.id));
  const edges = skills.flatMap((sk) =>
    placed.has(sk.id) ? sk.prerequisites.filter((p) => placed.has(p)).map((p) => ({ from: p, to: sk.id })) : [],
  );
  const tallest = Math.max(0, ...rows);
  return {
    nodes,
    edges,
    width: Math.max(0, stages.length * (NODE_W + COL_GAP) - COL_GAP),
    height: Math.max(0, tallest * (NODE_H + ROW_GAP) - ROW_GAP),
  };
}
