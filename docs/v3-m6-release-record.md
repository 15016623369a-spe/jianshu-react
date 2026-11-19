# V3 M6 发布记录

日期 2026 年 11 月 19 日。M6 进行中。

## 当前候选

```text
Branch        v3-foundation
Tests         301
M5            offline 146/150；model 150/150
Production    local HTTPS Compose smoke PASS
```

## Production HTTPS 冒烟

命令：

```bash
RAG_POLICY_SMOKE_PYTHON=/Users/jace/RAG/.venv/bin/python \
  bash scripts/policy_release_smoke.sh
```

结果：

```text
PASS TLS, anonymous denial, forged identity, versioned uploads,
     share/read/revoke and upload limits
PASS v3 retail/LEDGER/A1/manual/history/report/CSV
PASS revisions and revoked ACL persisted across API restart
PASS v3 history/report/CSV and owner isolation survived API restart
```

覆盖：

```text
[✓] Caddy 本地 CA TLS
[✓] 匿名拒绝
[✓] 伪造 X-Rag-User 拒绝/覆盖
[✓] alice 上传
[✓] 版本更新
[✓] 分享给 bob
[✓] bob 读取
[✓] 撤权
[✓] API 重启后版本和 ACL 仍生效
[✓] 上传大小限制
[✓] v3 历史/报告/CSV
[✓] v3 owner 隔离
[✓] v3 重启后历史/导出仍可读
```

## 候选镜像 rc2

```text
jianshu-react-api:v3.0.0-rc2              digests/ed98...
  sha256:ed98e326723c9f647d2cf3a0d1766760e0a4abf78b8708197bbd263c3a85f445
jianshu-react-ui:v3.0.0-rc2
  sha256:76babd6426cdfe74ea7f9673fc731ef453f9b03a2b771ed3114f385e932114d7
jianshu-react-sandbox-broker:v3.0.0-rc2
  sha256:40eb80bb7af62cc3d452e4f620cbf6c945c29360300ebbd2beecdcd3ab6d58b3
jianshu-react-worker:v3.0.0-rc2
  sha256:4ca8954bd9732a85201319100c3d13e7ae79036c0f0fb280afb893bbf500d410
```

## 本轮修复

生产镜像第一次构建后 API 不健康：

```text
app/rag/__init__.py
  → 导入 app.rag.dense
  → import numpy
  → API 镜像没有 numpy
```

修复：

```text
app/rag/__init__.py 不再 eager import dense/rerank
dense/rerank 只通过 app.rag.dense / app.rag.rerank 显式导入
```

重新构建后 API、UI、broker、gateway 全部 healthy，HTTPS 冒烟通过。

## M6 基础能力

```text
[✓] 调查结构化日志，无问题/引用/凭据
[✓] GET /api/v3/metrics（按 owner 统计）
[✓] scripts/state_backup.py backup/restore
[✓] 备份恢复测试：调查历史 + 政策文档
[✓] 生产 HTTPS 双账号冒烟
[✓] API 重启后版本/ACL 持久化
```

## 待完成

```text
[ ] 外部试点（1–3 名试用者）
[ ] 真实域名证书和外部访问范围
[ ] 生产状态卷备份/恢复演练
[ ] 回滚演练（rc2 ↔ 上一版镜像）
[ ] 镜像扫描/容量
[ ] 三分钟演示录制
[ ] v3.0.0 tag
```

## 结论

> **M6 的本地生产配置 HTTPS 冒烟已通过；真实域名、外部试点、备份恢复演练和回滚仍是发布前门槛。**
