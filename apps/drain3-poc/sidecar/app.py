"""Drain3 sidecar POC: Loki push 归一化 + PG 模板/计数持久化."""
import logging
import os
import re
import statistics
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

import psycopg2
import psycopg2.pool
from drain3 import TemplateMiner
from drain3.template_miner_config import TemplateMinerConfig
from fastapi import FastAPI, Request

LOG = logging.getLogger("drain3-sidecar")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

PG_DSN = os.getenv("PG_DSN", "host=postgres dbname=anomaly user=drain3 password=drain3poc")
PERSIST_PATH = os.getenv("PERSIST_PATH", "/data/templates.pkl")
FLUSH_INTERVAL = int(os.getenv("FLUSH_INTERVAL", "60"))
RELOAD_INTERVAL = int(os.getenv("RELOAD_INTERVAL", "60"))
SOURCE_HOST = os.getenv("SOURCE_HOST_LABEL", "nginx-poc-01")

SCHEMA = """
CREATE TABLE IF NOT EXISTS drain3_templates (
    id           BIGSERIAL PRIMARY KEY,
    cluster_id   INT NOT NULL,
    template     TEXT NOT NULL,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ,
    source_host  TEXT,
    sample_line  TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    confirmed_by TEXT,
    confirmed_at TIMESTAMPTZ,
    UNIQUE (template)
);
CREATE TABLE IF NOT EXISTS drain3_template_counts (
    template_id  BIGINT NOT NULL REFERENCES drain3_templates(id),
    bucket       TIMESTAMPTZ NOT NULL,
    source_host  TEXT NOT NULL,
    count        BIGINT NOT NULL DEFAULT 0,
    err_4xx      INT NOT NULL DEFAULT 0,
    err_5xx      INT NOT NULL DEFAULT 0,
    p95_rt_ms    INT,
    PRIMARY KEY (template_id, bucket, source_host)
);
"""

# --- Drain3: FilePersistence,重启后 cluster_id 不漂移 ---
cfg = TemplateMinerConfig()
cfg.drain_sim_th = 0.8
cfg.drain_depth = 4
from drain3.file_persistence import FilePersistence
miner = TemplateMiner(FilePersistence(PERSIST_PATH), cfg)

for attempt in range(20):   # PG 未就绪时重试,不 crash-loop
    try:
        pool = psycopg2.pool.SimpleConnectionPool(1, 4, dsn=PG_DSN)
        break
    except psycopg2.OperationalError as e:
        if attempt == 19:
            raise
        LOG.warning("PG not ready (%s), retry %d/20", e, attempt + 1)
        time.sleep(5)

lock = threading.Lock()
# window[min_epoch] -> {pg_tid: {"count","err4","err5","rts":[...]}}
window = defaultdict(lambda: defaultdict(lambda: {"count": 0, "err4": 0, "err5": 0, "rts": []}))
tid_by_template = {}   # template_mined 文本 -> PG id（进程内缓存）
known_templates = set()  # status=confirmed 的模板文本（周期 reload）

# --- 归一化: nginx combined log -> "METHOD /norm/path STATUS" ---
ACCESS_RE = re.compile(
    r'^(\S+) \S+ \S+ \[[^\]]+\] "(\S+) (\S+)(?:\?[^"]*)?[^"]*" (\d{3}) (\S+)(?: (\S+))?'
)
NUM_SEG = re.compile(r"^\d+$")
HEX_SEG = re.compile(r"^[0-9a-f]{16,}$", re.I)


def normalize(line: str):
    """返回 (normalized_text, status:int|None, rt:float|None)；无法解析返回 None。"""
    m = ACCESS_RE.match(line)
    if not m:
        return None
    _, method, url, status, _size, rt = m.groups()
    segs = []
    for s in url.split("?")[0].split("/"):
        if not s:
            continue
        if NUM_SEG.match(s):
            segs.append("{D}")
        elif HEX_SEG.match(s):
            segs.append("{HASH}")
        else:
            segs.append(s)
    norm = "/" + "/".join(segs)
    # status 不进 Drain3 消息:避免同路径不同状态码碎片化,status 只进计数器
    return (f"{method} {norm}", int(status),
            float(rt) if rt not in (None, "-") else None)


def db_register_template(template_text: str, sample_line: str) -> int:
    """按模板文本查/插 PG，返回 PG id。新模板默认 pending，不入已知库。"""
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status FROM drain3_templates WHERE template = %s",
                (template_text,))
            row = cur.fetchone()
            if row:
                tid, status = row
                cur.execute("UPDATE drain3_templates SET last_seen = now() WHERE id = %s", (tid,))
                conn.commit()
                if status == "confirmed":
                    known_templates.add(template_text)
                return tid
            cur.execute(
                """INSERT INTO drain3_templates
                       (cluster_id, template, last_seen, source_host, sample_line, status)
                   VALUES (%s, %s, now(), %s, %s, 'pending') RETURNING id""",
                (0, template_text, SOURCE_HOST, sample_line[:500]))
            tid = cur.fetchone()[0]
            conn.commit()
            LOG.warning("[NEW-TEMPLATE] id=%s status=pending template=%s sample=%.120s",
                        tid, template_text, sample_line)
            return tid
    finally:
        pool.putconn(conn)


def process_line(line: str):
    norm = normalize(line)
    if norm is None:
        return
    text, status, rt = norm
    result = miner.add_log_message(text)
    mined = result["template_mined"]
    if result.get("change_detected"):
        LOG.info("[DRAIN3-CHANGE] cluster=%s template=%s", result["cluster_id"], mined)

    tid = tid_by_template.get(mined)
    if tid is None:
        tid = db_register_template(mined, line)
        tid_by_template[mined] = tid

    bucket = int(time.time() // 60)
    with lock:
        d = window[bucket][tid]
        d["count"] += 1
        if rt is not None:
            d["rts"].append(rt)
        if status is not None:
            if status >= 500:
                d["err5"] += 1
            elif status >= 400:
                d["err4"] += 1


def p95_ms(rts):
    if not rts:
        return None
    if len(rts) == 1:
        return int(rts[0] * 1000)
    return int(statistics.quantiles(rts, n=20)[18] * 1000)


def flush_loop():
    """把已结束的分钟桶 UPSERT 累加进 PG。"""
    while True:
        time.sleep(FLUSH_INTERVAL)
        now_b = int(time.time() // 60)
        rows = []
        with lock:
            closed = [b for b in list(window) if b < now_b]
            for b in closed:
                ts = datetime.fromtimestamp(b * 60, tz=timezone.utc)
                for tid, d in window.pop(b).items():
                    rows.append((tid, ts, SOURCE_HOST, d["count"], d["err4"],
                                 d["err5"], p95_ms(d["rts"])))
        if not rows:
            continue
        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO drain3_template_counts
                           (template_id, bucket, source_host, count, err_4xx, err_5xx, p95_rt_ms)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (template_id, bucket, source_host)
                       DO UPDATE SET count    = drain3_template_counts.count + EXCLUDED.count,
                                     err_4xx  = drain3_template_counts.err_4xx + EXCLUDED.err_4xx,
                                     err_5xx  = drain3_template_counts.err_5xx + EXCLUDED.err_5xx""",
                    rows)
            conn.commit()
            LOG.info("[FLUSH] %d buckets rows=%d", len(closed), len(rows))
        finally:
            pool.putconn(conn)


def reload_loop():
    """周期 reload 人工确认的模板 → 已知库。"""
    while True:
        time.sleep(RELOAD_INTERVAL)
        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT template FROM drain3_templates WHERE status = 'confirmed'")
                known = {r[0] for r in cur.fetchall()}
            added = known - known_templates
            if added:
                LOG.info("[RELOAD] confirmed templates now=%d newly_added=%s",
                         len(known), sorted(added))
            known_templates.clear()
            known_templates.update(known)
        finally:
            pool.putconn(conn)


def init():
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        conn.commit()
    finally:
        pool.putconn(conn)
    for fn in (reload_loop, flush_loop):
        threading.Thread(target=fn, daemon=True).start()
    LOG.info("sidecar ready: flush=%ds reload=%ds", FLUSH_INTERVAL, RELOAD_INTERVAL)


app = FastAPI()


@app.on_event("startup")
def startup():
    init()


@app.post("/loki/api/v1/push")
async def loki_push(req: Request):
    body = await req.json()
    n = 0
    for stream in body.get("streams", []):
        for _ts, line in stream.get("values", []):
            try:
                process_line(line)
                n += 1
            except Exception:  # noqa: BLE001 单行失败不影响整批
                LOG.exception("process_line failed: %.120s", line)
    return {"status": "ok", "processed": n}


@app.get("/health")
def health():
    return {
        "drain3_clusters": len(tid_by_template),
        "known_confirmed": len(known_templates),
        "cached_templates": len(tid_by_template),
        "open_buckets": len(window),
    }
