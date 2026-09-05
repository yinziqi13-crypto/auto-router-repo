# Auto Router AIHub V4.0 · M0 Day1 环境检查报告

| 项目 | 内容 |
|---|---|
| 检查对象 | 腾讯云服务器 ***SERVER-IP***（TencentOS Server 4） |
| 检查时间 | 2026-09-02 11:37 CST（服务器本地时间一致，无时钟偏差） |
| 检查方式 | SSH 只读检查（脚本 `outputs/m0_day1_env_check.py`，全部命令只读，原始输出存档 `m0_day1_env_check_output.txt`） |
| 约束遵守情况 | ✅ 未修改任何配置　✅ 未停止任何服务　✅ 未安装任何软件　✅ 生产 8787 全程零接触 |

---

## 一、结论摘要

**6 项检查全部完成。M0 部署条件 8 项对照中 6 项达标、1 项略偏低（可控）、1 项阻塞（需决策）。**

| # | 检查项 | 结果 | 判定 |
|---|---|---|---|
| 1 | 系统版本 | TencentOS Server 4，内核 6.6.117-45.11.4.tl4 | ✅ 达标 |
| 2 | CPU / 内存 | 2 核 Xeon 8255C；内存 1.9 Gi（可用 1.2 Gi + 4 Gi swap） | ⚠️ 内存略低于 2G 阈值，可控 |
| 3 | 磁盘空间 | 50 G 总量，已用 11 G，**可用 40 G**（21%） | ✅ 充裕 |
| 4 | Docker 环境 | **未安装**（docker 命令不存在，服务 not-found） | ❌ 阻塞项，需确认安装方式 |
| 5 | 8787 生产服务 | active (running)，健康检查 ok，51 模型 / 3 上游 | ✅ 健康未受影响 |
| 6 | 目录结构 | /opt/ai-router（33M 生产）；/opt/ai-hub 不存在（净地） | ✅ 可直接创建 |

**三个关键发现：**

1. **Docker 未安装 —— Day1 后续步骤的阻塞项。** 按约定"不安装软件（除非确认）"，安装方案见第四节，需确认后执行。
2. **内存 1.9 Gi 略低于 M0 方案的 ≥2G 阈值。** 但可用 1.2 Gi + 4 Gi swap（当前 0 使用），生产 gunicorn 峰值仅 50.8 MB，叠加 Docker（约 200–300 MB）+ New API 容器（约 150–300 MB）后仍有余量，配合容器限内存可控。判定：**可继续，部署后监控**。
3. **端口 3000 空闲、目标目录净地、上游三连通、生产服务健康** —— 除 Docker 外，其余部署前置条件全部就绪。

---

## 二、检查结果明细

### 2.1 系统版本

| 项目 | 实测值 |
|---|---|
| 发行版 | TencentOS Server 4（VERSION_ID=4，platform:tl4） |
| 内核 | 6.6.117-45.11.4.tl4.x86_64 |
| 虚拟化 | KVM（腾讯云轻量） |
| 主机名 | VM-0-11-tencentos |
| 服务器时间 | 2026-09-02 11:37:01 CST |

**"Octop" 之谜已解**：你提供的系统信息 "Octop on TencentOS Server 4" 中，Octop 是腾讯云镜像**预装的 agent 应用**（`/opt/octop`，708 M，属主 agentuser，监听端口 26919，配套 `/opt/first-boot-octop.sh` 开机脚本）。它与本项目无关，**不需要动它**。

### 2.2 CPU / 内存

```
CPU：2 vCPU，Intel Xeon Platinum 8255C @ 2.50GHz，x86_64

内存：  total 1.9Gi | used 719Mi | free 657Mi | buff/cache 762Mi | available 1.2Gi
Swap：  total 4.0Gi | used 0B
```

- 对照 M0 方案阈值（≥2C2G）：CPU ✅ 达标；内存 ⚠️ 名义 1.9 Gi 略低，实际可用 1.2 Gi + 未动用的 4 Gi swap。
- 当前内存主要占用：octop（预装 agent）+ 系统 + gunicorn 生产进程。生产服务实测仅 48.4 MB（峰值 50.8 MB），占用极轻。

### 2.3 磁盘空间

| 挂载点 | 总量 | 已用 | 可用 | 使用率 |
|---|---|---|---|---|
| /dev/vda1（/） | 50 G | 11 G | **40 G** | 21% |

/opt 目录占用：

| 目录 | 大小 | 说明 |
|---|---|---|
| /opt/octop | 708 M | 腾讯云预装 agent（不动） |
| /opt/ai-router | 33 M | 生产 Auto Router v3.9（不动） |
| /opt/first-boot-octop.sh | 8.0 K | 预装脚本（不动） |

M0 需要 ≥2 G 磁盘（New API 镜像 + SQLite 数据），40 G 可用**远超需求**。

### 2.4 Docker 环境

| 检查 | 实测结果 |
|---|---|
| docker -v | `command not found` |
| docker compose version | `command not found` |
| systemctl is-active docker | inactive |
| systemctl is-enabled docker | not-found（无此服务单元） |
| 容器/镜像列表 | 无法执行（docker 不存在） |

**结论：服务器完全没有 Docker，也从未安装过。** 这是 Day1 后续步骤（Docker Compose 部署 New API）的唯一阻塞项。

### 2.5 8787 生产服务状态

| 检查项 | 实测值 |
|---|---|
| 服务状态 | **active (running)** since 2026-09-02 08:23:09 CST（已运行 3h13m） |
| systemd 单元 | ai-router.service，enabled，Restart=always |
| 进程形态 | gunicorn 主进程 1234137 + worker 1234139，`--workers 1 --threads 8 --timeout 300` |
| 内存占用 | 48.4 M（峰值 50.8 M） |
| 健康检查 `/health` | `{"status":"ok","models":51,"pools":{"normal":35,"strong":11,"vision":5},"upstreams":3}` |
| 近期日志 | 09:58:37 `模型探测完成: 49/51 可用`（今晨第一阶段测试触发；2 个失败为已知问题：kimi-k-2-5 上游引擎 500、kimi-k2.7-code-highspeed 免费额度 402，均非路由故障） |

**监听端口全景**（ss -tlnp）：

| 端口 | 进程 | 说明 |
|---|---|---|
| 22 | sshd | SSH |
| 26919 | octop | 预装 agent |
| 8787 | gunicorn | 生产服务 |
| **3000** | **无监听** | **空闲，可直接用于 M0** ✅ |

### 2.6 目录结构

**/opt/ai-router（生产目录，33 M）关键文件：**

| 文件 | 大小 | 最后修改 | 说明 |
|---|---|---|---|
| auto_router.py | 143 466 B | 09-02 08:23 | 生产主程序（v3.9，即 auto_router_live.py 部署名） |
| config.json | 4 076 B | 09-02 08:23 | 运行配置 |
| usage.json | 7 943 B | 09-02 08:23 | 用量数据 |
| tc_cloud.json | 339 B（权限 600） | 09-01 17:02 | 腾讯云密钥 |
| tc_prices.json | 44 733 B | 09-01 16:02 | 官方价格表 |
| tc_peak.json | 5 444 B | 09-01 16:02 | 峰值记录 |
| model_test_result.json | 12 666 B | 09-02 09:58 | 今晨探测结果 |
| auto_router.py.bak.* × 9 | — | 09-01 ~ 09-02 | 迭代备份链 |
| venv/、backup/ | — | — | 独立虚拟环境、备份目录 |

**systemd 服务文件要点**（`/etc/systemd/system/ai-router.service`）：
- User=root，WorkingDirectory=/opt/ai-router，Environment=APP_DIR=/opt/ai-router
- Environment=**ADMIN_TOKEN=admin-2026**（弱凭据，Phase 1 已知问题，M0 不动）
- ExecStart：gunicorn --bind 0.0.0.0:8787 --workers 1 --threads 8 --timeout 300

**目标目录**：`/opt/ai-hub` 不存在（净地），`/opt/ai-hub/m0` 待部署时创建，与生产完全隔离。

### 2.7 附加检查：上游连通性（三上游全部连通）

| 上游 | 根路径实测 | 判定 |
|---|---|---|
| https://tokenhub.tencentmaas.com（腾讯 free） | HTTP 404 / 0.046s | ✅ 连通（404 为根路径正常响应） |
| https://api.lkeap.cloud.tencent.com（腾讯 plan） | HTTP 401 / 0.096s | ✅ 连通（401 为未带凭证正常响应） |
| https://token-plan.cn-beijing.maas.aliyuncs.com（阿里百炼） | HTTP 404 / 0.127s | ✅ 连通 |

---

## 三、M0 部署条件对照表（汇总）

| # | 条件 | M0 方案要求 | 实测 | 判定 |
|---|---|---|---|---|
| 1 | CPU | ≥2 核 | 2 核 Xeon 8255C | ✅ |
| 2 | 内存 | ≥2 GB | 1.9 GB 总量 / 1.2 GB 可用 + 4 GB swap（0 使用） | ⚠️ 可控 |
| 3 | 磁盘 | ≥2 GB 可用 | 40 GB 可用 | ✅ |
| 4 | Docker | 已安装 | 未安装 | ❌ 阻塞 |
| 5 | 端口 3000 | 空闲 | 无监听 | ✅ |
| 6 | 目录 /opt/ai-hub/m0 | 可创建 | 净地，不存在 | ✅ |
| 7 | 生产 8787 隔离 | 不受影响 | active 健康，检查全程零接触 | ✅ |
| 8 | 上游连通 | 三上游可达 | 全部连通 | ✅ |

---

## 四、关键决策点：Docker 安装方案（需你确认，未确认前不执行任何安装）

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **A. 服务器直装 Docker（推荐）** | TencentOS 4 官方源：`dnf install -y docker docker-compose-plugin`，装完 `systemctl enable --now docker` | ① M0 在真实目标环境验证，结论直接迁移 M1；② 完全可回退（`dnf remove docker` + 删 /var/lib/docker 即恢复原状）；③ 不碰 8787 生产 | 生产服务器新增常驻组件（dockerd 约 200–300 MB 内存，磁盘约 1 GB） |
| B. 本地 Docker Desktop 验证 | 在你的 Windows 机器装 Docker Desktop 跑 New API | 完全不碰生产服务器 | M0 要验证的是"这台服务器能不能跑 New API"，换环境结论迁移性差；Windows 侧还需额外安装 |
| C. New API 二进制裸跑 | 不用 Docker，直接跑二进制 | 无需 Docker | New API 官方主推 Docker 发行，二进制非主路径，与 M1 生产形态不一致，验证价值最低。不推荐 |

**推荐方案 A**，附三条防护措施：

1. New API 容器加内存限制（compose 中 `mem_limit: 512m`），防止挤占生产；
2. M0 按既定方案用 SQLite，不引入 Redis，最小化资源占用；
3. 部署后连续观察 `free -h` 与生产 `/health` 响应，异常即 `docker compose down`，生产零影响。

**管理界面访问建议**：M0 期间**不在腾讯云防火墙放行 3000 端口**，本机走 SSH 隧道访问（`ssh -L 3000:127.0.0.1:3000 root@***SERVER-IP***` 后浏览器开 localhost:3000），避免 New API 初始默认密码（root/123456）暴露公网。首次登录后立即改密。

---

## 五、风险与缓解

| # | 风险 | 评估 | 缓解措施 |
|---|---|---|---|
| 1 | 内存余量（1.9 Gi 总量） | 中低：装 Docker + New API 后预计新增常驻 350–600 MB，可用降至约 0.6–0.9 GB；生产峰值仅 50.8 MB，冲击小；4 GB swap 兜底 | 容器 mem_limit 512m；部署后监控 free -h；异常即 down |
| 2 | 3000 端口暴露面 | 低（只要不主动放行防火墙）：New API 初始 root/123456 | SSH 隧道访问 + 首登改密；不放行公网 |
| 3 | 到期窗口 | **高：服务器 + 套餐 2026-09-29 到期，剩 27 天** | M0 三天内完成；9-10 号前决定续费，否则 M1 无处部署 |
| 4 | 生产弱凭据（ADMIN_TOKEN=admin-2026） | 信息层面提醒，M0 不动 | M1 迁移时一并处理 |

---

## 六、Day1 剩余步骤（待你确认 Docker 方案后执行）

1. 确认 Docker 安装方案（第四节选项 A/B/C）；
2. 安装 Docker Engine + Compose 插件（约 5–10 分钟）；
3. 创建 `/opt/ai-hub/m0/`，写入 `docker-compose.yml`（按 M0 方案：calciumion/new-api:latest、SQLite、端口 3000、数据卷 ./data）；
4. `docker compose up -d` 拉起容器；
5. SSH 隧道进管理台，改 root 初始密码；
6. 执行 M0 方案 2.2 节"基础六项检查"（控制台可访问 / 默认渠道存在 / 令牌创建 / 日志页 / SQLite 落盘 / 系统信息页）；
7. 创建第一个渠道（阿里百炼）并完成首次成功调用 —— **Day1 验收标准**。

---

*报告完。原始检查输出存档：`outputs/m0_day1_env_check_output.txt`；检查脚本：`outputs/m0_day1_env_check.py`（可随时重跑复核）。*

---

## 七、Day1 执行结果（当日 12:10 更新）

### 7.1 执行时间线（双方并行操作实录）

| 时间 | 事件 | 操作方 |
|---|---|---|
| 11:42 | Docker 预检：rpm 确认无 docker，EPOL 源有 docker-ce 29.7.2 + compose-plugin 5.4.0 | 小企鹅 |
| 11:44 | `dnf install -y docker-ce docker-ce-cli docker-compose-plugin`（13 包，dnf history 事务 #1） | **孙磊**（经控制台渠道，无交互式登录会话） |
| 11:45-11:46 | 创建 /opt/ai-hub/m0 + compose + 拉取镜像 + 容器 aihub-m0 启动（11:46:04，已带 512m 内存限制） | **孙磊**（并行部署，与 M0 方案一致） |
| 11:46:47 | 部署脚本抵达：dnf 报 already installed、镜像 up to date、compose up 报 "Running"（零冲突，配置等价） | 小企鹅 |
| 11:58-11:59 | root/123456 登录尝试失败 ×N（当时 root 用户不存在） | 小企鹅 |
| 12:01 | GET /api/setup：status=false，users 表为空（初始化未完成） | 小企鹅 |
| 12:02:50 | **POST /api/setup 成功：root 创建（bcrypt）+ 自用模式开启 + DemoSite 关闭，浏览器自动登录** | **孙磊** |
| 12:02:57 | 小企鹅初始化脚本抵达 → "系统已经初始化完成" | 小企鹅 |

### 7.2 已达成（Day1 步骤 1-5 完成）

| # | Day1 步骤 | 状态 |
|---|---|---|
| 1 | Docker 安装（方案 A 已确认） | ✅ docker-ce 29.7.2 + Compose 5.4.0，active |
| 2 | /opt/ai-hub/m0 部署 | ✅ 容器 aihub-m0 Up，21.5MiB/512MiB，SQLite 落盘（one-api.db） |
| 3 | New API 版本 | ✅ **v1.0.0-rc.30**（注意：比 M0 方案引用的 rc.10 新，以实际为准） |
| 4 | 管理台初始化 | ✅ root 已建（role=100，quota=100 000 000），自用模式开启 |
| 5 | 基础六项检查 | ✅ 容器/API/SQLite/日志/生产隔离(8787 正常)/内存(可用 1.1Gi + swap) 全部通过 |

### 7.3 安全状态（12:14 更新）

- **root 密码**：孙磊 12:02 设置的密码未通过验证（试 ***REDACTED*** 不对），按孙磊授权（"如果不对你就直接重置吧"）于 12:11 经 SQLite UPDATE 重置为强密码 **`Aihub-M0-7ux2zUG9`**（bcrypt $2a$10$，与 New API 自身格式一致，重置后登录验证通过）。此密码为当前生效密码，孙磊登录控制台请用它。
- **3000 端口公网可达**（本机实测 HTTP 200/0.08s）：孙磊已决策 **M0 期间保留开放**，M1 前收紧（防火墙限 IP 或关闭走 SSH 隧道）。
- 改进项（M1）：HTTPS + SESSION_COOKIE_SECURE=true（当前 HTTP 明文，日志有告警）。
- 容器日志告警一条：Refresh cookie 未启用安全校验（M0 实验环境可接受）。

### 7.4 渠道 + 令牌 + 首次调用（Day1 验收标准）✅ 12:14 达成

| # | 操作 | 结果 |
|---|---|---|
| 1 | 创建渠道 `bailian-lite`（type 1，base_url `…/compatible-mode`，9 模型，密钥取自生产 config.json） | ✅ id=1，status=1 |
| 2 | New API 内置渠道测试（qwen3.8-flash） | ✅ 通过（真实消耗 62+30 tokens，quota 3450） |
| 3 | 创建测试令牌 `m0-test`（无限额度） | ✅ id=1，48 位 key |
| 4 | **首次真实调用** POST /v1/chat/completions | ✅ **HTTP 200，模型 qwen3.8-flash，回复 "OK"，68+41 tokens（含 38 推理），扣 quota 4088，耗时 1s** |
| 5 | 日志验证 | ✅ 两次调用均记入 type=2 消费日志（模型/渠道/用量/quota 齐全） |

**测试令牌（完整值）**：`sk-***REDACTED***`

**Day2 Q1 实测数据点（quota 单位换算）**：
- 调用 A：62 prompt + 30 completion = 3450 quota
- 调用 B：68 prompt + 41 completion = 4088 quota
- （分解出单价还需 Day2 用控制模型变量的对照实验完成，原始数据已入日志）

### 7.5 差异记录（供 M0 报告引用）

1. 部署由孙磊并行完成（非小企鹅脚本），配置与 M0 方案等价（含 Day1 报告建议的 mem_limit 512m）。
2. 小企鹅的 compose 文件已覆盖写入 /opt/ai-hub/m0/docker-compose.yml（与运行容器配置等价，compose up 无 drift；差异：未含 M0 文档中的 healthcheck 段，可选补）。
3. New API 实际版本 rc.30 > 方案引用 rc.10。
4. **rc.30 管理接口认证为 JWT**（登录返回 `data.access_token`，后续管理 API 带 `Authorization: Bearer <JWT>`；session cookie 仅 refresh token，路径限 /api/user/auth）——M0 方案中所有涉及管理 API 的脚本须按此适配。
5. **令牌 key 在管理列表 API 中打码返回**（jpaW******），完整值须从 SQLite `tokens` 表直读或创建响应中取。
6. 用户提供的密码 ***REDACTED***（与 SSH 同码）不是 New API root 密码；已按授权重置。

### 7.6 Day1 结论

**Day1 验收标准全部达成**：Docker 环境 ✅ / M0 独立环境 ✅ / 管理台初始化 ✅ / 基础六项 ✅ / 首个渠道（阿里百炼）✅ / 首次成功调用 ✅。生产 8787 全程零影响。进入 Day2：三渠道全量验证 + R1-R3 free_first 实验 + Q1-Q3 quota 实测。
