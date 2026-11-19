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
PASS TLS, anonymous denial, forged identity,
     versioned uploads, share/read/revoke and upload limits
PASS revisions and revoked ACL persisted across API restart
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
```

## 镜像

```text
api              sha256:5115db6c9d7a9bc135a4de4e8830b6cb9ae695236f14a91a50089c1321185298
ui               sha256:a2d9e60f8d375a0b90e3f22ad04ab90696c5fe3aa0f89b7ccd4398416c7e406d
sandbox-broker   sha256:4c3dcb007c69de5864cb27be52f4451e435faf7024c97517f3097d519ecd095e
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
[ ] 回滚演练
[ ] 镜像扫描/容量
[ ] 三分钟演示录制
[ ] v3.0.0 tag
```

## 结论

> **M6 的本地生产配置 HTTPS 冒烟已通过；真实域名、外部试点、备份恢复演练和回滚仍是发布前门槛。**
