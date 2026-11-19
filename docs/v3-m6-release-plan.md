# V3 M6：部署、试点与发布

日期 2026 年 11 月 19 日。M6 已开始，当前完成可本地验证的观测和备份基础；生产域名/证书、外部试点和正式发布仍需目标环境执行。

## 1. 部署拓扑

```text
Caddy HTTPS + Basic Auth
  ├─ /api/v1/* /api/v2/* /api/v3/* → API
  └─ /* → Streamlit UI

API
  ├─ /state（调查历史、文档、追踪、上传）
  ├─ 只读订单/政策演示库
  └─ 可选 PostgreSQL / BIRD

sandbox-broker → 无网络 worker
```

生产配置：

```bash
cp .env.production.example .env.production.local
chmod 600 .env.production.local
```

至少设置：

```dotenv
RAG_DOMAIN=localhost
RAG_GATEWAY_BIND=127.0.0.1
RAG_HTTP_PORT=18080
RAG_HTTPS_PORT=18443
RAG_GATEWAY_KEY=<openssl rand -hex 32>
RAG_USERS_FILE=/absolute/path/to/users
RAG_SANDBOX_IMAGE=jianshu-react-worker:<release-tag>
LLM_API_KEY=<model key>
LLM_BASE_URL=https://api.deepseek.com
```

启动：

```bash
docker compose --env-file .env.production.local \
  -p rag-prod -f compose.yaml -f compose.production.yaml up -d --build --wait
```

## 2. 观测

调查完成后写入无内容结构化日志：

```text
event, investigation_id, domain, status, reason, parent_id,
evidence_count, sql_queries, model_calls, total_tokens,
retrievals, latency_ms, document_version, data_version
```

不包含：

```text
问题原文
引用原文
文档正文
API key
数据库凭据
```

新增：

```text
GET /api/v3/metrics
```

按当前账号返回 domain/status 计数。

## 3. 备份与恢复

通用脚本：

```bash
python scripts/state_backup.py backup /path/to/state /secure/rag-state.tar.gz
python scripts/state_backup.py restore /secure/rag-state.tar.gz /path/to/restored-state
```

生产 Compose 建议先停入口和写入，再归档状态卷：

```bash
dc stop gateway ui api
dc run --rm --no-deps -T --entrypoint tar api -C /state -czf - . > rag-state.tar.gz
dc up -d --wait
```

恢复必须在独立 Compose 项目和空状态卷演练，再切换入口。

## 4. 双账号权限验收

必须验证：

```text
[ ] alice 上传
[ ] alice 分享给 bob
[ ] bob 可读
[ ] bob 撤权后列表/版本/搜索/历史/导出均受限
[ ] 伪造 X-Rag-User 被入口覆盖
[ ] 匿名和错误密码被拒绝
[ ] 撤权跨 API 重启持久化
```

现有 `scripts/policy_release_smoke.sh` 覆盖 v2 政策文档双账号流程；M6 还需要把 v3 历史/导出/零售指标纳入同一 HTTPS 冒烟。

## 5. 试点

邀请 1–3 名未参与开发的试用者，用他们自己的典型问题：

```text
提问
→ 核对原文
→ 查看历史
→ 导出
→ 撤权后复查
```

逐项记录：

```text
能否独立完成
哪里需要改写
能否找到证据
是否出现金额/版本/权限错误
```

金额、版本、权限失败阻断发布。

## 6. 三分钟演示

```text
0:00 首页输入 A1
     2027 年 1 月华南电水壶净营收
     → 实际 ¥211.00 / 统一 ¥220.00 / 差 −¥9.00
0:40 词面补查
     → 12 月/11 月实际 −¥186.40 / 统一 −¥190.40
1:10 版本变化
     → v1 ¥292.80 / v2 ¥294.80
1:40 冲突/循环拒答
     → 无金额，SQL 0
2:10 引用审计
     → 文件、行号、版本、SQL、参数
2:40 手册
     → v1 蓝灯 / v2 绿灯
3:00 历史与导出
     → Markdown/CSV
```

## 7. 发布门槛

```text
[x] 150 题 M5 gate，模型兜底 150/150
[x] 错误出数 0
[x] 正常路径 P95 <1s
[x] 模型兜底 P95 <3s
[x] 文档/格式/retail/manual 回归
[x] 结构化调查日志
[x] 状态备份/恢复脚本与测试
[x] 生产 HTTPS 双账号冒烟（本地生产配置 PASS）
[ ] 外部试点
[ ] 目标主机状态卷备份恢复演练
[ ] 回滚演练
[ ] 真实域名证书
[ ] 镜像扫描和容量验收
```

## 8. 回滚

```text
1. 入口停止接收请求
2. 记录当前镜像 digest、配置、状态备份
3. 切回上一版镜像和对应状态备份
4. 跑 HTTPS 冒烟 + 双账号权限
5. 再开放入口
```

## 9. 当前判断

> **M6 的观测、备份恢复和发布流程已开始落地；生产 HTTPS 冒烟、外部试点、真实证书和回滚演练仍需目标环境完成。**
