// Client for the tutor API (backend/tutor/tutor_api.py). Turns stream through `runTurn` in tutor/chat.ts.
import { request, roadmapPath } from "./api";
import type { Interrupt, Today } from "./tutor/chat";

export type Mode = "tutor" | "qa";
export type TurnAction = "start" | "continue" | "quiz" | "recap" | "plan" | "research";

export interface SessionSummary {
  id: string;
  title: string | null;
  started_at: string | null;
  ended_at: string | null;
  summary: string | null;
  status: string;
}

export interface SessionDetail {
  id: string;
  roadmap: string;
  mode: Mode;
  status: string;
  started_at: string | null;
  ended_at: string | null;
  messages: unknown;
  summary: string;
  today: Today | null;
  pending: Interrupt | null;
}

export interface SkillMapSkill {
  id: string;
  title: string;
  stage_id: string;
  objectives: string[];
  prerequisites: string[];
  mastery: number;
  weak_spots: string[];
  locked: boolean;
  next_review_at: string | null;
  state: "locked" | "mastered" | "learning" | "not_started";
  resources: { title: string; url: string | null; kind: string; status: string }[];
}
export interface SkillMap {
  stages: { id: string; title: string }[];
  skills: SkillMapSkill[];
}

export interface StudySettings {
  weekly_hours: number | null;
  effective_weekly_hours: number;
  session_minutes: number;
  study_days: number[];
  deadline: string | null;
}
export type StudySettingsInput = Partial<Omit<StudySettings, "effective_weekly_hours">>;
export interface PlanView {
  plan: unknown;
  today: Today | null;
}

export const turnPath = (sessionId: string) => `/api/sessions/${encodeURIComponent(sessionId)}/turn`;

export function listSessions(slug: string): Promise<SessionSummary[]> {
  return request<SessionSummary[]>(roadmapPath(slug, "/sessions"));
}

export function createSession(slug: string, mode: Mode): Promise<{ id: string; mode: Mode; status: string; roadmap: string }> {
  return request(roadmapPath(slug, "/sessions"), { method: "POST", body: JSON.stringify({ mode }) });
}

export function getSession(id: string): Promise<SessionDetail> {
  return request<SessionDetail>(`/api/sessions/${encodeURIComponent(id)}`);
}

export function endSession(id: string): Promise<{ id: string; status: string }> {
  return request(`/api/sessions/${encodeURIComponent(id)}/end`, { method: "POST" });
}

export function getSkillMap(slug: string): Promise<SkillMap> {
  return request<SkillMap>(roadmapPath(slug, "/skills"));
}

export function getStudySettings(slug: string): Promise<StudySettings> {
  return request<StudySettings>(roadmapPath(slug, "/study-settings"));
}

export function putStudySettings(slug: string, settings: StudySettingsInput): Promise<StudySettings> {
  return request<StudySettings>(roadmapPath(slug, "/study-settings"), { method: "PUT", body: JSON.stringify(settings) });
}

export function getPlan(slug: string): Promise<PlanView> {
  return request<PlanView>(roadmapPath(slug, "/plan"));
}
