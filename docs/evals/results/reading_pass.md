# 阅读遍消融

- 生成日期：2026-11-12
- 复现命令：`translator-reading-bench --source d52ffadf-ed81-43d1-8801-c63bb3078ff7_Cyclonopedia.md --glossary CPglossary.json --translation-tokens 817372`
- 章节（阅读单位）：39；模型调用 39 次
- 提示 token：≈175049（按字符粗估；服务端回报 无）
- 输出 token：≈13650（按每章 350 粗估）
- 合计：≈188699（约为纯翻译 817372 token 的 23%）
- 累计状态最大 935 字符（上限见 multi_agent.reading_pass.max_state_chars）
- 画像：0 行章节概览、0 条术语提示，提示文本 9 字符 ≈ 9 token，随每块发送（242 块）；上限见 multi_agent.reading_pass.profile_max_chars

> 未调用模型：笔记为空（成本下界），只测了成本。召回要加 `--model` 用真实模型跑。
