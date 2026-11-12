# 审校 agent 分诊基准

- 生成日期：2026-11-12
- 复现命令：`translator-review-bench`
- 未被质检报出、因而不进入分诊的注入错误：11；质检没有报的误报样例：0
- 脚本化假审校员知道标准答案、按固定节奏犯错，只检验分诊流程与不退步闸门，不代表真实模型的能力。

| 策略 | 正确修复 | 正确驳回 | 转交人工 | 误报被改 | 真错被驳回 | 生效修改全部通过检查 | 被闸门拒绝的提交 | 每条最多 token | 总 token |
| --- | ---: | ---: | ---: | ---: | ---: | :---: | ---: | ---: | ---: |
| 不开 agent（现状） | 0% | 0% | 100% | 0% | 0% | ✅ | 0 | 0 | 0 |
| 脚本化假审校员 | 65% | 82% | 27% | 18% | 0% | ✅ | 8 | 4134 | 162600 |

混合集：注入错误 40 条，已知误报 11 条。

## 不开 agent（现状）

```json
{"correct_fix": 0, "fix_differs": 0, "wrong_dismiss": 0, "fix_handoff": 40, "correct_dismiss": 0, "wrong_fix": 0, "dismiss_handoff": 11}
```
按问题类型：audit:code {'handoff': 3}；audit:link {'handoff': 3}；fidelity {'handoff': 2}；semantic {'handoff': 31}；terminology {'handoff': 12}

## 脚本化假审校员

```json
{"correct_fix": 26, "fix_differs": 0, "wrong_dismiss": 0, "fix_handoff": 14, "correct_dismiss": 9, "wrong_fix": 2, "dismiss_handoff": 0}
```
按问题类型：audit:code {'handoff': 3}；audit:link {'handoff': 3}；fidelity {'fixed': 2}；semantic {'fixed': 19, 'handoff': 7, 'dismissed': 5}；terminology {'fixed': 7, 'handoff': 1, 'dismissed': 4}
- 误报被改：他们整个下午都在烈日下干活。 → 他们整个下午都在烈日下干活（太阳）。
- 误报被改：成群的游客挤满了广场。 → 成群的游客挤满了广场（游牧群）。
