# Auto Router AIHub V4.0

私有 AI API 中台 — 基于 New API 基座 + Python 增强层，实现免费额度优先路由、额度耗尽自动降级、4 池路由、自动冷却恢复。

## 架构

```
用户请求 → Auto Router (8080) → New API (3000) → 上游渠道
                                    ├─ ch1: tencent-plan (paid)
                                    ├─ ch2: bailian-lite (paid)
                                    ├─ ch3: tencent-free (free)
                                    └─ ch4: bailian-free (free)
```

Auto Router 通过双 token（free/paid）控制 New API 路由到免费或付费渠道。免费额度耗尽后自动降级到付费渠道。

## 核心能力

- **双 token 免费优先路由**：优先走免费渠道，耗尽后自动降级付费渠道
- **4 池路由**：按 task_type（text/vision/audio/image）选择模型
- **自动冷却恢复**：402 指数退避（1h→2h→4h→24h 上限），过期自动恢复
- **运营看板**：/router/stats 统计接口 + HTML 看板（Chart.js）
- **多供应商框架**：Provider 抽象接口，支持后续接入直连上游

## 技术栈

- Python 3.11 + FastAPI + uvicorn
- httpx（异步 HTTP 客户端）
- aiosqlite（SQLite 异步 ORM）
- Chart.js v4（看板前端）
- New API v1.0.0-rc.30（基座）

## 目录结构

```
src/
  __init__.py
  main.py          # FastAPI 入口 + 路由 + 看板
  adapter.py       # New API 适配器（继承 NewAPIProvider）
  decision.py      # 决策引擎 + StateManager + 冷却恢复
  models.py        # 数据模型 + 枚举 + 配置
  providers.py     # Provider ABC + NewAPIProvider + ProviderRegistry
  db.py            # SQLite 数据库操作
  config.json      # 配置文件（不入 Git，用 config.example.json）
  requirements.txt
  static/
    dashboard.html # 运营看板页面
tests/
  conftest.py
  test_decision.py
  test_cooldown.py
  test_providers.py
  test_stats.py
  test_task_routing.py
docs/
  (M0-M2 各阶段报告 + 架构设计文档)
```

## 部署

```bash
cd /opt/ai-hub/auto-router
python3 -m venv venv
source venv/bin/activate
pip install -r src/requirements.txt
cp src/config.example.json src/config.json
# 编辑 config.json 填入实际 token
uvicorn router.main:app --host 127.0.0.1 --port 8080
```

## 配置说明

复制 `src/config.example.json` 为 `src/config.json`，填入：
- `token_free`：New API 中 auto-router-free token
- `token_paid`：New API 中 auto-router-paid token
- `new_api_base_url`：New API 地址（默认 127.0.0.1:3000）

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| /v1/chat/completions | POST | OpenAI 兼容接口（流式+非流式） |
| /health | GET | 健康检查 |
| /router/stats | GET | 统计数据（支持 time_range 参数） |
| /router/decisions | GET | 决策日志查询 |
| /router/cooldown/status | GET | 冷却状态查看 |
| /dashboard | GET | HTML 运营看板 |

## 里程碑

- M0：环境部署 + 渠道验证 + 路由机制验证 + 免费额度实验
- M1：FastAPI 骨架 → DB 持久化 → 路由链路 → 部署 → 五场景集成验证
- M2：Quota 计量 → 冷却恢复 → 4 池路由 → 状态接口 → 运营看板 → 多供应商框架
- M3：生产加固 → 全模型覆盖 → DeepSeek 直连 → 流量切换（规划中）

## 红线

- 不改 New API 源码
- 不直连上游（M3 开始按需开放）
- 不直写 New API SQLite

## License

Private — 内部使用
