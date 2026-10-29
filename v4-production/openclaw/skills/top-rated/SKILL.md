---
name: top-rated
description: 当用户要"推荐 / 高分 / 最佳 / score 最高"的知识库文章时触发。典型用语：推荐几个 xxx / 最值得看的 / 评分最高的 / top N。基于本地知识库索引，不需要联网。
allowed-tools:
  - Read
---

# 高分推荐

## 触发词

- 推荐 / 推荐几个
- 最值得看的 / 最有价值的
- 评分最高 / score 最高 / 创新分最高
- top N / 前 N

## 评分口径（重要）

知识库没有 0-1 的 relevance_score。评分字段是 `innovation_score`（1-5 整数，可能为 null）。

**高分 = innovation_score = 5**（4 分不算高分）。

未评分（null）的 GitHub 仓库按 `stars` 折算等效分，只有折算到 5 分档的才进榜：

- ⭐ 50k 及以上 → 等效 5 分，进榜
- ⭐ 10k-50k → 等效 4 分，低于阈值，不进榜
- 其余 / 非 GitHub 来源 → 不进榜

折算条目必须在回复里注明"未评分，stars 折算"，不得冒充真实创新分。

## 做法

1. Read `knowledge/articles/index.json`
2. 候选 = 真实 `innovation_score` = 5 的条目 ＋ 未评分且 ⭐ 50k+ 的条目
3. 排序：等效分降序；同档位真实评分在前、折算在后；折算条目之间按 stars 降序；真实 5 分之间按 `collected_at` 新的在前
4. 默认取 top 5（用户给了数字就用用户的）
5. 去重：同一个 title 只保留一条
6. 回复格式（Telegram 纯文本，不用表格，先数量后列表）：

   ⭐ 高分推荐 top N：

   1. <title>（创新分 5 · <category>）
   2. <owner/repo>（未评分，⭐ 116k stars 折算 · <category>）

7. 用户追问某篇详情时，再用 `file` 字段拼路径 Read `knowledge/articles/<file>`（一次 ≤ 5 篇），给 summary / source_url。

## 空结果 / 不足 N 条

候选不足就如实给实际条数，不要拿 4 分或低星条目凑数；一条都没有就直说"目前没有高分条目"，并附索引第一条的 `collected_at` 说明知识库覆盖到哪天。

## 禁止

- 别 read 目录（EISDIR）——只需要 read index.json 一个文件，按需再读具体文章文件
- 别说"我没有 glob 工具"
- 别把 4 分条目或折算不足 5 分档的条目放进榜单
- 别给折算条目写"创新分 5"——必须标注 stars 折算
