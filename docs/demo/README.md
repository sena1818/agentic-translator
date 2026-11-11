# 公开演示页

`site/` 是 GitHub Pages 发布的内容：一份对照阅读器（`index.html`）和两种 EPUB。

- **原文**：`source.md`，节选自 Henry David Thoreau, *Walden* (1854)，公版；文字取自 Project Gutenberg 电子书 #205。
  章节标题沿用原书，段落有删节。
- **译文**：`translation.md`，为本演示另行翻译，与原文块结构一一对应。
- **脚注**：编者为演示添加的说明，不属于原书。
- **插图**：`images/pond-sketch.svg` 为本演示绘制的示意图，并非书中插图。

演示不调用模型，只走正式的结构化导出（对齐、目录同步、脚注、图片内联、EPUB）。重新生成：

```bash
python scripts/build_demo.py
```

有版权的书不放进这里，也不发布到 Pages。

发布：仓库 Settings → Pages 把 Source 设为 "GitHub Actions"，之后 `docs/demo/site/` 有改动并推到 main 时由
`.github/workflows/pages.yml` 发布；也可在 Actions 页手动运行。
