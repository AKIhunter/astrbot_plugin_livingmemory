"""敏感字段落库：分级、泛化文本与原文加密。

原则：索引只放泛化文本；等级 >=2 的原文加密后写入 metadata，不进入向量库。
加密密钥放数据目录 ``secret.key``（0600），也可用环境变量
``LIVINGMEMORY_SECRET_KEY`` 指定；两者都没有时只保留泛化文本，不落明文。
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

# 关键词只做粗判，用于决定是否加密/泛化；精确语义仍由后续阶段（输出兜底）处理。
_LEVEL3 = re.compile(
    r"身份证|证件号|银行卡|密码|验证码|病史|诊断|肿瘤|癌|抑郁症|自杀|家暴|"
    r"家庭矛盾|遗产|欠债|网贷|手术|住院|流产"
)
_LEVEL2 = re.compile(
    r"车牌|住址|门牌|小区|户口|单位|公司名|工资|存款|余额|转账|"
    r"\d{5,}元|电话|手机号|微信号|QQ号|UID"
)
_LEVEL1 = re.compile(r"加班|出差|这周|本周|明天|后天|临时|行程|请假|约会|面试")

_MASKS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"[\u4e00-\u9fa5]{1,2}[省市]?[\u4e00-\u9fa5]{2,8}(小区|花园|公寓)"), "address", "家附近"),
    (re.compile(r"(车牌|牌照)[：: ]*[\u4e00-\u9fa5A-Za-z0-9]{5,8}"), "plate", "一辆车"),
    (re.compile(r"(身份证|证件)(号)?[：: ]*[\dXx]{6,18}"), "id", "证件信息"),
    (re.compile(r"(银行卡|卡号)[：: ]*\d{6,20}"), "account", "那张卡"),
    (re.compile(r"1[3-9]\d{9}"), "contact", "联系方式"),
    (re.compile(r"(QQ|微信)(号)?[：: ]*[\w-]{5,20}", re.IGNORECASE), "account", "那个账号"),
    (re.compile(r"\d{3,}[\d,]*\s*元"), "amount", "不少钱"),
    (re.compile(r"(糖尿病|抑郁症|肿瘤|癌症|手术|住院|化疗)"), "health", "身体不太舒服"),
    (re.compile(r"([\u4e00-\u9fa5]{1,2})的(爸爸|妈妈|老公|老婆|儿子|女儿)"), "family", "家里人"),
]


@dataclass
class PrivacyResult:
    index_text: str
    metadata: dict[str, Any]


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
        cipher_text,
    )


def classify_sensitivity(text: str) -> int:
    value = str(text or "")
    if _LEVEL3.search(value):
        return 3
    if _LEVEL2.search(value):
        return 2
    if _LEVEL1.search(value):
        return 1
    return 0


def generalize(text: str) -> tuple[str, list[str]]:
    """把命中类别替换成泛称；返回泛化文本与使用的策略名。"""
    result = str(text or "")
    policies: list[str] = []
    for pattern, policy, replacement in _MASKS:
        if pattern.search(result):
            result = pattern.sub(replacement, result)
            if policy not in policies:
                policies.append(policy)
    return result, policies


def apply_privacy(
    content: str,
    metadata: dict[str, Any],
    data_dir: str,
    *,
    audience: str = "",
) -> PrivacyResult:
    """写入前统一处理：补 sensitivity/visible_to/mask_policy/加密原文。

    已显式标记 ``sensitivity`` 的记录以调用方为准；否则用关键词粗判。
    """
    metadata = dict(metadata or {})
    raw = str(content or "")
    try:
        sensitivity = int(metadata.get("sensitivity") or 0)
    except (TypeError, ValueError):
        sensitivity = 0
    if "sensitivity" not in metadata:
        sensitivity = classify_sensitivity(raw)
    sensitivity = max(0, min(3, sensitivity))

    general, policies = generalize(raw)
    mask_policy = metadata.get("mask_policy") or ",".join(policies)
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
