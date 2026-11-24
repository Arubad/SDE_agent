import { useCallback, useEffect, useMemo, useState } from "react";
import {
  type ApiError,
  approveProposal,
  approveRoadmap,
  type Choices,
  dismissProposal,
  getReport,
  getRoadmap,
  isAuthError,
  parseRoadmap,
  type ParseResult,
  type Report,
  type RoadmapDetail as Detail,
  type RoadmapSpec,
  TERMINAL_STATUSES,
} from "./api";
import { statusText } from "./format";
import { IngestProgress } from "./IngestProgress";
import { IngestReport } from "./IngestReport";
import { SkillMap } from "./SkillMap";
import { SpecReview } from "./SpecReview";
import { StudySettings } from "./StudySettings";
import { SessionList } from "./tutor/SessionList";
import { Tutor } from "./tutor/Tutor";

type Tab = "overview" | "tutor" | "skills" | "sessions";
const TUTOR_READY = ["ready", "ready_with_gaps"];

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : "Something went wrong.";
}

function withoutProposal(report: Report | null, id: string): Report | null {
  return report && { ...report, proposals: (report.proposals ?? []).filter((p) => p.id !== id) };
}

/** One roadmap through its lifecycle: draft → parse → review → ingesting (live) → report. */
export function RoadmapDetail({
  slug,
  onBack,
  onUnauthorized,
  onOpenRoadmap,
}: {
  slug: string;
  onBack: () => void;
  onUnauthorized: (err: ApiError) => void;
  onOpenRoadmap?: (slug: string) => void;
}) {
  const [tab, setTab] = useState<Tab>("overview");
  // A session picked from the Sessions tab; null lets the Tutor tab open the active one.
  const [openSession, setOpenSession] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [parsed, setParsed] = useState<ParseResult | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  // The report from before an approved proposal reopened ingestion: its agent activity stays visible.
  const [lastReport, setLastReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const fail = useCallback(
    (err: unknown) => {
      if (isAuthError(err)) onUnauthorized(err);
      else setError(messageOf(err));
    },
    [onUnauthorized],
  );

  const load = useCallback(async () => {
    setError(null);
    try {
      const d = await getRoadmap(slug);
      setDetail(d);
      if (TERMINAL_STATUSES.includes(d.status)) setReport(await getReport(slug));
    } catch (err) {
      fail(err);
    }
  }, [slug, fail]);

  useEffect(() => {
    void load();
  }, [load]);

  const skillTitles = useMemo(() => {
    const out = new Map<string, string>();
    for (const st of detail?.spec?.stages ?? []) for (const sk of st.skills) out.set(sk.id, sk.title);
    return out;
  }, [detail?.spec]);
  const skillTitle = useCallback((id: string | null) => (id && skillTitles.get(id)) || id || "", [skillTitles]);

  async function act(fn: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  }

  const parse = () =>
    act(async () => {
      const result = await parseRoadmap(slug);
      setParsed(result);
      setDetail((d) => d && { ...d, status: "parsed", spec: result.spec });
    });

  const approve = (spec: RoadmapSpec, choices: Choices) =>
    act(async () => {
      await approveRoadmap(slug, spec, choices);
      setParsed(null);
      setReport(null);
      setLastReport(null);
      setDetail((d) => d && { ...d, status: "ingesting", spec });
    });

  const settled = useCallback(
    async (status: string) => {
      setDetail((d) => d && { ...d, status });
      try {
        setReport(await getReport(slug));
        setLastReport(null);
      } catch (err) {
        fail(err);
      }
    },
    [slug, fail],
  );

  const approveSuggestion = (id: string) =>
    act(async () => {
      const result = await approveProposal(slug, id);
      const current = withoutProposal(report ?? lastReport, id);
      if (TERMINAL_STATUSES.includes(result.roadmap_status)) {
        setReport(await getReport(slug));
      } else {
        setLastReport(current);
        setReport(null);
        setDetail((d) => d && { ...d, status: result.roadmap_status });
      }
    });

  const dismissSuggestion = (id: string) =>
    act(async () => {
      await dismissProposal(slug, id);
      setReport((r) => withoutProposal(r, id));
      setLastReport((r) => withoutProposal(r, id));
    });

  let body;
  if (!detail) {
    body = error ? null : <p className="muted">Loading…</p>;
  } else if (detail.status === "draft") {
    body = (
      <section className="card stack">
        <p className="muted">
          Parse the pasted roadmap into stages, skills and resources. You review everything before anything is
          fetched.
        </p>
        <button type="button" onClick={parse} disabled={busy}>
          {busy ? "Parsing…" : "Parse roadmap"}
        </button>
      </section>
    );
  } else if (detail.status === "parsed") {
    const spec = parsed?.spec ?? detail.spec;
    body = spec ? (
      <>
        {!parsed && (
          <div className="card stack">
            <p className="muted small">Suggested sources are not loaded. Parse again to look them up.</p>
            <button type="button" className="secondary" onClick={parse} disabled={busy}>
              Parse roadmap
            </button>
          </div>
        )}
        <SpecReview
          key={parsed ? "parsed" : "stored"}
          spec={spec}
          resolution={parsed?.resolution ?? []}
          busy={busy}
          error={null}
          onApprove={approve}
        />
      </>
    ) : null;
  } else if (TERMINAL_STATUSES.includes(detail.status)) {
    body = report ? (
      <IngestReport
        report={report}
        skillTitle={skillTitle}
        busy={busy}
        error={null}
        onApproveProposal={approveSuggestion}
        onDismissProposal={dismissSuggestion}
      />
    ) : (
      !error && <p className="muted">Loading the report…</p>
    );
  } else {
    body = (
      <IngestProgress
        slug={slug}
        lastReport={lastReport}
        skillTitle={skillTitle}
        busy={busy}
        onSettled={settled}
        onUnauthorized={onUnauthorized}
        onApproveProposal={approveSuggestion}
        onDismissProposal={dismissSuggestion}
      />
    );
  }

  const tutorReady = !!detail && TUTOR_READY.includes(detail.status);
  const shown: Tab = (tab === "tutor" || tab === "skills") && !tutorReady ? "overview" : tab;
  const tabButton = (id: Tab, label: string, disabled = false, hint?: string) => (
    <button
      type="button"
      role="tab"
      id={`tab-${id}`}
      aria-selected={shown === id}
      aria-controls="tabpanel"
      className={`tab${shown === id ? " tab-active" : ""}`}
      disabled={disabled}
      title={disabled ? hint : undefined}
      onClick={() => setTab(id)}
    >
      {label}
    </button>
  );

  return (
    <div className="stack detail">
      <button type="button" className="back secondary" onClick={onBack}>
        ← Roadmaps
      </button>
      <div className="report-head">
        <h2 className="page-title">{detail?.title ?? slug}</h2>
        {detail && <span className={`status status-${detail.status}`}>{statusText(detail.status)}</span>}
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="tabs" role="tablist" aria-label="Roadmap sections">
        {tabButton("overview", "Overview")}
        {tabButton("tutor", "Tutor", !tutorReady, "The tutor is available once the roadmap is ready.")}
        {tabButton("skills", "Skill map", !tutorReady, "The skill map is available once the roadmap is ready.")}
        {tabButton("sessions", "Sessions")}
      </div>
      {!tutorReady && detail && <p className="muted small">The Tutor tab opens once the roadmap is ready.</p>}
      <div id="tabpanel" role="tabpanel" aria-labelledby={`tab-${shown}`} className="stack">
        {shown === "overview" && body}
        {shown === "skills" && <SkillMap slug={slug} onUnauthorized={onUnauthorized} />}
        {shown === "tutor" && <StudySettings slug={slug} onUnauthorized={onUnauthorized} />}
        {shown === "tutor" && (
          <Tutor
            slug={slug}
            sessionId={openSession}
            onNewSession={() => setOpenSession(null)}
            onOpenRoadmap={onOpenRoadmap}
            onUnauthorized={onUnauthorized}
          />
        )}
        {shown === "sessions" && (
          <SessionList
            slug={slug}
            onUnauthorized={onUnauthorized}
            onOpen={(id) => {
              setOpenSession(id);
              setTab("tutor");
            }}
          />
        )}
      </div>
    </div>
  );
}
