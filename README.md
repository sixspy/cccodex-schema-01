# cc-schema-lab

CC_Codex 二期（26.11）**数据工程**产品线：**契约换版台**（aiohttp + 原生 JS SPA + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**表契约版本登记 → 破坏性检查 → 切流落库 → 目录对账**

与现有九线正交（不是采集、ETL、同步、解析、编排、摄取、留存、清单、水印）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数仓与数据湖（表契约 / 换版闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、aiohttp、SQLite |
| 前端 | 原生 JS SPA（全中文），暖杏纸面 + 墨绿强调；顶栏胶囊导航 |
| 测试 | pytest + pytest-aiohttp（本地秒级闭环，优先于 compose） |

## 主链

1. **契约列表**：每张表亮契约现行版号、目录现行版号、读指针、最近一次切流结果（含失败因）。
2. **破坏性检查**：草稿对照现行，列出删列 / 改类型 / 新增。
3. **换版卡片**：草稿字段、目录列名与写状态、受影响清洗作业、最近失败因；选边后按选定命运切流。
4. **目录进度**：契约现行 = 目录现行 = 读指针、列名写状态、清洗作业绑定；清洗与目录各复跑一轮列名对齐。
5. **目录对账**：逐表核对契约现行 / 目录现行 / 读指针，双现行直接判漂移并停机。

## 切流只择一条命运（二选一，选定后不可更改）

| 命运 | 落库行为 | 旧列 | 入库认哪套字段 |
|------|----------|------|----------------|
| **锁表切流** | 同一事务锁表切现行，拒写旧列 | 目录里标「已停写」，落库整笔回滚 | 只认现行 v_n |
| **旁路双写** | 旁路双写新旧列，再原子切换读指针 | 目录里标「双写」，仍可写 | 新旧列都能写，读指针只指向现行 |

一次切流在 `BEGIN IMMEDIATE` 写事务里完成：核实现行唯一 → 比对页面带来的现行版号 →
停旧现行 / 升新现行 → 更新目录现行、读指针、写集、命运 → 记 `apply_runs` →
**清洗 / 目录两轮列名对齐复跑**。任一环节失败整事务回滚，失败因落到 `apply_runs`，
列表与卡片直接亮出来（白话，不吞异常）。

- **双现行立刻停**：同表出现两份「现行」时，切流、对齐、对账全部停机，页面顶栏亮红。
- **版号与目录现行同源**：读指针必须等于契约现行，否则对账判漂移。
- **并发互斥**：两名仓管同时抢切流时，后到者在写锁后排到，发现页面版号已过期，
  返回「现行指针已被另一名值班改动」，不会互相覆盖现行指针。

## 本地

```powershell
cd claude-workspace\cc-schema-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:7100/login · **admin / 123456**

本地库核对现行是否唯一（只读）：

```powershell
sqlite3 instance\schema_lab.db "SELECT table_name, COUNT(*) FROM schema_contracts WHERE status='现行' GROUP BY table_name HAVING COUNT(*) > 1;"
```

查询为空即现行唯一；有行即双现行，页面顶栏与 `/api/live-guard` 会同时亮红停机。

## 接口速览

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/contracts` | 契约列表（含字段、三处版号、最近切流） |
| GET | `/api/contracts/{id}/fields` | 字段详情（现行类型 / 目录写状态 / 清洗引用） |
| GET | `/api/contracts/{id}/cutover-card` | 换版卡片（含失败因） |
| POST | `/api/contracts/{id}/breaking-check` | 破坏性检查 |
| POST | `/api/contracts/{id}/apply` | 切流落库 `{strategy, expected_live_version}` |
| GET | `/api/live-guard` | 现行是否唯一 |
| GET | `/api/catalog` | 目录进度（列名写状态、清洗作业） |
| GET/POST | `/api/cleaning-jobs` | 清洗作业列表 / 新建 |
| POST | `/api/cleaning-jobs/{id}/rebind` | 改绑列名 |
| POST | `/api/cleaning-jobs/{id}/write` | 模拟落库（未知列名 / 旧列停写会整笔回滚） |
| GET/POST | `/api/progress-checks` | 对齐记录 / 复跑（清洗 + 目录） |

## Docker

端口 **7100**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
