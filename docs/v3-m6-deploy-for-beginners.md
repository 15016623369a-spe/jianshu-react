# M6 部署入门：域名、主机、试用者和风险接受

这份文档给不熟悉运维的人用。目标不是“大厂生产”，而是让 2–3 个真实用户通过 HTTPS 试用。

## 结论：推荐路线

```text
买一个域名
+ 一台海外/香港 VPS（2C4G 即可）
+ Docker Compose
+ Caddy 自动 HTTPS
+ 2–3 个账号
= 一次真实试点
```

这是最简单、最像正式发布的路线。

如果暂时不想买 VPS：

```text
Mac 常开 + Cloudflare Tunnel
+ 一个域名
+ 2–3 个账号
= 也可以做小范围 HTTPS 试点
```

但 Mac 方案依赖本机常开，不适合正式发布。

## 一、真实域名

“真实域名”就是：

```text
rag.example.com
```

### 怎么获得

1. 在 Cloudflare Registrar / Namecheap / 阿里云 / 腾讯云买一个域名。
2. 第一年通常几十元到一百元。
3. 建议用海外注册商 + 海外 DNS，避免备案和实名流程拖时间。
4. 如果服务器在中国大陆，域名通常需要 ICP 备案；试点建议用海外或香港 VPS，先不做大陆备案。

### 要提供什么

```text
域名：
DNS 管理权限：
```

### 配置

在 DNS 里加一条 A 记录：

```text
rag.example.com → 你的 VPS 公网 IP
```

如果你用 Cloudflare，先设成：

```text
DNS only（灰云）
```

不要一上来开橙云代理，否则 Caddy 的 HTTP 证书申请可能失败。

## 二、目标主机

“目标主机”就是跑 Docker 的那台机器。

### 推荐：海外/香港 VPS

配置：

```text
2 vCPU / 4 GB RAM / 40 GB 磁盘
Ubuntu 24.04
Docker + Compose
开放 80/443/22
```

常见选择：

```text
Hetzner CX22
DigitalOcean 2GB/4GB
Vultr
阿里云香港
腾讯云香港
```

成本大约每月 30–100 元人民币。

### 初始化

```bash
ssh root@你的VPS_IP

apt update && apt install -y git
curl -fsSL https://get.docker.com | sh

git clone <你的仓库地址> /opt/jianshu-react
cd /opt/jianshu-react

cp .env.production.example .env.production.local
chmod 600 .env.production.local
```

编辑 `.env.production.local`：

```dotenv
RAG_DOMAIN=rag.example.com
RAG_GATEWAY_BIND=0.0.0.0
RAG_HTTP_PORT=80
RAG_HTTPS_PORT=443
RAG_GATEWAY_KEY=<openssl rand -hex 32 的输出>
RAG_USERS_FILE=/opt/jianshu-react/deploy/users
RAG_SANDBOX_IMAGE=jianshu-react-worker:v3.0.0-rc2
LLM_API_KEY=<模型 key>
```

> Caddy 访问真实域名的 80/443 后会自动申请 Let's Encrypt 证书，不需要自己买证书。

### 账号文件

每个用户一行：

```bash
docker run --rm caddy:2.11.9-alpine caddy hash-password --plaintext '用户密码'
```

写入 `deploy/users`：

```text
alice $2a$14$...
bob   $2a$14$...
```

权限：

```bash
chmod 600 deploy/users
```

### 启动

```bash
docker compose --env-file .env.production.local \
  -p rag-prod -f compose.yaml -f compose.production.yaml up -d --build --wait
```

验证：

```text
https://rag.example.com
```

## 三、外部试用者

外部试用者不是“专家评审”，就是：

```text
2–3 个真实用户
```

可以是：

```text
同事
同学
做运营/财务/客服的朋友
```

### 怎么组织

```text
1. 给每人一个账号
2. 发 URL 和密码
3. 约定 20 分钟
4. 让对方用自己的问题操作
5. 你只观察，不提示
6. 记录卡住的位置
```

### 试用任务

```text
提问
核对原文
打开历史
导出报告/CSV
```

### 记录表

```text
用户：
问题：
是否完成：
卡在哪一步：
金额/版本/权限是否出错：
需要补充什么：
```

不要只让用户“随便看看”，要有任务和记录。

## 四、镜像扫描风险接受

“风险接受”不是忽略，而是：

```text
记录漏洞
→ 判断影响
→ 做缓解
→ 明确谁批准
→ 设有效期
```

### 当前扫描

```text
0 critical / 9 high
6 × openssl（Debian 暂无修复版本）
3 × Python build tool（镜像实际已升级，scanner 仍显示旧 dist-info）
```

### 处理选择

1. **升级/重建**：能修的先修。
2. **隔离**：只在内网/受控账号开放。
3. **缓解**：限制上传、限流、最少权限、日志审计。
4. **接受**：写风险接受单，设到期时间。
5. **不发布**：critical 或有可直接利用的高危，先停。

### 风险接受模板

```text
漏洞：
影响组件：
受影响功能：
是否有修复：
为什么暂时不能修：
缓解措施：
批准人：
批准日期：
到期日期：
复测日期：
```

### 当前建议

```text
内部 2–3 人试点：可以带风险接受发布
公开互联网发布：不建议，先等 openssl 修复或换基础镜像
```

## 五、最小发布路径

```text
第 1 天  买域名 + VPS
第 2 天  部署 + Caddy HTTPS + 两个账号
第 3 天  内部双账号验收 + 备份恢复
第 4 天  2–3 名外部试用
第 5 天  修复阻断问题 + 风险接受 + 打 tag
```

## 六、你需要提供什么

如果要我继续做真实部署，需要：

```text
1. 域名或允许我给出购买清单
2. VPS IP / SSH 方式
3. DNS 管理方式
4. 试用者人数和账号名
5. 是否接受当前 openssl 风险
```

没有这些信息，本地能做的都做完了，但真实发布验收不能凭空完成。
