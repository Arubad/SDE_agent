import { useMemo, useState } from "react";
import type { Candidate, Choices, Resolution, ResourceSpec, RoadmapSpec } from "./api";
import { ExternalLink } from "./ExternalLink";
import { hostOf, safeHref } from "./format";

/** What the learner picked for one resource. */
type Choice = { mode: "candidate"; url: string } | { mode: "paste"; url: string } | { mode: "skip" };

const KIND_LABELS: Record<string, string> = {
  video: "Video",
  playlist: "Playlist",
  doc: "Docs",
  article: "Article",
  course_site: "Course",
  interactive: "Interactive",
  pdf: "PDF",
  repo: "Repository",
  other: "Other",
};

function resources(spec: RoadmapSpec): ResourceSpec[] {
  return spec.stages.flatMap((s) => s.resources);
}

/** Candidates to offer: the resolver's, else the spec's own URL. */
function candidatesFor(res: ResourceSpec, resolution: Resolution | undefined): Candidate[] {
  if (resolution && resolution.candidates.length > 0) return resolution.candidates;
  return res.url ? [{ url: res.url, title: res.title, source: "url_hint", score: 1 }] : [];
}

function isLinkedOnly(res: ResourceSpec, resolution: Resolution | undefined): boolean {
  return resolution ? resolution.status === "linked_only" : res.kind === "interactive";
}

function initialChoice(res: ResourceSpec, resolution: Resolution | undefined): Choice | undefined {
  const top = candidatesFor(res, resolution)[0];
  if (isLinkedOnly(res, resolution)) return top ? { mode: "candidate", url: top.url } : { mode: "skip" };
  if (!top) return undefined;
  // `resolved` and `has_url` are confident enough to preselect; `needs_url` waits for the learner.
  if (!resolution || resolution.status === "resolved" || resolution.status === "has_url") {
    return { mode: "candidate", url: top.url };
  }
  return undefined;
}

export function isCrawlableUrl(url: string): boolean {
  return safeHref(url.trim()) !== undefined;
}

function choiceComplete(choice: Choice | undefined): boolean {
  if (!choice) return false;
  if (choice.mode === "skip") return true;
  return isCrawlableUrl(choice.url);
}

function ResourceChoice({
  res,
  resolution,
  choice,
  onChoice,
}: {
  res: ResourceSpec;
  resolution: Resolution | undefined;
  choice: Choice | undefined;
  onChoice: (c: Choice) => void;
}) {
  const candidates = candidatesFor(res, resolution);
  const name = `choice-${res.id}`;
  const linkedOnly = isLinkedOnly(res, resolution);
  const [pasted, setPasted] = useState(choice?.mode === "paste" ? choice.url : "");
  const pasteInvalid = choice?.mode === "paste" && pasted.trim() !== "" && !isCrawlableUrl(pasted);

  return (
    <div role="group" aria-label={res.title} className="resource-choice" data-testid="resource">
      <div className="resource-head">
        <span className="kind">{KIND_LABELS[res.kind] ?? res.kind}</span>
        <strong className="resource-title">{res.title}</strong>
        {!choice && <span className="needs">Choose a source</span>}
      </div>
      {linkedOnly ? (
        <p className="linked-only">
          <span className="tag">Linked for you, not ingested</span>{" "}
          {candidates[0] && <ExternalLink url={candidates[0].url} />}
        </p>
      ) : (
        <>
          {resolution?.message && <p className="muted small">{resolution.message}</p>}
          <div className="options">
            {candidates.map((c) => (
              <label key={c.url} className="option">
                <input
                  type="radio"
                  name={name}
                  checked={choice?.mode === "candidate" && choice.url === c.url}
                  onChange={() => onChoice({ mode: "candidate", url: c.url })}
                />
                <span className="option-text">
                  <span className="option-title">{c.title}</span>
                  <span className="option-meta">
                    {hostOf(c.url) || c.url} · {c.score.toFixed(2)}
                  </span>
                </span>
              </label>
            ))}
            <label className="option">
              <input
                type="radio"
                name={name}
                checked={choice?.mode === "paste"}
                onChange={() => onChoice({ mode: "paste", url: pasted })}
              />
              <span className="option-text">Paste a URL</span>
            </label>
            {choice?.mode === "paste" && (
              <label className="field paste">
                <span>URL to ingest</span>
                <input
                  type="url"
                  inputMode="url"
                  placeholder="https://"
                  value={pasted}
                  autoFocus
                  aria-invalid={pasteInvalid}
                  onChange={(e) => {
                    setPasted(e.target.value);
                    onChoice({ mode: "paste", url: e.target.value });
                  }}
                />
                {pasteInvalid && <span className="hint-error">Use an http:// or https:// address.</span>}
              </label>
            )}
            <label className="option">
              <input
                type="radio"
                name={name}
                checked={choice?.mode === "skip"}
                onChange={() => onChoice({ mode: "skip" })}
              />
              <span className="option-text">Skip</span>
            </label>
          </div>
        </>
      )}
    </div>
  );
}

export function SpecReview({
  spec: initialSpec,
  resolution,
  busy,
  error,
  onApprove,
}: {
  spec: RoadmapSpec;
  resolution: Resolution[];
  busy: boolean;
  error: string | null;
  onApprove: (spec: RoadmapSpec, choices: Choices) => void;
}) {
  const [spec, setSpec] = useState<RoadmapSpec>(initialSpec);
  const byId = useMemo(() => new Map(resolution.map((r) => [r.resource_id, r])), [resolution]);
  const [choices, setChoices] = useState<Record<string, Choice | undefined>>(() =>
    Object.fromEntries(resources(initialSpec).map((r) => [r.id, initialChoice(r, byId.get(r.id))])),
  );

  const all = resources(spec);
  const missing = all.filter((r) => !choiceComplete(choices[r.id])).length;

  function renameStage(i: number, title: string) {
    setSpec((s) => ({ ...s, stages: s.stages.map((st, j) => (j === i ? { ...st, title } : st)) }));
  }

  function renameSkill(i: number, k: number, title: string) {
    setSpec((s) => ({
      ...s,
      stages: s.stages.map((st, j) =>
        j === i ? { ...st, skills: st.skills.map((sk, m) => (m === k ? { ...sk, title } : sk)) } : st,
      ),
    }));
  }

  function approve() {
    const out: Choices = {};
    for (const r of all) {
      const c = choices[r.id];
      out[r.id] = c && c.mode !== "skip" ? c.url.trim() : null;
    }
    onApprove(spec, out);
  }

  return (
    <section className="stack" data-testid="spec-review" aria-labelledby="review-heading">
      <h2 id="review-heading">Review the roadmap</h2>
      <p className="muted">
        Check the stages and skills, then choose a source for every resource (or skip it). Nothing is fetched
        until you approve.
      </p>
      {spec.stages.map((stage, i) => (
        <fieldset key={stage.id} className="card stage stack" data-testid="stage">
          <legend className="sr-only">Stage {i + 1}</legend>
          <div className="stage-head">
            {stage.timeframe && <span className="timeframe">{stage.timeframe}</span>}
            <input
              className="inline-title stage-title"
              aria-label="Stage title"
              value={stage.title}
              onChange={(e) => renameStage(i, e.target.value)}
            />
          </div>
          {stage.skills.length > 0 && (
            <ul className="skills">
              {stage.skills.map((skill, k) => (
                <li key={skill.id}>
                  <input
                    className="inline-title"
                    aria-label="Skill title"
                    value={skill.title}
                    onChange={(e) => renameSkill(i, k, e.target.value)}
                  />
                </li>
              ))}
            </ul>
          )}
          {stage.resources.length === 0 ? (
            <p className="muted small">No resources in this stage.</p>
          ) : (
            stage.resources.map((res) => (
              <ResourceChoice
                key={res.id}
                res={res}
                resolution={byId.get(res.id)}
                choice={choices[res.id]}
                onChoice={(c) => setChoices((prev) => ({ ...prev, [res.id]: c }))}
              />
            ))
          )}
        </fieldset>
      ))}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="actions">
        <button type="button" onClick={approve} disabled={busy || missing > 0}>
          {busy ? "Approving…" : "Approve and ingest"}
        </button>
        {missing > 0 && (
          <span className="muted small">
            {missing} resource{missing === 1 ? "" : "s"} still need{missing === 1 ? "s" : ""} a source or skip.
          </span>
        )}
      </div>
    </section>
  );
}
