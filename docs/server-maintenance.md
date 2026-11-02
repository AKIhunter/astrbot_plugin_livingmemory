# 服务器维护分支与发布记录

维护仓库：https://github.com/AKIhunter/astrbot_plugin_livingmemory

分支：`server/shared-embedding-cache`

插件元数据中的仓库地址指向此 fork，默认分支设为维护分支，确保宿主常规更新
从维护源码获取。官方上游源码通过 `upstream` 单独跟踪。

## 源码来源

2026-11-02 基于官方 `master` 的 2.8.4：
`bfb8b1321daf93b628bf819c3a28328d940cb063`。
保留完整 Git 历史及 `upstream` remote，维护分支独立于官方更新。
服务器原版为 2.8.1，两处实际源码补丁见
[共享缓存说明](shared-embedding-cache.md)。

## 发布审阅

| 失效场景 | 必须保持的行为 | 证据 |
| --- | --- | --- |
| 重复或并发输入 | 相同输入只计算一次，等待者不重复计量 | `core/embedding_cache.py:get/_populate`，并发、取消、重启持久化测试通过 |
| 更换模型或账号 | 隔离缓存空间，原有索引提供商指纹不被包装改变 | `core/embedding_gateway.py:fingerprint/_wrap`，模型变更及指纹不变测试通过 |
| 上游错误或缓存损坏 | 明确返回错误；单条损坏只重算该条 | `tests/test_shared_embedding_cache.py`，HTTP、部分失败、SQLite 损坏测试通过 |
| 端口暴露或凭据泄露 | loopback 监听、独立本机令牌、数据不入 Git | `core/embedding_gateway.py:start_for/_authorized`；`.gitignore` 排除运行数据与令牌 |
| 升级误重建历史向量 | 保留原生配置、逐项核查启动维护条件 | `core/validators/index_validator.py:check_consistency`，冗余向量槽位不触发重建；部署时再次核查 |
| 发布回归 | 停机一致性备份，先恢复代码及配置 | 所有 SQLite/WAL、索引和配置备份；不得直接覆盖发布后新增记忆 |

验证结果：59 项缓存及相关专项测试、1195 项后端测试、149 项前端测试通过。
服务器隔离测试排除一项前端构建依赖检查，该项在本地完整 Node 环境单独通过。
Dashboard 构建和产物一致性、文档构建均通过。真实百炼 v4 原生参考向量与缓存
向量最大差值为 0；同一输入第二次兼容 HTTP 调用 usage 为 0。

## 发布操作约束

发布前比较服务器代码与取证快照，发现新增改动就重新适配。
使用 `git archive` 生成安装包，只包含提交中的文件。
停机后备份插件原目录、LivingMemory/BiliBot 配置、AstrBot 提供商配置及完整
LivingMemory 数据。启用缓存后，仅将 BiliBot 的向量地址和凭据改为本机接口，
保留原生提供商配置及 `text-embedding-v4` / 1024 维。

后续更新先将上游变更合入本维护分支并验证，再用具体提交部署。
直接从官方覆盖升级会移除本分支的共享缓存和服务器适配。

部署提交、校验和及验收结果在实际切换后追加。服务器地址和令牌不进入此文档。
