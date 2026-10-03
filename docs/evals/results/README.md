# 评测结果

本目录的 `report.md` / `report.json` 由 `translator-eval` 生成。

> ⚠️ **当前为 `--dry-run` 冒烟占位**：分数、token 均为假模型/假裁判产出，**不代表真实翻译质量**。
> 本轮未执行付费选手/裁判评测；零候选短语只表示样本不足，不代表 100% 一致。

补齐密钥后，一条命令回填真实数据：

```bash
translator-eval --out docs/evals/results --estimated-cost <人民币>
```

方法与复现细节见 [../methodology.md](../methodology.md)。
