# V3 M5 发布评测报告

日期 2026 年 11 月 19 日。

## 一、条件

```text
机器       本机 macOS
执行方式   串行、单进程
套件       150 题
并发       无
缓存       进程内模型/索引缓存
模型       DeepSeek（已配置 .env）
```

结果文件：

```text
evaluation/results/m5_gate_offline.json
evaluation/results/m5_gate_model.json
evaluation/results/m5_api_latency.json
evaluation/results/m5_browser_latency.json
```

## 二、150 题结果

| 指标 | 离线确定性 | 模型兜底 | 目标 |
| --- | ---: | ---: | --- |
| 通过 | 146/150 | **150/150** | 全部通过 |
| 错误出数 | **0** | **0** | 0 |
| Doc Recall | 93.3% | **100%** | ≥85% |
| Citation precision | **100%** | **100%** | ≥90% |
| 引用支持 | **100%** | **100%** | ≥90% |
| 拒答正确率 | **100%** | **100%** | 100% |
| 模型调用 | 0 | 4 | 正常路径 0 |
| 正常路径 P50 | 0.294 ms | 0.306 ms | — |
| 正常路径 P95 | 3.925 ms | 4.452 ms | <1 s |
| 兜底 P50 | — | 880.452 ms | — |
| 兜底 P95 | — | 1002.510 ms | <3 s |

## 三、失败分类

离线确定性模式唯一失败：

```text
4 × buried-backlink
  humidifier-buried-backlink
  humidifier-compare-buried-backlink
  router-buried-backlink
  router-compare-buried-backlink
```

原因：正文里的旧标题转引需要模型生成补查词。  
模型兜底模式 4 题全部通过，模型调用 4 次。

## 四、端到端

### HTTP API

```text
3 个正常问题 ×3 轮
P50 7.865 ms
P95 12.743 ms
错误 0
```

### 浏览器

```text
正常政策调查 3 轮
P50 304.656 ms
P95 323.473 ms
```

浏览器计时为“点击开始调查 → 首屏金额出现”，包含 UI 和 API。

## 五、发布门槛判断

```text
[x] 错误出数 0
[x] Doc Recall ≥85%
[x] Citation precision ≥90%
[x] 引用支持 ≥90%
[x] 拒答正确率 100%
[x] 正常路径模型调用 0（146/146）
[x] 正常路径 P95 <1s
[x] 模型兜底 P95 <3s
[x] 150 题模型兜底全通过
```

结论：**M5 评测门槛通过**（模型兜底路径）。

## 六、限制

```text
[ ] 浏览器端到端仅覆盖正常路径
[ ] 未做并发压测
[ ] 未做独立三轮重复（当前为 1 轮串行 + 延迟采样）
[ ] M6 生产 HTTPS、权限、备份、回滚未验收
```

## 七、复现

```bash
cd /Users/jace/jianshu-react

# 离线 146/150
PYTHONPATH=. .venv/bin/python -m evaluation.run_m5_gate \
  --output evaluation/results/m5_gate_offline.json

# 模型兜底 150/150
PYTHONPATH=. .venv/bin/python -m evaluation.run_m5_gate --model \
  --output evaluation/results/m5_gate_model.json

# API / 浏览器延迟
PYTHONPATH=. .venv/bin/python -m evaluation.run_m5_api_latency --repeats 3 \
  --output evaluation/results/m5_api_latency.json
python3 -m evaluation.run_m5_browser_latency --repeats 3 \
  --output evaluation/results/m5_browser_latency.json
```

## 八、结论

> **M5 的 150 题多域评测已通过：离线 146/150，模型兜底 150/150，错误出数 0；正常路径 P95 4.45 ms，模型兜底 P95 1002.51 ms，均满足门槛。剩余是 M6 生产验收。**
