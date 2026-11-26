"""anomaly-scorer: 流式异常评分层 (Stage 2 POC).

每 60s 消费 Stage 1 的分钟桶聚合,产出异常分数:
- stable 基线: 冷启动期(前 COLD_START_BUCKETS 个 calm 桶)训练 per-feature
  median/MAD robust-z,冻结写入 PG,之后永不更新(防污染核心)。
- candidate 基线: River RollingQuantile/RollingIQR 滚动统计,仅在 calm 桶
  (cand_z 低、stable_z 不告警、无新模板)才 update;攻击期零更新,审计留痕。
  两者互补: stable 永不漂移(慢攻击/渐进漂移靠它持续告警),candidate 跟随
  合法流量变化(合法变更人工确认后自然吸收)。
- final = max(stable_norm, cand_norm),归一化 z/10 封顶 1.0。

注: 评估过 River anomaly.HalfSpaceTrees,其设计场景为逐事件高频流,
在 1 样本/分钟的 POC 节奏下无统计意义(calm/attack 分数不可分),
故 candidate 采用 River 滚动统计量。生产若逐事件接入可换回 HS-Trees。

状态全部持久化在 PG(scorer_state 表),重启后恢复,不丢冻结基线。
"""
import json
import logging
import math
import os
import pickle
import statistics
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
from river import stats as rstats

PG_DSN = os.getenv("PG_DSN", "host=postgres dbname=anomaly user=drain3 password=drain3poc")
INTERVAL = int(os.getenv("SCORER_INTERVAL", "60"))
COLD_START_BUCKETS = int(os.getenv("COLD_START_BUCKETS", "3"))
Z_ALERT = float(os.getenv("Z_ALERT", "5.0"))        # stable robust-z 告警线
Z_NORM_CAP = 10.0                                    # z 归一化上限(z=10 -> 1.0)
CAND_GATE = float(os.getenv("CAND_GATE", "3.5"))    # candidate 更新门槛(cand_z < 3.5)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("scorer")


def pg_conn():
    for i in range(20):
        try:
            conn = psycopg2.connect(PG_DSN)
            conn.autocommit = True   # 每条语句独立事务,避免长连接旧快照看不到新数据
            return conn
        except psycopg2.OperationalError as e:
            LOG.warning("PG not ready (%s/20): %s", i + 1, e)
            time.sleep(5)
    raise SystemExit("PG unreachable")


def ensure_tables(c):
    with c.cursor() as cur:
        # 旧 POC schema 迁移:缺 cand_z 列的表直接重建(POC 阶段允许)
        cur.execute("""
            SELECT count(*) FROM information_schema.columns
            WHERE table_name='anomaly_scores' AND column_name='cand_z'
        """)
        if cur.fetchone()[0] == 0:
            cur.execute("DROP TABLE IF EXISTS anomaly_scores")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS anomaly_scores (
                bucket TIMESTAMPTZ PRIMARY KEY,
                qps NUMERIC, err_5xx_ratio NUMERIC, new_templates INT,
                stable_z NUMERIC, cand_z NUMERIC, final_score NUMERIC,
                candidate_updated BOOL, created_at TIMESTAMPTZ DEFAULT now())
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scorer_state (
                key TEXT PRIMARY KEY, value BYTEA,
                updated_at TIMESTAMPTZ DEFAULT now())
        """)
    c.commit()


def state_get(c, key):
    with c.cursor() as cur:
        cur.execute("SELECT value FROM scorer_state WHERE key=%s", (key,))
        r = cur.fetchone()
        return pickle.loads(r[0]) if r else None


def state_put(c, key, obj):
    with c.cursor() as cur:
        cur.execute("""
            INSERT INTO scorer_state (key, value, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value, updated_at = now()
        """, (key, pickle.dumps(obj)))
    c.commit()


def last_closed_bucket(now):
    """上一个已结束的整分钟桶."""
    return (now - timedelta(seconds=now.second, microseconds=now.microsecond)
            ).replace(tzinfo=timezone.utc) - timedelta(minutes=1)


def fetch_features(c, bucket):
    with c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT COALESCE(sum(count),0) AS total,
                   COALESCE(sum(err_5xx),0) AS e5
            FROM drain3_template_counts WHERE bucket = %s
        """, (bucket,))
        r = cur.fetchone()
        cur.execute("""
            SELECT count(*) AS n FROM drain3_templates
            WHERE first_seen >= %s AND first_seen < %s + interval '60 seconds'
        """, (bucket, bucket))
        n_new = cur.fetchone()["n"]
    total = int(r["total"])
    return {"qps": float(total),
            "err_5xx_ratio": (float(r["e5"]) / total) if total else 0.0,
            "new_templates": float(n_new)}, n_new


def fit_stable(window):
    """per-feature median/MAD robust 基线. MAD 退化时回退 |med|*5% (下限 0.01)."""
    base = {}
    for f in window[0]:
        vals = [w[f] for w in window]
        med = statistics.median(vals)
        mad = statistics.median([abs(v - med) for v in vals])
        mad = max(mad, abs(med) * 0.05, 0.01)
        base[f] = {"med": med, "mad": mad}
    return base


def new_candidate():
    """per-feature River 滚动 (中位数, IQR)."""
    return {f: (rstats.RollingQuantile(window_size=20, q=0.5),
                rstats.RollingIQR(window_size=20))
            for f in ("qps", "err_5xx_ratio", "new_templates")}


def zscores(model, feat):
    """model = {'stable': {f: {med,mad}} | 'candidate': {f: (rq, riqr)}}, 取 max z."""
    zs = {}
    for f, x in feat.items():
        if f not in model:
            continue
        m = model[f]
        if isinstance(m, dict):                      # stable: frozen med/mad
            med, spread = m["med"], m["mad"]
        else:                                        # candidate: river rolling
            rq, riqr = m
            med = rq.get()
            iqr = riqr.get()
            if med is None or iqr is None:           # 空窗口(未学习过): 不可评分
                continue
            spread = max(iqr * 0.7413, abs(med) * 0.05, 0.01)
        zs[f] = 0.6745 * abs(x - med) / spread
    if not zs:                                   # 无可评分特征(空窗口)
        return 0.0, "n/a", {}
    top = max(zs, key=zs.get)
    return zs[top], top, zs


def main():
    c = pg_conn()
    ensure_tables(c)
    LOG.info("scorer up: cold_start=%d buckets, interval=%ds", COLD_START_BUCKETS, INTERVAL)

    stable = state_get(c, "stable_baseline")
    cand = state_get(c, "candidate_model") or new_candidate()
    cold_window = state_get(c, "cold_window") or []
    cold_done = stable is not None
    if cold_done:
        LOG.info("recovered frozen baseline + candidate from PG (restart, no re-cold-start)")

    while True:
        time.sleep(INTERVAL)
        b = last_closed_bucket(datetime.now(timezone.utc))

        feat, n_new = fetch_features(c, b)
        if feat["qps"] == 0:
            LOG.info("bucket %s empty, skip", b)
            continue

        # --- stable 冻结基线 ---
        if not cold_done:
            cold_window.append(feat)
            state_put(c, "cold_window", cold_window)
            if len(cold_window) >= COLD_START_BUCKETS:
                stable = fit_stable(cold_window)
                state_put(c, "stable_baseline", stable)
                cold_done = True
                LOG.info("[COLD-START-DONE] baseline FROZEN from %d calm buckets: %s",
                         len(cold_window), json.dumps(stable, default=str))
            else:
                LOG.info("[COLD-START] %d/%d buckets collected, scoring paused",
                         len(cold_window), COLD_START_BUCKETS)
                continue
        stable_z, top_f, _ = zscores(stable, feat)
        cand_z, top_c, _ = zscores(cand, feat)
        final = round(max(min(stable_z / Z_NORM_CAP, 1.0),
                          min(cand_z / Z_NORM_CAP, 1.0)), 4)
        alert = stable_z >= Z_ALERT or cand_z >= Z_ALERT

        # --- candidate 更新门控: 仅 calm 桶滚动更新(防污染核心) ---
        updated = False
        if cand_z < CAND_GATE and stable_z < Z_ALERT and n_new == 0:
            for f, x in feat.items():
                if f in cand:
                    cand[f][0].update(x)
                    cand[f][1].update(x)
            state_put(c, "candidate_model", cand)
            updated = True
            LOG.info("[CANDIDATE-UPDATE] bucket=%s cand_z=%.2f", b, cand_z)

        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO anomaly_scores (bucket, qps, err_5xx_ratio, new_templates,
                    stable_z, cand_z, final_score, candidate_updated)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (bucket) DO UPDATE SET
                    stable_z=EXCLUDED.stable_z, cand_z=EXCLUDED.cand_z,
                    final_score=EXCLUDED.final_score, candidate_updated=EXCLUDED.candidate_updated
            """, (b, feat["qps"], feat["err_5xx_ratio"], int(n_new),
                  round(stable_z, 2), round(cand_z, 2), final, updated))
        c.commit()

        LOG.info("[SCORE] bucket=%s qps=%d err5xx=%.3f new_tpl=%d "
                 "stable_z=%.1f(top=%s) cand_z=%.1f(top=%s) FINAL=%s %s",
                 b, int(feat["qps"]), feat["err_5xx_ratio"], n_new,
                 stable_z, top_f, cand_z, top_c, final,
                 "🚨ALERT" if alert else "ok")


if __name__ == "__main__":
    main()
