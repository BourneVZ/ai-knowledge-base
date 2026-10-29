---
name: category-summary
description: 当用户按类别问知识库内容时触发。典型用语："Framework 类有哪些" / "RAG 项目有哪些" / "AI Agent 类有几篇" / "xx 分类下的文章"。基于本地知识库索引，不需要联网。
allowed-tools:
  - Read
---

# 分类查询

## 触发词

- "X 类有哪些 / X 类有几篇 / X 分类下的文章"
- "RAG 项目有哪些"（关键词式的类别问题）
- 按分类 / 类别 / category 统计

## 先知道 category 有哪些

索引里常见的 `category`：AI Application / AI Agent / AI Infrastructure / Research / AI Safety / Inference Engine / LLM Framework / Developer Tools（以索引实际出现的为准）。

注意：用户嘴里的"类别"不一定是 `category` 字段——"RAG"、"向量数据库"、"提示工程"这类是 tags / 关键词，不是 category。

## 做法

1. Read `knowledge/articles/index.json`
2. 判断用户问的是 category 还是关键词：
   - 能对上某个 category（容忍口语化："Framework" → LLM Framework，"应用类" → AI Application）→ 按 `category` 过滤
   - 对不上 → 退化为按 `tags` / `title` 关键词过滤，并在回复开头说明是按关键词筛的
3. 回复格式（先总数后列表，Telegram 纯文本，不用表格；创新分降序排，null 排最后）：

   <匹配维度，如"AI Agent 类"或"标签 rag">共 N 篇：

   1. <title>（创新分 4 · ⭐ 25k）
   2. ...

   超过 10 篇只列创新分最高的 10 篇，结尾注明"共 N 篇，以上为创新分最高的 10 篇，要看某篇详情说一声"。
4. 用户追问某篇详情时，再用 `file` 字段拼路径 Read `knowledge/articles/<file>`（一次 ≤ 5 篇）。

## 空结果

筛出来 0 篇就如实说没有，不要拿相近类别硬凑；可以顺带附一句索引里实际有哪些类别，方便用户换说法。

## 禁止

- 别 read 目录（EISDIR）——只 read index.json，按需再读具体文章文件
- 别用表格（Telegram 不支持）
- 别把关键词筛选的结果说成 category 筛选
