# Auto Router 运维手册（M3-0）

> 版本：M2-6（2026-09-05）  
> 适用环境：M0 验证机（***SERVER-IP***，TencentOS 4）  
> 下一步：M3 生产加固将改为 systemd 管理，本文记录当前 nohup 方式，作为过渡文档。

---

## 一、服务信息

| 项目 | 值 |
|---|---|
| 服务名 | auto-router |
| 部署路径 | `/opt/ai-hub/auto-router/` |
| 主程序 | `router/main.py` |
| 监听地址 | `127.0.0.1:8080`（仅本机 + SSH 隧道可达） |
| Python 版本 | 3.11（venv 路径 `./venv/`） |
| 虚拟环境 | `/opt/ai-hub/auto-router/venv/` |
| 核心依赖 | fastapi / uvicorn / httpx / aiosqlite |
| 配置文件 | `/opt/ai-hub/auto-router/router/config.json` |
| 数据库 | `/opt/ai-hub/auto-router/router/router.db`（aiosqlite，自动创建） |
| 日志文件 | `/tmp/auto-router.log`（nohup 重定向） |
| 上游 New API | `http://127.0.0.1:3000`（Docker 容器 `aihub-m0`） |

---

## 二、启停命令（当前 nohup 方式）

> ⚠️ M3-1 将改为 systemd，届时本节命令会变化。当前方式在 M3 完成前仍有效。

### 2.1 启动

```bash
# 方式一：cd 进目录后用相对路径（推荐，避免 ModuleNotFoundError）
cd /opt/ai-hub/auto-router
nohup ./venv/bin/python -m uvicorn router.main:app \
  --host 127.0.0.1 --port 8080 \
  > /tmp/auto-router.log 2>&1 &
disown

# 方式二：绝对路径（需确保 router 包能被找到）
nohup /opt/ai-hub/auto-router/venv/bin/python -m uvicorn \
  --app-dir /opt/ai-hub/auto-router router.main:app \
  --host 127.0.0.1 --port 8080 \
  > /tmp/auto-router.log 2>&1 &
disown
```

### 2.2 停止

```bash
# 查找进程
pgrep -af uvicorn
# 输出示例：12345 /opt/ai-hub/auto-router/venv/bin/python -m uvicorn ...

# 停止（优雅终止，等待当前请求完成）
kill 12345

# 强制停止（如果优雅终止失败）
kill -9 12345
```

### 2.3 重启

```bash
# 先停后启
kill $(pgrep -f "uvicorn.*auto-router") 2>/dev/null; sleep 2
cd /opt/ai-hub/auto-router
nohup ./venv/bin/python -m uvicorn router.main:app \
  --host 127.0.0.1 --port 8080 \
  > /tmp/auto-router.log 2>&1 &
disown
```

### 2.4 查状态

```bash
# 进程是否存在
pgrep -af "uvicorn.*auto-router" && echo "✅ 进程在跑" || echo "❌ 进程不存在"

# 端口是否监听
ss -tlnp | grep 8080 && echo "✅ 端口 8080 监听中" || echo "❌ 端口未监听"

# 健康检查（最快确认服务正常）
curl -s http://127.0.0.1:8080/health | python3 -m json.tool
```

---

## 三、配置说明（config.json）

配置文件路径：`/opt/ai-hub/auto-router/router/config.json`

### 3.1 完整字段说明

| 字段 | 类型 | 必填 | 说明 | 示例值 |
|---|---|---|---|---|
| `new_api_base_url` | string | ✅ | New API 上游地址（Docker 容器 :3000） | `http://127.0.0.1:3000` |
| `token_free` | string | ✅ | 免费额度令牌（sk- 开头，来自 New API 后台） | `sk-xxx` |
| `token_paid` | string | ✅ | 付费额度令牌 | `sk-yyy` |
| `provider_models` | dict | ✅ | 每个 provider 持有的模型列表 | 见下 |
| `model_mapping` | dict | ⬡ | 上游模型名映射（New API 渠道要求特定名时用） | 见下 |
| `free_providers` | list | ✅ | 标记为"免费池"的 provider 名列表 | `["tencent_free","bailian_free"]` |
| `provider_priority` | list | ✅ | provider 优先级（数字越小越优先） | `["tencent_free",...,"tencent_plan"]` |
| `three_pool_keywords` | dict | ⬡ | 任务类型检测关键词（中英文各 10-15 个） | 见 M2-3 报告 |
| `routing_strategy` | string | ⬡ | 路由策略（当前仅 `cost_optimized` 有效） | `"cost_optimized"` |
| `model_routes` | dict | ⬡ | 任务类型 → 推荐模型映射 | `{"text":"deepseek-v4-flash",...}` |
| `providers` | dict | ✅ | 供应商传输层配置（M2-6 新增） | 见下 |

### 3.2 provider_models（模型持有关系）

```json
"provider_models": {
  "tencent_free":  ["deepseek-v4-flash", "qwen3.8-flash", "glm-5.2"],
  "bailian_free": ["deepseek-v4-flash", "qwen3.8-flash", "glm-5.2"],
  "bailian_lite":  ["qwen3.8-flash", "qwen3.7-max", "qwen3.6-flash"],
  "tencent_plan":  ["deepseek-v4-flash", "deepseek-v4-pro", "qwen3.8-flash"]
}
```

> **规则**：`decision.py` 根据 `provider_models` 判断"哪些 provider 持有该模型"，再结合 `free_providers` 决定走 free 还是 paid。

### 3.3 model_mapping（上游模型名映射）

```json
"model_mapping": {
  "tencent_plan": {
    "deepseek-v4-flash": "deepseek-v4-flash-202605"
  }
}
```

> **用途**：New API 某些渠道要求特定模型名（如 `tencent_plan` 的 `deepseek-v4-flash` 需映射为 `deepseek-v4-flash-202605`）。无映射时直接用原模型名。

### 3.4 free_providers + provider_priority（免费池 + 优先级）

```json
"free_providers": ["tencent_free", "bailian_free"],
"provider_priority": ["tencent_free", "bailian_free", "bailian_lite", "tencent_plan"]
```

> **降级规则**（M1 孙磊修正版）：
> - 单个 free provider exhausted → 选下一个 free provider（**不降级 paid**）
> - **所有**持有该模型的 free provider 全 exhausted → 才降级 paid

### 3.5 providers（M2-6 多供应商框架）

```json
"providers": {
  "new_api": {
    "type": "new_api",
    "base_url": "http://127.0.0.1:3000",
    "is_default": true
  }
}
```

> **当前**：所有 provider（`tencent_free` 等）都走 `new_api` 这个 transport。  
> **M3 接直连上游时**：加一个新 provider 配置（如 `deepseek_direct`），transport 自动切换，路由逻辑不动。

### 3.6 routing_strategy + model_routes（M2-3 四池路由）

```json
"routing_strategy": "cost_optimized",
"model_routes": {
  "text": "deepseek-v4-flash",
  "vision": null,
  "audio": null,
  "image": null
}
```

> **行为**：
> - `task_type=text` → 用 `deepseek-v4-flash` 做路由
> - `task_type=vision/audio/image` 且 `model_routes[xxx]=null` → **fallback 到 text 模型**
> - `routing_strategy` 非 `cost_optimized` → 记日志，按 cost_optimized 行为执行（占位分支）

---

## 四、健康检查

### 4.1 基础健康检查

```bash
curl -s http://127.0.0.1:8080/health | python3 -m json.tool
```

**预期返回**（M2-6 版本）：
```json
{
  "status": "ok",
  "version": "M2-6",
  "new_api_base_url": "http://127.0.0.1:3000",
  "free_providers": ["tencent_free", "bailian_free"],
  "paid_providers": ["bailian_lite", "tencent_plan"]
}
```

### 4.2 实时统计数据

```bash
# 最近 24 小时统计（默认）
curl -s "http://127.0.0.1:8080/router/stats?time_range=24h" | python3 -m json.tool

# 最近 1 小时
curl -s "http://127.0.0.1:8080/router/stats?time_range=1h" | python3 -m json.tool
```

**关键字段**：`total_requests`、`success_rate`、`free_ratio`、`provider_distribution`、`fallback_count`

### 4.3 冷却状态

```bash
curl -s http://127.0.0.1:8080/router/cooldown/status | python3 -m json.tool
```

**用途**：查看哪些 provider+model 当前处于冷却中（`exhausted=true`），以及预计恢复时间。

---

## 五、日志查看

### 5.1 日志文件位置

| 文件 | 路径 | 说明 |
|---|---|---|
| 主日志 | `/tmp/auto-router.log` | uvicorn 访问日志 + 应用日志（nohup 重定向） |
| 数据库 | `/opt/ai-hub/auto-router/router/router.db` | aiosqlite，存决策事件 + 冷却状态 |
| New API 容器日志 | `docker logs aihub-m0` | 上游调用记录（channel_id / 模型名 / 状态码） |

### 5.2 关键日志行解读

```bash
# 查看最近 50 行
tail -50 /tmp/auto-router.log

# 过滤决策日志（每次路由选择都会打）
grep "decision" /tmp/auto-router.log | tail -20

# 过滤 402 降级事件
grep "402\|fallback\|exhausted" /tmp/auto-router.log | tail -20

# 过滤冷却恢复事件
grep "cooldown\|recovered\|expired" /tmp/auto-router.log | tail -20
```

**典型日志示例**：
```
INFO:auto_router.decision:task_type=text, logical_model=deepseek-v4-flash, selected_provider=tencent_free, token=free
WARNING:auto_router.decision:provider tencent_free/deepseek-v4-flash exhausted, retrying with paid token
INFO:auto_router.cooldown:tencent_free/deepseek-v4-flash recovered after 3600s
```

### 5.3 决策事件查询（从数据库）

```bash
# 在 M0 上直接查 router.db
sqlite3 /opt/ai-hub/auto-router/router/router.db \
  "SELECT request_id, task_type, selected_provider, selected_token, success, created_at
   FROM router_decision_event
   ORDER BY created_at DESC
   LIMIT 10;"
```

---

## 六、故障排查

### 6.1 端口 8080 被占用

**现象**：启动时报 `OSError: [Errno 98] Address already in use`

```bash
# 查占用进程
ss -tlnp | grep 8080
# 或
pgrep -af "uvicorn.*8080"

# 杀掉旧进程
kill $(pgrep -f "uvicorn.*auto-router")

# 确认释放
ss -tlnp | grep 8080  # 应无输出
```

### 6.2 进程不在了（服务挂掉）

**现象**：`curl /health` 连接拒绝

```bash
# 确认进程状态
pgrep -af "uvicorn.*auto-router" || echo "进程不存在，需要重启"

# 查最后日志（确认 crash 原因）
tail -30 /tmp/auto-router.log

# 常见 crash 原因：
# 1. router.db 被删除 → 重新创建（自动）或检查路径
# 2. config.json 格式错误 → 检查 JSON 语法
# 3. venv 损坏 → 重建 venv（见 6.5）
```

### 6.3 402 降级不触发

**现象**：某模型免费额度用完，但仍在用 free token 调用（持续 402）

```bash
# 1. 查冷却状态（是否真的标记了 exhausted）
curl -s http://127.0.0.1:8080/router/cooldown/status | python3 -m json.tool

# 2. 查 config.json 的 free_providers 是否正确
cat /opt/ai-hub/auto-router/router/config.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['free_providers'])"

# 3. 查 provider_models 是否漏配模型
# （如果模型不在 free provider 的 provider_models 里，_free_pool_check 会漏判）
cat /opt/ai-hub/auto-router/router/config.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['provider_models']['tencent_free'])"
```

### 6.4 router.db 路径错误 / 数据库损坏（紧急恢复）

**现象**：启动时报 `aiosqlite.Error` 或决策日志全为空。

**原则**：先保全完整现场，再在副本上诊断，不要直接删除生产数据库文件。

```bash
# 1. 停服务
pkill -f "uvicorn router.main:app --host 127.0.0.1 --port 8080" || true
sleep 3

# 2. 保全完整数据库文件集（主库 + WAL + SHM）
TS=$(date +%Y%m%d_%H%M%S)
mkdir -p /tmp/router.db.rescue.$TS
cp /opt/ai-hub/auto-router/router/router.db* /tmp/router.db.rescue.$TS/

# 3. 在副本上诊断是否可打开
sqlite3 /tmp/router.db.rescue.$TS/router.db "PRAGMA integrity_check;"
# 输出 "ok" 表示主库完整；否则需要进一步恢复

# 4. 如果主库完整但缺少 WAL/SHM，可尝试用 SQLite 自带恢复
#    SQLite 会在打开时自动处理残留 WAL，通常能恢复到最后一次提交
sqlite3 /tmp/router.db.rescue.$TS/router.db "PRAGMA wal_checkpoint(TRUNCATE);"

# 5. 只有确认主库无法修复时，才放弃历史数据重建空库
#    mv /opt/ai-hub/auto-router/router/router.db /tmp/router.db.corrupt.$TS
#    重启服务会自动创建新表结构
```

> ⚠️ **教训**：M2 部署 tar 包必须排除 `*.db*`，否则覆盖 router.db 导致历史决策日志丢失！

### 6.5 SQLite WAL/SHM 与数据库运维

**背景**：aiosqlite 默认启用 WAL（Write-Ahead Logging）模式。WAL 中可能包含**已经提交、但尚未写回主文件**的数据，不是普通缓存。SQLite 官方说明：提交先发生在 WAL，checkpoint 再将内容写回主库。因此：

- 不能只复制主库而丢弃 WAL/SHM
- 不能在服务运行时直接打包数据库
- 不能把 WAL/SHM 当作可删除的临时文件

数据库目录下会出现 3 个文件：

| 文件 | 说明 |
|---|---|
| `router.db` | 主数据库文件 |
| `router.db-wal` | WAL 日志（含已提交但未 checkpoint 的数据） |
| `router.db-shm` | 共享内存索引（WAL 读取加速） |

以下按三种运维场景分别给出流程。

#### 场景 A：代码部署（不动数据库）

**目标**：只更新代码、静态资源和示例配置，不覆盖/迁移数据库。

```bash
# 部署包必须排除全部数据库运行文件
tar czf auto-router-deploy.tar.gz \
  --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
  -C /opt/ai-hub/auto-router router/ tests/ pyproject.toml requirements.txt

# 解压覆盖代码后重启服务，数据库保持原样
```

> 禁止：把 `router.db` 及其 `-wal`/`-shm` 打进部署包，或在部署脚本里 `rm` 掉这些文件。

#### 场景 B：数据库备份与恢复

**目标**：获得一个可随时恢复、数据一致的数据库副本。

**推荐做法（使用 SQLite 内置 backup API）**：

```bash
# 1. 停服务，确保无写入
pkill -f "uvicorn router.main:app --host 127.0.0.1 --port 8080" || true
sleep 3

# 2. 执行在线一致性备份（SQLite backup API，不依赖 checkpoint 时机）
cd /opt/ai-hub/auto-router
./venv/bin/python - << 'PY'
import sqlite3
src = 'router/router.db'
dst = '/tmp/router.db.bak.' + __import__('datetime').datetime.now().strftime('%Y%m%d_%H%M%S') + '.db'
with sqlite3.connect(src) as s:
    with sqlite3.connect(dst) as d:
        s.backup(d)
print(f"backup done: {dst}")
PY

# 3. 启动服务
cd /opt/ai-hub/auto-router
setsid nohup ./venv/bin/python -m uvicorn router.main:app \
  --host 127.0.0.1 --port 8080 > /tmp/auto-router.log 2>&1 < /dev/null &
```

**替代做法（命令行 checkpoint + 停写复制）**：

```bash
# 1. 停服务
pkill -f "uvicorn router.main:app --host 127.0.0.1 --port 8080" || true
sleep 3

# 2. checkpoint 并确认完成
sqlite3 /opt/ai-hub/auto-router/router/router.db "PRAGMA wal_checkpoint(TRUNCATE);"

# 3. 复制主库（此时 WAL 已空，复制主库即完整）
TS=$(date +%Y%m%d_%H%M%S)
cp /opt/ai-hub/auto-router/router/router.db /tmp/router.db.bak.$TS.db

# 4. 启动服务
```

**恢复验证流程**：

```bash
# 1. 再次停服务
pkill -f "uvicorn router.main:app --host 127.0.0.1 --port 8080" || true
sleep 3

# 2. 移动原库（保留）
mv /opt/ai-hub/auto-router/router/router.db /tmp/router.db.before_restore
mv /opt/ai-hub/auto-router/router/router.db-wal /tmp/router.db-wal.before_restore 2>/dev/null || true
mv /opt/ai-hub/auto-router/router/router.db-shm /tmp/router.db-shm.before_restore 2>/dev/null || true

# 3. 从备份恢复（用 backup API 比 cp 更能保证一致性）
/opt/ai-hub/auto-router/venv/bin/python - << 'PY'
import sqlite3
src = '/tmp/router.db.bak.YYYYMMDD_HHMMSS.db'  # 替换为实际备份路径
dst = '/opt/ai-hub/auto-router/router/router.db'
with sqlite3.connect(src) as s:
    with sqlite3.connect(dst) as d:
        s.backup(d)
print(f"restore done: {dst}")
PY

# 4. 启动服务并核对数据
cd /opt/ai-hub/auto-router
setsid nohup ./venv/bin/python -m uvicorn router.main:app \
  --host 127.0.0.1 --port 8080 > /tmp/auto-router.log 2>&1 < /dev/null &
sleep 5
curl -s http://127.0.0.1:8080/router/decisions?limit=1 | python3 -m json.tool
```

#### 场景 C：数据库搬迁

**目标**：把数据库从一台机器搬到另一台机器。

```bash
# 源机器：停服务后备份完整文件集
pkill -f "uvicorn router.main:app --host 127.0.0.1 --port 8080" || true
sleep 3
mkdir -p /tmp/router-db-export
cp /opt/ai-hub/auto-router/router/router.db* /tmp/router-db-export/

# 把 /tmp/router-db-export 传输到目标机器
# 目标机器：直接放到 router/ 目录下启动即可
# SQLite 会在打开时自动处理 WAL/SHM
```

**禁止**：只复制 `router.db` 而不带 `-wal`/`-shm`。如果这么做，数据会回退到最后一次 checkpoint，期间已提交的决策记录可能丢失。

#### 场景 D：强制清理 WAL/SHM（仅限数据库损坏无法打开）

**仅在以下情况使用**：
- 数据库文件损坏，无法正常打开
- 已按场景 C 保全完整文件集
- 在副本上诊断后确认需要清理

```bash
# 1. 先停服务
pkill -f "uvicorn router.main:app --host 127.0.0.1 --port 8080" || true
sleep 3

# 2. 保全完整现场
TS=$(date +%Y%m%d_%H%M%S)
mkdir -p /tmp/router.db.rescue.$TS
cp /opt/ai-hub/auto-router/router/router.db* /tmp/router.db.rescue.$TS/

# 3. 在副本上尝试打开并 checkpoint
sqlite3 /tmp/router.db.rescue.$TS/router.db "PRAGMA wal_checkpoint(TRUNCATE);"

# 4. 如果副本能正常打开，再用副本替换生产文件
#    不要直接在生产目录删除 WAL/SHM
```

> ⚠️ **绝对禁止**：服务运行中直接 `rm -f router.db-wal router.db-shm`。这会破坏正在进行的事务，导致数据库损坏。

#### 备份恢复演练记录（M2.5 复核）

**时间**：2026-09-07
**环境**：M0 验证环境 `/opt/ai-hub/auto-router/router/router.db`
**步骤与结果**：

```bash
# 备份前记录数
sqlite3 router.db "SELECT COUNT(*) FROM router_decision_event WHERE original_model='deepseek-v4-pro';"
# → 3

# 停服务 → SQLite backup API 备份 → 移动原库 → 从备份恢复 → 启动服务

# 恢复后记录数
sqlite3 router.db "SELECT COUNT(*) FROM router_decision_event WHERE original_model='deepseek-v4-pro';"
# → 3

# /health 检查：200 OK
```

**结论**：backup API 备份 + 恢复后数据一致，服务可正常启动。

### 6.6 venv 损坏（Python 依赖缺失）

**现象**：启动时报 `ModuleNotFoundError: No module named 'fastapi'` 等

**重建步骤**（使用项目自带的 `requirements.txt`）：

```bash
# 1. 停服务
kill $(pgrep -f "uvicorn.*auto-router") 2>/dev/null
sleep 2

# 2. 删除旧 venv
cd /opt/ai-hub/auto-router
rm -rf venv

# 3. 创建新 venv
python3 -m venv venv

# 4. 用 requirements.txt 安装依赖（版本锁定）
./venv/bin/pip install -r requirements.txt
# 或使用 pyproject.toml（如果已安装 pip >= 21.3）
./venv/bin/pip install -e .

# 5. 验证依赖完整
./venv/bin/python -c "import fastapi, uvicorn, httpx, aiosqlite; print('✅ 依赖完整')"

# 6. 重启服务
nohup ./venv/bin/python -m uvicorn router.main:app \
  --host 127.0.0.1 --port 8080 \
  > /tmp/auto-router.log 2>&1 &
disown

# 7. 验证服务正常
curl -s http://127.0.0.1:8080/health | python3 -m json.tool
```

> **依赖清单**（`requirements.txt`）：
> ```
> fastapi==0.115.6
> uvicorn[standard]==0.34.0
> httpx==0.28.1
> pydantic==2.10.4
> python-multipart==0.0.20
> aiosqlite==0.20.0
> ```
>
> M3-1 改为 systemd 管理后，venv 重建需在 `ExecStartPre` 中加入依赖检查。

> **包目录结构（T-R3 修复）**：仓库源码在 `src/router/` 子目录下，`pyproject.toml` 的 `pythonpath=["src"]` 使 `import router` 直接解析到 `src/router/`。仓库结构与线上部署路径 `/opt/ai-hub/auto-router/router/` 对齐。`tests/conftest.py` 仅保留 aiosqlite mock 污染修复，不再需要包映射兜底。

---

## 七、回退流程（客户端切回旧服务）

> 如果 Auto Router 出问题，需要临时切回 v3.9 旧服务（端口 8787）。

### 7.1 旧服务信息

| 项目 | 值 |
|---|---|
| 服务路径 | `/opt/ai-router/` |
| 启动方式 | `systemctl start ai-router` |
| 监听端口 | `8787` |
| 健康检查 | `curl http://127.0.0.1:8787/health` |
| 配置文件 | `/opt/ai-router/config.json` |
| systemd 服务名 | `ai-router.service` |

### 7.2 回退步骤

```bash
# 1. 停 Auto Router
kill $(pgrep -f "uvicorn.*auto-router") 2>/dev/null
sleep 2
pgrep -af "uvicorn.*auto-router" || echo "✅ Auto Router 已停止"

# 2. 确认旧服务未运行
systemctl is-active ai-router

# 3. 如果旧服务没跑，启动它
systemctl start ai-router
systemctl status ai-router  # 确认 Active: active (running)

# 4. 验证旧服务正常
curl -s http://127.0.0.1:8787/health

# 5. 客户端改回 8787（WorkBuddy / Trae 等）
#    base_url: http://127.0.0.1:8787/v1
```

### 7.3 恢复 Auto Router（回退后修复完成）

```bash
# 1. 停旧服务（如果不再需要）
systemctl stop ai-router

# 2. 启动 Auto Router
cd /opt/ai-hub/auto-router
nohup ./venv/bin/python -m uvicorn router.main:app \
  --host 127.0.0.1 --port 8080 \
  > /tmp/auto-router.log 2>&1 &
disown

# 3. 验证
curl -s http://127.0.0.1:8080/health

# 4. 客户端切回 8080
#    base_url: http://127.0.0.1:8080/v1
```

---

## 八、config.example.json（参考模板）

```json
{
  "new_api_base_url": "http://127.0.0.1:3000",
  "token_free": "sk-xxx-free-token-here",
  "token_paid": "sk-xxx-paid-token-here",
  "provider_models": {
    "tencent_free": ["deepseek-v4-flash", "qwen3.8-flash"],
    "bailian_free": ["deepseek-v4-flash", "qwen3.8-flash"],
    "bailian_lite":  ["qwen3.8-flash", "qwen3.7-max"],
    "tencent_plan":  ["deepseek-v4-flash", "deepseek-v4-pro"]
  },
  "model_mapping": {
    "tencent_plan": {
      "deepseek-v4-flash": "deepseek-v4-flash-202605"
    }
  },
  "free_providers": ["tencent_free", "bailian_free"],
  "provider_priority": [
    "tencent_free",
    "bailian_free",
    "bailian_lite",
    "tencent_plan"
  ],
  "three_pool_keywords": {
    "vision": ["图片", "图像", "image", "vision"],
    "video":  ["视频", "video"],
    "audio":  ["语音", "audio", "whisper"],
    "image":  ["画图", "绘图", "image generation"]
  },
  "routing_strategy": "cost_optimized",
  "model_routes": {
    "text": "deepseek-v4-flash",
    "vision": null,
    "audio": null,
    "image": null
  },
  "providers": {
    "new_api": {
      "type": "new_api",
      "base_url": "http://127.0.0.1:3000",
      "is_default": true
    }
  }
}
```

---

*文档版本：M2-6 | 更新时间：2026-09-05 | 作者：Auto Router AIHub 项目组*
