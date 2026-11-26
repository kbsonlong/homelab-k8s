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

# 观察 generator 完成后（约4分钟）:
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
