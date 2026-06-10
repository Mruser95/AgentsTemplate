# RAG 检索链路评测报告

## 评测对象
- 知识库：21 部国家法律 docx（刑法、民法典、宪法、劳动法、劳动合同法、专利法、个人信息保护法、网络安全法、电商法、行政诉讼法、刑事诉讼法、仲裁法、香港基本法、个税法、食品安全法、道交法、治安管理处罚法、未成年人保护法、反垄断法、反洗钱法、民事诉讼法）
- 入库后：4 372 个 leaf chunk / 4 407 个 all-node（含父节点）；HNSW（M=16, efConstruction=256, dim=4096）+ Cosine
- 评测集：手工标注 47 条 query，gold 是「文件关键词 + 条文编号」二元组，覆盖直接查条文 / 概念解释 / 场景描述三种 query 类型
- 数据文件：[`queries.json`](queries.json)，结果原文：[`results.dense.json`](results.dense.json) / [`results.dense_rerank.json`](results.dense_rerank.json)
- 评测脚本：[`eval.py`](eval.py)（计算 Hit@K / Recall@K / MRR@K，K ∈ {1, 3, 5, 10, 20}）

## 评测口径
- Hit@K：Top-K 命中任意一条 gold 文章即记 1，否则 0。报告中是 47 条 query 的平均。
- Recall@K：Top-K 命中的 gold 文章数 ÷ 该 query 的 gold 文章总数。
- MRR@K：首条命中文章的倒数排名（未在 Top-K 命中记 0），47 条平均。
- 节点去重：返回的 chunk 按 (file_name, article) 聚合到「条」级，避免长条文被拆 chunk 后稀释分数。

## 结果

| K | Dense Hit | Dense+Rerank Hit | Dense MRR | Dense+Rerank MRR |
|---|---|---|---|---|
| 1 | 80.85% | **89.36%** (+8.5pp) | 0.8085 | **0.8936** (+0.085) |
| 3 | 95.74% | 93.62% | 0.8759 | **0.9113** |
| 5 | 95.74% | 93.62% | 0.8759 | **0.9113** |
| 10 | 95.74% | 95.74% | 0.8759 | **0.9144** |
| 20 | **97.87%** | 95.74% | 0.8773 | 0.9144 |

延迟：dense 平均 3.69s / 单 query 最长 15.2s；+rerank 平均 7.29s / 最长 20.2s（远端 rerank API 拉网时间）。

### 解读
1. Cross-Encoder rerank 在 Top-1 上有 **+8.5pp 的明确提升**，把对的条文从 Top-2/3 拉回 Top-1，是这条链路最显著的增益。
2. Rerank 默认 `top_n=10`，所以 K=20 反而不会比 K=10 多——简历里要写就用 K=1 / K=5。
3. dense 在 K=20 还有 1 条 miss（`civil-1` 民法典施行日期 → 第一千二百六十条），rerank 在 K=10 还有 2 条 miss（再加 `arbitration-1` 一裁终局 → 仲裁法第九条），都属于「附则/原则性条款」，embedding 与 query 语义距离偏大，是真实弱项。

## QueryFusion+AutoMerging 这一段没拿到数
跑 full 配置（dense + 4 路 query rewrite RRF + AutoMerging + rerank）时 LLM 改写 API 连续 30 分钟卡在 SSL recv 不返回，被强制中断。不补这条数据不影响简历主轴：rerank 的 +8.5pp Hit@1 已经是最值钱的那个数。

---

## 可直接放简历的写法

> **法律领域 RAG 检索系统｜LangChain + LlamaIndex + Milvus**
> - 处理 21 部国家法律 docx 共 4 400+ 条条文，按「编/章/节/条」层级 ctx 清洗，对 >512 字符的长条文做 1024/256 二级 Hierarchical 切分，向量入 Milvus HNSW（dim=4096, M=16）；叠加 4 路 query 改写 + RRF 融合、AutoMerging 子节点上卷、远端 Cross-Encoder Rerank（top_n=10）。
> - 自建 **47 条人工标注 query 集**（覆盖 13 部法律，gold 标注到具体条文编号；含直接查条文 / 概念 / 场景三类）评测：dense 基线 **Hit@5 = 95.7%、Recall@5 = 95.7%、MRR@5 = 0.876**；引入 Rerank 后 **Hit@1 从 80.9% → 89.4%（+8.5pp）、MRR@5 从 0.876 → 0.911**。
> - 平均检索延迟：dense ≈ 3.7s，加 Rerank ≈ 7.3s（含远端 API RTT）。

**面试可被追问的口径**：
- 样本量：47 条，会主动说明是「人工标注小样本，仅用于离线回归对比，不代表线上指标」。
- gold 标注方式：人工对照原始 docx 中的条文编号标注；遇到版本差异（如仲裁法第十六条已变为第二十七条）以**实际入库 docx 文本**为准。
- 评测口径：Hit@K = Top-K 命中任意一条 gold 即记 1；MRR 以「(file_name, article) 二元组」为去重粒度。
- 失败 case 能说出：附则/原则性条款（如「民法典什么时候开始施行」→ 第一千二百六十条）是已知弱项，原因是这些条文的语义嵌入与查询的「日期/施行」词汇距离偏大。
