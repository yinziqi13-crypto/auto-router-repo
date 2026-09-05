# Auto Router AIHub 升级改造项目 · 第一阶段技术分析报告

**版本：** V4.0 升级预研 · 阶段一
**日期：** 2026-09-02
**分析对象：** AI 智能路由看板 v3.9（`auto_router_live.py`，141KB / 约 2900 行）
**线上服务：** `http://***SERVER-IP***:8787`
**分析方式：** 源码精读 + 线上 API 实测（非推测）

---

## 〇、模型接入源现状（权威口径，孙磊确认）

系统当前聚合 **3 个上游接入源**，可用模型合计 **76 个**：

| # | 接入源 | 可用模型数 | 说明 |
|---|---|---|---|
| 1 | 腾讯 TokenHub · Token Plan 个人版 | **11** | 780 积分/月，39 元/月，周期 2026-08-29 ~ 09-29 |
| 2 | 阿里百炼 Lite 套餐 | **18** | 首调起算，达额度后等 7 天重置，周期内不结转 |
| 3 | 腾讯 TokenHub 免费体验 | **47** | 每模型赠 1,000,000 token，大部分到期 2027-08 |

**免费体验源 47 个模型的构成（API 实测 `DescribePaymentSummary`：`free_total=47, free_claimed=47`）：**
- 20 个 token 文本类（`DescribeFreeTrialPackageList`）
- 27 个视觉/视频/图像/语音类（`DescribeVisionFreeTrialPackageList`）

**免费体验源已用尽 3 个（状态停止）：** `glm-5.3`、`kimi-k2.7-code-highspeed`、`minimax-m3`。

> ⚠️ **数量口径澄清（重要）**：免费体验源 `/v1/models` 原始返回 **122 个**（96 online），这是上游全量模型清单（含大量非对话的视频/3D/图像/语音/向量模型），**不是**"可用免费额度模型"。路由层用 `NON_CHAT_KEYWORDS` 黑名单过滤后，仅 37 个进入对话模型候选，三源合并去重后 **51 个进路由**。对外 `/v1/models` 返回 52（含 `auto-free` 别名）。**"76 个可用模型"是业务口径，"51 个进路由"是运行时口径，两者不矛盾。**

---

## 一、当前项目技术架构

### 1.1 架构总览

```
                    ┌─────────────────────────────────────────┐
                    │         客户端（WorkBuddy / Trae / SDK）  │
                    │   base_url = http://***SERVER-IP***:8787/v1│
                    └───────────────────┬─────────────────────┘
                                        │ OpenAI 兼容协议
                                        ▼
                    ┌─────────────────────────────────────────┐
                    │        AI 路由服务（单进程 Flask）         │
                    │  /v1/chat/completions  /v1/models  /health│
                    │  /dashboard /admin/* (27 端点) /stats      │
                    │                                          │
                    │  ┌───────────────────────────────────┐   │
                    │  │  路由核心：resolve_model → pick_model│   │
                    │  │  三池：vision / strong / normal      │   │
                    │  │  加权轮询 + 健康度 + 额度降权          │   │
                    │  └───────────────────────────────────┘   │
                    │  语义缓存(内存) · 熔断冷却 · 自动发现线程   │
                    └───────┬───────────────┬───────────────┘
                            │               │               │
                    ┌───────▼──────┐ ┌──────▼──────┐ ┌──────▼──────┐
                    │ 腾讯 free    │ │ 腾讯 plan   │ │ 阿里百炼     │
                    │ 免费体验47个  │ │ 个人版11个  │ │ Lite套餐18个 │
                    └──────────────┘ └─────────────┘ └─────────────┘
```

### 1.2 部署形态

| 项 | 值 |
|---|---|
| 服务器 | 腾讯云轻量，IP `***SERVER-IP***`，TencentOS 4 |
| Python | 3.11.6（服务器）/ 3.13.12（本地 managed） |
| WSGI | gunicorn `--workers 1 --threads 8 --timeout 300` |
| 端口 | 8787（`0.0.0.0:8787`） |
| 进程管理 | systemd 服务 `ai-router` |
| 到期日 | **2026-09-29**（服务器 + 套餐 + 免费 token 同步到期）|

### 1.3 架构特征（决定升级路线的事实）

**单进程内存态架构**，这是当前最核心的约束：
- 健康度、用量统计、语义缓存、熔断冷却全部存在**进程内存**（`stats`/`usage`/`cache` 全局 dict）
- 因此 gunicorn **强制 1 worker**，多进程会导致各算各的
- 重启即丢失健康度评分（用量/配置靠 JSON 落盘，健康度不持久化）

---

## 二、前后端技术栈

### 2.1 前端

| 项 | 现状 |
|---|---|
| 框架 | **无**（Flask 内联 f-string 模板，`dashboard()` 函数一次性拼完整 HTML）|
| 样式 | 纯 CSS，内联 `<style>`，无外部库 |
| 脚本 | 原生 JS，内联 `<script>`，无框架 |
| 图表 | 纯 CSS 堆叠条 + 进度条（无 Chart.js/ECharts）|
| 主题 | 黑背景 `#000`，主色 `#5DCAA5`，v3.9 凌天AI风格 |
| 刷新 | `<meta http-equiv="refresh" content="30">` 30 秒整页刷新 |
| 交互 | `adm()`/`startTest()`/`fillMp()`/`tcPost()` 原生 fetch |

### 2.2 后端

| 项 | 现状 |
|---|---|
| 语言 | Python 3.11+ |
| 框架 | Flask 2.x（无 ORM、无蓝图、单文件）|
| HTTP 客户端 | requests |
| 并发 | threading（探测/告警/发现线程）+ ThreadPoolExecutor(4) |
| 签名 | `tc_cloud.py` 纯标准库 TC3-HMAC-SHA256（不装腾讯云 SDK）|
| 依赖 | flask / requests / paramiko（仅 3 个）|

---

## 三、数据库结构

**无传统数据库**，纯 JSON 文件持久化（`/opt/ai-router/`）：

| 文件 | 内容 | 关键结构 |
|---|---|---|
| `config.json` | 上游/密钥/设置/服务器/套餐窗口 | `upstreams[]`、`settings`、`discovered`、`disabled_models[]`、`bailian_window`、`alert` |
| `usage.json` | token 用量统计 | `total_*`、`models{}`、`by_upstream{}`、`by_upstream_model{}`、`plan_cycle{}` |
| `tc_cloud.json` | 腾讯云 API 密钥 | `secret_id`/`secret_key`（权限 600）|
| `logs/YYYY-MM-DD.jsonl` | 每日调用日志 | JSON Lines，循环双文件 |

**关键数据口径（踩坑记录，必须保留）：**
- `prompt_tokens` **已含** `cached_tokens`；真实 input 计费 = `prompt_tokens - cached_tokens`
- `by_upstream_model` 是**嵌套字典**，不是数组
- 套餐已用额度官方 API 查不到（7 条独立证据），用「本地实测 token × 官方单价」折算

---

## 四、核心业务流程

### 4.1 AI 请求处理（`chat()` 主流程）

```
POST /v1/chat/completions
  → 鉴权（Bearer Key，config.api_keys）
  → 语义缓存命中？（1h TTL，相同请求直返）
  → resolve_model(body, messages)  判定模式
       ├─ 点名池内模型 → explicit（尊重）
       ├─ 容错匹配命中 → explicit_fuzzy
       └─ auto-free/瞎编 → auto（自动选路）
  → 最多 4 次尝试循环
       ├─ attempt 0：点名模型优先
       ├─ attempt 1-3：pick_model 自动选路
       └─ 每次遍历该模型所有上游（free→plan→bailian）
  → 200 → record_success + record_usage + 封装 _router 头
  → 429 → 冷却 60s；400/401/403/404/500 → 换模型
  → 全失败 → 503 "all models failed"
```

### 4.2 路由选路（`pick_model`）

```
1. 按消息类型选池：有图→vision / 强任务→strong / 其他→normal
2. 候选模型权重 = health × discount × quota_factor × upstream_pref
   - health：最近 20 次指数衰减
   - discount：9月限时(GL M-5.3系×3) / 夜间5折
   - quota_factor：额度剩余（>95% 归零）
   - upstream_pref：free=1.6 / plan=1.0 / bailian=0.8
3. strict free_first：有免费模型可用就绝不用套餐/百炼
4. random.choices 按权重抽样
```

### 4.3 Token 统计（`record_usage`）

- 按「上游 × 模型」拆分 in/out/cached
- 套餐周期跟踪（`plan_cycle`，周期变自动清零）
- 百炼 Credits 折算（`BAILIAN_TIER` 单价表）

---

## 五、当前 AI 路由实现方式

| 能力 | 实现 | 成熟度 |
|---|---|---|
| 多模型聚合 | 3 上游硬编码于 `DEFAULT_UPSTREAMS` + 自动发现 | ✅ 可用 |
| 模型池分类 | 视觉/强/普通三池（`VISION_PATTERNS`/`STRONG_PATTERNS` 子串匹配）| ✅ 可用 |
| 加权轮询 | `random.choices(weights)` | ✅ 可用 |
| 健康度降权 | 最近 20 次指数衰减 | ✅ 可用（不持久化）|
| 额度降权 | 免费>95%/百炼>95% 归零 | ✅ 可用 |
| 熔断冷却 | 连续失败 3 次冷却（429=60s，其他=300s）| ✅ 可用 |
| 点名模型 | v3.8 修复，容错匹配（大小写/去前缀/前缀命中）| ✅ 可用 |
| 故障转移 | 最多 4 次尝试 | ✅ 可用 |
| 语义缓存 | 内存 dict，1h TTL | ✅ 可用（单进程）|
| 流式 SSE | 原样转发 + usage 解析 | ✅ 可用 |

**路由的"智能"本质**：**规则驱动 + 加权随机**，非模型驱动（无 LLM 参与选路判断）。任务类型判定靠关键词/长度，成本/速度目前是**静态权重**（`upstream_pref`/`discount_weight`），**没有**基于实时价格的动态成本路由，**没有**基于实测延迟的速度路由。

---

## 六、可以复用的模块

| 模块 | 文件/函数 | 复用方式 | 价值 |
|---|---|---|---|
| TC3-HMAC-SHA256 签名 | `tc_cloud.py::_sign()` | **原样复用**，这是最值钱的部分 | 高 |
| 腾讯云 API 封装（25 函数）| `tc_cloud.py` | 复用：免费包/单价/水位/订单/用量 | 高 |
| 路由核心算法 | `pick_model`/`resolve_model`/`_fuzzy_match_model` | 抽成独立 router 模块 | 高 |
| 模型探测/自动发现 | `probe_model`/`discover_all`/`rebuild_pools` | 复用（需从内存态抽离）| 高 |
| Token 统计口径 | `record_usage`/`track_plan_cycle` | 复用（cached 口径已踩坑）| 高 |
| 熔断/冷却/健康度 | `record_fail`/`health_score` | 复用（需持久化改造）| 中 |
| 语义缓存 | `cache` + `cached_key` | 复用（需换 Redis）| 中 |
| 告警（SMTP/Webhook）| `send_alert`/`alert_worker` | 复用 | 中 |
| 百炼窗口管理 | `check_bailian_window` | 复用 | 中 |
| 部署脚本模式 | `deploy_v39.py` | 复用模式（备份→上传→校验→重启→验证→回滚）| 中 |

---

## 七、改造成 AI Hub 需要新增的模块

对照改造方向，**当前缺的能力**和对应新增模块：

| 新增模块 | 说明 | 优先级 |
|---|---|---|
| **1. Provider 抽象层** | 把"3 个硬编码上游"改成可注册的 Provider 插件（OpenAI/GPT/Claude/Gemini/DeepSeek/国内），统一 base_url/协议差异 | 🔴 P0 |
| **2. API Key 管理** | 当前 `config.api_keys` 是简单列表，需升级为多用户 Key + 配额 + 权限（用户级限额）| 🔴 P0 |
| **3. 数据库层** | JSON 文件 → 关系型（SQLite 起步 / PostgreSQL），用户/Key/Provider/用量/日志 结构化 | 🔴 P0 |
| **4. 统一 Gateway 路由** | 当前只有 `/v1/chat/completions`，需扩展到 embeddings/images/audio 等全协议端点 | 🟡 P1 |
| **5. 成本计算引擎** | 当前套餐折算写死在代码里，需通用化：每 Provider×模型×计费项 的价格表 + 动态成本路由 | 🟡 P1 |
| **6. 动态速度路由** | 当前速度是静态偏好，需实测延迟反馈闭环 | 🟡 P1 |
| **7. 前后端分离** | f-string 模板 → 独立前端（Vue/React）+ REST API | 🟡 P1 |
| **8. 状态共享层** | Redis 替代内存 dict（健康度/缓存/用量跨进程共享，解除 1-worker 限制）| 🟡 P1 |
| **9. 模型性能分析** | 健康度持久化 + 按模型×任务的性能报告 | 🟢 P2 |
| **10. Docker 化** | Dockerfile + compose，一键部署 | 🟢 P2 |

---

## 八、与 LiteLLM / One API / New API / OpenRouter 的差异分析

| 维度 | 当前系统 v3.9 | LiteLLM Proxy | One API / New API | OpenRouter |
|---|---|---|---|---|
| **定位** | 个人自用聚合路由 + 看板 | 开源 LLM 网关（Python）| 开源 API 聚合分发（Go）| 商业 LLM 聚合市场 |
| **Provider 支持** | 3 个（硬编码）| 100+ Provider | 主流厂商 + 自定义 | 100+ 模型 |
| **协议兼容** | OpenAI `/v1/chat/completions` | OpenAI + Anthropic + 更多 | OpenAI 格式 | OpenAI 格式 |
| **路由能力** | 加权轮询 + 健康度（自研）| fallback/retry/负载均衡/预算 | 渠道分组/权重/重试 | 动态路由/按成本 |
| **成本计算** | 套餐折算（自研，针对腾讯）| 通用 cost tracking | 倍率折算 | 实时价格 |
| **多用户/Key** | ❌ 无（单一 Key）| ✅ 虚拟 Key | ✅ 多用户多 Key 分级 | ✅ 按量付费 |
| **数据库** | ❌ JSON 文件 | 内存+Redis | ✅ MySQL/SQLite | ✅ 平台托管 |
| **看板** | ✅ 自研（黑背景）| 有（LiteLLM UI）| ✅ 有 | ✅ 有 |
| **企业级** | ❌ 无认证体系/限流/审计 | 部分 | ✅ 较完整 | ✅ SaaS |
| **部署复杂度** | 单文件 Flask，简单 | 中等（需 Redis）| 中等（Go 单二进制）| 无（SaaS）|

**核心判断：**
1. 当前系统本质是 **LiteLLM 的"极简自研版"**，路由思想（fallback/健康度/额度）与 LiteLLM 同源，但规模小一个数量级。
2. **One API / New API 是最贴近目标形态的开源方案**——它们就是"多 Provider 聚合 + 多用户 Key + 计费倍率 + 看板"的完整实现，正是项目说明书里写的"未来希望发展成"的样子。
3. **不建议从零造轮子**。两条路线：
   - **路线 A（推荐）**：直接以 **New API（One API 的增强版）** 为基础部署，把现有自研的「腾讯云用量查询（tc_cloud.py 的 25 个函数）+ 三池路由 + 套餐折算」作为**上游渠道/自定义能力**接入。复用成熟的多用户/Key/计费/看板，保留自研的腾讯云专用价值。
   - **路线 B（保底）**：若坚持自研，把当前 v3.9 的**路由核心 + tc_cloud.py** 抽成独立模块，补齐 Provider 抽象 + 数据库 + 多用户，本质上是"重写一个 One API"——工作量大且易踩坑，**不推荐**。

---

## 九、V4.0 升级路线图

### 阶段一（已完成）：技术分析
- ✅ 源码精读、架构梳理、模型数量核实、测试报错排查、竞品对比

### 阶段二：选型决策（本阶段出口）
- [ ] **决策点：自研 vs 基于 New API 二次开发**（建议路线 A，需孙磊拍板）
- [ ] 确认 2026-09-29 到期风险处置（续费/迁移）

### 阶段三：数据层改造（P0）
- [ ] JSON 文件 → 数据库（SQLite 起步，PostgreSQL 预留）
- [ ] 用户表 / Key 表 / Provider 表 / 用量表 / 日志表 schema 设计
- [ ] 迁移脚本（usage.json / config.json 无损导入）

### 阶段四：Provider 抽象 + Gateway（P0）
- [ ] Provider 注册机制（替换硬编码 upstreams）
- [ ] 接入 GPT/Claude/Gemini/DeepSeek/国内（协议适配）
- [ ] 扩展端点（embeddings/images/audio）

### 阶段五：路由增强（P1）
- [ ] 通用成本引擎（Provider×模型×计费项价格表）
- [ ] 动态速度路由（实测延迟反馈）
- [ ] 保留现有三池 + 点名模型 + 容错匹配能力

### 阶段六：多用户 + 看板分离（P1）
- [ ] 多用户 API Key + 配额 + 限流
- [ ] 前后端分离（Vue/React + REST API）
- [ ] 保留 v3.9 看板视觉风格（黑背景 #5DCAA5）

### 阶段七：企业级 + 部署（P2）
- [ ] Redis 状态共享（解除 1-worker）
- [ ] 健康度持久化 + 性能分析报告
- [ ] Docker 化 + 一键部署
- [ ] 主备多节点（远期）

---

## 附：测试报错排查结论（已闭环）

探测 51 个进路由模型，**失败 2 个**，均已定位：

| 模型 | 报错 | 根因 | 处置 |
|---|---|---|---|
| `kimi-k-2-5` | HTTP 500 `model engine error` | plan 上游模型引擎故障 | 非路由问题，等上游恢复 |
| `kimi-k2.7-code-highspeed` | HTTP 402 `free trial quota exhausted` | 免费额度耗尽 | 正常（孙磊已确认这 3 个用尽）|

免费体验源用尽 3 个模型：`glm-5.3`、`kimi-k2.7-code-highspeed`、`minimax-m3`（API 实测 minimax-m3 为 UsedUp 状态）。

---

*报告生成：2026-09-02 · 数据来源：源码精读 + 线上 API 实测*
