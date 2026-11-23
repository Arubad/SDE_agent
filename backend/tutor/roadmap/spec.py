"""The validated shape of a parsed roadmap: what the parser produces and the rest of ingestion consumes."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

ResourceKind = Literal["video", "playlist", "doc", "article", "course_site", "interactive", "pdf", "repo", "other"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResourceSpec(_Strict):
    id: str
    title: str
    kind: ResourceKind
    url: str | None  # only ever a URL that appears in the source text
    url_hint: str | None  # a guessed or altered URL, never trusted as a link
    query: str  # what to type into a search engine to find this exact resource
    note: str = ""
    focus: list[str] = []  # named sub-pages or sections, e.g. "Components of a Cluster"
    skill_ids: list[str]


class SkillSpec(_Strict):
    id: str
    title: str
    objectives: list[str]
    prerequisites: list[str]  # skill ids


class StageSpec(_Strict):
    id: str
    title: str
    timeframe: str | None  # e.g. "Week 1", "Weeks 2–3"
    summary: str
    skills: list[SkillSpec]
    resources: list[ResourceSpec]
    practice_goals: list[str]


class RoadmapSpec(_Strict):
    title: str
    goals: list[str]
    weekly_hours: float | None
    stages: list[StageSpec]

    @classmethod
    def json_schema(cls) -> dict:
        """A structured-output schema: every object closed, every field required (optional ones nullable)."""
        schema = cls.model_json_schema()

        def strict(node: Any) -> None:
            if isinstance(node, dict):
                node.pop("default", None)
                if "properties" in node:
                    node["required"] = list(node["properties"])
                    node["additionalProperties"] = False
                for value in node.values():
                    strict(value)
            elif isinstance(node, list):
                for value in node:
                    strict(value)

        strict(schema)
        return schema
