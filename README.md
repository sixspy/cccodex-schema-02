# cc-schema-lab

CC_Codex 二期（26.11）**数据工程**产品线：**契约换版台**（aiohttp + 原生 JS SPA + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**表契约版本登记 → 破坏性检查 → 换版应用 → 目录对账**

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

## 基线已通主链

1. 表契约版本（表名 / 版本号 / 现行或草稿）
2. 对候选字段做破坏性检查（删列 / 改类型），结论只追加进只读历史 `breaking_checks`
3. 换版应用：引擎在排他写事务（`BEGIN IMMEDIATE`）内按硬闸**一次判写**——破坏性草稿硬拒，白话短句回页面，硬拒留档 `apply_runs`/`gate_events`，草稿不得占现行
4. 误升可整笔回滚：现行退回草稿、旧现行复位、目录回拨，随即按同一硬闸重判
5. 契约与目录表对账，水线带 `blocked` 硬拒计数，与队列/详情/换版页亮同一颗硬闸芯片

### 硬闸同源约束

- 破坏性检查与升版闸门共用 `evaluate_diff` 纯函数，结论只能同源，不存在“黄字警告但放行”。
- 升版时不信前端、不信旧报告：锁内重算；留存报告与重算结论不一致（结论偏了）同样硬拒。
- 检查报告只追加，无更新/删除入口；硬拒事件先 `commit` 再抛错，回滚冲不掉留档。
- 并发升版由 `BEGIN IMMEDIATE` 串行化，同一草稿并发点升版只可能一笔成功，杜绝双写现行行。

预留切口（未做，供后续功能题）：双写迁移 vs 锁表切流、跨表依赖冻结、回滚窗口时限等。

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

## Docker

端口 **7100**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
