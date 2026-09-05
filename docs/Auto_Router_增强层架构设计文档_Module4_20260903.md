# Auto Router 增强层架构设计文档（Module 4）

**日期**：2026-09-03
**项目**：Auto Router AIHub V4.0 · M0 选型验证之后
**设计角色**：软件架构师（主笔）· AI 工程师（模型语义视角）· 后端架构师（部署/可观测视角）联合评审
**设计输入**：《第二阶段产品规划》+ M0 Day1/Day2 全量实测证据（渠道路由机制最终报告、免费额度耗尽实验报告）
**状态**：待孙磊确认，确认后进入开发（本文档不含代码实现）

---

## 一、设计输入：已被实测钉死的事实

架构不是从白纸开始。以下事实来自 M0-M3 真实实验，是本设计不可违背的地基：

| # | 实测事实 | 对架构的约束 |
|---|---|---|
| 1 | New API 路由读 **abilities 表**（group+model 精确匹配 → priority 分组 → 组内 weight 加权随机） | 增强层可通过 New API 管理 API 配置 abilities 来"编程"路由，无需改源码 |
| 2 | 连接失败/5xx **触发重试**并降级到下一 priority 渠道 | 传输层故障交给 New API 原生处理，增强层不重复造轮子 |
| 3 | **402（额度耗尽）触发重试但不跨模型池**（候选池内无兜底则透传 402） | 额度降级必须由增强层在"请求前"决策，不能指望请求后重试 |
| 4 | **400（无效模型）不重试、直接失败** | 跨上游模型名差异必须提前用 model_mapping 解决，运行时无法自愈 |
| 5 | 腾讯 plan 接入必须 type=8 + 完整 URL（无 /v1 段） | 渠道接入规范固化，新上游先测路径拼接 |
| 6 | 耗尽模型**不会自动下线**；无额度查询 API（404） | 额度状态只能靠：①tc-cloud-sync 腾讯云控制台数据 ②402 事件被动感知，二者结合 |
| 7 | `channels.status` 与 `abilities.enabled` 需同步改（SQL 直改不同步） | 增强层一律走 **New API 管理 API**（自动同步），禁止直接写 SQLite |
| 8 | **rc.30 自定义 group 路由不可用**（M1-0 实测）：`token.group`=`free` + 渠道挂 `group=free` → 403；用户级分组权限只认 `default` 等内置组；请求级 body.group 仅在 `/pg/` 路径生效 | free/paid 分流**不能依赖 group 机制**，ADR-002 锁定双 token 方案 |
| 9 | **model_mapping 可把同一逻辑模型名指向不同上游真实名**（M1-0 补充实测：glm-5.2 → ch1 映射 deepseek-v4-flash-202605 生效） | 双 token 方案里"同一模型不同上游"用 model_mapping 落地 |

**核心命题**：New API 是一个能力完备但"没有腾讯云领域知识"的通用网关。增强层的本质 = **把腾讯云免费额度、套餐积分、百炼窗口这些领域知识，翻译成 New API 能执行的路由配置（group / priority / weight / model_mapping）**，并在运行时持续维护这个翻译。

---

## 二、职责边界：谁干什么

### 2.1 边界总表

| 能力域 | New API 基座 | Auto Router 增强层 |
|---|---|---|
| 协议接入（OpenAI 兼容/流式） | ✅ | — |
| 协议转换（OpenAI⇄Claude/Gemini，未来） | ✅ | — |
| 渠道管理（增删改、密钥） | ✅（界面+API） | 通过 API 自动同步配置 |
| 令牌/用户/额度扣减 | ✅ | — |
| 计费倍率、调用日志、看板 | ✅ | cost-portal 做**套餐折算**（New API 不懂 780 积分） |
| 候选池内重试、失败切渠道（传输层） | ✅（RetryTimes） | — |
| **额度耗尽（402）前的主动降级** | ❌ 无此能力 | ✅ 请求前决策（核心职责 1） |
| **耗尽模型的自动下线/恢复** | ❌ 不会自动做 | ✅ 状态机（核心职责 2） |
| **腾讯云额度数据同步**（免费包/积分/百炼窗口） | ❌ | ✅ tc-cloud-sync（核心职责 3） |
| **auto-free 虚拟模型/任务感知选型** | ❌ | ✅ smart-router（核心职责 4） |
| **跨上游模型名映射维护** | ✅ 执行映射 | ✅ 发现并配置映射（deepseek-v4-flash 教训） |
| 免费源 60 RPM 限流保护 | ❌（只有用户级） | ✅ 渠道级速率窗口 |

### 2.2 三条红线（架构约束）

1. **不改 New API 源码**——AGPL-3.0 规避 + 升级自由度（rc.30 还在快速迭代）
2. **不直连上游**——所有模型流量必须经 New API，保证计费日志、协议转换、重试能力完整（增强层直连 = 复刻 v3.9 的老路）
3. **不直接写 New API 的 SQLite**——只走管理 API，避免 channels/abilities 不同步类事故（M0 实测教训）

### 2.3 一个请求的职责接力

```
客户端 → [增强层] 令牌校验·限流·模型解析·额度决策·选group
       → [New API] 按group+model路由·渠道重试·协议转换·计费记账
       → [增强层] 解析结果·402/500事件回写状态表·透传响应(+_router标记)
       → 客户端
```

增强层是**薄代理**：只加决策，不碰字节流（流式响应直接管道透传）。单请求增强层开销目标 < 5ms（一次内存状态表查询）。

---

## 三、额度状态管理模型（领域模型）

### 3.1 实体关系

```
Provider (1) ──< Channel (1) ──< ChannelModel
                                      │
ModelCatalog (1) ──< ModelCapability    │
      │                                │
      └────────< QuotaState >──────────┘
                    │
              ErrorEvent (事件流)
              RouterDecisionEvent (决策事件流)
```

### 3.2 实体字段设计

**Provider（供应商）** —— 对应上游账号

| 字段 | 说明 | 示例 |
|---|---|---|
| provider_id | 主键 | tencent-free / tencent-plan / bailian |
| quota_type | 额度模型 | free_quota（免费池）/ points（积分制）/ window（7天窗口） |
| quota_sync_method | 数据源 | tc-cloud-api / passive_402（被动）/ manual |
| rpm_limit | 速率上限 | 60（腾讯免费实测值） |

**Channel（渠道）** —— 与 New API channel 一一对应，增强层只存引用

| 字段 | 说明 |
|---|---|
| channel_id | New API 渠道 ID（1/2/3） |
| provider_id | 所属供应商 |
| health | healthy / degraded / down（滚动错误率计算） |
| last_test_at / latency_p50 | 渠道探测数据 |

**QuotaState（额度状态）** —— 核心聚合，粒度 = (provider, model)

| 字段 | 类型 | 说明 |
|---|---|---|
| provider_id + model | 联合键 | 例：(tencent-free, kimi-k3) |
| quota_status | enum | **available**（可用）/ **suspected_low**（tc-cloud 报低）/ **exhausted**（402 实锤）/ **unknown**（从未探测） |
| exhausted_at | datetime | 首次观测到 402 的时刻 |
| error_status | enum | none / cooldown / probing / circuit_open |
| cooldown_until | datetime | 冷却截止时间（指数退避） |
| consecutive_errors | int | 连续错误计数（成功清零） |
| health_score | 0-100 | 综合评分 = 额度状态 × 错误率 × 延迟 |
| last_success_at | datetime | 最近一次成功调用 |
| source_of_truth | enum | tc_cloud_sync / event_402 / probe（记录当前状态由谁判定，冲突时按优先级仲裁） |

**状态判定优先级**（冲突仲裁，铁律）：
`402 实锤事件 > tc-cloud-sync 主动数据 > 探针推断`。
理由：402 是上游亲口说的；控制台数据有同步延迟；探针可能遇到限流假象。

**ModelCatalog（模型目录）** —— 跨供应商的"逻辑模型"视图

| 字段 | 说明 |
|---|---|
| logical_model | 对外统一名（如 deepseek-v4-flash） |
| capability_tags | vision / strong / code / fast / cheap（三池遗产：VISION/STRONG_PATTERNS） |
| fallback_chain | 等价降级链：[tencent-free/deepseek-v4-flash → tencent-plan/deepseek-v4-flash-202605 → bailian/deepseek-v4-flash-0731] |
| upstream_names | 各供应商真实模型名（model_mapping 的数据源） |

**ErrorEvent（错误事件流，append-only）**

| 字段 | 说明 |
|---|---|
| ts / channel_id / model / http_code / error_code | 402/401008、400/20033 等 |
| newapi_request_id | 关联 New API 日志 |
| action_taken | 触发了什么状态变更 |

**RouterDecisionEvent（调度决策事件流，append-only，架构评审 2026-09-03 新增）**

每次请求调度记录一条，用于问题定位、成本归因、运营看板：

| 字段 | 类型 | 说明 |
|---|---|---|
| request_id | str | 增强层生成的全链路追踪 ID |
| ts | datetime | 决策时刻 |
| original_model | str | 客户端原始请求模型名 |
| logical_model | str | 解析后的逻辑模型名（容错/映射后） |
| task_type | enum | named（点名）/ auto-free（一期仅 named，预留） |
| selected_provider | str | 决策选中的供应商 |
| selected_group | str | free / paid |
| fallback_reason | enum | none / quota_exhausted / cooldown / health_down / no_candidate |
| quota_status | str | 决策时刻该模型的额度状态快照 |
| latency_ms | int | 增强层决策耗时（不含上游） |
| newapi_request_id | str | 回填：New API 侧请求 ID（结果阶段关联） |
| final_channel | int | 回填：实际命中渠道（从日志解析） |
| outcome | enum | success / error_402 / error_400 / error_other |

### 3.3 状态机：一个 (provider, model) 的额度生命周期

```
unknown ──tc-cloud报有额度/首次成功──→ available
available ──402事件──→ exhausted ──(T+冷却)──→ probing ──探针200──→ available
   │                                              └─探针402─→ exhausted(冷却×2)
   └──tc-cloud报0──→ suspected_low ──402──→ exhausted
```

**冷却策略**：首次耗尽冷却 24h（腾讯免费额度按日/周期重置，短探测无意义）；探针失败则 ×2 指数退避，上限 72h。**恢复不靠猜**：probing 用最小代价探针（1 次 5-token 调用），不拿真实流量试错。

---

## 四、智能调度流程（请求生命周期）

### 4.1 五步决策（对应流程图）

**① 令牌校验 + 限流**：校验客户端 Key → 查该 Key 的模型白名单 → 渠道级速率窗口（免费源 60 RPM 保护，令牌桶）。

**② 模型解析**（三分支）：
- 点名真实模型 → 容错匹配（大小写/前缀，v3.8 逻辑迁移）→ 查 ModelCatalog 得 logical_model
- `auto-free` 虚拟模型 → 任务类型识别（视觉请求→vision 池，长推理→strong 池，其余→fast 池）→ 池内选**可用额度中 health_score 最高**的模型
- 僵尸/未知模型 → 回退 auto-free 逻辑（不直接报错）

**③ 额度决策（核心）**：查 QuotaState(logical_model 的候选供应商)：
- 免费供应商 available → 选 **group=free** 转发
- 免费供应商 exhausted/冷却中 → 按 fallback_chain 选付费供应商 → **group=paid** 转发
- 全部不可用 → 明确报错（含"哪些模型也挂了"的信息），不瞎试

**④ 转发 New API**：携带选定的 group 令牌。New API 内部照常执行 priority/weight 路由 + 原生重试（传输层兜底）。

**⑤ 结果回写**：
- 200 → 清错误计数、更新 last_success、记录命中渠道（从 New API 日志或响应解析）
- **402** → 立即把该 (provider, model) 置 exhausted，事件入库；**若是 auto-free 请求且请求尚未终结，增强层用下一个候选供应商重试 1 次**（这是唯一允许增强层重试的场景，因为 New API 不会跨模型池救）
- 500/连接错 → 交给 New API 原生重试；增强层只记账，连续失败累计到渠道级 circuit_open
- 400 invalid model → 告警（说明 model_mapping 缺了条目），自动提交修复建议，不静默

### 4.2 free_first 的落地机制（关键设计）

**问题**：增强层决策了"走免费"，怎么让 New API 执行？

**方案**：利用 New API 的 **group 机制**。

- 同一模型在 abilities 表挂两个 group：`free`（免费渠道，高 priority）和 `paid`（付费渠道）
- 增强层持有两个服务端令牌：`token-free`（绑定 group=free）、`token-paid`（绑定 group=paid）
- 请求前决策选 group → 用对应令牌转发

**为什么不用"动态改 priority"**：每次请求改库 = 写放大 + 竞态，且 channels/abilities 同步是坑（实测教训）。group 是读路径参数，零写操作。

**⚠️ 待验证项（M1 第一件事）**：rc.30 是否支持请求级指定 group（若不支持，用双令牌方案兜底，已含在设计内）。

### 4.3 降级链完整示例

请求 `auto-free` + 视觉任务：
```
① 选模型：kimi-k3（vision 池，free available）→ group=free
② New API 路由 → tencent-free 渠道
③ 若返回 402：
   增强层标记 (tencent-free, kimi-k3)=exhausted
   → fallback_chain 查：kimi-k3 无 plan/bailian 等价物
   → 降级策略：同池次优模型（hy4-preview，free available）重试 1 次
④ 成功 → 响应 + _router 标记（model=kimi-k3→hy4-preview, reason=quota_exhausted）
```

---

## 五、组件设计与技术选型

| 组件 | 职责 | 技术选型 | 理由（含放弃项） |
|---|---|---|---|
| smart-router 代理 | 请求入口、决策、转发 | **FastAPI + httpx（异步流式透传）** | 团队已有 Python 栈（tc_cloud.py/ai_router.py）；放弃 Go：增强层逻辑轻，团队维护成本优先 |
| 状态存储 | QuotaState/ErrorEvent | **SQLite（独立文件，/opt/ai-hub/router/router.db）** | 单机单进程够用；与 New API 的 DB 物理隔离（红线 3）；放弃 Redis/PG：无多实例需求，复杂度不达标 |
| tc-cloud-sync | 定时拉取免费包/积分/窗口额度 | 现有 tc_cloud.py 25 函数原样复用 + APScheduler 定时（15min） | 4 个月验证过的资产；放弃实时推送：腾讯云无 webhook |
| 探针调度 | 耗尽模型恢复探测 | 复用代理的转发通道，每日窗口内最小代价探测 | 不直连上游（红线 2） |
| cost-portal | 套餐折算面板、告警 | FastAPI 只读页 + QQ 邮件（mailer 经验） | P1 交付，本期只留数据接口 |

**部署形态**：单 Docker Compose 增加 1 个容器 `auto-router`（mem_limit 256m），端口 :8000 对外，New API :3000 改仅内网监听。生产切换 = 改客户端 base_url 一个值。

**失败模式分析（架构师必答题）**：

| 故障 | 行为 | 设计保证 |
|---|---|---|
| 增强层挂了 | 客户端全断 | systemd/Docker restart always；客户端可临时直连 New API :3000（运维后门，文档化） |
| 状态表损坏 | 决策失效 | 状态表可从 tc-cloud-sync 全量重建（<5min），QuotaState 是**可再生缓存**而非数据源 |
| New API 挂了 | 全断 | 超出本期范围（单点），V5.0 多节点；当前靠 restart always |
| tc-cloud-sync 拉不到 | 状态停更 | 降级为纯 402 事件驱动模式（被动但可用） |

---

## 六、未来扩展能力（扩展点设计）

| 扩展 | 扩展点 | 需要动什么 / 不需要动什么 |
|---|---|---|
| **多供应商接入** | Provider 插件接口：`sync_quota() / health_check() / quota_type` | 新供应商 = 新增 Provider 配置 + New API 渠道，**核心调度代码零修改** |
| **免费额度池管理** | QuotaState 已按 (provider, model) 建模；加"池"聚合视图（池总额度 = Σ 模型额度） | 只加查询聚合，模型不变 |
| **成本优化** | cost-portal 消费 New API 日志 + 倍率；决策器预留 `cost_weight` 因子（health_score 公式里权重可配） | 公式加项，不改结构 |
| **自动降级** | fallback_chain 是数据不是代码：降级链存 ModelCatalog，改配置即改策略 | 已内建 |
| **模型智能选择** | auto-free 的任务识别当前是规则（三池关键词）；接口预留 `TaskClassifier` 抽象，未来可换小模型分类器 | 规则→模型的替换点已留 |

---

## 七、关键架构决策记录（ADR）

### ADR-001：增强层采用"前置薄代理"而非"改 New API 源码/插件"

- **Status**：Proposed（待确认）
- **Context**：New API AGPL-3.0；rc 版本迭代快；团队无 Go 能力；自研资产全在 Python
- **Decision**：独立 Python 进程做前置代理，经管理 API 与 New API 交互
- **Consequences**：✅ 升级 New API 无耦合、协议合规、复用团队技能 ❌ 多一跳延迟（<5ms 可接受）、多一个进程要运维。**放弃**：fork 改源码（协议+维护成本）、直连上游（丢计费/重试/协议转换）

### ADR-002：free_first 用"双 token 方案"实现，不依赖 New API group 机制【已裁决 · M1-0 实测结论】

- **Status**：Accepted（2026-09-03 17:40，M1-0 实测后裁决）
- **Context**：M1-0 实测发现 rc.30 自定义分组（free/paid）路由不可用（403 无权访问）；`token.group` 仅对内置分组（default/vip）生效；请求级 body.group 仅在 `/pg/` 路径生效，普通 `/v1/` 路径不支持
- **Decision**：放弃 group 路由方案，采用**双 token + 独立入口**：
  - 免费渠道绑定 `token-free`（group=default，model_mapping 指向免费上游真实名）
  - 付费渠道绑定 `token-paid`（group=default，model_mapping 指向套餐上游真实名）
  - 增强层根据 QuotaState 选 token，转发时换 Authorization 头
  - 独立入口 :8000（New API :3000 仅内网）
- **Consequences**：✅ 不依赖 rc.30 未文档化的 group 机制、方案稳定可预期 ❌ 多一个 token 管理维度（可接受，token 由增强层自动创建维护）

### ADR-003：额度状态采用"事件驱动 + 定时同步"双源，探针只做恢复确认

- **Status**：Proposed
- **Context**：无额度查询 API（实测 404）；402 报文 100% 可识别（实测）；tc-cloud 控制台数据可拉但非实时
- **Decision**：402 事件为第一信号源，tc-cloud-sync 为水位数据源，探针仅用于耗尽后的恢复验证
- **Consequences**：✅ 不浪费真实流量试探 ❌ 首次耗尽必然被用户遇到一次（用 auto-free 的增强层重试兜住）

### ADR-004：增强层仅对"额度耗尽"做 1 次请求级重试，其余交给 New API

- **Status**：Proposed
- **Context**：402 重试不跨模型池（实测）；传输层重试 New API 已覆盖（实测）
- **Decision**：重试职责切分——传输层归 New API，额度层归增强层，各重试一次，上限可控
- **Consequences**：✅ 最坏情况单请求 2 次上游调用（时延可控）❌ 两套重试逻辑需要清晰的错误分类表（已设计：402/400/5xx 三分法）

---

## 八、开发里程碑（架构评审 2026-09-03 后修订）

**M1 范围严格收敛**（评审确认）：

| # | 交付 | 说明 |
|---|---|---|
| M1-0 | **rc.30 group 机制验证**（今天执行） | 请求级 group 是否支持；不支持即锁定双 token 方案；不允许阻塞 M2 |
| M1-1 | smart-router 代理骨架 | FastAPI + httpx，转发 New API，流式透传 |
| M1-2 | 状态表 | QuotaState / ErrorEvent / RouterDecisionEvent（SQLite router.db） |
| M1-3 | 点名模型路由链路 | 精确名 + 容错匹配 → 决策 → 转发 → 决策日志 |
| M1-4 | 基础决策日志 | RouterDecisionEvent 落库 + 查询接口 |

**明确移出 M1**：auto-free、cost-portal、复杂任务分类、~~minimax-m3 跨池 402 实锤~~（评审决定**暂缓**——M0 已证明 402 核心行为，该验证对架构决策价值有限）。

**资源优先投入**（评审指定）：① group 机制验证 ② router 决策链路验证 ③ model_mapping 完整性验证。

**M1 前置验证补充**：ch1 其余 11 模型与 plan 上游名称核对（model_mapping 完整性，纳入 M1-3 验收）。

| 后续阶段 | 交付 |
|---|---|
| M2 | auto-free + 402 状态机 + tc-cloud-sync + 腾讯云面板 |
| M3 | cost-portal + 告警 + 客户端迁移双跑 |

---

## 九、开放问题（需要你拍板）

1. **ADR-001~004 是否确认**（尤其 ADR-002 的 group 方案）
2. **M1 前置验证③**（minimax-m3 跨池 402 实锤）是否批准少量 plan 积分消耗
3. **增强层端口**：对外用 :8000 还是沿用现有 8787 域名？（建议新端口，8787 生产保留到切换完成）
4. **auto-free 的任务识别**：一期沿用三池关键词规则（零成本），还是顺手做简单请求特征分类（含图片→vision）？建议前者，后者放 P1

---

*本文档为设计提案，全部机制均映射到 M0-M3 实测证据；确认前不写一行实现代码*
*架构师：小企鹅 🐧 · 联合评审：AI 工程师 / 后端架构师视角已并入各节*
