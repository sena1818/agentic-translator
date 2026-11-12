# 术语候选基准：藏起术语表条目后的召回

- 生成日期：2026-11-12
- 复现命令：`translator-term-bench --source d52ffadf-ed81-43d1-8801-c63bb3078ff7_Cyclonopedia.md --glossary CPglossary.json`
- 术语表 72 条，其中在本书出现 ≥ 2 次的 53 条；2 折轮流藏起，每条都被藏过一次。全书 1038 段。

| 方法 | 每折候选数 | 找回（全部条目） | 召回（全部） | 找回（出现 ≥ 2 次） | 召回（出现 ≥ 2 次） |
| --- | --- | ---: | ---: | ---: | ---: |
| 现状：只抽大写专名（上限 120） | [120, 120] | 18 | 25% | 18 | 34% |
| 多路候选（专名 + 关键度 + 结构） | [240, 240] | 29 | 40% | 29 | 55% |

- 新找回：Dustism, Fog of War, Hole complex, Horde, Monotheism, Petropolitics, Plot Hole, Polytics, Radical Openness, Reza Negarestani, War Machine
- 现状找到而多路丢失：无
- 仍未找回（出现 ≥ 2 次）：Apocalypse, Cthulhu, Cyclonopedia, Fluid, Gog and Magog, Hydrocarbon, Inauthenticity, Incognitum Hactenus, Lubricant, Mesopotamia, Nomadic, Paranoia, Porosity, Putrefaction, Schizophrenia, Solar, Solar Capitalism, Solid, Spiral, Swarm, Tellurian Insider, Tiamat, Vortex, War on Terror
- 找回条目的来源（一个候选可有多个来源）：{'proper': 18, 'keyness': 21, 'structure': 6}

## 正式运行（整份术语表都给出）

- 候选：现状 120 条 → 多路 240 条，来源 {'proper': 120, 'keyness': 126, 'structure': 58}
- 带歧义标记：93 条，信号分布 {'mixed_case': 59, 'common_word': 53, 'sentence_initial': 6}
- 每个候选平均上下文：3.33 处
- 账本提示（粗估，不含摘要与已有术语）：现状 1 次调用 / ≈6189 token → 多路 4 次 / ≈42577 token
- 已记录误判（专名拆成碎词）出现在候选中：无
- 已知易错词的歧义标记：（未成为候选）

### 小写概念词实例（关键度一路）

warmachine（176）, cross of akht（34）, ibn maimun（20）, occultural（32）, polytical（32）, petropolitical undercurrents（11）, petropolitical（23）, defacing the ancient（35）, exteriority（38）, parsani defacing（9）, persia parsani（8）, ungrounding（17）, trison-cell（16）, islamic warmachines（7）, vowelless alphabets（7）, unlife of war（12）, schizotrategy（14）, urbanized war（26）, anglossic qabalah（6）, corkscrewing motion（6）, hyperstition team（6）, occultural meltdown（6）, monotheistic（50）, affordance（30）, poromechanics（13）
