"""长期保留：只压缩有重复证据的日常记录，不用低重要性判断历史价值。"""

from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from typing import Any

from .utils.number_utils import safe_float

_LEADING_DATE = re.compile(
    r"^\s*(?:\d{4}年\d{1,2}月\d{1,2}[日号]|\d{4}-\d{1,2}-\d{1,2})"
    r"\s*[，,：:]\s*"
)
LONG_TERM_ATOM_TYPES = frozenset({"factual", "relational", "preference", "episodic"})


class RetentionSampler:
    """按相同来源和相同正文取时间样本；正文、数量和事实值不改写。"""

    def __init__(self, config: dict[str, Any], now: float):
        self.now = now
        self.recent_days = max(1, int(config.get("retention_recent_days", 180)))
        self.interval_days = max(1, int(config.get("retention_sample_days", 90)))
        self.min_occurrences = max(3, int(config.get("retention_min_occurrences", 3)))
        self.groups: dict[tuple, list[tuple[int, float]]] = defaultdict(list)
        self.protected: set[int] = set()

    def observe(self, doc_id: int, text: str, metadata: dict[str, Any]) -> None:
        # 无法证明来源或时间时保守保留；不同账号/会话/可见范围不能相互去重。
        source = metadata.get("source_session_id") or metadata.get("session_id")
        created = safe_float(metadata.get("create_time"), 0.0)
        if not source or not math.isfinite(created) or created <= 0 or not text.strip():
            self.protected.add(doc_id)
            return
        types = metadata.get("atom_types") or []
        if isinstance(types, list) and set(types) & {"factual", "relational", "preference"}:
            self.protected.add(doc_id)
            return
        # 仅删除开头的记录日期。正文内部日期、数字、名字、否定词全部参与签名。
        normalized = re.sub(r"\s+", "", _LEADING_DATE.sub("", text, count=1))
        signature = hashlib.sha256(normalized.encode("utf-8")).digest()
        scope = tuple(str(metadata.get(key) or "") for key in (
            "adapter_id", "persona_id", "source_account", "owner_account",
            "audience", "sensitivity",
        ))
        self.groups[(str(source), scope, signature)].append((doc_id, created))

    def protected_ids(self) -> set[int]:
        keep = set(self.protected)
        cutoff = self.now - self.recent_days * 86400
        for items in self.groups.values():
            # 至少在三个不同日期重复，才有足够证据认为可压缩。
            if len({int(created // 86400) for _, created in items}) < self.min_occurrences:
                keep.update(doc_id for doc_id, _ in items)
                continue
            ordered = sorted(items, key=lambda item: (item[1], item[0]))
            keep.add(ordered[0][0])
            keep.add(ordered[-1][0])
            buckets: set[int] = set()
            for doc_id, created in ordered:
                bucket = int(created // (self.interval_days * 86400))
                if bucket not in buckets or created >= cutoff:
                    keep.add(doc_id)
                buckets.add(bucket)
        return keep


def retained_atom_ttl(
    atom_type: str, ttl: float, config: dict[str, Any] | None
) -> float:
    """长期原子有有限保留期；计划/未知类型仍遵守原 TTL。"""
    if config and config.get("long_term_retention_enabled", False):
        if atom_type in LONG_TERM_ATOM_TYPES:
            return max(ttl, float(config.get("retention_atom_days", 3650)))
    return ttl
