# Auto Router AIHub V4.0 · M0 选型验证实施方案

**项目：** Auto Router AIHub 升级改造项目
**文档版本：** M0 验证方案 V1.0
**日期：** 2026-09-02
**前置文档：** 第一阶段技术分析报告 / 第二阶段产品规划（已确认，含 Quota 积分引擎补充决策）
**M0 性质：** 纯验证，**不正式开发、不生产迁移、不影响 8787 生产服务**

---

## M0 验证目标（总）

| # | 验证命题 | 判定标准 |
|---|---|---|
| 1 | New API 可否作为 V4.0 基座 | 部署/渠道/调用/计费/看板全链路走通 |
| 2 | 三层架构可否实现 | New API 核心层 + Python 增强层 + Quota 积分引擎，各层验证有落地路径 |
| 3 | 三渠道接入保真度 | 腾讯 free / 腾讯 plan / 百炼行为与 v3.9 一致（含 free_first、streaming、Token/成本口径）|
| 4 | Quota 积分引擎数据模型可行性 | 三层换算逻辑跑通一次完整闭环（成本→倍率→积分→扣减）|

**M0 出口：Go/No-Go 决策报告**。任一关键项不通过，先解决再进 M1，不带病开发。

---

## 一、验证环境设计

### 1.1 部署位置决策

**首选：现有服务器 ***SERVER-IP*** 并行部署**（与生产 8787 隔离共存）。

理由：
1. 到三个上游的网络连通性已验证（v3.9 跑了 4 个月）
2. 零新增成本，符合"不盲目加预算"
3. M0 验证环境与 M1 生产环境同构，验证结论直接复用
4. 服务器 9-29 到期若续费，环境无缝转正；若换服务器，compose 文件直接搬迁

**前置条件（Day 1 第一件事）：** 服务器资源盘点，不达标则降级。

| 检查项 | 命令 | 达标线 | 不达标降级方案 |
|---|---|---|---|
| CPU/内存 | `free -h` / `nproc` | ≥2C2G（New API 单容器约 100-300MB）| 本地 Docker Desktop 验证 |
| 磁盘 | `df -h /opt` | ≥2G 可用 | 清理后重试 |
| Docker | `docker -v` / `docker compose version` | 已装或可装 | 宝塔面板 Docker 管理器安装 |
| 端口冲突 | `ss -tlnp \| grep -E '3000\|8787'` | 3000 空闲（8787 为生产）| 改用 3080 |

### 1.2 Docker Compose 部署方案

目录规划（与生产完全隔离）：

```
/opt/ai-hub/m0/                 # M0 验证目录
├── docker-compose.yml
├── .env                        # 环境变量（密钥不入库）
├── data/                       # New API 持久化卷（SQLite + 日志 + 上传）
└── logs/                       # 容器日志
```

`docker-compose.yml`（M0 最小配置，SQLite 单机版，无 Redis——单机部署不需要）：

```yaml
services:
  new-api:
    image: calciumion/new-api:latest   # 固定为验证时的具体版本号
    container_name: aihub-m0
    restart: unless-stopped
    ports:
      - "3000:3000"
    environment:
      - TZ=Asia/Shanghai
      # SQLite 模式无需 SQL_DSN；M0 不启用 Redis
      # SESSION_SECRET 与 CRYPTO_SECRET 在 M1 多机/生产时再配
    volumes:
      - ./data:/data
    healthcheck:
      test: ["CMD", "wget", "-q", "-O", "-", "http://localhost:3000/api/status"]
      interval: 30s
      timeout: 5s
      retries: 3
```

启动与验证：

```bash
mkdir -p /opt/ai-hub/m0/data && cd /opt/ai-hub/m0
# 写入 compose 文件后
docker compose up -d
docker logs -f aihub-m0 --tail 50    # 观察初始化日志
```

### 1.3 数据持久化方案

| 数据 | 存储 | 说明 |
|---|---|---|
| New API 业务数据 | `./data/one-api.db`（SQLite）| 渠道/令牌/用户/日志/额度全在这一个文件，备份即拷贝 |
| 容器日志 | `docker logs` + `./logs/` | 排障用 |
| 密钥 | `.env` + 渠道配置内 | 权限 600，不入 git |

M0 结论：SQLite 足够（个人平台单机）。M1 保留升级路径：`SQL_DSN` 切 MySQL/PostgreSQL 时数据迁移工具验证（M0 顺带确认 New API 自带的数据库切换说明）。

### 1.4 网络架构

```
WorkBuddy / Trae / curl（验证客户端）
        │
        ▼ http://***SERVER-IP***:3000/v1   ← M0 验证入口（新）
        │
┌───────▼────────────────────────────┐
│ Docker: aihub-m0 (New API :3000)   │
└──┬──────────────┬──────────────┬───┘
   │ HTTPS        │ HTTPS        │ HTTPS
   ▼              ▼              ▼
腾讯 free        腾讯 plan      阿里百炼
tokenhub.        api.lkeap.     token-plan.cn-beijing.
tencentmaas.com  cloud.tencent  maas.aliyuncs.com
/v1              /plan/v3       /compatible-mode/v1

（生产 8787 ai-router 服务全程不动，双跑共存）
```

防火墙：轻量服务器防火墙需放行 3000 端口（腾讯云控制台操作）；M0 结束后按需关闭。

---

## 二、New API 基础验证

六项验证，逐项打勾，任何一项失败先修复再继续：

| # | 验证项 | 操作 | 通过标准 |
|---|---|---|---|
| 2.1 | 部署成功 | `docker compose up -d` + `docker ps` | 容器 Up (healthy)，日志无 panic/error |
| 2.2 | 管理后台 | 浏览器开 `http://***SERVER-IP***:3000` | 首次登录 root 初始密码成功 → **立即改密**；中文界面正常 |
| 2.3 | 数据库初始化 | `ls -la data/` + 后台"系统设置" | `one-api.db` 生成；渠道/令牌/用户菜单可访问 |
| 2.4 | 渠道创建 | 后台添加首个渠道（先用百炼，配置见 §3.3）| 渠道列表出现，状态正常 |
| 2.5 | 模型调用 | 生成令牌（Token）→ curl 调用 | HTTP 200，返回合法 JSON，内容正确 |
| 2.6 | OpenAI API 兼容 | `/v1/models` + `/v1/chat/completions`（标准 OpenAI SDK）| 标准 SDK（openai-python `base_url` 指向 3000）零改动可用 |

**2.6 验证脚本（M0 核心验收件之一）：**

```bash
# /v1/models
curl -s http://***SERVER-IP***:3000/v1/models \
  -H "Authorization: Bearer <M0测试令牌>" | python3 -m json.tool

# 非流式
curl -s http://***SERVER-IP***:3000/v1/chat/completions \
  -H "Authorization: Bearer <M0测试令牌>" -H "Content-Type: application/json" \
  -d '{"model":"<渠道内模型>","messages":[{"role":"user","content":"只回复OK"}],"max_tokens":10}'

# Python SDK 兼容
# from openai import OpenAI; client = OpenAI(base_url="http://***SERVER-IP***:3000/v1", api_key="<令牌>")
```

---

## 三、三渠道验证

三个渠道的创建参数（密钥从 v3.9 生产配置读取，即 `auto_router_live.py` 的 `DEFAULT_UPSTREAMS` / 服务器 `/opt/ai-router/config.json`，**不复制进本文档**）：

### 3.1 渠道创建参数

| 渠道 | 渠道类型 | Base URL | 密钥来源 | 模型来源 |
|---|---|---|---|---|
| 腾讯 free | OpenAI 自定义兼容 | `https://tokenhub.tencentmaas.com/v1` | v3.9 free 的 sk-m22... | 手动/接口拉取（47 免费模型中对话模型）|
| 腾讯 plan | OpenAI 自定义兼容 | `https://api.lkeap.cloud.tencent.com/plan/v3` | v3.9 plan 的 sk-tp-... | 手动（11 个套餐模型）|
| 阿里百炼 | OpenAI 自定义兼容 | `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` | v3.9 bailian 的 sk-sp-... | 手动（18 个套餐模型）|

> ⚠️ 渠道类型选择是 M0 关键验证点：New API 内置渠道类型里若无腾讯 TokenHub 专属类型，用「自定义 OpenAI 兼容」渠道 + 上述 base_url。M0 需实测该模式下：模型列表拉取、调用、流式、usage 统计四项是否全部正常。

### 3.2 每渠道统一验证清单

| # | 验证项 | 通过标准 |
|---|---|---|
| a | 模型列表获取 | 后台"获取模型列表"成功（或手动配置后渠道测试通过）|
| b | 非流式调用（点名 2 个代表模型）| HTTP 200，`usage.prompt_tokens/completion_tokens` 与上游直连结果同量级 |
| c | 流式调用 | SSE 格式正确、`data:` 块可解析、`[DONE]` 结尾；**流式 usage 统计是否计入**（New API 流式计费是历史常见坑）|
| d | Token 统计 | 管理后台日志页显示该次调用的 token 数，与响应 usage 一致 |
| e | 成本统计 | 配置模型倍率后，令牌额度按预期扣减；**扣减数值单位待实测确认**（见 §5.3）|
| f | cached 口径 | DeepSeek 系模型有缓存命中时，验证 New API 缓存计费是否正确拆分（v3.9 口径：真实 input = prompt − cached）|

### 3.3 三渠道特殊验证项

| 渠道 | 特殊验证 | 对应 v3.9 行为 |
|---|---|---|
| 腾讯 free | ① 已用尽的 3 个模型（glm-5.3 / kimi-k2.7-code-highspeed / minimax-m3）调用返回什么错误码；② 60 RPM 限流时 New API 的重试行为（连续打 20 次观察）| 402 → v3.9 会换模型；429 → 冷却 60s |
| 腾讯 plan | ① sk-tp- 套餐积分扣减是否被 New API 正确透传；② kimi-k-2-5 的 500 错误处理 | v3.9：500 → 换下一模型 |
| 阿里百炼 | ① sk-sp- 套餐价调用；② 7 天窗口额度（New API 无感知，确认由 cost-portal 旁路接管）| v3.9：超 95% 移出路由 |

### 3.4 路由机制保真度验证（M0 最高风险项）

**free_first 等价验证：**

| # | 实验 | 预期 |
|---|---|---|
| R1 | 同一模型（如 deepseek-v4-pro）同时配在 free 和 plan 两个渠道，调 20 次 | 调用应全部/绝大多数落在 free 渠道 |
| R2 | 查 New API 渠道选择机制：优先级（priority）与权重（weight）的**确切语义**（读官方文档 + 实测）| 若支持严格优先级 → 直接配置 free=高优先级；若仅权重随机 → 权重倾斜近似（free=100/plan=1）或分组隔离方案 |
| R3 | 停用 free 渠道，再调 20 次 | 全部落到 plan 渠道（故障转移）|

**auto-free 虚拟模型 PoC（渠道映射法）：**

```
思路：让多个渠道都"提供" auto-free 这个模型名，各渠道用模型映射把它转到真实模型
  - free 渠道：模型列表含 auto-free，映射 {auto-free → kimi-k2.6}
  - plan 渠道：模型列表含 auto-free，映射 {auto-free → glm-5-3}（示例）
  - 百炼渠道：模型列表含 auto-free，映射 {auto-free → qwen3.8-flash}
预期效果：请求 model=auto-free → New API 按渠道权重选一个 → 实际调用映射后的真实模型
```

验证点：
- [ ] 请求 `model=auto-free` 能否成功返回（多渠道命中其一）
- [ ] 响应里的 `model` 字段显示什么（真实模型名还是 auto-free）——影响客户端兼容
- [ ] 多次调用是否呈现权重分布
- [ ] 此法扩展出 `auto-vision` / `auto-strong` 两个变体虚拟模型，即可等价实现 v3.9 三池（普通/视觉/强任务）——M0 验证 1 个即可证明可行性

**失败降级路径：** 若渠道映射法不成立（如 New API 不支持同模型名跨渠道映射），auto-free 改走 smart-router 前置网关方案（Python 层先决策、注入真实模型名再转发 New API）——架构可行但多一跳，属于 M1 设计输入，M0 只需确认是否需要启用此备选。

---

## 四、Auto Router 能力验证（增强层四件套）

### 4.1 tc_cloud.py 旁路接入验证

**方式：服务器独立 venv 直跑，与 New API 零耦合。**

```bash
# 服务器上（不动生产 venv /opt/ai-router/venv）
python3 -m venv /opt/ai-hub/m0/tcenv
/opt/ai-hub/m0/tcenv/bin/pip install requests
# 拷贝 tc_cloud.py + tc_cloud.json（真实密钥，从 /opt/ai-router/ 拷贝，权限 600）
/opt/ai-hub/m0/tcenv/bin/python -c "
import tc_cloud, json
ok, r = tc_cloud.free_tokens_summary(force=True)
print('免费包:', r['count'], '个, 消耗', round(r['pct'],1), '%')
ok, o = tc_cloud.plan_order(force=True)
print('套餐周期:', o.get('cycle_start'), '→', o.get('cycle_end'))
ov = tc_cloud.get_overview(force=True)
print('prices:', ov['prices']['count'], '| peak:', len(ov['peak']['items']))
"
```

**通过标准**：与第一阶段实测数据一致（免费包 20 个 token 类 + payment_summary 47、套餐周期 2026-08-29 → 09-29、单价 19-20 模型、水位 96 模型）。此项是纯复用验证，**预期 100% 通过**（同一份代码同一份密钥）。

M1 输入：tc-cloud-sync 服务化方案（定时拉取 → 写入独立 SQLite → New API 外挂面板读），M0 不做服务化。

### 4.2 smart-router 插件方案验证

M0 验证其**最低可行形态**：§3.4 的 auto-free 渠道映射法若通过，smart-router 在 V4.0 就退化为「配置生成器」（Python 脚本根据三池规则批量生成渠道的模型映射配置），无需常驻进程。

验证点：
- [ ] 用脚本生成 free 渠道的 37 个对话模型 → 3 个虚拟模型（auto-free/auto-vision/auto-strong）的映射配置草案
- [ ] 配置导入 New API 后调用通过

### 4.3 auto-free 虚拟模型方案

已并入 §3.4（渠道映射法 PoC），此处不重复。产出物：映射配置样例 + 调用记录。

### 4.4 _router 路由标记兼容方案

**预期结论（待实测确认）：** New API 对上游响应做计费封装转发，自定义 `_router` 字段大概率不透传。

替代方案验证（M0 二选一或都验）：
- [ ] **方案 A（优先）**：调用后查 New API 日志 API / 管理后台，确认日志含：请求模型、实际渠道、实际模型、token 数、耗时、状态——若字段齐全，验证脚本（verify_plan_panel.py 的 V4 版）改从日志侧断言
- [ ] **方案 B**：确认响应体里 New API 是否注入自有字段（如渠道/模型信息）可直接用

**通过标准**：能拿到"这次 auto-free 调用实际命中了哪个渠道哪个模型"，断言方式与 v3.9 的 `_router` 等价。

---

## 五、Quota 积分引擎验证（设计定稿 + 一次闭环 PoC）

### 5.1 三层模型（定稿）

```
┌─────────────────────────────────────────────┐
│ L1 Provider 真实成本层                        │
│   腾讯: tc_cloud.model_prices() 19-20 模型官方单价│
│   百炼: BAILIAN_TIER 单价表（已有）             │
│   单位: 元/百万token（in/out/cache 三价）        │
├─────────────────────────────────────────────┤
│ L2 倍率层（三层倍率，全部预留）                 │
│   模型倍率: 模型复杂度系数（New API model_ratio）│
│   渠道倍率: free=0 / plan=套餐折扣 / bailian=系数 │
│   用户组倍率: V4.0 恒 1.0，V4.1 分组差异化       │
├─────────────────────────────────────────────┤
│ L3 积分 Quota 层                              │
│   New API 原生 quota 字段（单位待 M0 实测）      │
│   → 用户余额（V4.0 单管理员，预留多用户）         │
└─────────────────────────────────────────────┘
```

**核心决策：不造轮子，New API 原生 quota 体系 = Quota 引擎的执行与存储层。**
依据：New API 本身就是「模型倍率 × 分组倍率 → quota 扣减 → 余额」的完整实现（One API 继承）。自研部分只剩：**换算系数生成器 + 对账（reconciliation）**。这符合"优先成熟开源方案"的开发原则。

### 5.2 数据模型设计（V4.0 预留，V4.1/V4.2 启用）

**New API 原生表（直接复用，不改动）：**

| 表 | 用途 | 启用版本 |
|---|---|---|
| users | 用户/余额（quota 字段）| V4.0（仅管理员）|
| tokens | 令牌/额度/模型限制 | V4.0 |
| channels | 渠道/倍率/权重/优先级 | V4.0 |
| logs | 调用日志：model_name、quota、prompt/completion tokens、耗时 | V4.0 |
| redemptions | 兑换码（积分兑换预留）| V4.2 |

**自研增强表（cost-portal 的 SQLite，独立于 New API 数据库）：**

| 表 | 字段（核心） | 用途 | 启用版本 |
|---|---|---|---|
| price_book | provider, model, input/out/cache 三价, 生效时间 | L1 真实成本权威表（替代写死在代码）| V4.0 |
| quota_ledger | ts, user, token, model, channel, real_cost(元), ratio_product, quota_delta | 消耗记录 + **成本归因**（同一行同时记真实成本和积分扣减）| V4.0 |
| plan_windows | plan 周期/780积分水位, bailian 7 天窗口 | 套餐额度（tc-cloud-sync 写入）| V4.0 |
| group_ratios | group, ratio, 生效时间 | 用户组倍率（V4.1 启用）| 预留 |

### 5.3 与 New API 倍率体系的关系（M0 必须实测确认的三件事）

| # | 待确认事实 | 验证方法 | 影响 |
|---|---|---|---|
| Q1 | **quota 精确单位与换算**（1 quota = 多少元/美元？）| 建渠道配一个已知单价模型（如 tc_prices 里 glm-5 输入 0.324 元/百万token），调用 1000 token，读 logs 表 quota 扣减值反推 | 决定换算系数生成器公式 |
| Q2 | 模型倍率/分组倍率的配置入口与计算顺序（先乘后乘、是否含缓存计费）| 后台配置 + 对照实验 | 决定 L2 层映射方式 |
| Q3 | free 渠道如何表达"成本为零"（渠道倍率=0 是否合法、额度是否扣减）| free 渠道倍率置 0 调用观察 | 决定免费源是否耗积分（设计目标：**free 调用不扣积分余额，但 quota_ledger 仍记真实成本用于归因**）|

### 5.4 成本 → 积分转换逻辑（V4.0 定稿公式）

```
一次调用的积分扣减（New API 自动执行）:
  quota_delta = f(模型倍率, 渠道倍率, 分组倍率, tokens)     ← 基座原生

一次调用的真实成本（cost-portal 对账计算）:
  real_cost = (in − cached)/1e6 × P_in + cached/1e6 × P_cache + out/1e6 × P_out
              （P_* 来自 price_book，口径延续 v3.9 踩坑结论）

两者写入同一行 quota_ledger → 任意时刻可算:
  积分消耗/真实成本 比 = 平台倍率水位（V4.2 成本控制的核心指标）
```

### 5.5 演进方案

| 版本 | 能力 | 实现载体 |
|---|---|---|
| **V4.0** | 内部 Quota 引擎：数据模型全预留 + 换算系数生成器 + quota_ledger 对账跑通；单管理员；不注册不充值 | New API 原生 quota + cost-portal |
| **V4.1** | 小范围用户：用户管理/Key 分配/积分额度管理（分组倍率启用）| New API 原生 users/tokens + group_ratios 启用 |
| **V4.2** | 运营能力：套餐体系/积分兑换/成本控制/用户运营 | New API redemptions + cost-portal 运营面板 |

---

## 六、M0 执行计划（3 天）

### Day 1（9-02 周二）：环境 + New API 基础

| 时段 | 任务 | 产出 |
|---|---|---|
| 上午 | 服务器资源盘点（§1.1 检查表）；Docker/防火墙确认 | 资源清单 + Go 判定 |
| 下午 | Docker Compose 部署 New API；后台初始化；SQLite 验证（§2.1-2.3）| 容器运行截图 + 后台可访问 |
| 傍晚 | 创建首个渠道（百炼）；首次调用打通（§2.4-2.6）| 首次 200 响应记录 |

**Day 1 验收**：§2 六项全过。不过夜排障，卡住超 2 小时即记录降级评估。

### Day 2（9-03 周三）：三渠道 + 路由保真度（M0 核心）

| 时段 | 任务 | 产出 |
|---|---|---|
| 上午 | 三渠道全部创建；每渠道非流式/流式/Token/成本四项（§3.2-3.3）| 三渠道验证矩阵 |
| 下午 | R1-R3 free_first 实验（§3.4）；auto-free 渠道映射法 PoC | 渠道选择机制结论 + PoC 调用记录 |
| 傍晚 | Q1-Q3 quota 单位实测（§5.3）；60 RPM 限流行为观察 | 换算系数公式定稿 |

**Day 2 验收**：三渠道四项全过 + R1/R3 行为明确 + auto-free PoC 成败结论 + Q1 单位确认。**这是 Go/No-Go 最多证据的一天。**

### Day 3（9-04 周四）：增强层 PoC + 定稿

| 时段 | 任务 | 产出 |
|---|---|---|
| 上午 | tc_cloud.py 服务器旁路验证（§4.1）；_router 替代方案验证（§4.4）| 旁路数据截图 + 日志 API 字段清单 |
| 下午 | smart-router 配置生成器原型（§4.2）；Quota 引擎闭环 PoC（一次调用 → real_cost + quota_delta 双记录）| 映射配置草案 + ledger 样例行 |
| 傍晚 | 汇总《M0 选型验证报告》：逐项结论 + Go/No-Go + M1 输入清单 | M0 报告（正式文档）|

**Day 3 验收**：四件套 PoC 全有结论 + M0 报告完成。

### M0 总验收标准（Go 判定）

1. §2 六项全过（New API 基座可行）
2. 三渠道 §3.2 四项全过（接入保真）
3. R1/R3 有明确结论且 free_first 可表达（严格优先级 / 权重倾斜 / 分组，三选一）
4. auto-free 映射法有明确成败结论（成败都可进 M1，只是方案不同）
5. Q1-Q3 实测数据在手（Quota 引擎公式定稿）
6. tc_cloud.py 旁路数据与第一阶段实测一致
7. 生产服务 8787 全程无影响（`systemctl status ai-router` + `/health` 每日检查）

### 风险清单

| # | 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|---|
| 1 | 服务器资源不足装 Docker | 中 | M0 延迟 | Day1 首查；降级本地 Docker Desktop（网络用公网 key，验证同样有效）|
| 2 | New API 渠道优先级语义不满足严格 free_first | 中 | 路由保真度 | 权重倾斜（free=100/plan=1）近似 or 分组隔离；R2 实测定方案 |
| 3 | auto-free 映射法不成立 | 中 | 架构调整 | smart-router 前置网关备选（M1 设计输入）|
| 4 | 腾讯 TokenHub 非标准响应导致渠道异常 | 低 | 渠道验证失败 | 自定义渠道模式 + 逐项抓包比对 v3.9 直连响应 |
| 5 | 流式 usage 统计缺失 | 中 | 计费不准 | New API 该问题社区有历史记录，实测确认；不行则流式禁用该渠道或等版本修复 |
| 6 | 影响生产 8787 | 低 | 业务中断 | 隔离原则：独立端口/目录/容器名；不动 systemd；每日 /health 巡检 |
| 7 | 9-29 到期窗口挤压 | 确定 | 时间压力 | M0 纯验证可复用；若 9-10 前 M1 未启动，先决策续费 |
| 8 | 密钥泄露面扩大 | 低 | 资损 | 密钥仅存服务器 .env/渠道配置；不进 git/文档/聊天记录；M0 测试令牌用后即删 |

---

## M0 交付物清单

1. 《M0 选型验证报告》（Go/No-Go + 逐项证据）
2. docker-compose.yml 定稿（M1 直接复用）
3. 三渠道配置参数表（脱敏版）
4. auto-free 映射配置草案（smart-router V4.0 形态）
5. Quota 引擎换算公式 + quota_ledger 表结构定稿
6. M1 开发输入清单（含所有 M0 未决项的决策）

---

*下一步：按 Day 1 计划开始执行。执行前提确认：①服务器 Docker 可装/已装；②3000 端口可用；③密钥从生产 config.json 就地读取。*
