# Drain3 异常检测方案 V2（POC 部分实现）

> 本节替代下方 POC 的生产演进假设。下方 Stage 1–3 仅保留为历史实验记录，其中“隔离”“兼容 Alloy”“计数可靠”等结论不能作为 V2 已实现或生产验收的依据。V2 先只做旁路观测，不触发自动封禁。

## 容量输入与部署边界

用户提供的近 7 天速率是 exporter 的发送速率，而不是已验证的接收或解析吞吐。各集群独立部署、独立队列、状态、资源预算和故障域，不把低流量集群与高流量集群混合训练。下表的压测目标为观测峰值的 1.5 倍，向上取整至百位；这是初始验收目标，不是机器规格或吞吐承诺。实际还需覆盖更短时间粒度的突发和重放流量。

| 集群 | 平均条/s | P95 条/s | 观测峰值条/s | 初始持续压测目标条/s |
|---|---:|---:|---:|---:|
| 集群 A | 14,793 | 17,599 | 19,340 | 29,100 |
| 集群 B | 6,823 | 9,353 | 11,790 | 17,700 |
| 集群 C | 1,173 | 1,289 | 1,440 | 2,200 |
| 集群 D | 449 | 760 | 1,908 | 2,900 |
| 集群 E | 89 | 112 | 144 | 300 |
| 集群 F | 5 | 12 | 34 | 100 |
| 集群 G | 0 | 0 | 0 | 待测 |

高流量集群不采用现有单 Python 进程 + 单 PVC 聚类树作为生产架构。30,000 条/s 连续 1 小时约 1.08 亿条，20,000 条/s 约 7,200 万条；高基数模板或每条日志单独写 PG 都不可取。压测前不估算 CPU、内存或磁盘规格。队列容量按**实测压缩后字节/s × 要求容忍的故障秒数 × 安全系数**计算，并验证恢复吞吐高于新流入速率；记录压缩比、保留期限和满盘行为。

## 数据流与三个优先修复

```text
Alloy accesslogs ──独立旁路──> Loki Push 接入层 ──持久队列──> 分区处理器
                                                          ├─ 已发布模板快照：只读匹配
                                                          ├─ 未匹配日志：隔离候选聚类
                                                          └─ 事件时间分钟桶：可重放聚合
审批服务 ──> 模板版本/发布/回滚 ──> 已发布模板快照
聚合结果 ──> PG/指标存储 ──> 评分器（数据完整后评分）
```

### 1. 模板隔离和发布

- 已发布库和候选库使用**独立** Drain3 实例与持久状态；已发布库处理在线流量时只调用 `match()`，绝不调用 `add_log_message()`。未匹配日志进入候选实例；候选实例只能产出待审核模板，不能修改已发布库。单独隔离的训练流程可以构建新快照。
- 模板状态至少为 `pending / approved / rejected / retired`；审批绑定集群、服务范围、规范化规则版本、模板内容、审核人和时间。已发布快照有不可变版本、发布记录和回滚入口。审核动作与发布动作明确区分；校验通过后原子切换，处理中的批次固定使用一个版本。
- 模板泛化会改变匹配范围：发布前对已确认模板及历史样本回放，比较合并/拆分、误匹配和未匹配率，避免候选路径扩大为能覆盖其他已知路径的通配模板。未匹配、候选变化、审批拒绝、版本切换分别计数；不要仅用首次创建模板数代表持续未知流量。
- 原始样本可能含敏感路径或参数，保存前进行脱敏、访问控制与保留期限约束。候选数量、样本容量、单租户增长速度均须限制；达到上限时产生显式降级告警，不得静默并入已发布库。

### 模板版本更新与切换方案

人工审核候选模板后生成不可变版本快照，记录 scope、模板内容、规范化规则版本、审核人与变更说明；先通过历史样本回放和匹配范围检查，再以数据库事务更新活动版本指针。回滚通过复制旧快照形成新版本，保留版本链和审计记录。

生产消费者将已发布版本常驻内存，以 `LISTEN/NOTIFY` 获取变更提示，并通过定期批量检查版本和重连检查兜底。后台构建并验证新快照，成功后在批次边界原子切换；构建失败继续使用旧版本并告警。聚合结果记录实际版本，各消费者上报加载状态。多消费者可能短暂使用不同版本；若需要在同一队列位置严格切换，须另行设计协调屏障。

**POC 范围**：先使用数据库中的审核记录、不可变快照、活动版本和批次级加载，验证发布与回滚闭环；暂不实现通知、内存热更新、多消费者协调和自动历史回放。未经验证的候选模板不得发布到生产。

### 2. Alloy 接入与故障隔离

- 接入层实现并测试 Loki `/loki/api/v1/push` 的 `application/x-protobuf` + **Snappy block** 和 JSON 两种格式，处理时间戳、stream labels 和可选 structured metadata；验证实际 Alloy 版本及请求编码，限制解压后大小与单批条数。兼容的是协议能力，不能仅凭相同 URL 宣称兼容。
- 根据标签映射集群和服务；禁止使用固定的 `SOURCE_HOST_LABEL` 冒充来源。未识别标签、非法格式和无法解析的 access log 有可观测的拒绝/死信处理，不能对整批静默返回成功。
- 仅在批次已持久写入队列之后返回 2xx；队列不可用或过载时返回可重试的 5xx/429，明确 Alloy 的重试次数、WAL、容量和丢弃行为。持久队列的复制、确认语义和故障域须按所选实现实测。
- 同一 `loki.write` 的多个 endpoint 可能相互影响；生产方案先验证独立旁路的 fan-out、队列和 WAL 是否与主日志路径真正隔离，演练旁路停机、限速和满盘，确保不影响主链路。禁止为了 POC 改动现有主链路交付保证。

### 3. 计数与重放语义

- 日志的 Loki 时间戳用于事件时间分桶；定义允许迟到时间、时钟偏差上限和 watermark。晚于 watermark 的数据进入修正/重算流程并产生指标，超过保留范围的记录进入可审计的死信，而不是写入当前桶。评分器按完成标记和 watermark 消费，不能靠固定 `:50` 采样或将“无数据”等同于零流量。
- 队列记录分区和 offset。单个分区顺序消费；**聚合增量、已处理 offset/批次标记与消费进度在同一事务中提交**，提交后再确认消费。重启或重放已提交 offset 不得再次累加；未提交的事务允许重新处理。多个消费者使用租约/分区所有权和 fencing，避免同一分区并发写入。异构存储无法共用事务时，使用事务性 outbox 或可重算的确定性分区结果，禁止仅凭 UPSERT 累加声称幂等。
- 标准 Loki Push 请求没有通用的稳定批次 ID。发送端收到成功前发生超时或断连时可能重发已持久化的请求，接入端无法仅靠标准协议判断两个完全相同的批次是重试还是两次合法发送；内容哈希去重会误删合法重复。V2 对已持久入队批次提供可重试处理，接收边界可能重复；上游重试耗尽等情况仍可能丢失，不声称端到端至少一次或精确一次。若业务需要精确计数，必须先增加由发送方持久生成、重试时不变的事件/批次 ID 或引入具备相应身份与事务语义的上游；评估延迟、重复率和实现成本后再选型。
- 将解析失败、无流量、数据迟到、队列积压和真实流量下降分别表示；计数表记录来源维度、时间桶、模板版本和修订版本。P95 延迟使用可合并的直方图或 sketch，不对分批 P95 直接求和或覆盖。
- 宕机恢复、提交前后崩溃、数据库故障、请求响应丢失、队列重放和跨桶迟到均要有自动化故障测试。接收、持久化、处理、输出的数量及差异必须可核对，容许误差由接收边界的重复语义决定。

## 落地顺序和验收

1. **接口契约与接入 POC**：捕获真实 Alloy 发送格式，完成 Snappy/Protobuf、JSON、标签和事件时间解析；实际 Alloy 联调，验证成功响应只发生在持久化之后。确认独立旁路对主链路的影响。
2. **隔离与持久处理**：实现已发布/候选双实例、版本发布和回滚；选定队列后实现事务性消费与事件时间聚合，建立计数对账、积压、丢弃、解析失败及迟到监控。明确请求重试重复的处理边界。
3. **分集群压测与故障演练**：以表中目标分别测试持续流量、短时突发、故障期间缓冲及恢复追赶；记录 CPU、内存、磁盘、吞吐、P95/P99 延迟、积压和对账误差。零流量集群不能套用其他集群速率。
4. **评分器再接入**：按已完成桶、数据质量和来源维度评分；先用真实历史数据回放、人工审核，独立评估误报、漏报和时延。三桶冷启动、固定阈值、全局 QPS 和 approve 最近三桶均不作为生产验收条件。

### 当前 POC 的实现与限制

接收器使用 PostgreSQL `poc_v2_batches` 作为持久队列，仅在入队事务提交后返回 204。单消费者通过 advisory lock 串行处理；计数、候选模型状态、拒绝计数和批次完成标记在同一事务提交。数据库重试消费不会重复计算已完成批次；发送端因响应丢失而重复发送仍会重复计数。数据库、磁盘或上游重试耗尽也可能导致丢失，不能称为端到端至少一次保证。

`poc_v2_templates` 以完整 stream labels 为范围：`approved` 模板各自拥有独立只读匹配器，未匹配日志仅进入该范围的候选模型。POC 已提供审核记录、版本快照和回滚命令，每个批次按 scope 加载当前版本；标签集合可能因 Pod 等高基数字段变化而产生大量 scope。事件按 Loki 时间戳入分钟桶；V2 评分器使用本地队列状态和允许迟到窗口做暂定完成判断，迟到事件会修订桶并重新评分。V1 评分器不再启动，V2 的结果仅供 POC 观察。处理失败事务回滚并重试，解析不了的 access log 在 `poc_v2_rejections` 按批计数。原始批次目前保存在 PG，需限制访问并自行管理本地 POC 数据生命周期；候选状态使用 jsonpickle，数据库写权限必须可信，不能将该持久化方式直接用于生产。

POC 是单进程、单数据库处理器，每次请求建立数据库连接，候选模板可逐条写库；未实现生产级队列、吞吐保障、横向扩缩容、现网 Alloy 配置联调、端到端水位、审批身份认证、自动回放校验和压力测试。不得根据 V2 设计目标推断这些能力已经具备，更不得直接用于上述高流量集群或自动封禁。

### 桶完成、迟到修正与评分（POC）

- `poc_v2_buckets` 按 scope 与事件时间分钟桶记录 `open / complete` 和修订号。评分器仅在**本地持久队列没有待处理批次**，且当前时间已超过桶结束时间加 `ALLOWED_LATENESS_SECONDS`（默认 120 秒）时暂定完成。接收、聚合和评分共享数据库事务锁，避免检查期间的新批次造成竞争。此锁是低并发 POC 做法；上游丢失、采集停止和未到达的日志不能从本地队列判断。没有数据的桶不会自动补零，也不会产生正常分数。
- 迟到批次持久入队时就会在同一事务内把受影响的旧分数标记 `stale`，然后才返回成功；后台计数写入在同一事务中将桶改回 `open`、增加 revision。队列再次清空后评分器重新完成并重新计算；读取者必须排除 stale、非 complete 和没有有效基线的记录。如果迟到事件落入已审核的基线窗口，相关基线及其所有衍生分数失效，必须重新人工审核基线，不自动学习更改。
- 基线由操作人指定某个 scope 的连续、非空、已完成分钟区间，至少 3 桶；不允许含 candidate 流量或待处理队列。每次审批创建新基线并记录窗口、操作人和说明；这是 POC 操作记录，并非身份认证。基线使用 QPS（分钟计数除以 60）和 5xx 比例的 median/MAD；评分使用各特征最大 robust-z，默认阈值 5，归一化分数上限 1。存在 candidate 流量时单独触发告警并使分数为 1。分数是启发式指标，不是攻击概率；合法新路径也可能触发。
- `poc_v2_scores` 状态包括 `no_baseline / baseline_invalid / ok / alert`。前两种没有有效评分；只有 `status='alert' AND NOT stale` 且对应桶为 `complete` 的记录才表示当前 POC 告警。结果存 PG，尚未接外部通知或自动封禁。单点 PG、全局事务锁、时间窗口、阈值和 scope 基数均需在后续真实数据及故障测试中评估。

本地 kind 部署（在仓库根目录执行；仅供隔离环境）：

```bash
docker build -t drain3-sidecar:v2-poc -f apps/drain3-poc/docker/Dockerfile apps/drain3-poc/sidecar
kind load docker-image drain3-sidecar:v2-poc --name poc
kubectl apply -f apps/drain3-poc/k8s/postgres.yaml
kubectl apply -f apps/drain3-poc/k8s/
kubectl -n anomaly-poc exec deploy/postgres -- psql -U drain3 -d anomaly -c "SELECT id,scope,kind,template FROM poc_v2_templates ORDER BY id"
kubectl -n anomaly-poc exec deploy/postgres -- psql -U drain3 -d anomaly -c "SELECT template_id,bucket,source,count,revision FROM poc_v2_counts ORDER BY bucket,template_id"
kubectl -n anomaly-poc exec deploy/postgres -- psql -U drain3 -d anomaly -c "SELECT id,processed_at,parsed_count,rejected_count FROM poc_v2_batches ORDER BY id"
kubectl -n anomaly-poc exec deploy/postgres -- psql -U drain3 -d anomaly -c "SELECT scope,bucket,revision,status FROM poc_v2_buckets ORDER BY bucket"
kubectl -n anomaly-poc exec deploy/postgres -- psql -U drain3 -d anomaly -c "SELECT scope,bucket,status,qps,err_5xx_ratio,candidate_count,score,stale FROM poc_v2_scores ORDER BY bucket"
```

POC 审核和发布命令（以查询到的候选 ID 和 scope 为准）：

```bash
# 仅做本地人工审核；actor 是操作人自行填写的标识，尚无身份认证。
kubectl -n anomaly-poc exec deploy/drain3-sidecar -- python /app/template_admin.py review <candidate_id> approve --actor reviewer --note "manual review"
kubectl -n anomaly-poc exec deploy/drain3-sidecar -- python /app/template_admin.py publish <candidate_id> --actor operator --note "publish reviewed candidate"
kubectl -n anomaly-poc exec deploy/postgres -- psql -U drain3 -d anomaly -c "SELECT id,scope,parent_id,action,source_version,actor,note FROM poc_v2_versions ORDER BY id"
# 传入查询得到的完整 scope JSON；回滚生成新版本，不修改原快照。
kubectl -n anomaly-poc exec deploy/drain3-sidecar -- python /app/template_admin.py rollback '<scope-json>' <target_version> --actor operator --note "rollback"
```

`review` 支持 `approve` / `reject`，发布要求该候选模板最新审核为 `approve`；`reject` 不会自动撤销已发布版本，需另行回滚。每个批次在首次遇到某 scope 时读取活动版本，该批次中的该 scope 固定使用同一快照；计数按 `template_version` 分开，0 表示未发布版本或升级前历史数据。审核、发布和回滚操作记录在 PG，但这仅是 POC 审计，不能替代身份认证、权限控制或发布前回放。

审批基线的窗口是左闭右开，时间必须含时区且对齐整分钟；请先人工核对模板、窗口和采集质量：

```bash
kubectl -n anomaly-poc exec deploy/anomaly-scorer -- python /app/scorer_v2.py approve-baseline '<scope-json>' '2026-01-01T00:00:00+00:00' '2026-01-01T00:03:00+00:00' --actor reviewer --note "reviewed baseline window"
```

升级说明：旧表原有计数保留并标记版本 0；旧的 `kind=approved` 行不会自动成为新版本。必须重新选择候选项并审核、发布后才会用于在线匹配。历史计数不重新分类。

测试依赖使用 `pytest`、`httpx` 和 `requirements.txt`；设置 `POC_TEST_DSN` 指向**专用测试数据库**后运行 `pytest -q apps/drain3-poc/tests`。测试会清空该数据库中的 `poc_v2_*` 表；运行数据库故障测试时不要同时启动外部处理器。未设置 DSN 时数据库测试会跳过。

真实 Alloy 冒烟测试使用 `tests/alloy-smoke.alloy`，让 Alloy 的 `loki.source.api` 接收测试日志、经 `loki.write` 发送到独立测试 sidecar；设置 `POC_ALLOY_PUSH_URL` 为该 API 的 Push 地址和 `POC_TEST_DSN` 为专用测试库后，单独运行 `pytest -q apps/drain3-poc/tests/test_v2.py::test_real_alloy_loki_write_to_postgres`。2026-11-27 本地使用 Alloy v1.22.0（arm64）通过了该测试，验证 204、计数、5xx 及事件时间桶；这不代表现网版本、旁路故障隔离或性能已经验证。

### 真实 Alloy 完整流程与恢复测试

2026-11-27 在本地隔离 Docker 容器中，使用 Alloy v1.22.0、PostgreSQL 16 和 V2 镜像，完成一次真实端到端场景：候选模板产生 → 人工审核发布 → 三个已完成桶审批基线 → 既有路径流量突增告警 → 新路径迟到后重算 → 基线窗口迟到导致基线失效 → 人工重新审核后恢复评分。随后重启接收器和评分器，验证已发布模板、基线和原有计数恢复；短暂停止 PostgreSQL，观察接收器返回 503，恢复后 Alloy 重试的 7 条日志全部计入并完成评分。最后一次复跑数据库不可用约 3.8 秒，队列清空后继续观察 3 秒，未见这 7 条的重复计数；恢复后另验证了 5xx 比例异常告警。

测试使用历史时间戳、1 秒评分周期和 0 秒允许迟到窗口，以便快速触发各阶段；这不是生产参数，也不证明任意故障都不会丢失或重复。它没有覆盖上游中断、长时间积压、处理事务中途强杀、网络分区、真实业务日志或生产负载。

可在具备 Docker、`rtk`、`pytest` 及 `requirements.txt` 依赖的本地环境复跑（仅使用专门创建的隔离测试容器，不指向已有数据库）：

```bash
docker build -t drain3-v2-e2e-test-local -f apps/drain3-poc/docker/Dockerfile apps/drain3-poc/sidecar
POC_E2E_DOCKER=1 POC_E2E_IMAGE=drain3-v2-e2e-test-local:latest pytest -s -q apps/drain3-poc/tests/test_alloy_recovery_e2e.py
```

测试运行时创建带唯一名称的 PostgreSQL、接收器、评分器和 Alloy 容器；结束时停止它们，保留已停止容器供检查。

具体队列、数据保留时间、允许迟到窗口、资源规格与验收阈值需根据实际环境及压测确定。

---

# Drain3 异常检测 POC（本地 kind 集群）

> 代码位置：`apps/drain3-poc/`。历史 POC 使用一次性凭据，仅限本地验证。

流式日志归一化 + 模板持久化 + 计数累加的端到端验证。对应技术路线：River + Drain3 + PySAD 的第一步（Drain3 特征提取层），验证防污染基线原则中的"新模板默认隔离"。

## 架构

```
log-generator (合成 nginx access log)
      │  Loki push 协议 (/loki/api/v1/push)
      ▼
drain3-sidecar (FastAPI + Drain3)
      │  ├─ 归一化: URL 数字段→{D}, 长hex→{HASH}, 去query
      │  ├─ Drain3 聚类: 模板文本, FILE 持久化(/data/templates.pkl)
      │  ├─ 新模板 → PG drain3_templates (status=pending)  ← 隔离,不入已知库
      │  └─ 每分钟计数 → PG drain3_template_counts (UPSERT 累加)
      ▼
postgres (模板元数据 + 计数时序 = 基线特征来源)
```

## 验证点

1. **模板归一化聚类**：`/api/v1/users/123` 与 `/api/v1/users/456` 聚为同一模板
2. **新模板隔离**：基线期模板 + 攻击期新模板（/wp-login.php 等）均落 pending
3. **人工确认闭环**：SQL 模拟人工确认基线模板 → confirmed → sidecar 60s reload 进已知库
4. **攻击模板不被吸收**：确认后攻击模板仍 pending，不污染基线
5. **计数累加**：`ON CONFLICT DO UPDATE count = count + EXCLUDED.count` 原子累加

## 部署与验证

```bash
kind create cluster --name poc --config kind-config.yaml
docker build -t drain3-sidecar:poc -f docker/Dockerfile ./sidecar
kind load docker-image drain3-sidecar:poc --name poc
kubectl apply -f k8s/

# 观察 generator 完成后（Stage 1 约4分钟；Stage 2 约7分钟）:
kubectl -n anomaly-poc exec deploy/postgres -- psql -U drain3 -d anomaly \
  -c "SELECT id,template,status,count(*) FROM drain3_templates GROUP BY 1,2,3 ORDER BY 1"

# 计数累加验证
kubectl -n anomaly-poc exec deploy/postgres -- psql -U drain3 -d anomaly \
  -c "SELECT template_id,bucket,count FROM drain3_template_counts ORDER BY bucket,template_id LIMIT 20"
```

## 清理

```bash
kubectl delete namespace anomaly-poc
kind delete cluster --name poc
```

## 生产演进映射（POC → 真实环境）

| POC 组件 | 生产对应 |
|---------|---------|
| log-generator | Alloy 旁路 loki.write → VictoriaLogs / sidecar |
| drain3-sidecar /loki push | 同协议直接对接 Alloy 双写 |
| postgres | PG（source of truth）+ VictoriaMetrics（计数时序） |
| SQL UPDATE 确认 | review Web 界面 / 审批流 |
| confirmed reload 60s | 同机制 |

## POC 验证结果（2026-11-26，kind v0.30 / k8s v1.34.0）✅ 全部通过

| 验证点 | 结果 |
|--------|------|
| 归一化聚类 | `/api/v1/users/{D}` 数字段归一，不同路径独立成簇（sim_th=0.8） |
| 新模板默认隔离 | 8 个模板全部先落 pending，无一自动进已知库 |
| 攻击检测 | 60s 攻击波 3 个新路径（wp-login/config.php/passwd）即时捕获，计数 ~2.5× 基线 |
| 人工确认闭环 | SQL 确认 5 个基线模板 → sidecar 60s RELOAD 进已知库 |
| 攻击模板不被吸收 | 攻击模板持续 pending，基线不受污染 |
| 计数累加 | `ON CONFLICT DO UPDATE count=count+EXCLUDED` 跨 5 个分钟桶正确累加；5xx 单独计数（/orders err5=35） |
| 重启恢复 | FilePersistence pickle 恢复模板树，老模板零重复注册，新路径照常 pending |

踩坑记录（对生产实现有效）：
1. `TemplateMiner(persistence_handler, config)` — **参数顺序 persistence 在前**，与直觉相反
2. `change_detected` 键仅在模板变化时存在，必须 `.get()`
3. Dockerfile 必须设 `WORKDIR`，否则 `uvicorn app:app` 会 import 到同名目录（namespace package）
4. status 不进 Drain3 消息（避免同路径不同状态码碎片化），只进计数器列
5. drain3 0.9.11 无 `get_num_cluster_ids`/`cfg.set()`，配置走 `cfg.drain_sim_th/drain_depth` 属性

---

# Stage 2：anomaly-scorer 流式异常评分（防污染双层基线）✅

> 核心原则：**异常分数可以实时生成，模型基线不能无条件实时更新。** 使用隔离的稳定基线、候选基线以及人工确认反馈，防止恶意行为逐渐污染正常模型。

## 架构（Stage 1 之上追加）

```
drain3_template_counts (分钟桶聚合)
      │ 每 60s (anomaly-scorer Deployment, 独立入口 python /app/scorer.py)
      ▼
特征: qps / err_5xx_ratio / new_templates
      │
      ├─ stable 基线: 前 3 个 calm 桶拟合 per-feature median/MAD robust-z → 冻结写入 PG scorer_state, 永不更新
      ├─ candidate 基线: river.stats RollingQuantile(0.5)+RollingIQR(w=20), 仅 calm 期 update
      │    (calm 判定 = stable 不告警 且 new_templates==0; 攻击期零更新 + candidate_updated 审计列留痕)
      └─ final = max(stable_z, cand_z) / Z_NORM_CAP(10) 封顶 1.0; Z_ALERT=5.0 → 🚨ALERT
      ▼
PG anomaly_scores (bucket, features, stable_z, cand_z, final_score, candidate_updated)
```

设计要点：
- **stable 抓慢攻击/渐进漂移**（永不漂移）；**candidate 跟随合法流量变化**（合法变更人工确认后自然吸收）——两层互补
- robust-z 公式：`0.6745×|x−med|/mad`，MAD 退化回退 `max(mad, |med|×5%, 0.01)`
- candidate spread：`max(iqr×0.7413, |med|×5%, 0.01)`；样本不足时回退 stable 评分
- River `anomaly.HalfSpaceTrees` 经实测**弃用**：设计场景为逐事件高频流，1 样本/分钟节奏下加 MinMaxScaler 仍不可分（calm 0.9486 反而最高）；生产若逐事件接入可回评

## 时序参数（k8s/generator.yaml）

`DURATION=420 / ATTACK_AT=200`：保证前 3 个整分钟桶为纯 calm 冷启动期；scorer `COLD_START_BUCKETS=3`，冷启动期只采桶不评分。

## Stage 2 验证结果（2026-11-26 实测）

| bucket (UTC) | qps | new_tpl | stable_z | final | candidate_updated |
|---|---|---|---|---|---|
| calm | 1200 | 0 | 0.0 | 0.000 | t |
| 攻击首桶 | 2225 | **3** | **202.4** | 1.000 🚨 | **f** |
| 攻击×3 | 3613–3671 | 0 | 27.1–27.8 | 1.000 🚨 | **f** |
| 恢复尾桶 | 964 | 0 | 2.7 | 0.265 | t |

1. 基线低分 ✓（calm 0.0，恢复 0.265 不误报）
2. 攻击超阈 ✓（new_templates 出现即 z=202，纯流量放大 z≈27，连续 4 桶 ALERT）
3. candidate 攻击期零更新 ✓（`candidate_updated` 审计列：攻击 4 桶全 f，仅 calm 前后 t）
4. stable 冻结 ✓（`[COLD-START-DONE] baseline FROZEN`，MAD 回退生效无退化误报）
5. 重启恢复 ✓（`recovered frozen baseline + candidate from PG (restart, no re-cold-start)`）

## Stage 2 踩坑记录

1. **MAD 退化误报**：合成基线 qps 恒值 → MAD=0 → 微小偏差 z=13.5 假告警 → 回退 `max(mad, |med|×5%, 0.01)`
2. **River 滚动统计空窗口**：未学习过时 `RollingQuantile/IQR.get()` 返回 **None**（不是 0），直接参与计算 TypeError 崩溃 → 必须判 None
3. **PG 长连接旧快照**：psycopg2 默认 autocommit=False，scorer 常驻连接 idle-in-transaction——既看不到其他进程新提交的数据（评分"empty"假象），又持有锁阻塞 TRUNCATE → `conn.autocommit = True`
4. **表 schema 漂移**：POC 迭代改列名后 `CREATE TABLE IF NOT EXISTS` 不生效 → ensure_tables 检测缺列则 DROP 重建（生产应用 ALTER 迁移）
5. **HalfSpaceTrees 不可用于分钟桶**（见上，已换 RollingQuantile/IQR）
6. kind load 后 `crictl images` 显示旧 hash 有误导，以 pod 内 `exec grep <新代码特征串>` 为准
7. **kind load 不更新同名 tag**：节点 ctr 里 tag 引用可能仍指旧镜像 ID（kubelet IfNotPresent 拿旧镜像）→ 用 `docker save <img> | docker exec -i <node> ctr --namespace=k8s.io images import -` 手动导入并以 `ctr images list` 核对 ID

---

# Stage 3：人工确认反馈闭环 + 慢速爬坡检测（防污染原则收口）✅

> 补齐原则最后一块：**基线只能经人工确认更新。** 并验证冻结基线对"温水煮青蛙"式慢攻击的拦截。

## 场景时序（generator 多相位，DURATION=900）

| 相位 | 时间 | 内容 | 预期 |
|------|------|------|------|
| calm | 0–240s | 20 行/s 基线 | 冷启动 3 桶冻结 |
| LEGIT_SURGE_AT=240 | 240s 起 | qps×2 + 新端点 /api/v2/checkout 上线 | **未确认 → 持续 ALERT**（正确） |
| 人工确认 | ~470s | SQL: 模板 confirmed + `INSERT baseline_feedback('approve_refit')` | 60s 内重训 → **静默** |
| RAMP_AT=600 | 600s 起 | 对已有端点每秒 +0.15 行线性爬坡 | candidate 渐升不越阈，**stable 冻结基线越阈持续 ALERT** |

## 反馈闭环机制（scorer）

- PG 表 `baseline_feedback(id, action, note, created_by, created_at, applied_at)`——**stable 基线唯一的变更通道**
- `action='approve_refit'`：从最近 REFIT_WINDOW=3 个已评分桶重训 stable（median 对混合桶鲁棒）并重新冻结；candidate 同步重置 + 同窗口重播种（否则 candidate 停在旧水位永久误报）
- `applied_at` 落表 + `[REFIT-APPROVED]` 日志 = 审计留痕；窗口不足时保留待处理不吞指令
- 采样对齐：scorer 每 tick 对齐到每分钟 `TICK_SECOND=50`s（sidecar flush 是启动锚定周期，采样点早于 flush 点会永远读到空桶）+ 空桶 30s 重试兜底

## Stage 3 验证结果（2026-11-26 实测，13 桶）

| 相位 | qps | stable_z | candidate_updated | 结果 |
|------|-----|----------|-------------------|------|
| calm | 1200–1220 | 0.0–0.2 | t | FINAL 0.02 |
| surge 未确认 | 2640–2684 (+new_tpl) | 16.2–67.5 | f | 持续 🚨ALERT（正确） |
| approve 后 | 2640–2684 | 0.0–0.2 | t | FINAL 1.0 → **0.0 静默** |
| 爬坡 | 2655→3094→3587→4125 | 0.1→2.3→4.8→**7.6** | t→f | z=4.8 精准卡在阈下；+56% 越阈持续 🚨ALERT |

1. 合法变更闭环 ✓（告警→确认→静默，全程 ≤2 tick）
2. 基线变更唯一通道=人工确认 ✓（feedback 表 + applied_at 审计）
3. 煮青蛙拦截 ✓（candidate 渐升吸收，stable 冻结基线越阈拦截）
4. 攻击期 candidate 零更新审计 ✓

## Stage 3 踩坑记录

1. **flush/tick 相位竞态**：sidecar flush 周期启动锚定，scorer 采样点早于 flush 点 → 每个桶都"空"（Stage 3 首跑全空桶真因）→ 采样对齐 :50s + 空桶重试
2. **approve_refit 后 candidate 必须重播种**：合法变更期 candidate 被门控挡住未学到新水位，只重训 stable 会永久 cand_z≈16 误报（首轮实测）→ 重训时同步重置 candidate
3. 重试消耗 tick 周期可能跳过一个桶（POC 可接受；生产应改为独立重试队列）
