import { useEffect, useMemo, useState } from "react";
import { type ApiError, isAuthError } from "./api";
import { ExternalLink } from "./ExternalLink";
import { COL_GAP, layout, NODE_H, NODE_W } from "./skillLayout";
import { getSkillMap, type SkillMap as Data, type SkillMapSkill } from "./tutorApi";

const STATES: { id: SkillMapSkill["state"]; label: string }[] = [
  { id: "mastered", label: "Mastered" },
  { id: "learning", label: "Learning" },
  { id: "not_started", label: "Not started" },
  { id: "locked", label: "Locked" },
];
const stateLabel = (s: SkillMapSkill["state"]) => STATES.find((x) => x.id === s)?.label ?? s;
const PAD = 4;

function reviewText(iso: string | null): string {
  if (!iso) return "Not scheduled";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString();
}

function Details({ skill, titles }: { skill: SkillMapSkill; titles: Map<string, string> }) {
  return (
    <aside className="card stack skill-panel" aria-label={`Skill: ${skill.title}`}>
      <div className="report-head">
        <h3>{skill.title}</h3>
        <span className={`state-chip state-${skill.state}`}>{stateLabel(skill.state)}</span>
      </div>
      <p>
        <strong>Mastery:</strong> {Math.round(skill.mastery * 100)}%
      </p>
      <p className="small">
        <strong>Next review:</strong> {reviewText(skill.next_review_at)}
      </p>
      <div className="small">
        <strong>Weak spots</strong>
        {skill.weak_spots.length > 0 ? (
          <ul>
            {skill.weak_spots.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        ) : (
          <span className="muted"> none</span>
        )}
      </div>
      <div className="small">
        <strong>Prerequisites</strong>
        {skill.prerequisites.length > 0 ? (
          <ul>
            {skill.prerequisites.map((p) => (
              <li key={p}>{titles.get(p) ?? p}</li>
            ))}
          </ul>
        ) : (
          <span className="muted"> none</span>
        )}
      </div>
      <div className="small">
        <strong>Resources</strong>
        {skill.resources.length > 0 ? (
          <ul>
            {skill.resources.map((r, i) => (
              <li key={i}>
                <ExternalLink url={r.url} label={r.title} /> <span className="muted">({r.kind})</span>
              </li>
            ))}
          </ul>
        ) : (
          <span className="muted"> none</span>
        )}
      </div>
    </aside>
  );
}

/** The roadmap as a graph: a column per stage, nodes coloured by state. Click a node or a list item for details. */
export function SkillMap({ slug, onUnauthorized }: { slug: string; onUnauthorized: (err: ApiError) => void }) {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    getSkillMap(slug)
      .then((d) => live && setData(d))
      .catch((err: unknown) => {
        if (!live) return;
        if (isAuthError(err)) onUnauthorized(err);
        else setError(err instanceof Error ? err.message : "Could not load the skill map.");
      });
    return () => {
      live = false;
    };
  }, [slug, onUnauthorized]);

  const geometry = useMemo(() => (data ? layout(data.skills, data.stages) : null), [data]);
  if (error) return <p className="error" role="alert">{error}</p>;
  if (!data || !geometry) return <p className="muted">Loading…</p>;

  const byId = new Map(data.skills.map((s) => [s.id, s]));
  const titles = new Map(data.skills.map((s) => [s.id, s.title]));
  const pos = new Map(geometry.nodes.map((n) => [n.id, n]));
  const selected = picked ? byId.get(picked) : undefined;
  const summary = `Skill map: ${data.skills.length} skills in ${data.stages.length} stages. ` +
    STATES.map((s) => `${data.skills.filter((k) => k.state === s.id).length} ${s.label.toLowerCase()}`).join(", ") + ".";

  return (
    <div className="stack skillmap">
      <ul className="legend" aria-label="Legend">
        {STATES.map((s) => (
          <li key={s.id}>
            <span className={`swatch state-${s.id}`} aria-hidden="true" /> {s.label}
          </li>
        ))}
      </ul>
      <div className="skillmap-body">
        <div className="skillmap-scroll">
          <svg
            role="img"
            aria-label={summary}
            width={geometry.width + PAD * 2}
            height={geometry.height + PAD * 2 + 20}
            viewBox={`${-PAD} ${-PAD - 20} ${geometry.width + PAD * 2} ${geometry.height + PAD * 2 + 20}`}
          >
            {data.stages.map((st, i) => (
              <text key={st.id} className="stage-label" x={i * (NODE_W + COL_GAP)} y={-8}>
                {st.title.length > 24 ? `${st.title.slice(0, 23)}…` : st.title}
              </text>
            ))}
            {geometry.edges.map((e) => {
              const a = pos.get(e.from)!;
              const b = pos.get(e.to)!;
              const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2;
              const mid = (x1 + x2) / 2;
              return <path key={`${e.from}>${e.to}`} className="edge" d={`M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`} />;
            })}
            {data.skills.map((sk) => {
              const p = pos.get(sk.id);
              if (!p) return null;
              return (
                <g
                  key={sk.id}
                  className={`node state-${sk.state}${picked === sk.id ? " node-picked" : ""}`}
                  data-testid={`node-${sk.id}`}
                  data-state={sk.state}
                  transform={`translate(${p.x},${p.y})`}
                  onClick={() => setPicked(sk.id)}
                >
                  <rect width={NODE_W} height={NODE_H} rx={8} />
                  <text x={10} y={NODE_H / 2 + 4}>{sk.title.length > 22 ? `${sk.title.slice(0, 21)}…` : sk.title}</text>
                </g>
              );
            })}
          </svg>
        </div>
        {selected && <Details skill={selected} titles={titles} />}
      </div>
      <section aria-label="Skills list">
        <h3 className="list-heading">All skills</h3>
        <ul className="skill-list">
          {data.skills.map((sk) => (
            <li key={sk.id} data-state={sk.state}>
              <button type="button" className="link-button" aria-pressed={picked === sk.id} onClick={() => setPicked(sk.id)}>
                {sk.title}
              </button>{" "}
              <span className={`state-chip state-${sk.state}`}>{stateLabel(sk.state)}</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
