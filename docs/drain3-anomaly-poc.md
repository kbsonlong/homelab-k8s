# Drain3 异常检测 POC（本地 kind 集群）

> 代码位置：`apps/drain3-poc/`。POC 密码 `drain3poc` 为一次性凭据，仅限本地验证，禁止用于任何真实环境。

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
