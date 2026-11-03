"""敏感字段落库：分级、泛化文本与原文加密。

原则：索引只放泛化文本；等级 >=2 的原文加密后写入 metadata，不进入向量库。
加密密钥放数据目录 ``secret.key``（0600），也可用环境变量
``LIVINGMEMORY_SECRET_KEY`` 指定；两者都没有时只保留泛化文本，不落明文。

词表已外置为 JSON（阶段 2 改动）：
- 默认词表随插件发布：``privacy_lexicon.default.json``；
- 首次运行复制到数据目录 ``privacy_lexicon.json``，之后直接编辑该文件即生效；
- 人工改级/误判纠正写在数据目录 ``privacy_lexicon.overrides.json``（热加载）；
- 也可用环境变量 ``LIVINGMEMORY_PRIVACY_LEXICON`` /
  ``LIVINGMEMORY_PRIVACY_OVERRIDES`` 指向自定义路径。
上述文件变更后无需重启，下一次写入记忆时按 mtime 自动重载。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

_DEFAULT_LEXICON_FILE = "privacy_lexicon.default.json"
_RUNTIME_LEXICON_FILE = "privacy_lexicon.json"
_OVERRIDES_FILE = "privacy_lexicon.overrides.json"
_PRIVACY_PLACEHOLDER = "[隐私内容]"

# 词表缓存：key = 数据目录/环境变量路径，value = (指纹, 解析结果)。
# 指纹包含文件 mtime_ns 与 size，编辑 JSON 后下一次调用即自动重载。
_LEXICON_CACHE: dict[str, tuple[tuple[Any, ...], "_Lexicon"]] = {}
_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class _Rule:
    """一条已编译的词表规则：分组、等级、策略、泛称与匹配器。"""

    name: str
    policy: str
    sensitivity: int
    replacement: str
    keywords: tuple[re.Pattern[str], ...]
    patterns: tuple[re.Pattern[str], ...]


@dataclass(frozen=True)
class _Lexicon:
    rules: tuple[_Rule, ...]
    ignore_patterns: tuple[re.Pattern[str], ...]
    sources: tuple[str, ...]
    fingerprint: tuple[Any, ...]


@dataclass
class PrivacyResult:
    index_text: str
    metadata: dict[str, Any]


# ------------------------------------------------------------------ 密钥与加解密


def _key_path(data_dir: str) -> str:
    return os.path.join(str(data_dir or "."), "secret.key")


def _load_key(data_dir: str) -> str:
    env_key = str(os.environ.get("LIVINGMEMORY_SECRET_KEY") or "").strip()
    if env_key:
        return env_key
    path = _key_path(data_dir)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                key = handle.read().strip()
                if key:
                    return key
        key = os.urandom(32).hex()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(key)
        os.chmod(path, 0o600)
        return key
    except OSError as exc:
        logger.warning(f"[Privacy] 无法读写密钥文件，敏感原文将不落库: {exc}")
        return ""


def _openssl(args: list[str], payload: str) -> str:
    try:
        completed = subprocess.run(
            ["openssl", *args], input=payload, text=True,
            capture_output=True, check=True, timeout=10,
        )
        return completed.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(f"[Privacy] openssl 加解密失败，敏感原文将不落库: {exc}")
        return ""


def encrypt_text(text: str, data_dir: str) -> str:
    key = _load_key(data_dir)
    if not key or not text:
        return ""
    return _openssl(
        ["enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-a", "-pass", f"file:{_key_path(data_dir)}"],
        text,
    )


def decrypt_text(cipher_text: str, data_dir: str) -> str:
    key = _load_key(data_dir)
    if not key or not cipher_text:
        return ""
    return _openssl(
        ["enc", "-d", "-aes-256-cbc", "-pbkdf2", "-a", "-pass", f"file:{_key_path(data_dir)}"],
        # 阶段 2 自测修复：openssl 的 base64 解码要求输入以换行结束，
        # 否则部分长度的密文会报 “error reading input file”。
        f"{str(cipher_text).strip()}\n",
    )


# ------------------------------------------------------------------ 词表加载


def _default_lexicon_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        _DEFAULT_LEXICON_FILE,
    )


def _path_with_env(env_name: str, data_dir: str, filename: str) -> str:
    override = str(os.environ.get(env_name) or "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(str(data_dir or "."), filename)


def lexicon_paths(data_dir: str) -> dict[str, str]:
    """返回当前生效的词表路径，便于运维排查与手工改级。"""
    return {
        "default": _default_lexicon_path(),
        "runtime": _path_with_env("LIVINGMEMORY_PRIVACY_LEXICON", data_dir, _RUNTIME_LEXICON_FILE),
        "overrides": _path_with_env("LIVINGMEMORY_PRIVACY_OVERRIDES", data_dir, _OVERRIDES_FILE),
    }


def _read_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"[Privacy] 词表文件解析失败，将沿用默认值: {path} ({exc})")
        return None


def _ensure_runtime_lexicon(data_dir: str) -> None:
    """首次运行时把随插件发布的默认词表复制到数据目录，后续只改数据目录文件。"""
    path = lexicon_paths(data_dir)["runtime"]
    if os.path.exists(path):
        return
    default_path = _default_lexicon_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.exists(default_path):
            shutil.copyfile(default_path, path)
        else:
            # 默认文件缺失时不把词表写死在代码里，只落一个空骨架并告警。
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"schema_version": 1, "categories": []}, handle, ensure_ascii=False, indent=2)
            logger.warning(f"[Privacy] 未找到默认词表 {default_path}，已生成空词表 {path}")
    except OSError as exc:
        logger.warning(f"[Privacy] 无法初始化词表文件: {exc}")


def _stat_fingerprint(path: str) -> tuple[str, int, int]:
    try:
        stat = os.stat(path)
        return (os.path.abspath(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return (os.path.abspath(path), -1, -1)


def _compile_keyword(keyword: Any) -> re.Pattern[str] | None:
    text = str(keyword or "").strip()
    if not text:
        return None
    return re.compile(re.escape(text), re.IGNORECASE)


def _compile_pattern(pattern: Any) -> re.Pattern[str] | None:
    text = str(pattern or "").strip()
    if not text:
        return None
    try:
        return re.compile(text, re.IGNORECASE)
    except re.error as exc:
        logger.warning(f"[Privacy] 词表正则无效，已跳过: {text} ({exc})")
        return None


def _normalize_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, min(3, int(value)))
    except (TypeError, ValueError):
        return default


def _merge_categories(
    categories: list[dict[str, Any]],
    overrides: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """把数据目录词表与人工改级文件合并。

    overrides 支持的入口（全部热加载）：
    - category_overrides: {"love_relationship": {"sensitivity": 3, "replacement": "..."}}
    - ignore_keywords: ["单位"]   # 误判：整词从词表剔除
    - ignore_patterns: ["..."]    # 误判：命中这些正则的片段不参与分级
    - extra_keywords: [{"keyword": "新词", "category": "love_relationship"}]
    - extra_rules: [{"name": "...", "policy": "...", "sensitivity": 3,
                     "replacement": "...", "keywords": [...], "patterns": [...]}]
    """
    merged = [dict(item) for item in categories if isinstance(item, dict)]
    by_name = {str(item.get("name") or ""): item for item in merged if item.get("name")}

    category_overrides = overrides.get("category_overrides")
    if isinstance(category_overrides, dict):
        for name, patch in category_overrides.items():
            target = by_name.get(str(name))
            if not target or not isinstance(patch, dict):
                logger.warning(f"[Privacy] 人工改级指向未知分组: {name}")
                continue
            if "enabled" in patch and not patch.get("enabled"):
                merged = [item for item in merged if item is not target]
                by_name.pop(str(name), None)
                continue
            for field in ("policy", "replacement"):
                if field in patch:
                    target[field] = str(patch.get(field) or "")
            if "sensitivity" in patch:
                target["sensitivity"] = _normalize_int(patch.get("sensitivity"), 0)

    ignored = {
        str(item or "").strip().casefold()
        for item in overrides.get("ignore_keywords") or []
        if str(item or "").strip()
    }
    if ignored:
        for item in merged:
            keywords = item.get("keywords")
            if isinstance(keywords, list):
                item["keywords"] = [
                    keyword for keyword in keywords
                    if str(keyword or "").strip().casefold() not in ignored
                ]

    extra_keywords = overrides.get("extra_keywords")
    if isinstance(extra_keywords, list):
        for entry in extra_keywords:
            if not isinstance(entry, dict):
                continue
            keyword = str(entry.get("keyword") or "").strip()
            name = str(entry.get("category") or "").strip()
            if not keyword or not name:
                continue
            target = by_name.get(name)
            if target is None and entry.get("policy") and entry.get("replacement"):
                # 人工新增分组：默认等级按登记值，缺省 2。
                target = {
                    "name": name,
                    "policy": str(entry.get("policy")),
                    "sensitivity": _normalize_int(entry.get("sensitivity"), 2),
                    "replacement": str(entry.get("replacement")),
                    "keywords": [],
                    "patterns": [],
                }
                merged.append(target)
                by_name[name] = target
            if target is None:
                logger.warning(f"[Privacy] 人工新增词指向未知分组: {name}")
                continue
            keywords = target.setdefault("keywords", [])
            if isinstance(keywords, list):
                keywords.append(keyword)

    extra_rules = overrides.get("extra_rules")
    if isinstance(extra_rules, list):
        for entry in extra_rules:
            if isinstance(entry, dict) and entry.get("name"):
                merged.append(dict(entry))

    ignore_patterns = [
        str(item) for item in overrides.get("ignore_patterns") or [] if str(item or "").strip()
    ]
    return merged, sorted(ignored), ignore_patterns


def _build_lexicon(data_dir: str, fingerprint: tuple[Any, ...]) -> _Lexicon:
    paths = lexicon_paths(data_dir)
    base = _read_json(paths["runtime"]) or _read_json(paths["default"]) or {}
    overrides = _read_json(paths["overrides"]) or {}
    categories, _, ignore_pattern_texts = _merge_categories(
        list(base.get("categories") or []), overrides
    )

    rules: list[_Rule] = []
    for item in categories:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        keywords = tuple(
            compiled for compiled in (_compile_keyword(k) for k in item.get("keywords") or [])
            if compiled is not None
        )
        patterns = tuple(
            compiled for compiled in (_compile_pattern(p) for p in item.get("patterns") or [])
            if compiled is not None
        )
        if not keywords and not patterns:
            continue
        rules.append(
            _Rule(
                name=name,
                policy=str(item.get("policy") or name),
                sensitivity=_normalize_int(item.get("sensitivity"), 0),
                replacement=str(item.get("replacement") or _PRIVACY_PLACEHOLDER),
                keywords=keywords,
                patterns=patterns,
            )
        )
    ignore_patterns = tuple(
        compiled
        for compiled in (_compile_pattern(p) for p in ignore_pattern_texts)
        if compiled is not None
    )
    # 高等级优先，避免低等级规则先替换掉高等级命中的片段。
    rules.sort(key=lambda rule: (-rule.sensitivity, rule.name))
    return _Lexicon(
        rules=tuple(rules),
        ignore_patterns=ignore_patterns,
        sources=(paths["runtime"], paths["overrides"]),
        fingerprint=fingerprint,
    )


def load_lexicon(data_dir: str) -> _Lexicon:
    """加载词表；按文件 mtime/size 做热加载缓存。"""
    _ensure_runtime_lexicon(data_dir)
    paths = lexicon_paths(data_dir)
    key = f"{os.path.abspath(str(data_dir or '.'))}|{paths['runtime']}|{paths['overrides']}"
    fingerprint = (
        _stat_fingerprint(paths["runtime"]),
        _stat_fingerprint(paths["overrides"]),
        _stat_fingerprint(paths["default"]),
    )
    with _CACHE_LOCK:
        cached = _LEXICON_CACHE.get(key)
        if cached and cached[0] == fingerprint:
            return cached[1]
        lexicon = _build_lexicon(data_dir, fingerprint)
        _LEXICON_CACHE[key] = (fingerprint, lexicon)
        return lexicon


def invalidate_lexicon_cache() -> None:
    """词表被外部程序改写后主动失效；正常编辑文件无需调用（mtime 会自动识别）。"""
    with _CACHE_LOCK:
        _LEXICON_CACHE.clear()


def save_manual_overrides(data_dir: str, overrides: dict[str, Any]) -> str:
    """人工改级入口：原子写入 overrides JSON，下一次写入记忆时自动生效。"""
    configured = str(os.environ.get("LIVINGMEMORY_PRIVACY_OVERRIDES") or "").strip()
    path = os.path.abspath(configured) if configured else os.path.join(
        str(data_dir or "."), _OVERRIDES_FILE
    )
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temp = f"{path}.tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(overrides or {}, handle, ensure_ascii=False, indent=2)
    os.replace(temp, path)
    invalidate_lexicon_cache()
    return path


# ------------------------------------------------------------------ 匹配与泛化


def _ignored_spans(text: str, lexicon: _Lexicon) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pattern in lexicon.ignore_patterns:
        spans.extend(match.span() for match in pattern.finditer(text))
    return spans


def _intersects(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _collect_matches(
    text: str, lexicon: _Lexicon
) -> list[tuple[int, int, _Rule]]:
    ignored = _ignored_spans(text, lexicon)
    matches: list[tuple[int, int, _Rule]] = []
    for rule in lexicon.rules:
        for pattern in (*rule.keywords, *rule.patterns):
            for match in pattern.finditer(text):
                start, end = match.span()
                if start >= end or _intersects(start, end, ignored):
                    continue
                matches.append((start, end, rule))
    return matches


def _select_non_overlapping(
    matches: list[tuple[int, int, _Rule]]
) -> list[tuple[int, int, _Rule]]:
    """同级重叠时取更长匹配；高等级优先于低等级。"""
    ordered = sorted(
        matches, key=lambda item: (-item[2].sensitivity, -(item[1] - item[0]), item[0])
    )
    selected: list[tuple[int, int, _Rule]] = []
    occupied: list[tuple[int, int]] = []
    for start, end, rule in ordered:
        if _intersects(start, end, occupied):
            continue
        selected.append((start, end, rule))
        occupied.append((start, end))
    selected.sort(key=lambda item: item[0])
    return selected


def _classify_with(lexicon: _Lexicon, text: str) -> int:
    return max((rule.sensitivity for _, _, rule in _collect_matches(text, lexicon)), default=0)


def _generalize_with(lexicon: _Lexicon, text: str) -> tuple[str, list[str]]:
    selected = _select_non_overlapping(_collect_matches(text, lexicon))
    if not selected:
        return text, []
    pieces: list[str] = []
    policies: list[str] = []
    cursor = 0
    previous_end = -1
    previous_replacement = ""
    for start, end, rule in selected:
        # 相邻命中且泛称相同时合并，避免“一段感情一段感情”这类重复泛称。
        if start == previous_end and rule.replacement == previous_replacement:
            cursor = end
            previous_end = end
        else:
            pieces.append(text[cursor:start])
            pieces.append(rule.replacement)
            cursor = end
            previous_end = end
            previous_replacement = rule.replacement
        if rule.policy and rule.policy not in policies:
            policies.append(rule.policy)
    pieces.append(text[cursor:])
    return "".join(pieces), policies


def classify_sensitivity(text: str, data_dir: str = "") -> int:
    return _classify_with(load_lexicon(data_dir), str(text or ""))


def generalize(text: str, data_dir: str = "") -> tuple[str, list[str]]:
    """把命中类别替换成泛称；返回泛化文本与使用的策略名。"""
    return _generalize_with(load_lexicon(data_dir), str(text or ""))


def explain_privacy(text: str, data_dir: str = "") -> dict[str, Any]:
    """排查用：返回命中明细（分组/等级/策略/原文片段），不写库。"""
    raw = str(text or "")
    lexicon = load_lexicon(data_dir)
    matches = [
        {
            "category": rule.name,
            "policy": rule.policy,
            "sensitivity": rule.sensitivity,
            "text": raw[start:end],
        }
        for start, end, rule in _collect_matches(raw, lexicon)
    ]
    generalized, policies = _generalize_with(lexicon, raw)
    return {
        "sensitivity": max((item["sensitivity"] for item in matches), default=0),
        "policies": policies,
        "matches": matches,
        "generalized": generalized,
        "sources": list(lexicon.sources),
    }


def apply_privacy(
    content: str,
    metadata: dict[str, Any],
    data_dir: str,
    *,
    audience: str = "",
) -> PrivacyResult:
    """写入前统一处理：补 sensitivity/visible_to/mask_policy/加密原文。

    已显式标记 ``sensitivity`` 的记录以调用方为准；否则用词表分级。
    """
    metadata = dict(metadata or {})
    raw = str(content or "")
    lexicon = load_lexicon(data_dir)
    try:
        sensitivity = int(metadata.get("sensitivity") or 0)
    except (TypeError, ValueError):
        sensitivity = 0
    if "sensitivity" not in metadata:
        sensitivity = _classify_with(lexicon, raw)
    sensitivity = max(0, min(3, sensitivity))

    general, policies = _generalize_with(lexicon, raw)
    if sensitivity >= 2 and general == raw:
        # 词表没命中（例如调用方显式标记高等级）：索引也不能落原文。
        general = _PRIVACY_PLACEHOLDER
    mask_policy = metadata.get("mask_policy") or ",".join(policies)
    if sensitivity >= 2 and not mask_policy:
        mask_policy = "sensitive"
    visible_to = str(metadata.get("visible_to") or "")
    if not visible_to:
        if sensitivity >= 3:
            visible_to = "self_private"
        elif sensitivity == 2:
            visible_to = "origin_group" if audience.startswith("origin_group:") else "self_private"
        else:
            visible_to = audience or "internal"

    metadata["sensitivity"] = sensitivity
    metadata["visible_to"] = visible_to
    metadata["mask_policy"] = mask_policy
    metadata["content_text"] = general if sensitivity >= 2 else raw
    metadata["encrypted"] = bool(metadata.get("encrypted", False))

    index_text = raw
    if sensitivity >= 2:
        # 索引只放泛化文本；原文加密后仅留在 metadata，不进向量库。
        encrypted = str(metadata.get("secret_text_enc") or "")
        if not encrypted:
            encrypted = encrypt_text(raw, data_dir)
        if encrypted:
            metadata["secret_text_enc"] = encrypted
            metadata["encrypted"] = True
            index_text = general
        else:
            # 没有可用密钥时不落明文，也不把原文送进索引。
            metadata["secret_text_enc"] = ""
            metadata["encrypted"] = False
            index_text = general
            logger.warning("[Privacy] 敏感记忆未加密（无可用密钥），仅保留泛化文本")
    return PrivacyResult(index_text=index_text, metadata=metadata)
