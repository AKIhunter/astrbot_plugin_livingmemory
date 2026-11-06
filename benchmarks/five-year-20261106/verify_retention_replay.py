import os
"""无模型、无数据写入：核对整个冻结语料逐日采样与终点采样一致。"""
import datetime as dt
import hashlib
import importlib
import json
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).parent
PLUGIN = Path(os.environ["LM_PLUGIN_DIR"])
OUT = ROOT
for name, path in (
    ("astrbot_plugin_livingmemory", PLUGIN),
    ("astrbot_plugin_livingmemory.core", PLUGIN / "core"),
    ("astrbot_plugin_livingmemory.core.utils", PLUGIN / "core/utils"),
):
    package = types.ModuleType(name)
    package.__path__ = [str(path)]
    sys.modules[name] = package
RetentionSampler = importlib.import_module("astrbot_plugin_livingmemory.core.retention").RetentionSampler
DATA = json.loads((ROOT / "dataset.json").read_text())
START = dt.datetime(2021, 11, 6, 12, tzinfo=dt.timezone.utc).timestamp()
CONF = dict(retention_recent_days=180, retention_sample_days=90, retention_min_occurrences=3)
curve = [0.5]
for day in range(1, DATA["days"] + 1):
    curve.append(max(0.01, round(curve[-1] * 0.99, 4)))
records = []
by_day = {}
for doc_id, row in enumerate(DATA["records"], 1):
    foreign = row["owner"] == "B"
    platform = "qq" if foreign or row["day"] % 2 == 0 else "bili"
    account = "900000002" if foreign else "900000001"
    meta = dict(session_id=f"livingmemory:user:{platform}:{account}",
                source_session_id=f"{platform}:FriendMessage:{account}",
                source_account=f"{platform}:{account}", owner_account=f"{platform}:900000099",
                adapter_id="benchmark", persona_id="benchmark", audience="self_private", sensitivity=0,
                importance=row["importance"], create_time=START + row["day"] * 86400)
    item = (doc_id, row["text"], meta, row)
    records.append(item)
    by_day.setdefault(row["day"], []).append(item)


def clear(items, day):
    sampler = RetentionSampler(CONF, START + day * 86400)
    for doc_id, text, meta, _ in items:
        sampler.observe(doc_id, text, meta)
    protected = sampler.protected_ids()
    kept = []
    for item in items:
        doc_id, _, meta, row = item
        age = day - row["day"]
        importance = meta["importance"] if meta["importance"] >= 1 else curve[age]
        if doc_id in protected or age <= 30 or importance >= 0.3:
            kept.append(item)
    return kept


start = time.perf_counter()
survivors = []
for day in range(DATA["days"]):
    survivors.extend(by_day.get(day, []))
    survivors = clear(survivors, day + 1)
single = clear(records, DATA["days"])
daily_ids = {row[0] for row in survivors}
single_ids = {row[0] for row in single}
assert daily_ids == single_ids
keys = {row[3]["key"] for row in survivors}
targets = {key for case in DATA["cases"] for key in case["keys"]}
targets.update(version["key"] for case in DATA["cases"] for version in case.get("versions", []))
assert targets <= keys
expected = dict(
    corpus_sha256=hashlib.sha256((ROOT / "dataset.json").read_bytes()).hexdigest(),
    periods=DATA["days"], daily_and_endpoint_identical=True,
    surviving_A=sum(row[3]["owner"] == "A" for row in survivors),
    surviving_B=sum(row[3]["owner"] == "B" for row in survivors),
    unique_answer_sources=len(targets), preserved_answer_sources=len(targets & keys),
    survivor_ids=sorted(single_ids),
    elapsed_seconds=time.perf_counter() - start,
    boundary="整套冻结语料逐日调用实际RetentionSampler；清理阈值与已验证衰减曲线，不重建每日FAISS")
(OUT / "逐日保留重放核查.json").write_text(json.dumps(expected, ensure_ascii=False, indent=2))
print(json.dumps({k: v for k, v in expected.items() if k != "survivor_ids"}, ensure_ascii=False))
