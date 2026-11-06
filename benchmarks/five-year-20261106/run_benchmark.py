"""真实模型、宿主 SQLite/FAISS、LivingMemory 原生检索；只写隔离目录。"""
import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import patch

import faiss
import numpy as np
from openai import AsyncOpenAI


ROOT = Path(os.environ.get("LM_BENCHMARK_DIR", Path(__file__).parent)).resolve()
PLUGIN = Path(os.environ["LM_PLUGIN_DIR"]).resolve()
assert ROOT != PLUGIN and not (ROOT/"memory.db").exists(), "请使用新的隔离目录"
package = types.ModuleType("astrbot_plugin_livingmemory")
package.__path__ = [str(PLUGIN)]
sys.modules["astrbot_plugin_livingmemory"] = package
from astrbot.core.db.vec_db.faiss_impl import FaissVecDB
from astrbot.core.provider.sources.dashscope_embedding_source import DashScopeEmbeddingProvider
import astrbot.core.provider.sources.dashscope_embedding_source as embedding_module
from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_livingmemory.core.identity_access import IdentityAccess, identity_access
from astrbot_plugin_livingmemory.storage.identity_store import IdentityStore
from astrbot_plugin_livingmemory.core.retrieval.bm25_retriever import with_cjk_bigrams
from astrbot_plugin_livingmemory.core.retrieval.graph_vector_retriever import GraphVectorRetriever
from astrbot_plugin_livingmemory.core.models.memory_atom import AtomType, compute_ttl
from astrbot_plugin_livingmemory.core.managers.atom_lifecycle_manager import AtomLifecycleManager
from astrbot_plugin_livingmemory.storage.atom_store import AtomStore
from astrbot_plugin_livingmemory.core.models.memory_atom import MemoryAtom

logging.getLogger().setLevel(logging.WARNING)
faiss.omp_set_num_threads(1)
DATA = json.loads((ROOT / "dataset.json").read_text())
START = dt.date.fromisoformat(DATA["start"])

CONF = json.loads((ROOT/"benchmark-config.json").read_text())
EMBED_CONFIG = {
    "id":"benchmark-embedding", "type":"dashscope_embedding",
    "key":[os.environ["DASHSCOPE_API_KEY"]],
    "embedding_model":"text-embedding-v4", "embedding_dimensions":1024,
}
LLM_CONFIG = {"id":"benchmark-answer", "model":os.environ["LM_ANSWER_MODEL"]}
LLM_SOURCE = {"key":os.environ["LM_ANSWER_API_KEY"],
              "api_base":os.environ["LM_ANSWER_API_BASE"]}

def stamp(day):
    return dt.datetime.combine(START + dt.timedelta(days=day), dt.time(12),
                               dt.timezone.utc).timestamp()

def progress(stage, **values):
    info = dict(stage=stage, **values)
    (ROOT / "progress.json").write_text(json.dumps(info, ensure_ascii=False))
    print(json.dumps(info, ensure_ascii=False), flush=True)

class CachedRealEmbedding:
    def __init__(self):
        self.provider = DashScopeEmbeddingProvider(EMBED_CONFIG, {})
        self.db = sqlite3.connect(ROOT / "vectors.sqlite")
        self.db.execute("CREATE TABLE IF NOT EXISTS vec (text TEXT PRIMARY KEY, value BLOB)")
        self.dim = self.provider.get_dim()
        self.lock = asyncio.Lock()
        prior = json.loads((ROOT/"prior-usage.json").read_text()) if (ROOT/"prior-usage.json").exists() else {}
        self.calls = prior.get("api_calls", 0)
        self.inputs = prior.get("completed", 0)
        self.tokens = prior.get("measured_tokens", 0)
        self.unknown_usage = 0
        self.prior_unrecorded_inputs = prior.get("unrecorded_cached_inputs", 0)
        self.usage_lock = threading.Lock()
        self.sdk_call = embedding_module.TextEmbedding.call
        def tracked(*args, **kwargs):
            resp = self.sdk_call(*args, **kwargs)
            usage = getattr(resp, "usage", None) or {}
            count = usage.get("total_tokens", usage.get("input_tokens"))
            with self.usage_lock:
                self.calls += 1
                self.inputs += len(kwargs.get("input", []))
                if isinstance(count, (int, float)):
                    self.tokens += int(count)
                else:
                    self.unknown_usage += 1
                with (ROOT/"embedding-usage.jsonl").open("a") as f:
                    f.write(json.dumps(dict(inputs=len(kwargs.get("input",[])), tokens=count,
                                            status=getattr(resp,"status_code",None)))+"\n")
            return resp
        embedding_module.TextEmbedding.call = tracked

    def get_dim(self):
        return self.dim

    async def ensure(self, texts):
        unique = list(dict.fromkeys(texts))
        missing = [text for text in unique
                   if not self.db.execute("SELECT 1 FROM vec WHERE text=?", (text,)).fetchone()]
        batches = [missing[i:i+10] for i in range(0, len(missing), 10)]
        semaphore = asyncio.Semaphore(2)
        done = 0
        async def fetch(batch):
            nonlocal done
            async with semaphore:
                vectors = await asyncio.wait_for(self.provider.get_embeddings(batch), timeout=45)
                assert len(vectors) == len(batch)
                values = []
                for text, vector in zip(batch, vectors):
                    arr = np.asarray(vector, dtype=np.float32)
                    assert arr.shape == (self.dim,) and np.isfinite(arr).all()
                    values.append((text, arr.tobytes()))
                async with self.lock:
                    self.db.executemany("INSERT OR REPLACE INTO vec VALUES (?,?)", values)
                    self.db.commit()
                    done += len(batch)
                    if done % 500 == 0 or done == len(missing):
                        progress("真实向量生成", completed=done, new_inputs=len(missing),
                                 api_calls=self.calls, measured_tokens=self.tokens)
        # 控制待运行任务数量；中途失败时缓存保留，可从已完成部分继续。
        for i in range(0, len(batches), 100):
            await asyncio.gather(*(fetch(batch) for batch in batches[i:i+100]))

    async def get_embedding(self, text):
        row = self.db.execute("SELECT value FROM vec WHERE text=?", (text,)).fetchone()
        if row is None:
            raise RuntimeError("冻结检索阶段出现未预先向量化的文本")
        return np.frombuffer(row[0], dtype=np.float32)

    async def get_embeddings_batch(self, contents, **kwargs):
        return np.stack([await self.get_embedding(text) for text in contents])

def engine_config():
    result = {}
    for section in ("recall_engine", "importance_decay", "forgetting_agent"):
        result.update(CONF.get(section, {}))
    result.update(rrf_k=CONF.get("fusion_strategy", {}).get("rrf_k", 60),
                  graph_memory_enabled=CONF["graph_memory"]["enabled"],
                  search_cache_enabled=False, retrieval_timeout_seconds=30)
    graph = CONF["graph_memory"]
    for raw, target in {
        "document_route_weight":"document_route_weight", "graph_route_weight":"graph_route_weight",
        "cross_route_bonus":"cross_route_bonus", "expansion_limit":"graph_expansion_limit",
        "expansion_hops":"graph_expansion_hops", "second_hop_weight":"graph_second_hop_weight",
        "dynamic_route_weighting":"dynamic_route_weighting", "max_topics_per_memory":"graph_max_topics",
        "max_participants_per_memory":"graph_max_participants", "max_facts_per_memory":"graph_max_facts",
        "atom_enabled":"atom_enabled",
    }.items():
        if raw in graph:
            result[target] = graph[raw]
    result["fallback_enabled"] = result.pop("fallback_to_vector", True)
    return result

def metadata(row):
    foreign = row["owner"] == "B"
    # A 的 QQ 与 bili 绑定到同一 OneID，历史分属两个用户会话。
    platform = "qq" if foreign or row["day"] % 2 == 0 else "bili"
    account = "900000002" if foreign else "900000001"
    source = f"{platform}:FriendMessage:{account}"
    return dict(session_id=f"livingmemory:user:{platform}:{account}", persona_id="benchmark",
                source_account=f"{platform}:{account}", source_session_id=source,
                owner_account=f"{platform}:900000099", adapter_id="benchmark",
                audience="self_private", sensitivity=0, status="active",
                importance=row["importance"], create_time=stamp(row["day"]),
                last_access_time=stamp(row["day"]), access_count=0,
                topics=[row["topic"]], key_facts=[row["text"]],
                canonical_summary=row["text"])

async def noop(*args, **kwargs):
    pass

async def initialize_embedding_and_store():
    provider = CachedRealEmbedding()
    records = DATA["records"]
    metas = [metadata(row) for row in records]
    db = FaissVecDB(str(ROOT/"memory.db"), str(ROOT/"memory.faiss"), provider)
    graph_db = FaissVecDB(str(ROOT/"graph.db"), str(ROOT/"graph.faiss"), provider)
    await db.initialize()
    await graph_db.initialize()
    engine = MemoryEngine(str(ROOT/"memory.db"), db, graph_vector_db=graph_db,
                          config=engine_config())
    await engine.initialize()
    await engine.atom_lifecycle_manager.stop()
    # 每题均为首次访问，防止前一题强化旧事实，污染后续题目。
    engine._update_access_times_internal = noop
    store = IdentityStore(str(ROOT/"memory.db"))
    await store.initialize()
    store.coordinate_with(engine)
    engine.identity_store = store
    for one_id in ("benchmark-human-A", "benchmark-human-B"):
        await store.register_entity(one_id=one_id, entity_type="human", operator="benchmark")
    for platform, account, one_id in (
        ("qq","900000001","benchmark-human-A"),
        ("bili","900000001","benchmark-human-A"),
        ("qq","900000002","benchmark-human-B"),
    ):
        app = await store.submit_application(
            one_id=one_id, platform=platform, account_id=account,
            operator="benchmark", idempotency_key=platform+account,
            evidence={"ref":"synthetic:benchmark-account-ownership"})
        await store.review_application(app["application_id"], expected_application=app,
                                       reviewer="benchmark", idempotency_key="r"+platform+account,
                                       reason="隔离合成账号归属已确认")
    graph_texts = []
    for index, (row, meta) in enumerate(zip(records, metas), 1):
        extracted = engine.graph_extractor.extract(index, row["text"], meta)
        text, _ = GraphVectorRetriever._aggregate_memory_entries(
            [(entry.content, entry.metadata) for entry in extracted.entries])
        graph_texts.append(text)
    all_texts = [row["text"] for row in records] + graph_texts + [
        row["question"][:2000] for row in DATA["cases"]]
    start = time.perf_counter()
    await provider.ensure(all_texts)
    embed_seconds = time.perf_counter()-start
    progress("建立原生索引", records=len(records))
    start = time.perf_counter()
    ids = await db.insert_batch([row["text"] for row in records], metas)
    rows = []
    graph_entries, representative_ids = [], []
    for i, (doc_id, row, meta) in enumerate(zip(ids, records, metas), 1):
        tokens = engine.text_processor.tokenize(row["text"], remove_stopwords=True)
        rows.append((doc_id, " ".join(with_cjk_bigrams(tokens, row["text"]))))
        entries, entry_ids = await engine.graph_memory_manager._store_graph_structure(
            doc_id, row["text"], meta)
        graph_entries.append([(entry.content, dict(entry.metadata)) for entry in entries])
        representative_ids.append(entry_ids[0])
        if i % 1000 == 0:
            progress("建立原生图谱", completed=i, total=len(records))
    await engine.db_connection.executemany(
        f"INSERT INTO {engine.bm25_retriever.fts_table}(doc_id,content) VALUES (?,?)", rows)
    await engine.db_connection.commit()
    graph_ids = await engine.graph_vector_retriever.add_memory_entries_batch(graph_entries)
    await engine.graph_store.update_entry_vector_doc_ids(dict(zip(representative_ids, graph_ids)))
    assert db.embedding_storage.index.ntotal == len(records)
    assert graph_db.embedding_storage.index.ntotal == len(records)
    index_seconds = time.perf_counter()-start
    key_ids = dict(zip([row["key"] for row in records], ids))
    (ROOT/"key_ids.json").write_text(json.dumps(key_ids))
    projection = await engine.project_identity(one_id="benchmark-human-A")
    assert projection["attributed_memory_count"] == DATA["principal_memories"]
    return provider, engine, db, store, key_ids, embed_seconds, index_seconds

def answers_at(case, horizon):
    if case.get("versions"):
        current = max((v for v in case["versions"] if v["day"] < horizon),
                      key=lambda item: item["day"])
        return [current["answer"]], [current["key"]]
    return case["answers"], case["keys"]

async def retrieve_view(engine, store, key_ids, label, days, policy, rows):
    existing = {int(row[0]) for row in await (await engine.db_connection.execute(
        "SELECT id FROM documents")).fetchall()}
    allowed = {key_ids[row["key"]] for row in DATA["records"]
               if row["owner"] == "A" and row["day"] < days and key_ids[row["key"]] in existing}
    access = IdentityAccess(frozenset(allowed), await store.get_revision(),
                            store, "benchmark-human-A")
    for i, case in enumerate(DATA["cases"], 1):
        gold, source_keys = answers_at(case, days)
        targets = {key_ids[key] for key in source_keys}
        start = time.perf_counter()
        with identity_access(access), patch("time.time", return_value=stamp(days)):
            result = await engine.search_memories(case["question"], k=5, persona_id="benchmark",
                                                 allow_legacy_migration=False)
        elapsed = (time.perf_counter()-start)*1000
        result = result[:5]
        actual_ids = [int(item.doc_id) for item in result]
        assert all(item in allowed for item in actual_ids), "跨账号或未来数据泄漏"
        relevant = sum(item in targets for item in actual_ids)
        rows.append(dict(horizon=label, days=days, policy=policy, qid=case["id"],
                         category=case["category"], question=case["question"], gold=gold,
                         aliases=case["aliases"], targets=sorted(targets),
                         available_targets=sorted(targets & existing),
                         positive=case["positive"], hit1=int(bool(targets & set(actual_ids[:1]))),
                         hit5=int(bool(targets) and targets <= set(actual_ids)),
                         relevant_count=relevant, retrieved_count=len(actual_ids),
                         latency_ms=elapsed, leak_count=0,
                         recalled=[{"id": item.doc_id, "text": item.content,
                                    "create_time": item.metadata["create_time"],
                                    "score": item.final_score,
                                    "routes": item.metadata.get("recall_routes", [])}
                                   for item in result]))
        if i % 24 == 0:
            progress("原生检索测试", view=f"{label}/{policy}", completed=i,
                     total=len(DATA["cases"]))
    return len(allowed)

def normalize(value):
    return re.sub(r"[\s，。！？、:：;；'\"“”‘’\[\]]", "", str(value)).lower()

def score_answer(row, output):
    raw = output.get("answer", "")
    actual = raw if isinstance(raw, list) else [raw]
    expected = row["gold"]
    norm = [normalize(item) for item in actual]
    correct = sorted(norm) == sorted(normalize(item) for item in expected)
    if len(expected) == 1 and len(norm) == 1:
        correct |= norm[0] in [normalize(item) for item in row["aliases"]]
        if expected == ["未知"]:
            correct |= norm[0] in ["未知", "不知道", "无法确定"]
    citations = output.get("evidence_ids", [])
    try:
        citations = {int(item) for item in citations}
    except (TypeError, ValueError):
        citations = {-1}
    recalled = {item["id"] for item in row["recalled"]}
    valid = citations <= recalled
    if row["positive"]:
        valid &= set(row["targets"]) <= citations
    else:
        valid &= not citations
    row.update(answer=raw, answer_correct=int(correct), grounding_valid=int(valid),
               grounded_correct=int(correct and valid), citation_ids=sorted(citations))

async def evaluate_answers(rows):
    key = LLM_SOURCE.get("key")
    api_key = key[0] if isinstance(key, list) else key
    client = AsyncOpenAI(api_key=api_key, base_url=LLM_SOURCE["api_base"],
                         timeout=75, max_retries=0)
    usage = {"calls":0, "prompt_tokens":0, "completion_tokens":0, "errors":0}
    semaphore = asyncio.Semaphore(2)
    selected = [row for row in rows if row["horizon"] == "5年"]
    completed = 0
    async def run(row):
        nonlocal completed
        evidence = [
            {"id": item["id"], "date": dt.datetime.fromtimestamp(
                item["create_time"], dt.timezone.utc).date().isoformat(), "text": item["text"]}
            for item in row["recalled"]]
        messages = [
            {"role":"system", "content":
             "你是记忆问答助手。只依据给定的记忆回答，不要猜测。"
             "当前模拟日期是2026年11月6日；问现在的情况时使用日期最新且明确更新的记录。"
             "若材料没有明确答案，answer必须为未知，evidence_ids为空数组。"
             "仅输出JSON对象：answer只填答案值，多个答案按问题顺序填字符串数组；"
             "evidence_ids填支持答案的记忆id数组。不要解释。"},
            {"role":"user", "content":json.dumps(
                {"question":row["question"], "memories":evidence}, ensure_ascii=False)},
        ]
        async with semaphore:
            start = time.perf_counter()
            try:
                response = await client.chat.completions.create(
                    model=LLM_CONFIG["model"], messages=messages, temperature=0,
                    max_tokens=384, response_format={"type":"json_object"},
                    extra_body=LLM_CONFIG.get("custom_extra_body") or {})
                usage["calls"] += 1
                if response.usage:
                    usage["prompt_tokens"] += response.usage.prompt_tokens
                    usage["completion_tokens"] += response.usage.completion_tokens
                text = response.choices[0].message.content or ""
                output = json.loads(text)
                score_answer(row, output)
                row["model_finish_reason"] = response.choices[0].finish_reason
            except Exception as exc:
                usage["errors"] += 1
                row.update(answer=f"调用或解析失败：{type(exc).__name__}",
                           answer_correct=0, grounding_valid=0, grounded_correct=0,
                           citation_ids=[], model_error=type(exc).__name__)
            row["answer_latency_ms"] = (time.perf_counter()-start)*1000
            completed += 1
            if completed % 12 == 0 or completed == len(selected):
                progress("真实模型回答测试", completed=completed, total=len(selected),
                         errors=usage["errors"])
                (ROOT/"answers-progress.json").write_text(json.dumps(rows, ensure_ascii=False))
    for i in range(0, len(selected), 24):
        await asyncio.gather(*(run(row) for row in selected[i:i+24]))
    await client.close()
    return usage

async def verify_decay_and_atoms(engine):
    # 用另一个隔离库调用真实每日衰减，核对批量重放的递推值。
    temp = ROOT/"decay-control.db"
    control = MemoryEngine(str(temp), None, config=engine_config())
    import aiosqlite
    control.db_connection = await aiosqlite.connect(temp)
    control.db_connection.row_factory = aiosqlite.Row
    await control.db_connection.execute(
        "CREATE TABLE documents(id INTEGER PRIMARY KEY, metadata TEXT)")
    await control.db_connection.executemany("INSERT INTO documents VALUES (?,?)", [
        (1,json.dumps(dict(importance=0.5, access_count=0, last_access_time=stamp(0)))),
        (2,json.dumps(dict(importance=1.0, access_count=0, last_access_time=stamp(0)))),
    ])
    await control.db_connection.commit()
    curve = [0.5]
    with patch("time.time", return_value=stamp(DATA["days"])):
        for day in range(1, 70):
            assert await control.apply_daily_decay(0.01, days=1) == 1
            result = await (await control.db_connection.execute(
                "SELECT json_extract(metadata,'$.importance') FROM documents WHERE id=1")).fetchone()
            curve.append(float(result[0]))
    assert curve[50] >= 0.3 and curve[51] < 0.3
    for day in range(70, DATA["days"]+1):
        curve.append(max(0.01, round(curve[-1]*(1-0.01),4)))
    await control.close()
    atom_store = AtomStore(str(ROOT/"atom-control.db"))
    await atom_store.initialize()
    examples = []
    for atom_type in AtomType:
        for importance in (0.5, 1.0):
            ttl, decay_type = compute_ttl(atom_type, importance)
            atom = MemoryAtom(parent_memory_id=len(examples)+1, atom_type=atom_type,
                              content=f"{atom_type.value}测试事实", importance=importance,
                              created_at=stamp(0), last_accessed_at=stamp(0),
                              ttl_days=ttl, expires_at=stamp(0)+ttl*86400, decay_type=decay_type)
            await atom_store.insert(atom)
            examples.append(dict(type=atom_type.value, importance=importance, ttl_days=ttl,
                                 expired_5y=atom.is_expired(stamp(DATA["days"]))))
    lifecycle = AtomLifecycleManager(atom_store, engine_config())
    with patch("time.time", return_value=stamp(DATA["days"])):
        maintenance = await lifecycle.run_maintenance()
    return curve, examples, maintenance

async def main():
    started = time.perf_counter()
    provider, engine, db, store, key_ids, embed_seconds, index_seconds = (
        await initialize_embedding_and_store())
    rows, counts = [], []
    try:
        for horizon in DATA["horizons"]:
            count = await retrieve_view(engine, store, key_ids, horizon["label"],
                                        horizon["days"], "全部保留", rows)
            counts.append(dict(horizon=horizon["label"], policy="全部保留", records=count))
        curve, atoms, atom_maintenance = await verify_decay_and_atoms(engine)
        # 正文、向量不变；逐日重放在无访问、无强化条件下的 importance。
        updates = []
        for row in DATA["records"]:
            age = DATA["days"]-row["day"]
            importance = row["importance"] if row["importance"] >= 1.0 else curve[age]
            updates.append((importance, key_ids[row["key"]]))
        await engine.db_connection.executemany(
            "UPDATE documents SET metadata=json_set(metadata,'$.importance',?) WHERE id=?",
            updates)
        await engine.db_connection.commit()
        engine._invalidate_search_cache()
        with patch("time.time", return_value=stamp(DATA["days"])):
            deleted = await engine.cleanup_old_memories()
        count = await retrieve_view(engine, store, key_ids, "5年", DATA["days"],
                                    "现有衰减清理", rows)
        counts.append(dict(horizon="5年", policy="现有衰减清理", records=count))
        progress("衰减清理结果", remaining_A=count, deleted=deleted)
        usage = await evaluate_answers(rows)
        result = dict(
            dataset_version=DATA["version"],
            dataset_sha256=hashlib.sha256((ROOT/"dataset.json").read_bytes()).hexdigest(),
            harness_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            plugin_version=(PLUGIN/"metadata.yaml").read_text().split("version:")[-1].splitlines()[0].strip(),
            faiss_version=faiss.__version__, dimension=provider.dim,
            embedding_model=EMBED_CONFIG["embedding_model"], answer_model=LLM_CONFIG["model"],
            effective_engine_config=engine_config(), counts=counts, rows=rows,
            decay_control=curve[:70], atoms=atoms, atom_maintenance=atom_maintenance,
            native_cleanup_deleted=deleted, embedding_generation_seconds=embed_seconds,
            native_index_build_seconds=index_seconds, elapsed_seconds=time.perf_counter()-started,
            embedding_usage=dict(calls=provider.calls, inputs=provider.inputs, tokens=provider.tokens,
                                 unknown_usage_calls=provider.unknown_usage,
                                 prior_unrecorded_inputs=provider.prior_unrecorded_inputs),
            answer_usage=usage, assumptions=[
                "受控中文合成语料；每一天10条已正确总结的A账号记忆，另300条B账号干扰。",
                "所有问题与答案在运行前冻结；答案模型不接触标准答案。",
                "摘要按旧版无原子记忆路径写入；真实图谱提取、BM25、FAISS、融合与最终检索均执行。",
                "1年/3年以同一完整索引上的历史允许集限制候选，未来记录不可见；不是独立小索引耗时基准。",
                "A的QQ/bili账号绑定同一OneID；检索使用真实OneID允许集机制，排除B和未来记录。",
                "检索计时排除网络向量生成；复用真实向量缓存，关闭检索结果缓存与访问强化写入。",
                "五年衰减清理模拟没有复习、自动强化和新增固定知识库；重要性0.5逐日衰减，1.0保护。",
                "最初69天衰减与原生apply_daily_decay逐日调用核对；剩余天数按相同四位舍入递推。",
                "调用当前配置deepseek-flash，使用中性结构化问答提示；不包含人设、完整对话链和自主工具补查。",
                "每题每个条件仅生成一次回答；样本共享部分事实，区间不等于独立真实生活总体的置信保证。",
                "不能外推为真实五年生产准确率；不评估原始聊天提炼质量、合并质量或未来模型变化。",
                "首次向量生成阶段因测试脚本收尾接口修正而暂停，复用已完成向量续跑；耗时不含暂停前预生成部分。",
                "向量用量含暂停前最后已记录快照及续跑逐笔用量，暂停前尾批未流水采集部分待确认；已记录Tokens是下界。",
            ])
        (ROOT/"result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        progress("完成", counts=counts, answer_usage=usage, elapsed_seconds=result["elapsed_seconds"])
    finally:
        await engine.close()
        await db.close()
        provider.db.close()
        embedding_module.TextEmbedding.call = provider.sdk_call

if __name__ == "__main__":
    asyncio.run(main())
