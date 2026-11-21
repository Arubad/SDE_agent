"""Loose roadmap text -> RoadmapSpec: one Haiku structured call, then a deterministic clean-up."""
from __future__ import annotations

import re

from backend.core.llm import haiku_json
from backend.tutor.roadmap.spec import ResourceSpec, RoadmapSpec

MAX_STAGES = 20
MAX_SKILLS_PER_STAGE = 40
MAX_RESOURCES = 200

PARSE_SYSTEM = """You turn a learner's pasted study roadmap into a structured plan.

The pasted text may have lost its line breaks, so headings, bullets and descriptions can run together.
Recover the stages (phases, weeks, modules), the skills each stage teaches, the resources it recommends and any
practice goals. Use only what the text says; do not add stages, skills or resources of your own.

Rules:
- Never invent URLs. Set `url` only to a URL that appears verbatim in the text. If you can only guess a link, put
  the guess in `url_hint` and leave `url` null.
- `query` is what you would type into a search engine to find that exact resource, including the author or
  channel if the text names one.
- Classify each resource's `kind` as one of video, playlist, doc, article, course_site, interactive, pdf, repo,
  other.
- Copy named sub-pages or sections a resource points to (for example "Components of a Cluster") into `focus`.
- Copy a stage's timeframe as written (for example "Week 1" or "Weeks 2–3"), or null if there is none.
- Link each resource to the skills it teaches through `skill_ids`, and list the skills a skill depends on in
  `prerequisites`.
- Leave a list empty and a nullable field null when the text says nothing about it.
"""

_URL_RE = re.compile(r"https?://\S+")
_TRAILING = ").,;]>—"


def extract_urls(text: str) -> list[str]:
    """http(s) URLs in source order (first occurrence only), with trailing punctuation trimmed."""
    urls: list[str] = []
    for match in _URL_RE.finditer(text):
        url = match.group().rstrip(_TRAILING)
        if url and url not in urls:
            urls.append(url)
    return urls


def parse_roadmap(source_text: str) -> RoadmapSpec:
    raw = haiku_json(system=PARSE_SYSTEM, prompt=f"Roadmap:\n\n{source_text}", schema=RoadmapSpec.json_schema(),
                     effort="low", max_tokens=8000, span_name="roadmap_parse")
    return normalize_spec(raw, source_text)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _unique(prefix: str, wanted: str, taken: set[str]) -> str:
    base = f"{prefix}-{_slug(wanted) or 'x'}"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    taken.add(candidate)
    return candidate


def normalize_spec(raw: dict, source_text: str) -> RoadmapSpec:
    """Validate the model's output, then make ids unique and the URLs trustworthy. Deterministic."""
    spec = RoadmapSpec.model_validate(raw)
    if len(spec.stages) > MAX_STAGES:
        raise ValueError(f"roadmap has {len(spec.stages)} stages (max {MAX_STAGES})")
    for stage in spec.stages:
        if len(stage.skills) > MAX_SKILLS_PER_STAGE:
            raise ValueError(f"stage {stage.title!r} has {len(stage.skills)} skills (max {MAX_SKILLS_PER_STAGE})")
    if sum(len(s.resources) for s in spec.stages) > MAX_RESOURCES:
        raise ValueError(f"roadmap has more than {MAX_RESOURCES} resources")

    skill_ids: set[str] = set()
    resource_ids: set[str] = set()
    skill_map: dict[str, str] = {}  # the model's skill id -> ours (first one wins on a duplicate)
    for i, stage in enumerate(spec.stages, 1):
        stage.id = f"stage-{i}"
        for skill in stage.skills:
            new = _unique("skill", skill.id or skill.title, skill_ids)
            skill_map.setdefault(skill.id, new)
            skill.id = new
    for stage in spec.stages:
        for skill in stage.skills:
            skill.prerequisites = [skill_map[p] for p in skill.prerequisites if p in skill_map and skill_map[p] != skill.id]
        for res in stage.resources:
            res.id = _unique("res", res.id or res.title, resource_ids)
            res.skill_ids = [skill_map[s] for s in res.skill_ids if s in skill_map]

    source_urls = extract_urls(source_text)
    carried: set[str] = set()
    for stage in spec.stages:
        for res in stage.resources:
            if res.url is None:
                continue
            if res.url in source_urls:
                carried.add(res.url)
            else:
                res.url_hint, res.url = res.url, None
    for url in source_urls:
        if url in carried:
            continue
        pos = source_text.find(url)
        stage = spec.stages[-1] if spec.stages else None
        best = -1
        for candidate in spec.stages:
            at = source_text.find(candidate.title)
            if best < at <= pos:
                stage, best = candidate, at
        if stage is None:
            break
        stage.resources.append(_other_resource(url, resource_ids))
    if sum(len(s.resources) for s in spec.stages) > MAX_RESOURCES:
        raise ValueError(f"roadmap has more than {MAX_RESOURCES} resources")
    return spec


def _other_resource(url: str, resource_ids: set[str]) -> ResourceSpec:
    return ResourceSpec(id=_unique("res", url.split("//", 1)[-1], resource_ids), title=url, kind="other", url=url,
                        url_hint=None, query=url, skill_ids=[])
