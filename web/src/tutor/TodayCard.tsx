import { ExternalLink } from "../ExternalLink";
import type { Today } from "./chat";

/** Today's study card: the planned session and its skills, with objectives, weak spots and resources. */
export function TodayCard({ today }: { today: Today }) {
  if (!today.session && today.skills.length === 0) return null;
  return (
    <section className="card stack today" aria-label="Today">
      <div className="report-head">
        <h3>Today</h3>
        {today.session && (
          <span className="muted small">
            Session {today.session.index} · {today.session.date} · {today.session.minutes} min
          </span>
        )}
      </div>
      {today.skills.map((sk) => (
        <div key={sk.id} className="today-skill">
          <div className="report-head">
            <strong>{sk.title}</strong>
            <span className="badge">{Math.round(sk.mastery * 100)}% mastery</span>
          </div>
          {sk.objectives.length > 0 && (
            <ul className="small">
              {sk.objectives.map((o) => (
                <li key={o}>{o}</li>
              ))}
            </ul>
          )}
          {sk.weak_spots.length > 0 && <p className="muted small">Weak spots: {sk.weak_spots.join(", ")}</p>}
          {sk.resources.length > 0 && (
            <ul className="small">
              {sk.resources.map((r, i) => (
                <li key={i}>
                  <ExternalLink url={r.url} label={r.title} /> <span className="muted">({r.kind})</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      ))}
    </section>
  );
}
