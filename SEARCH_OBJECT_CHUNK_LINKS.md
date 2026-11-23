# 知识对象到原文 Chunk 的检索关联

状态：V7 以 `KnowledgeObject/ObjectTerm/ObjectMention/ClaimEvidence` 为主模型。检索 chunk 绑定真实最深章节；Compose 按该章节全文取证，并附加祖先章节中不属于任何子章节的公共正文。

## 构建路径

1. 原文解析为 `DocumentVersion/StructuralNode/RetrievalChunk`。
2. 摄入载荷直接写入对象、词项、来源、mention、Claim 和证据。
3. 查询投影定位对象规范名或别名在关联原文 chunk 中的连续出现位置。
4. 主模型或 chunk 变化后重建投影，避免悬空映射。

## 查询短路标准

1. 对问题执行最长对象名/别名匹配。
2. 优先返回 ObjectMention 或连续词项命中的原文 chunk。
3. 问题还有对象之外的限定词时，只在 ObjectSource 对应文档内执行局部 FTS。
4. 精确对象证据已覆盖问题时跳过全项目 FTS。
5. 对象未定位、没有可靠原文关联或局部证据不足时才执行全项目 FTS。

搜索响应通过 `object-linked`、`object-source-local-fts`、`global-fts` 表示检索模式；结果命中类型使用 `exact-object`、`object-source-structured`、`object-source-exact`、`object-source-evidence`、`exact-title`、`exact-phrase` 或 `fulltext`。

## 性能原则

- 全项目 FTS 是兜底，不是必做步骤。
- 不在查询阶段扫描 Markdown、DOCX 或 PDF。
- 不为了凑满 Top K 而强制执行全项目 FTS；已有充分证据时允许返回较少但更准确的结果。
- 优先减少传给 LLM 的无关切片；SQLite FTS 的毫秒级耗时不是主要瓶颈，LLM 上下文规模和噪声才是主要成本。
