# 临床研究病例队列重算服务

面向多中心临床研究的病例筛选与可复算分析队列。目标：在**相同方案修订 + 相同数据截止批次**下，
全量重算与增量重算必须得到**逐病例一致**的队列；补传后只重算受影响病例，同时不遗漏间接影响。
在此之上，服务还提供**方案修订的差异归因与分析迁移评估**：当方案与中心资料同时变化时，
解释某个病例为何进入、退出或改变了终点资格，而不只是知道新旧人数不同。

技术栈：Python 3.12 + FastAPI + PostgreSQL 18。人群、编码、检验值和中心名称**全部为虚构数据**。

---

## 1. 快速启动

### 方式 A：Docker Compose（一键启动）

```bash
cp .env.example .env
docker compose up --build -d
```

- API：<http://127.0.0.1:8000>
- Swagger UI：<http://127.0.0.1:8000/docs>
- OpenAPI 3.1 文档：<http://127.0.0.1:8000/openapi.json>（仓库内另存 `openapi/openapi.json`）
- 健康检查：`curl http://127.0.0.1:8000/health`

### 方式 B：本地进程（使用本机已装的 PostgreSQL / Redis）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # 运行所需
.venv/bin/pip install -r requirements-dev.txt    # 另加 pytest/httpx/pyflakes

# 建库（已存在则忽略报错）
PGPASSWORD=ccdev /opt/postgresql-18/bin/createdb -h 127.0.0.1 -U ccdev cohorthub

export COHORT_DATABASE_URL="postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/cohorthub"
export COHORT_SUBJECT_SALT="dev-only-salt-change-me"
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

首次启动会自动建表并写入演示账号。也可以直接用 Makefile：`make install`、`make serve`、`make demo`、`make test`。

> 本任务只要求 PostgreSQL；Redis 未使用（运行中断恢复通过数据库中逐中心检查点实现，不依赖外部队列）。

### 一键复现完整业务场景

```bash
make demo
# 等价于 COHORT_DATABASE_URL=... .venv/bin/python -m scripts.seed_demo
```

脚本会依次执行并打印：全量筛选 → 各中心对账表 → 隐私管理员裁决身份候选 → 锁定快照并发布报告 v1
→ 迟到补传（含检验结果更正）→ 增量重算 → 校验与全量重算逐病例一致 → 新快照取代旧快照、旧报告字节不变。

---

## 2. 测试账号（虚构，仅本地）

鉴权为 `Authorization: Bearer <token>`。`GET /demo-accounts` 可列出，**生产部署必须替换为医院 SSO**。

| 账号 | Token | 角色 | 能力 |
| --- | --- | --- | --- |
| `admin` | `admin-token` | admin | 全部权限 + 审计日志 |
| `rule_reviewer` (`reviewer`) | `reviewer-token` | 规则审核 | 建中心/映射、方案修订与审批、重放映射、等价性校验 |
| `analysis.lead` | `analysis-token` | 分析负责人 | 冻结截止批次、发起筛选、建/锁快照、随访计划、统计与报告、审计 |
| `privacy.admin` | `privacy-token` | 隐私管理员 | 裁决身份候选、查看原始患者引用、撤回授权 |
| `north.uploader` | `center-north-token` | 中心上传 | 仅 NORTH 中心的数据上传与自身拒收报告 |
| `south.uploader` | `center-south-token` | 中心上传 | 同上，SOUTH |
| `east.uploader` | `center-east-token` | 中心上传 | 同上，EAST |

角色隔离要点：
- 中心上传者**只能看到随机研究号**（`study_id`），看不到 `patient_ref`；
- 身份候选**只能由隐私管理员**确认/拒绝，普通角色调用返回 403；
- 锁快照、发起统计只有分析负责人可做；规则审核员不能锁快照。

---

## 3. 使用 API 走完整流程

```bash
B=http://127.0.0.1:8000
SR='Authorization: Bearer reviewer-token'
SA='Authorization: Bearer analysis-token'
SP='Authorization: Bearer privacy-token'
SN='Authorization: Bearer center-north-token'
```

1. **建中心 + 编码映射**（审核员）`POST /centers`、`POST /mappings`
2. **方案 v1.0**（审核员）`POST /protocols` → `POST /protocols/{id}/revisions` → `POST /protocols/revisions/{id}/approve`
   筛选只接受 **approved** 修订。
3. **中心上传**（各中心上传者）`POST /uploads`，行会被立即标准化；`GET /uploads/{id}/rejection-report`
   查看无法标准化的行及原因（含“未被激活的重复补发”清单）。
   中断后 `POST /uploads/{id}/resume` 从最后已提交行继续。
4. **冻结截止批次**（分析负责人/审核员）`POST /cutoffs {label, upload_ids}`，只接受 `completed` 上传。
5. **执行筛选** `POST /screening-runs {protocol_revision_id, cutoff_batch_id, strategy}`。
6. **逐中心对账** `GET /screening-runs/{id}` → `reconciliation[]` 与 `counts`，全部 `balanced=true` 才能继续。
7. **身份候选**（隐私管理员）`GET /identity-candidates` → `POST /identity-candidates/{id}/decide`。
8. **快照** `POST /screening-runs/{id}/snapshots` → `POST /snapshots/{id}/lock`（锁定后不可改）。
9. **随访计划** `POST /snapshots/{id}/follow-up-plans`（与快照、统计引用同一方案修订）。
10. **统计与报告** `POST /snapshots/{id}/analysis-runs` → `POST /reports`（版本化、冻结）。
    统计结果按中心给出纳入数与该快照冻结时的治疗分组分布（`treatment_by_center`），
    治疗分组在**建快照时冻结**写入 `snapshot_cases.treatment_code`，后续修订不会回改已有统计。
11. **迟到补传**：新上传 → 新截止批次 → `strategy=incremental` + `base_run_id`
    → `POST /screening-runs/{id}/verify-equivalence` 与全量结果逐病例比对。
12. **修订差异归因**：`POST /revision-evaluations {old_revision_id, new_revision_id, old_cutoff_id, new_cutoff_id}`
    实际运行四种组合并逐病例归因；`GET /revision-evaluations/{id}/cases|conditions|impact`
    查看四组合对照、条件语义差异与影响目录；`POST /protocols/revisions/compare` 只做条件树语义比较。
13. **迁移案卷**：`POST /revision-evaluations/{id}/migration-dossiers`（分析负责人）
    先核对评估输入是否仍然有效（失效 409 必须重评），再冻结影响目录为案卷；
    `POST /migration-dossiers/{id}/acknowledge` 记录研究者已复核。案卷从不重写旧报告、不重新批准方案。

---

## 4. 关键设计

### 4.1 两种重算方式与一致性保证

| | 全量 `full` | 增量 `incremental` |
| --- | --- | --- |
| 做法 | 扫描冻结范围内所有中心与病例 | 复用基线运行中指纹未变且未被依赖索引标记的决策，其余重算 |
| 依赖 | 无 | 新上传行 → 病例；新增/变更编码映射 → 相关源编码病例；身份候选**新建或裁决** → 双侧病例及其合并族 |
| 风险 | 每次补传都要处理所有中心 | 需维护依赖索引、防遗漏间接影响 |

**一致性由三点共同保证**：

1. **判定函数是纯函数**：`(方案规则, 截止范围内该病例的全部规范化记录, 身份裁决状态)` → 决策。
   两种策略调用同一个 `evaluate_subject`，不存在两套口径。
2. **输入指纹**：每条决策保存 `input_fingerprint`（规则哈希 + 每条有效源行的 revision/payload 哈希
   + 标准化结果 + 身份待判状态）。指纹相同的复用与重算结果必然相同。
3. **依赖索引 + 强制复核**：增量只复用「指纹相同 **且** 未被依赖索引标记」的决策；
   被标记的病例即使指纹偶然相同也会重算。`POST /screening-runs/{id}/verify-equivalence`
   再用另一种策略跑一遍同一修订/截止点，逐病例比对 outcome、原因码与纳入集合校验和。

增量还受两道闸门约束：**跨方案规则变更时拒绝**（必须全量）；**新截止批次必须包含基线批次全部上传**。

中断恢复：`ScreeningRunCenter` 逐中心检查点 —— 崩溃后 `POST /screening-runs/{id}/resume`
只重跑未完成中心，已完成中心不重算。中心内部会先清空该中心本运行的决策再重写，因此半途中断也安全。

### 4.2 五类结局严格分开

| outcome | 含义 | 是否入分析队列 |
| --- | --- | --- |
| `included` | 满足全部入排 | 是 |
| `insufficient_data` | 记录可读，但方案所需信息缺失（如无肌酐） | 否，**不判为不符合** |
| `pending_adjudication` | 标准化未决（未映射编码、不可换算单位）或身份候选未裁决 | 否，等待人工 |
| `excluded_ineligible` | 明确不符合（年龄、诊断、数值越界等） | 否 |
| `consent_withdrawn` | 撤回同意 / 拒绝参加 | 否；仅 `allow_retention` 且方案允许时进入**安全分析** |

“缺资料”与“明确不符合”不会互相折叠：只要方案关心的位置读不出来，就是 `pending`/`insufficient`，
绝不因为读不出来而判成排除。每条决策都带 `reason_codes` 与 `evidence[]`（指向 `source_record_id`），
可从统计一路追到原始行。

### 4.3 对账（锁快照的前置条件）

分析负责人必须在锁快照前确认每个中心守恒。`GET /screening-runs/{id}` 返回：

- `raw_rows == normalized_ok + pending + failed`（行守恒）
- `subjects_total == included + missing_data + pending_subjects + excluded_ineligible + consent_withdrawn`（病例守恒）
- `pending_subjects` 含 `identity_review` 子计数（身份待判），它不额外占桶
- 无孤儿决策、无重复决策、纳入病例不含未解决的有效源行
- `row_linkage.unlinked_but_ok == 0`：能标准化却挂不到任何病例的行同样会使对账失败
- `no_polluted_inclusions`：纳入病例若仍有 `pending/failed` 的有效源行则失败

任一检查失败 → `balanced=false` → `POST /screening-runs/{id}/snapshots` 返回 409。

### 4.4 数据导入与修订

- 源行**只追加**：同一 `source_uid` 的新内容生成 `revision+1`，旧版本 `active=false` 但保留，
  故统计可追到任一历史版本。
- 字节完全相同的重复补发会落库留痕，但**不激活**，且可见（`GET /uploads/{id}/rejection-report`
  的 `duplicates_not_activated` 清单）。
- 单位换算**并列保存**：`original_value/original_unit` 与 `converted_value/study_unit` 同时保留，
  并记录换算规则。边界值按换算后精确比较（如 176.8 µmol/L → 2.000000 mg/dL 落在纳入上界内）。
- 编码映射可版本化；新增映射后用 `POST /mappings/{id}/reapply?dry_run=true` 预览，
  再 `dry_run=false` 重放标准化（只改派生值，不动源行）。

### 4.5 快照、随访、统计、报告

- 快照、随访计划、统计运行**都携带同一个 `protocol_revision_id` 与截止批次**。
- 快照锁定后不可变；新数据产生**新快照**，`supersedes_snapshot_id` 指向旧快照，
  旧快照标记 `superseded` 但仍可读。
- 报告版本递增、冻结、带 `content_hash`；旧报告内容与哈希**逐字节不变**，仍指向旧快照。
- 撤回授权：`POST /subjects/{study_id}/withdraw` 立即取消未来随访任务（已完成任务保留）；
  下一轮筛选会把该病例记为 `consent_withdrawn`，仅在 `allow_retention` 且方案允许时进入安全分析。

### 4.6 身份与隐私

- 病例默认按 `(中心, 患者引用哈希)` 识别；姓名首字母 + 生日相同**只生成候选**，绝不自动合并。
- 只有隐私管理员能裁决；确认后保留较小研究号为存活主体，另一主体 `merged_into`，
  归一化行改挂到存活主体（源行仍指向原中心原始记录）。
- `GET /subjects/{study_id}/trace` 对普通角色把 `patient_ref` 置空，仅隐私管理员可见。

### 4.7 方案修订的差异归因与分析迁移评估

方案与中心资料**同时**变化时，单看新旧人数无法解释原因。一次评估固定四个输入——
旧方案修订、新方案修订、旧资料截止批次、新资料截止批次——并**实际运行四种组合**：

| 组合 | 含义 |
| --- | --- |
| `old_old` | 旧方案 × 旧资料（基线） |
| `new_old` | 新方案 × 旧资料（仅方案变化） |
| `old_new` | 旧方案 × 新资料（仅资料变化） |
| `new_new` | 新方案 × 新资料（联合） |

每个病例得到四组纳入状态、缺失原因、索引日期与终点资格对照，归因分类为
`unchanged / protocol_only / data_only / joint / masked`（最终状态不变但中间组合有差异，
即放宽与收紧相互抵消或联合遮蔽）。**最终人数不变也会保留成员替换**（进入/离开清单）与联合影响。

**条件树与语义比较**。方案条件可写成原生条件树（`all_of/any_of/not` + 叶子条件，
观察窗与缺失三态：缺资料 = `unknown`，绝不因读不出来而判不符合）；旧的扁平
`inclusion/exclusion` 形式自动编译为同构条件树，两个判定路径在样例数据上逐病例一致
（`test_compiled_tree_matches_flat_evaluator`）。每个条件有稳定 `id` 与**语义键**
（哈希临床内容，忽略显示名称）——仅改条件显示名不触发任何成员变化；
**骨架键**（结构 + 条件 id）用于对齐两人并发编辑的条件树：骨架不同则条件块整体作为一个
原子变更，绝不把各自合法的片段拼出第三套含义；骨架相同才允许按条件混合求值。

**最小充分组合（MSC）**。对确有状态变化或条件交互的病例，在变更全集（每个变更条件、
索引日期修订、资料批次）上按子集大小枚举能复现最终结局的**最小**组合。两条条件必须同时
变更才生效时，结果是一个二元组合，不会拆成两次独立新增。搜索受预算限制
（`budget_per_case`）：预算耗尽时保留已证明的组合并明确标记 `partial` 与未解释范围，
绝不把先找到的一条理由宣称为唯一原因。

**索引日期修订的依赖重判**。索引日期定义改变会同时改变年龄、检验观察窗与随访起点；
终点资格（`endpoints` 配置，相对索引日期的窗口 + 应答要求）沿这些依赖重新判定，
归因直接读自四组合对照（方案侧/资料侧效应 + 索引日期平移）。

**边界纪律**：

- 旧资料批次在新方案下缺少必要字段（如新增的 HBA1C）时保持**待判**，绝不为凑齐四组结果借用新批次资料；
- 补传、编码映射补建、身份裁决若也发生在评估窗口内，**另列为数据修订**（`data_revisions`），
  不混作方案影响；源行被更正取代后，旧截止的当前视图如实反映（更正当列明）；
- 评估运行保存输入组合指纹与条件证据（可追到原始 `source_record_id`）；
- **影响目录**连接病例资格、终点数据与已发布分析：逐分析给出
  `reusable`（可继续引用旧队列）或 `needs_reconfirmation`（需研究者重新确认），
  未受影响分析不会被整批标记；索引日期平移的随访锚点只报告数量，**不改动任何随访任务**；
- 预览后若中心再次补传、映射或身份状态变化，提交案卷时核对评估输入，
  **失效结果必须重评**（409），不能沿用旧差异清单；
- 案卷只记录影响与处置建议：不自动重写旧报告（旧报告字节与哈希不变）、不重新批准方案修订；
- 本功能只提供研究数据口径与影响解释，**不改变受试者治疗或随访安排**。

---

## 5. 验证方式

### 5.1 自动化测试

```bash
make db-test          # 建 cohorthub_test 库
make test             # 或：COHORT_DATABASE_URL=.../cohorthub_test .venv/bin/pytest tests -q
```

测试强制要求数据库名为 `cohorthub_test`（防止误清库），覆盖 55 个用例：

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_normalize.py` | 多中心编码差异、µmol/L↔mg/dL 与 g/L↔g/dL 边界、修订保留旧版、上传断点续传 |
| `tests/test_screening.py` | 全量对账守恒、五类结局分列、同日重复就诊归并、身份候选阻塞、角色隔离、增量=全量、更正后翻转、等价性校验、中断续跑、未平衡禁止锁快照、跨规则拒绝增量 |
| `tests/test_corrections.py` | 映射补建（dry-run/apply）、增量捕获映射更正、身份确认合并、**间接身份影响**、重复补发不改队列、撤回修订生效 |
| `tests/test_cohort.py` | 快照锁定与报告绑定、迟到补传送新快照且旧报告不变、草稿快照禁统计、随访计划共享方案版本、撤回只停后续联系、角色隔离、审计、报告可追溯、旧快照标记 superseded |
| `tests/test_revision_eval.py` | 条件树三态/语义键/骨架对齐、四组合实际运行、阈值+补传联合生效、放宽收紧抵消、人数不变的成员替换、索引日期迁移与终点重判、缺失条件待判不借用新批次、数据修订单列、可追溯性、未受影响分析不误标、评估后补传失效重评、预算耗尽保留部分解释、编译树与扁平求值逐病例一致 |

### 5.2 手工验证（已实际执行）

```bash
make demo
```

实际运行输出（节选自 `make demo`）：

```
每中心对账（分析负责人签核前必须全部 balanced=true）
中心        原始行    标准化    待判行    失败行    病例    纳入    缺资料    待判    不符合    撤回    守恒
EAST        19       19        0       0      3      0       1      0        1      1   True
NORTH       42       42        0       0      7      3       1      0        2      1   True
SOUTH       36       34        1       1      6      0       0      4        2      0   True
合计          97       95        1       1     16      3       2      4        5      2   True
...
增量：复用 14 例，重算 3 例，受影响 3 例
与全量重算比较 17 例：完全一致，Mismatch 0
新快照 #2（取代 #1）病例 8；报告 v2 引用方案修订 v1.0
报告 v1 仍指向快照 #1，内容校验和未变：True
```

样例数据里刻意包含的边界/异常：
- NORTH `P-N04` 肌酐 **176.8 µmol/L = 2.000000 mg/dL**，正好在纳入上界 → 纳入；
- EAST `P-E03` 血红蛋白 **80 g/L = 8.0 g/dL**，正好在排除阈值 → 排除；
- NORTH `P-N02` 同日两次筛选访视 → 归并为 1 个索引日；
- NORTH `P-N03` 撤回且允许保留 → 进入安全分析（`safety_count=1`）；EAST `P-E01` 撤回不允许保留 → 不进入；
- SOUTH `P-S03/P-S03B` 同首字母同生日 → 仅生成候选，待隐私管理员裁决；
- SOUTH `P-S05` 日期 `31/02/2024` → 标准化失败，病例记 `pending_adjudication` 而非排除；
- SOUTH `P-S02` 用药编码 `M-99` 未映射 → `pending_adjudication`，补建映射后可重算；
- NORTH `P-N07` 先为 2.5 mg/dL（越界），补传更正为 1.1 mg/dL → 下一轮筛选中翻转为纳入。

修订评估样例（demo 第 7 章实际输出节选）：

```
四种组合的病例结局分布（实际运行，不借用其他组合的资料）
组合                             纳入    缺资料    待判    不符合    撤回    不在范围
旧方案 × 旧资料（基线）                  14      3     1      5     2       1
新方案 × 旧资料（仅方案变化）                0     19     1      3     2       1
旧方案 × 新资料（仅资料变化）               16      1     1      6     2       0
新方案 × 新资料（联合）                  16      4     1      3     2       0

纳入人数 14 → 16（净 +2）；进入 4，离开 2
归因分类：data_only=2，joint=2，masked=12，protocol_only=4，unchanged=6
...
评估二：纳入人数 16 → 16（净变化 0），但成员被替换：进入 [P-S06]，离开 [P-N09]
基于评估一提交案卷：HTTP 409（评估已失效：EAST 在评估完成后又有补传 east-2024-wk16）
重评捕获新补传影响：P-E04 insufficient_data → included，最小充分组合 [[criteria_block, data]]
分析 #1/#2（快照 #1/#2 的疗效与安全分析）：可继续引用旧队列
分析 #3（快照 #3 疗效分析）：需研究者重新确认
旧报告内容校验和未变：v1=True，v2=True
```

修订评估样例的关键病例（均可由结果追到原始检验行与逐条条件判定）：
- NORTH `P-N08`：旧批次无肌酐；wk15 补传 2.1 mg/dL。**阈值放宽与补传须共同成立**：
  旧方案×新资料仍排除（2.1>2.0），新方案×旧资料仍缺资料 → 联合影响，MSC=`[criteria_block, data]`；
- NORTH `P-N10`：年龄 74 且肌酐 2.1。放宽（肌酐上限 2.0→2.2）本可纳入，收紧（年龄上限 75→70）将其拦下
  → 结局不变但理由翻转、两条条件反向交互；MSC 同时列出 `{criteria_block}`、`{index_date}`、`{data}` 三个充分组合；
- NORTH `P-N09` ↔ SOUTH `P-S06`：v1.0→v1.2 同一截止批次下人数 16→16 不变，
  但 P-N09（74 岁）离开、P-S06（180 µmol/L≈2.035≤2.05）进入 —— 成员替换完整保留；
- EAST `P-E05`：索引日期由首次就诊（2024-03-15）改为首次诊断（2024-02-20），年龄 64→63、
  检验窗与随访锚点平移；2024-04-24 随访由 +40 天（终点窗外）变为 +64 天（窗内）
  → 主要疗效终点 not_eligible → eligible，归因标记为方案侧效应（索引日期平移）；
- SOUTH `P-S07`：v1.1 新增 HBA1C 必需条件，该病例任何批次都无 HBA1C → 新方案下保持**待判**，
  不判不符合，也不借用其他批次资料；
- SOUTH `P-S01`：肌酐 200→170 µmol/L 的更正与 EAST `P-E06` 新病例 → 仅资料影响（data_only）；
- SOUTH `P-S02` 的纳入来自 M-99 映射补建、SOUTH `P-S08/P-S08B` 来自身份裁决 —— 另列为数据修订；
- EAST `P-E04`：评估完成后才补传 HBA1C → 旧评估失效（409），重评后捕获其 insufficient→included。

### 5.3 Swagger / OpenAPI

后端项目，提供可运行 OpenAPI 3.1 与 Swagger UI：服务启动后访问 `/docs`；
仓库内 `openapi/openapi.json` 由 `make openapi` 生成（43 个路径）。

### 5.4 本次实际执行过的验证

- `pytest tests -q`：**55 passed**（对本机 PostgreSQL 18 的 `cohorthub_test` 库，每次重建 schema）。
- `python -m scripts.seed_demo`：完整流程跑通（含第 7 章修订评估），输出见 5.2。
- `uvicorn app.main:app` 实际启动后：`/health` 200、`/docs` 200、`/openapi.json` 为 OpenAPI 3.1.0 且含 43 个路径、
  `Bearer analysis-token` 调用 `/whoami` 返回 `analysis_lead`。
- `python -m scripts.export_samples` 导出并校验 `samples/*.json`（178 条源行、3 个方案修订）。
- 增量 vs 全量实测：增量复用 14 例、重算 3 例；全量重算 17 例；两者逐病例 outcome 完全一致。
- 修订评估实测（demo 第 7 章）：评估一四组合实际运行（14/0/16/16 纳入），
  联合影响病例的最小充分组合为 `[criteria_block, data]`；评估二人数 16→16 成员替换 1 出 1 进；
  评估后补传导致案卷 409 失效，重评后案卷生成；旧报告 v1/v2 内容哈希未变。

关于耗时，需要如实说明：在 16 例样例上，增量与全量的**墙钟时间基本持平**
（实测 full≈230–280 ms，incremental≈300 ms），因为样例极小，逐中心对账、依赖闭包与
每例提交的固定开销占主导。增量节省的是**判定工作量**（3 次判定 vs 17 次），
这一收益随病例总量与中心数增长才会体现在耗时上；本项目没有在更大数据量上做性能压测。

**未在当前容器验证**：容器内无 `docker` 命令，因此 `Dockerfile` 与 `docker-compose.yml` 只做了静态编写，
未实际构建/启动。本地进程方式（方式 B）已完整验证。

---

## 6. 目录结构

```
app/
  main.py            FastAPI 装配、健康检查、审计、演示账号
  models.py          ORM 模型（源记录/标准化/依赖索引/运行/决策/快照/随访/统计/报告/修订评估/迁移案卷）
  security.py        Bearer 鉴权、角色校验、审计、演示账号种子
  services/
    normalize.py     多中心编码/单位/日期标准化，源行修订，断点续传，映射重放
    screening.py     判定纯函数（扁平与条件树两套条件形式）、全量与增量执行、依赖索引、中断检查点
    conditions.py    条件树：扁平规则编译、三态求值、语义键/骨架键、语义 diff、混合构造、终点评估
    evaluate.py      修订评估：四组合运行、归因分类、最小充分组合、数据修订、影响目录、失效检查、迁移案卷
    reconcile.py     逐中心行/病例守恒与污染检查
    cohort.py        快照、随访计划、统计、报告版本化
  routers/           setup / uploads / runs / cohort / privacy / revisions
scripts/
  fixtures.py        虚构多中心样例数据（含 v1.1/v1.2 修订与 wk11/wk15/wk16 批次）
  scenario.py        供 demo 与测试复用的 API 场景
  seed_demo.py       一键复现完整业务流程（含修订评估章节）
  export_samples.py  导出/校验 samples/*.json
  dump_openapi.py    生成 openapi/openapi.json
samples/             导出的样例输入（JSON）
tests/               pytest 用例
```

---

## 7. 已知边界与后续工作

- 鉴权为固定演示 token，生产需接医院 SSO 与真实审计留存策略（`audit_log` 表已就位）。
- 筛选在请求内同步执行；真实多中心规模应改为后台任务队列，但检查点/续跑语义可直接沿用。
- 未实现真实文件解析（只接受 JSON 行）。真实导入需增加 CSV/HL7 解析，标准化入口不变。
- 未使用 Redis：中断恢复依靠数据库检查点，避免额外运行时依赖。
- `POST /screening-runs` 的 `crash_after_centers` / `max_centers` 是故障注入开关（用于验证中断续跑），
  由 `COHORT_ALLOW_FAULT_INJECTION` 控制，真实部署应设为 `false`。
- 修订评估同样在请求内同步执行；样例规模（26 例 × 4 组合 + MSC 枚举）为秒级。
  更大病例量应改为后台任务，MSC 预算与全局上限已预留（`budget_per_case`）。
- 截止批次冻结的是上传集合；源行被更正取代后，旧截止的**当前视图**会如实变化
  （评估把更正列为数据修订）。若需"冻结时点的逐行版本"视图，需另存批次级行版本快照。
