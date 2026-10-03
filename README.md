# 动物园谱系与繁育协调

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8308`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁、动物修订、批准快照和复核队列。
- `src/service.py`：用例编排、幂等处理、版本控制、按日期重建谱系、复核重算、后台重试worker和审计写入。
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

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤（如`?status=needs_review`）。
- `POST /api/<kind>`：创建对象；请求体为JSON。创建动物时可带`effective_date`（建档生效日，默认今天）。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

### 谱系修订与按日期重建

- `POST /api/entities/<动物id>/actions`，动作为`revise`：修改`name/sex/sire_id/dam_id`，必填`reason`，可带`effective_date`（默认今天）。每次修改生成一个新版本修订。
- `GET /api/animals/<id>/revisions`：列出某只动物的全部修订（版本、生效日、状态、数据、原因）。
- `GET /api/animals/<id>/pedigree?date=YYYY-MM-DD`：用当天有效的修订版本重建三代祖先（含sire/dam两条线、版本号、状态）。早于该动物第一个生效日时`root`为`null`。
- 服务启动时自动升级旧库：旧动物补一条`baseline`起始修订；旧的已批准/已完成配对补一条基于当时数据重建的批准记录。

### 配对批准快照与失效复核

- 配对`approve`时记录双方动物当时的版本、完整快照，并用当天版本重建的三代谱系存入批准历史。
- 之后只要任一方的**父母关系**发生修订，原批准立即失效：配对状态转为`needs_review`，原结论保留为`INACTIVE`（可查作废原因），并进入复核队列。非父母修改（改名等）不影响已批准配对；已完成配对保留历史结论。
- `GET /api/pairings/<id>/approvals`：列出该配对全部批准历史（含已作废记录和作废原因）。
- `GET /api/pending_reviews`：列出待复核配对及原因（`pending/processing/blocked`、尝试次数、最近错误、当前配对快照）。
- 后台worker每秒扫描队列：规则仍不通过（如近亲、非active）标记`blocked`保留原因，修复数据后可重试；技术性失败保持`pending`自动重试；进程重启后`processing`的卡死项会被重新接管。
- `POST /api/pairings/<id>/retry`（coordinator/admin）：人工触发一次复核重算，通过则转回`approved`并写入新批准记录，不通过返回当前待复核项和原因。
- `POST .../actions`动作为`recheck`：人工确认复核通过，同样生成新批准记录。

### 并发修改

所有动物修订和配对批准都走乐观锁：请求带`expected_version`，与库中版本不一致时返回`409 ConflictError`，只有一版入库；后到者拿到最新版本号后可重试。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖：修订时间线与按日期三代重建、批准快照与作废、并发修订冲突、复核队列（通过/规则失败保留/瞬时失败重试/重启接管）、旧数据升级补起始版本、HTTP端到端。

## 局限

谱系系数是简化亲缘规则，不替代专业谱系软件、遗传咨询或法定动物运输许可。
