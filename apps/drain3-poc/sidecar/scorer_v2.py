"""POC event-time bucket finalization, explicit baseline approval, and scoring.

Completion uses a processing-time lateness horizon and an empty local queue; it
is provisional and does not prove upstream delivery or detect missing traffic.
"""
import argparse
import json
import logging
import os
import statistics
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

import psycopg2
from psycopg2.extras import Json
import app

LOG = logging.getLogger("scorer-v2")
LATENESS_SECONDS = int(os.getenv("ALLOWED_LATENESS_SECONDS", "120"))
INTERVAL = int(os.getenv("SCORER_INTERVAL_SECONDS", "10"))
ALERT_Z = float(os.getenv("ALERT_Z", "5"))


def features(cur, scope, bucket):
    cur.execute("""SELECT COALESCE(sum(c.count),0), COALESCE(sum(c.err_5xx),0),
        COALESCE(sum(c.count) FILTER (WHERE t.kind='candidate'),0)
        FROM poc_v2_counts c JOIN poc_v2_templates t ON t.id=c.template_id
        WHERE c.source=%s AND c.bucket=%s""", (scope, bucket))
    total, err5, candidates = (int(n) for n in cur.fetchone())
    if not total:
        raise ValueError("bucket has no valid events")
    return {"qps": total / 60.0, "err_5xx_ratio": err5 / total,
            "candidate_count": candidates}


def fit_model(samples):
    model = {}
    for name in ("qps", "err_5xx_ratio"):
        vals = [s[name] for s in samples]
        median = statistics.median(vals)
        mad = statistics.median(abs(v - median) for v in vals)
        model[name] = {"median": median, "spread": max(mad, abs(median) * .05, .01)}
    return model


def require_utc_minute(value):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("window timestamps require timezone")
    value = value.astimezone(timezone.utc)
    if value.second or value.microsecond:
        raise ValueError("window timestamps must be minute-aligned")
    return value


def approve_baseline(conn, scope, start, end, actor, note):
    app.require_actor_note(actor, note)
    start, end = require_utc_minute(start), require_utc_minute(end)
    minutes = int((end - start).total_seconds() // 60)
    if minutes < 3 or end <= start:
        raise ValueError("baseline requires at least three contiguous minutes")
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (app.WORKER_LOCK + 1,))
            cur.execute("SELECT count(*) FROM poc_v2_batches WHERE processed_at IS NULL")
            if cur.fetchone()[0]:
                raise ValueError("pending batches; baseline approval blocked")
            cur.execute("""SELECT bucket,status FROM poc_v2_buckets
                WHERE scope=%s AND bucket>=%s AND bucket<%s ORDER BY bucket""", (scope, start, end))
            found = cur.fetchall()
            expected = [start + timedelta(minutes=i) for i in range(minutes)]
            if [r[0] for r in found] != expected or any(r[1] != "complete" for r in found):
                raise ValueError("baseline requires complete contiguous nonempty buckets")
            samples = [features(cur, scope, bucket) for bucket in expected]
            if any(s["candidate_count"] for s in samples):
                raise ValueError("baseline window includes candidate traffic")
            model = fit_model(samples)
            cur.execute("""INSERT INTO poc_v2_baselines(scope,window_start,window_end,model,actor,note)
                VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""", (scope, start, end, Json(model), actor, note))
            baseline = cur.fetchone()[0]
            cur.execute("""INSERT INTO poc_v2_baseline_heads(scope,active_baseline) VALUES (%s,%s)
                ON CONFLICT(scope) DO UPDATE SET active_baseline=EXCLUDED.active_baseline""", (scope, baseline))
            cur.execute("UPDATE poc_v2_scores SET stale=TRUE WHERE scope=%s", (scope,))
            return baseline


def score_values(feat, model):
    zs = {name: .6745 * abs(feat[name] - model[name]["median"]) / model[name]["spread"]
          for name in ("qps", "err_5xx_ratio")}
    max_z = max(zs.values())
    reasons = {"z": zs, "candidate_count": feat["candidate_count"]}
    alert = max_z >= ALERT_Z or feat["candidate_count"] > 0
    return max(min(max_z / 10.0, 1.0), 1.0 if feat["candidate_count"] else 0.0), alert, reasons


def run_once(conn, now=None, lateness_seconds=LATENESS_SECONDS):
    if lateness_seconds < 0:
        raise ValueError("allowed lateness must be nonnegative")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must have timezone")
    with conn:
        with conn.cursor() as cur:
            # Enqueue, count writes and finalization share this transaction lock.
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (app.WORKER_LOCK + 1,))
            cur.execute("SELECT count(*) FROM poc_v2_batches WHERE processed_at IS NULL")
            if cur.fetchone()[0]:
                return {"blocked": "pending_batches", "finalized": 0, "scored": 0}
            cutoff = now - timedelta(seconds=lateness_seconds + 60)
            cur.execute("""UPDATE poc_v2_buckets SET status='complete',completed_at=%s
                WHERE status='open' AND bucket<=%s""", (now, cutoff))
            finalized = cur.rowcount
            cur.execute("""SELECT b.scope,b.bucket,b.revision,h.active_baseline,base.valid,base.model
                FROM poc_v2_buckets b
                LEFT JOIN poc_v2_baseline_heads h ON h.scope=b.scope
                LEFT JOIN poc_v2_baselines base ON base.id=h.active_baseline
                LEFT JOIN poc_v2_scores s ON s.scope=b.scope AND s.bucket=b.bucket
                WHERE b.status='complete' AND (s.scope IS NULL OR s.stale OR s.bucket_revision<>b.revision
                  OR s.baseline_id IS DISTINCT FROM h.active_baseline)
                ORDER BY b.bucket,b.scope""")
            buckets = cur.fetchall()
            for scope, bucket, revision, baseline, valid, model in buckets:
                feat = features(cur, scope, bucket)
                score = alert = None
                reasons = {"candidate_count": feat["candidate_count"]}
                if baseline is None:
                    status = "no_baseline"
                elif not valid:
                    status = "baseline_invalid"
                else:
                    score, alert, reasons = score_values(feat, model)
                    status = "alert" if alert else "ok"
                cur.execute("""INSERT INTO poc_v2_scores(scope,bucket,bucket_revision,baseline_id,status,
                    qps,err_5xx_ratio,candidate_count,score,is_alert,reasons,stale,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE,now())
                    ON CONFLICT(scope,bucket) DO UPDATE SET bucket_revision=EXCLUDED.bucket_revision,
                    baseline_id=EXCLUDED.baseline_id,status=EXCLUDED.status,qps=EXCLUDED.qps,
                    err_5xx_ratio=EXCLUDED.err_5xx_ratio,candidate_count=EXCLUDED.candidate_count,
                    score=EXCLUDED.score,is_alert=EXCLUDED.is_alert,reasons=EXCLUDED.reasons,
                    stale=FALSE,updated_at=now()""", (scope,bucket,revision,baseline,status,feat["qps"],
                    feat["err_5xx_ratio"],feat["candidate_count"],score,alert,Json(reasons)))
            return {"blocked": None, "finalized": finalized, "scored": len(buckets)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    approval = sub.add_parser("approve-baseline")
    approval.add_argument("scope")
    approval.add_argument("start", help="inclusive ISO8601 time with timezone")
    approval.add_argument("end", help="exclusive ISO8601 time with timezone")
    approval.add_argument("--actor", required=True)
    approval.add_argument("--note", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.command == "approve-baseline":
        with closing(psycopg2.connect(app.PG_DSN)) as conn:
            print(approve_baseline(conn, args.scope, datetime.fromisoformat(args.start),
                                   datetime.fromisoformat(args.end), args.actor, args.note))
        return
    while True:
        try:
            with closing(psycopg2.connect(app.PG_DSN)) as conn:
                result = run_once(conn)
                if result["finalized"] or result["scored"] or result["blocked"]:
                    LOG.info("scorer tick %s", json.dumps(result))
        except Exception:
            LOG.exception("scorer failed; transaction rolled back")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
