# 五年合成记忆实验与复现

所有账号、身份、聊天内容及标准答案都是测试构造，不是生产数据或真实用户。

完整数据下载：[2.10.0 Release](https://github.com/AKIhunter/astrbot_plugin_livingmemory/releases/tag/2.10.0) 中的 `livingmemory-retention-benchmark-20261106.zip`，校验值见同一Release的 `SHA256SUMS.txt`。

| 数据 | 固定值 |
| --- | --- |
| dataset.json SHA256 | fcb4df2ab4e1f44c12258e5786e4ab4e743b5a0777c109b6ad144a4303c2bca3 |
| 天数 | 1826 |
| 记录 | A18260+B300 |
| 问题 | 108：84正例、24负例 |
| Embedding | 真实text-embedding-v4，1024维，批量10 |
| 答题 | Deepseek-flash，温度0，最大384输出Tokens，冻结中性提示 |

历史严格/语义结果：旧规则44.4%/44.4%，优化84.3%/89.8%，全保留86.1%/90.7%。优化保留3580条、正例证据齐全@5为88.1%，原90%目标仍差1题。完整失败、引用、语义补分理由和使用量均公开，费用单价未知标为待确认。

历史候选包含其他在途源码，不能作为2.10.0安装包。正式候选对三组原生保留集合和324道查询重新核查，284题有序ID、正文、日期和命中一致；优化组108题全部一致。全保留13题、旧规则27题存在证据内容或排序差异，命中率不变；每题只补生成一次，回答来源和评分变化在正式候选附件中记录。

## 附件内容

冻结dataset、初轮基线原始/复核结果、优化原始/复核结果、同版本基线核查与28题补测、逐日重放、失败分析、来源哈希、正式候选核查与必要补测、统计及校验清单。向量缓存约172MiB不上传；环境字段变换记录在 `export-transformations.json`，冻结正文与标准答案不改变。

历史相关回归为371 passed/2 deselected，不能视为正式候选全量通过。安装包、源码提交、生产验收和CI在Release manifest/report中各自关联。

## 无模型复核

解压完整附件到独立目录，指定2.10.0源码检出：

```bash
export LM_PLUGIN_DIR=/path/to/astrbot_plugin_livingmemory
python verify_retention_replay.py
```

这一步逐日重放采样，不读取生产配置、不调用模型；与终点采样和冻结标准来源比较。

## 真实原生重跑

在Python3.12隔离环境安装固定AstrBot测试宿主及本插件依赖。推荐宿主源码固定到 `a6265fa6c9471d499574c119edeba54a125db314`；真实生产宿主为4.29.1，两者环境差异需单列。

将 `dataset.json`、`benchmark-config.json`、`run_retention.py`复制到一个新的隔离目录，不与插件或生产数据目录共用。设置：

```bash
export LM_PLUGIN_DIR=/path/to/astrbot_plugin_livingmemory
export DASHSCOPE_API_KEY=your_embedding_key
export LM_ANSWER_API_KEY=your_answer_key
export LM_ANSWER_API_BASE=https://your-authorized-provider.example/v1
export LM_ANSWER_MODEL=your_authorized_model_id
python run_retention.py
```

替换提供商只影响新的复现，不能冒充历史同模型结果。脚本建立真实SQLite/FAISS/BM25/图谱、生成或复用内容键向量缓存、执行原生清理，再仅依据召回证据答题；标准答案只用于调用后的评分，不进入模型输入。

`run_benchmark.py`用于重跑全保留/旧规则基线，必须在另一个新的目录中执行。新的真实运行可能产生大量Embedding/答题用量和生成波动；不使用随机向量，不对失败题反复生成以挑选成功答案。不要对生产时钟或真实历史执行实验性衰减。
