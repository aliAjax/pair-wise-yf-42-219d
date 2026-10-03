# 动物园谱系与繁育协调

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8308`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8308
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `animal`：个体谱系；`pairing`：配对建议；`transfer`：机构和运输记录。

## 谱系修订与按日期重建

动物档案只保留最新的父母和状态，但每次个体变更都会按**生效日**保存一条修订（`entity_revisions`）。创建个体或通过 `update_parents` 动作变更父母时，可在数据中传入 `effective_date`（默认为当天）。服务启动时会自动为没有修订的存量动物补一条起始版本（`ensure_baseline_revisions`）。

- `GET /api/animals/<id>/pedigree?date=YYYY-MM-DD&generations=3`：按日期重建该个体的三代祖先。每个祖先都取其在该生效日当天的版本；日期早于首条修订时该祖先标记为 `known:false`。
- `GET /api/animals/<id>/revisions`：列出该个体的全部修订。

## 配对批准快照与失效复核

- 批准配对（`approve`）时会记下双方当时的版本：`sire_version`、`dam_version` 和亲缘系数，存入配对数据的 `pedigree_snapshot`，作为本次批准的依据。
- 之后若任一亲本的父母关系发生改动，所有已批准且涉及该亲本的配对会立即失效，状态转为 `pending_review`（待复核），并生成一条待复核任务；原批准结论和快照仍保留可查。
- 复核重算会用最新修订重建三代谱系并计算亲缘系数：系数 ≤ 阈值则自动复核通过（回到 `approved` 并刷新快照）；超过阈值则保持 `pending_review` 并写明原因。
- 重算失败时保留待复核项并记录错误，稍后重试；服务重启后会自动接着处理未完成的复核任务。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/animals/<id>/pedigree?date=&generations=`：按日期重建三代祖先。
- `GET /api/animals/<id>/revisions`：列出个体修订。
- `GET /api/review/pending`：列出待复核配对及原因。
- `POST /api/review/process`：手动触发复核重算。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

谱系系数是简化亲缘规则，不替代专业谱系软件、遗传咨询或法定动物运输许可。
