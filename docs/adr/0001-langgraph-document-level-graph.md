# ADR-0001: 多 Agent 编排迁移到文档级 LangGraph StateGraph

日期: 2026-07-12
状态: 已接受；native 路径已于 2026-11-12 删除（#25）

## 背景

翻译流水线的多 Agent 协作（分析员 → 翻译员 → 质检 → 审校修复）目前由手写 asyncio 编排实现（Semaphore 并发 + Token Bucket 限流 + OutputManager 顺序写盘）。项目定位为 Agent 开发方向的作品集，需要展示图状态机编排能力；同时手写编排的条件分支（质检不过 → 修复 → 复检）正是图模型的典型场景。

## 决策

1. **图粒度：文档级大图**。分析员节点、chunk 翻译 fan-out（Send API）、质检条件边、修复循环、汇总节点全部建入一张 StateGraph，而非只把单 chunk 流程建图。
2. **引擎开关**：`multi_agent.engine: langgraph | native`，默认 `langgraph`。两条路径共用同一批组件（prompt_builder、translation_client、quality_pipeline）。native 路径保留至评测产出两引擎对比数据后删除。
3. **不使用 LangGraph checkpointer**：断点恢复由既有的 chunk 级 SQLite 翻译缓存承担，图内状态持久化与其职责重叠，徒增 IO。
4. **顺序流式写盘保留**：翻译节点完成后仍调用 OutputManager 按序落盘，图的汇总节点只做统计；保证长文档中途崩溃仍有可读半成品。
5. **进度上报沿用 callback 闭包**：节点函数捕获现有 progress_callback 调用，CLI 与 Web worker 消费端零改动；不迁移到 astream_events（避免耦合框架事件格式）。
6. **并发与限流**：限流器保留在 translation_client 内部；并发上限由 Semaphore 换为图执行的 `max_concurrency` 配置，语义等价。

## 备选方案

- **仅单 chunk 建图**：改动小、风险低，但批处理层仍是手写，作品集叙事弱。
- **永久保留双引擎**：会漂移，维护成本翻倍，仅作为过渡与评测对照。
- **checkpointer + SqliteSaver**：可演示框架原生续跑，但与业务缓存双写，放弃。

## 后果（原记录）

- 批处理编排层（batch_orchestrator）将被图取代，评测确认无回归后删除 native 路径。
- 失败隔离需在每个 Send 分支内自行捕获，维持"失败块占位符"语义。
- 评测报告需包含 native vs langgraph 的质量/延迟/token 对比，作为删除 native 的依据。

## 落地情况（2026-11-12）

- native 引擎、`multi_agent.engine` 开关和 `batch_orchestrator` 都已删除，`TranslationEngine` 不再接受 `engine` 参数。
- 删除的依据是：两引擎等价性测试长期通过（输出文件、结果列表、进度事件逐字段一致）。评测框架里原来的"引擎对照"组同时移除。
- 原先的等价性测试改成单引擎的契约测试，见 `tests/test_engine_contract.py`。
- 没有补跑 native 与 langgraph 的真实模型对比。两者共用同一批组件，等价性测试已能证明两者产出相同；花钱再跑一遍得不到新信息。
