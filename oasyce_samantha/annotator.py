"""Annotation Cost Tiers — three levels of observation understanding.

Level 0: Rule-based keyword/entity extraction (zero cost, synchronous)
Level 1: Batch LLM annotation (amortized, background daemon)
Level 2: Deep on-demand annotation (single LLM call, triggered in _enrich)
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from oasyce_sdk.agent.cognitive import Annotation, Observation

if TYPE_CHECKING:
    from oasyce_sdk.agent.llm import LLMProvider
    from oasyce_sdk.agent.store import ObservationStore

logger = logging.getLogger(__name__)

TOPIC_KEYWORDS: dict[str, list[str]] = {
    "travel": [
        "旅行", "旅游", "景点", "snow mountain", "雪山", "出发", "自驾",
        "飞机", "airport", "酒店", "hotel", "民宿", "hiking", "徒步",
        "beach", "海边", "trip", "vacation", "度假",
    ],
    "food": [
        "美食", "restaurant", "餐厅", "好吃", "做饭", "cooking", "recipe",
        "咖啡", "coffee", "奶茶", "火锅", "烤肉", "甜品", "dessert",
    ],
    "fitness": [
        "健身", "gym", "running", "跑步", "瑜伽", "yoga", "锻炼",
        "workout", "exercise", "游泳", "swimming",
    ],
    "work": [
        "工作", "加班", "deadline", "meeting", "会议", "项目", "project",
        "上班", "办公", "同事", "colleague",
    ],
    "mood": [
        "开心", "难过", "焦虑", "happy", "sad", "anxious", "累了",
        "tired", "relaxing", "放松", "excited", "兴奋", "郁闷",
    ],
    "social": [
        "朋友", "friend", "聚会", "party", "约饭", "见面", "hangout",
        "gathering", "生日", "birthday",
    ],
    "creative": [
        "画画", "painting", "摄影", "photography", "写作", "writing",
        "音乐", "music", "设计", "design", "手工", "craft",
    ],
    "learning": [
        "学习", "reading", "看书", "课程", "course", "考试", "exam",
        "学校", "school", "研究", "research",
    ],
    "pets": [
        "猫", "cat", "狗", "dog", "宠物", "pet", "喵", "汪",
    ],
    "nature": [
        "花", "flower", "sunset", "日落", "星空", "sky", "rain", "雨",
        "雪", "snow", "春天", "summer", "autumn", "winter",
    ],
}

_KEYWORD_INDEX: dict[str, str] | None = None


def _build_keyword_index() -> dict[str, str]:
    global _KEYWORD_INDEX
    if _KEYWORD_INDEX is not None:
        return _KEYWORD_INDEX
    index: dict[str, str] = {}
    for topic, keywords in TOPIC_KEYWORDS.items():
        for kw in keywords:
            index[kw.lower()] = topic
    _KEYWORD_INDEX = index
    return index


def _extract_entities(content: str, location: str) -> list[str]:
    entities: list[str] = []
    if location:
        entities.append(location)
    at_mentions = re.findall(r"@(\w+)", content)
    entities.extend(at_mentions[:5])
    return entities


def annotate_level0(obs: Observation) -> Annotation | None:
    """Zero-cost rule annotation — keyword matching + entity extraction.

    Returns an Annotation with source="rule" and confidence=0.6,
    or None if no signal detected.
    """
    index = _build_keyword_index()
    content_lower = obs.content.lower()

    topics: set[str] = set()
    for keyword, topic in index.items():
        if keyword in content_lower:
            topics.add(topic)

    if obs.location:
        topics.add("travel")

    if obs.media_urls:
        topics.add("visual_content")

    entities = _extract_entities(obs.content, obs.location)

    if not topics and not entities:
        return None

    return Annotation(
        target_type="observation",
        target_id=obs.source_id,
        topics=sorted(topics),
        entities=entities,
        confidence=0.6,
        source="rule",
    )


_BATCH_PROMPT_TEMPLATE = """\
Annotate these observations. For each, return JSON with: topics (list[str]), \
entities (list[str]), sentiment ("positive"/"negative"/"neutral"), summary (one sentence).

{observations}

Return a JSON array with one object per observation, in order. Keys: \
obs_id, topics, entities, sentiment, summary."""


@dataclass
class _PendingObs:
    obs_id: int
    obs: Observation
    enqueued_at: float


class BatchAnnotator:
    """Level 1 — batch LLM annotation with amortized cost.

    Accumulates observations and flushes when batch is full or oldest
    entry exceeds MAX_WAIT_SECONDS. A daemon thread checks periodically.
    """

    BATCH_SIZE = 8
    MAX_WAIT_SECONDS = 120
    CHECK_INTERVAL = 30

    def __init__(self, store: "ObservationStore", get_llm):
        self._store = store
        self._get_llm = get_llm
        self._pending: list[_PendingObs] = []
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._stopped = False
        self._start_timer()

    def _start_timer(self) -> None:
        if self._stopped:
            return
        self._timer = threading.Timer(self.CHECK_INTERVAL, self._tick)
        self._timer.daemon = True
        self._timer.start()

    def _tick(self) -> None:
        try:
            self._try_flush()
        except Exception:
            logger.debug("BatchAnnotator tick failed", exc_info=True)
        finally:
            self._start_timer()

    def enqueue(self, obs_id: int, obs: Observation) -> None:
        batch = None
        with self._lock:
            self._pending.append(_PendingObs(
                obs_id=obs_id, obs=obs, enqueued_at=time.monotonic(),
            ))
            if len(self._pending) >= self.BATCH_SIZE:
                batch = list(self._pending)
                self._pending.clear()
        if batch is not None:
            self._annotate_batch(batch)

    def _try_flush(self) -> None:
        with self._lock:
            if not self._pending:
                return
            oldest = self._pending[0].enqueued_at
            if time.monotonic() - oldest < self.MAX_WAIT_SECONDS:
                return
            batch = list(self._pending)
            self._pending.clear()
        self._annotate_batch(batch)

    def flush(self) -> None:
        with self._lock:
            if not self._pending:
                return
            batch = list(self._pending)
            self._pending.clear()
        self._annotate_batch(batch)

    def _annotate_batch(self, batch: list[_PendingObs]) -> None:
        if not batch:
            return
        try:
            llm = self._get_llm()
        except Exception:
            logger.debug("BatchAnnotator: no LLM available", exc_info=True)
            return

        obs_text = "\n---\n".join(
            f"[obs_id={p.obs_id}] {p.obs.content[:300]}"
            for p in batch
        )
        prompt = _BATCH_PROMPT_TEMPLATE.format(observations=obs_text)

        try:
            resp = llm.generate(
                [{"role": "user", "content": prompt}],
            )
            text = getattr(resp, "text", None) or str(resp)
            annotations = _parse_batch_response(text, batch)
            for ann in annotations:
                self._store.save_annotation(ann)
            logger.info("BatchAnnotator: annotated %d observations", len(annotations))
        except Exception:
            logger.warning("BatchAnnotator: batch annotation failed", exc_info=True)

    def stop(self) -> None:
        self._stopped = True
        if self._timer:
            self._timer.cancel()
            self._timer = None
        self.flush()

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)


def _parse_batch_response(
    text: str, batch: list[_PendingObs],
) -> list[Annotation]:
    text = text.strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        items = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []

    annotations: list[Annotation] = []
    for i, item in enumerate(items):
        if i >= len(batch):
            break
        p = batch[i]
        ann = Annotation(
            target_type="observation",
            target_id=p.obs_id,
            topics=item.get("topics", []),
            entities=item.get("entities", []),
            sentiment=item.get("sentiment", "neutral"),
            summary=item.get("summary", ""),
            confidence=0.75,
            source="batch_llm",
        )
        annotations.append(ann)
    return annotations


def annotate_level2(
    obs: Observation,
    obs_id: int,
    llm: "LLMProvider",
    query: str,
) -> Annotation | None:
    """Level 2 — deep on-demand annotation for query-relevant observations.

    Single LLM call per observation. Returns Annotation with source="deep_llm"
    and confidence=0.9, or None on failure.
    """
    prompt = (
        f"Analyze this observation in the context of the query.\n\n"
        f"Query: {query}\n\n"
        f"Observation: {obs.content[:500]}\n"
        f"Location: {obs.location or '(none)'}\n"
        f"Media: {'yes' if obs.media_urls else 'no'}\n\n"
        f"Return JSON: {{\"topics\": [...], \"entities\": [...], "
        f"\"sentiment\": \"positive\"/\"negative\"/\"neutral\", "
        f"\"summary\": \"one sentence\"}}"
    )
    try:
        resp = llm.generate([{"role": "user", "content": prompt}])
        text = getattr(resp, "text", None) or str(resp)
        text = text.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            return None
        data = json.loads(text[start : end + 1])
        return Annotation(
            target_type="observation",
            target_id=obs_id,
            topics=data.get("topics", []),
            entities=data.get("entities", []),
            sentiment=data.get("sentiment", "neutral"),
            summary=data.get("summary", ""),
            confidence=0.9,
            source="deep_llm",
        )
    except Exception:
        logger.debug("Level 2 annotation failed", exc_info=True)
        return None
