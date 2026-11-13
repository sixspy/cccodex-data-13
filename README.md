# cc-data-lab

CC_Codex 二期（26.11）**数据工程**主线基线：**采集任务控制台**（Flask 后端 + HTMX 前端页 + 站内 Mock 目录）。

## 领域

- **所属领域**：数据工程  
- **子方向**：数据采集、爬虫与 RPA  

## 技术栈

- Python 3.12+、Flask 3、SQLite  
- HTMX（CDN）+ Jinja 模板  
- pytest  

## 本地运行

```powershell
cd claude-workspace\cc-data-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

浏览器打开 http://127.0.0.1:5590 ，登录 **admin / 123456**。

流程：创建任务 → 选种子 `/mock/catalog/alpha` 或 `beta` → 详情页「运行采集」→ 查看落库（alpha 含重复标题，会演示去重跳过）。

## 附页核齐子系统（目录声明 / 账册 / 附页库 / 核齐）

围绕一个采集任务，四件套串成一套（附页是附页库里的**独立库页**，不是账册行内备注）：

| 件 | 表 | 规则 |
|----|----|------|
| 目录声明 | `catalog_declarations` | 每个编码声明**应附份数**与**附页模板名**；建任务时按种子目录自动生成，可在详情页改 |
| 账册 | `ledger_rows` | 每行标 **应附 / 可不附** |
| 附页库 | `attachment_pages` | 独立库页，按「编码 + 份序」存放，`UNIQUE(job_id, page_name, seq)` |
| 核齐 | `reconcile_sessions` / `reconcile_policy_locks` | 打开核齐拍条数快照，提交时按写死口径校验 |

核齐规则：

- **应附行**必须在附页库留下同名页，份数与声明一致；少一份或多一份都算未齐并列出编码。
- **可不附行**若误建附页，标成**多附**；多附存在时整题不得核齐（账册外编码的附页也算多附）。
- 卡片分开展示 **应附 / 多附 / 未齐 / 已齐** 四种状态，并写出**当日核齐口径名字**。
- **已齐之后再写入应附行**：卡片自行回到未齐（核齐会话保存数据指纹，指纹变化即失效），旧已齐不能继续当完成。

### 核齐口径（写死在后台，只能选一种，当日不能改口）

| 口径常量 | 名字 | 条数变化时的行为 |
|----------|------|------------------|
| `snapshot` | 打开核齐瞬间快照口径 | 以打开核齐瞬间的快照为准；提交时条数若变了，**整笔失败**并写「条数变了」 |
| `latest` | 提交瞬间最新条数口径 | 以提交瞬间最新条数为准；打开核齐后又有写入，**要求重新打开核齐**，不许静默成功 |

- 口径落在 `reconcile_policy_locks`（按日期主键），当日首次选择即锁定，换选项报「不能改口」。
- 提交核齐时 `algorithm` 与当日写死选项（或本会话口径）对不上，**当场挡住**并写「算法不对」。
- 份数不对时停在未齐（会话保持打开，补正后可重提）；有多附时不能已齐。

### 附页库写锁与并发

- **采集未收束**（任务未到 `done`）时附页库**锁写**，建附页请求被挡且不落任何页。
- 两个操作员并发给同一编码建附页：独立连接 `BEGIN IMMEDIATE` 抢 SQLite 写锁（`busy_timeout=0`），
  只留一笔写入，后到者以**冲突**结束；顺序不重叠的建页正常生成第 2、第 3 份。

### 七段验收

`tests/test_reconcile.py` 按七段组织，共 25 个用例（全量 `pytest`）：

1. 份数不对（少一份/多一份）停在未齐并列编码
2. 有多附时不能已齐
3. 条数变化：快照口径整笔失败写「条数变了」/ 最新口径必须重开
4. 核齐算法不一致被挡住（「算法不对」），当日口径不能改口
5. 并发建附页只有一笔成功，另一笔冲突
6. 采集未收束时附页写不进去（锁写）
7. 页面分清四种状态与口径名字；已齐后再写入回到未齐

## Docker 一键启动（推荐用于快照前验收）

需已打开 **Docker Desktop**：

```powershell
cd claude-workspace\cc-data-lab
docker compose up -d --build
docker compose ps
```

- 页面：http://127.0.0.1:5590/login  
- Mock：http://127.0.0.1:5590/mock/catalog/alpha  
- 停止：`docker compose down`（保留库卷）· 清卷：`docker compose down -v`

`docker compose ps` 中 `STATUS` 含 **healthy** 表示服务可访问。线下表「环境可复现等级」可填 **已容器化，可一键起环境**。

## 前后端说明

| 层 | 说明 |
|----|------|
| 后端 | `app/` 路由、任务状态机、同步 runner、SQLite |
| 前端 | `templates/` + `static/`，浏览器可点操作 |
| Mock 源站 | `/mock/catalog/<id>` HTML，无需外网 |

## 初始快照

在首次 Claude 单轮跑题前 commit + push 本树（不含 `.venv/`、`instance/*.db`）。后续题目在同 repo 上叠加调度、RPA 步骤、增量水位等能力。

## 基线 vs 跑题目录

| 路径 | 角色 |
|------|------|
| `claude-workspace/cc-data-lab`（本目录） | **基线**：改功能、docker 验收 |
| `ccproject/cc-data-lab/data-XX` | **单题**：复制基线后挂容器、出快照 |

复制：`..\scripts\Copy-BaselineToQuestion.ps1 -QuestionId data-01`

## 跑题 Harness

见 `CCcodex/docs/ClaudeCode_Windows使用说明.md`（镜像 2.0）。容器挂载 **ccproject 下该题目录**，不是本基线路径。
