# V3 三分钟演示 Runbook

## 准备

```bash
bash scripts/demo.sh
# 或
docker compose up --build --wait
```

打开：

```text
http://127.0.0.1:28501/?workspace=policy-exploration
```

## 演示顺序

1. A1：2027 年 1 月单月跨口径 → 211 / 220 / −9
2. 词面补查：12 月 vs 11 月 → −46.40 / −190.40 口径影响 +4.00
3. 版本：v1 ¥292.80 → v2 ¥294.80
4. 冲突/循环：无金额、SQL 0
5. 引用：文件、行号、版本、SQL、参数
6. 手册：v1 蓝灯 / v2 绿灯
7. 历史：打开旧调查 → Markdown/CSV 导出
8. 系统状态：模型、数据、rerank 状态

## 讲解边界

```text
这是垂直政策/手册/零售调查 Agentic RAG
不是通用平台
模型兜底只用于补查
证据不足会拒答
```
