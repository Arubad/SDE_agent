import { cacheHitRate, costText, lanes, type TurnTrace } from "./chat";

function toolLanes(t: TurnTrace) {
  const tools = t.spans.filter((s) => s.kind === "tool");
  return { tools, lanes: lanes(tools) };
}

/** One row per turn, newest first, collapsed to its totals; expanded it lists the nodes and tool-call lanes. */
export function Timeline({ turns }: { turns: TurnTrace[] }) {
  if (turns.length === 0) return null;
  return (
    <section className="card stack timeline" aria-label="Agent timeline">
      <h3>Agent timeline</h3>
      {[...turns].reverse().map((t, i) => {
        const nodes = t.spans.filter((s) => s.kind === "node").map((s) => s.name.replace(/^node:/, ""));
        const { tools, lanes: rows } = toolLanes(t);
        return (
          <details key={`${t.trace_id}-${turns.length - i}`} className="turn">
            <summary>
              <span>Turn {turns.length - i}</span>
              <span className="badge">{t.total_ms} ms</span>
              <span className="badge">{t.llm_calls} LLM</span>
              <span className="badge">
                {t.input_tokens} in / {t.output_tokens} out / {t.cache_read_tokens} cached
              </span>
              <span className="badge">{costText(t.cost_usd)}</span>
              <span className="badge">cache {Math.round(cacheHitRate(t) * 100)}%</span>
            </summary>
            <div className="stack small">
              <p>
                <strong>Nodes: </strong>
                {nodes.length > 0 ? nodes.join(" → ") : <span className="muted">none</span>}
              </p>
              <div>
                <strong>Tool calls</strong>
                {rows.length === 0 && <span className="muted"> none</span>}
                {rows.map((lane, li) => (
                  <ul key={li} className="lane" aria-label={`Lane ${li + 1}`}>
                    {lane.map((si) => (
                      <li key={si} className="badge">
                        {tools[si].name.replace(/^mcp:/, "")} · {tools[si].duration_ms} ms
                        {tools[si].status && tools[si].status !== "ok" ? ` · ${tools[si].status}` : ""}
                      </li>
                    ))}
                  </ul>
                ))}
              </div>
            </div>
          </details>
        );
      })}
    </section>
  );
}
