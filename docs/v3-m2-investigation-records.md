# V3 M2：可核查、可回放的调查记录

日期 2026 年 11 月 18 日。对应产品执行计划的 M2。

## 已实现

### 1. 调查记录落盘

新增：

```text
app/investigation_history.py
data/investigations.sqlite
```

每次 `POST /api/v3/investigations` 都生成新的调查 ID，并保存：

```text
investigation_id
domain
question
status
created_at
scope
evidence
document_version
data_version
metrics
actual / counterfactual
tool_trace / retrieval_trace
```

状态映射：

```text
answer        → completed
clarification → clarification
abstained     → insufficient
其他          → failed
```

金额仍以整数分保存。

### 2. 历史读取

新增：

```text
GET /api/v3/investigations
GET /api/v3/investigations/{investigation_id}
```

- 按 owner 隔离；
- 列表按时间倒序；
- 支持 `limit` / `before`；
- 不存在的 ID 与无权限统一 404；
- 历史读取返回当时的完整结果，不重新执行计算。

### 3. 执行失败边界

现有政策路径已经保证：

```text
冲突             → 无金额，SQL 0
部分月份缺政策   → 无金额，SQL 0
公告缺失/循环    → 无金额，SQL 0
```

M1 已补充无覆盖和同优先级冲突的入口级回归测试。

### 4. 数据版本

记录中保存：

```text
document_version：命中的文档版本/上传文档版本
data_version：演示数据或数据源版本标识
```

重新调查会生成新的 investigation ID，不覆盖旧记录。

## 验收

```text
Ran 290 tests in 14.214s
OK
```

新增覆盖：

- 调查记录保存、状态、证据和数据版本；
- owner 隔离；
- 列表/读取/未知 ID；
- 完成与待澄清记录；
- A6 同优先级冲突；
- A7 无覆盖期间；
- 原有 A1/A2/A3/A4/A5/A8 回归。

## 尚未做

```text
[ ] 历史页 UI（留给 M3）
[ ] Markdown/CSV 导出（留给 M3）
[ ] policy 域接入通用 RetrievalService（保留现有语义）
[ ] 生产 HTTPS/备份/回滚（M6）
```

## 当前结论

M2 的核心已经完成：

> **每次调查都有独立 ID、状态、范围、证据、金额、SQL、参数、耗时和模型调用记录；历史读取的是当时保存的结果，而不是用今天的数据重新算。**
