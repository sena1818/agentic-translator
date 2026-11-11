# 注入错误基准：规则质检

- 生成日期：2026-11-12
- 复现命令：`translator-qa-bench`
- 检出口径：改坏后的译文报出了原样译文上没有的问题。误报口径：原样段对上报出任何问题。
- 注入错误比真实错误更规整，召回率只说明质检能否抓住这类已知错误，不是真实译文的检出率。

## 自带段对

- 原样段对：36，误报 0（0%）
- 总召回率：18%

| 错误类型 | 注入数 | 检出数 | 召回率 | 漏检示例 |
| --- | ---: | ---: | ---: | --- |
| 反转否定 | 10 | 0 | 0% | 删去「没有」 |
| 改变情态强弱 | 8 | 0 | 0% | 「可能」改为「必然」 |
| 交换数字 | 6 | 0 | 0% | 70 与 80 互换 |
| 漏译半句 | 15 | 3 | 20% | 删去「模型无法恢复原始信号。」 |
| 换错术语 | 6 | 6 | 100% |  |
| 改链接地址 | 3 | 0 | 0% | https://example.org/project → https://example.org/projecb |
| 改代码内容 | 3 | 0 | 0% | weights 与 lr 互换 |

### 种子反例

| 类型 | 原文 | 改坏的译文 | 检出 |
| --- | --- | --- | :---: |
| 反转否定 | `` The method does not improve accuracy. `` | `` 该方法提高了准确率。 `` | ❌ |
| 改变情态强弱 | `` The result may suggest a correlation. `` | `` 该结果证明了因果关系。 `` | ❌ |
| 交换数字 | `` Accuracy rose from 70 to 80. `` | `` 准确率从 80 提升至 70。 `` | ❌ |
| 漏译半句 | `` The method improves accuracy but doubles memory use. `` | `` 该方法提高了准确率。 `` | ❌ |
| 改代码内容 | `` ```python left = right + offset ``` `` | `` ```python right = left + offset ``` `` | ❌ |
| 改链接地址 | `` [Paper](https://example.org/a) `` | `` [论文](https://example.org/b) `` | ❌ |
