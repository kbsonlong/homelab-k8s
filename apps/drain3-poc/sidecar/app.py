"""V2 POC: durable Loki batches, isolated Drain3 models and transactional counts.

PostgreSQL is a POC queue, not a throughput claim. One worker is enforced by an
advisory lock. Standard Loki push has no sender batch ID; HTTP retries can duplicate.
"""
import asyncio
import gzip
import io
import json
import logging
import os
import re
import threading
import time
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone

import jsonpickle
import psycopg2
from drain3 import TemplateMiner
from drain3.template_miner_config import TemplateMinerConfig
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from google.protobuf.message import DecodeError
from psycopg2.extras import Json, execute_values
import snappy

from loki_proto import PushRequest

LOG = logging.getLogger("drain3-v2")
logging.basicConfig(level=logging.INFO)
PG_DSN = os.environ.get("PG_DSN", "host=postgres dbname=anomaly user=drain3 password=drain3poc")
MAX_BODY = int(os.getenv("MAX_BODY_BYTES", str(8 * 1024 * 1024)))
MAX_DECODED = int(os.getenv("MAX_DECODED_BYTES", str(32 * 1024 * 1024)))
MAX_ENTRIES = int(os.getenv("MAX_BATCH_ENTRIES", "10000"))
MAX_PENDING = int(os.getenv("MAX_PENDING_BATCHES", "10000"))
WORKER_LOCK = 48339712

SCHEMA = """
CREATE TABLE IF NOT EXISTS poc_v2_batches (
 id BIGSERIAL PRIMARY KEY, payload JSONB NOT NULL,
 received_at TIMESTAMPTZ NOT NULL DEFAULT now(), processed_at TIMESTAMPTZ,
 parsed_count BIGINT, rejected_count BIGINT
);
CREATE INDEX IF NOT EXISTS poc_v2_batches_pending ON poc_v2_batches(id) WHERE processed_at IS NULL;
CREATE TABLE IF NOT EXISTS poc_v2_templates (
 id BIGSERIAL PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('candidate','approved')),
 template TEXT NOT NULL, first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(scope,kind,template)
);
CREATE TABLE IF NOT EXISTS poc_v2_reviews (
 id BIGSERIAL PRIMARY KEY, candidate_id BIGINT NOT NULL REFERENCES poc_v2_templates(id),
 decision TEXT NOT NULL CHECK (decision IN ('approve','reject')), actor TEXT NOT NULL,
 note TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS poc_v2_versions (
 id BIGSERIAL PRIMARY KEY, scope TEXT NOT NULL, parent_id BIGINT REFERENCES poc_v2_versions(id),
 action TEXT NOT NULL CHECK (action IN ('publish','rollback')),
 source_version BIGINT REFERENCES poc_v2_versions(id), actor TEXT NOT NULL, note TEXT NOT NULL,
 normalization_version TEXT NOT NULL, templates JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE OR REPLACE FUNCTION poc_v2_version_immutable() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'published version snapshots are immutable'; END;
$$ LANGUAGE plpgsql;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='poc_v2_versions'::regclass AND tgname='poc_v2_version_immutable_trigger') THEN
   CREATE TRIGGER poc_v2_version_immutable_trigger BEFORE UPDATE OR DELETE ON poc_v2_versions
   FOR EACH ROW EXECUTE FUNCTION poc_v2_version_immutable();
 END IF;
END $$;
CREATE TABLE IF NOT EXISTS poc_v2_scope_heads (
 scope TEXT PRIMARY KEY, active_version BIGINT REFERENCES poc_v2_versions(id)
);
CREATE TABLE IF NOT EXISTS poc_v2_candidate_state (scope TEXT PRIMARY KEY, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS poc_v2_counts (
 template_id BIGINT NOT NULL REFERENCES poc_v2_templates(id),
 bucket TIMESTAMPTZ NOT NULL, source TEXT NOT NULL,
 count BIGINT NOT NULL, err_4xx BIGINT NOT NULL, err_5xx BIGINT NOT NULL,
 revision BIGINT NOT NULL DEFAULT 1,
 PRIMARY KEY(template_id,bucket,source)
);
ALTER TABLE poc_v2_counts ADD COLUMN IF NOT EXISTS template_version BIGINT NOT NULL DEFAULT 0;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='poc_v2_counts'::regclass AND conname='poc_v2_counts_version_key') THEN
   ALTER TABLE poc_v2_counts DROP CONSTRAINT poc_v2_counts_pkey;
   ALTER TABLE poc_v2_counts ADD CONSTRAINT poc_v2_counts_version_key PRIMARY KEY(template_id,bucket,source,template_version);
 END IF;
END $$;
CREATE TABLE IF NOT EXISTS poc_v2_buckets (
 scope TEXT NOT NULL, bucket TIMESTAMPTZ NOT NULL, revision BIGINT NOT NULL DEFAULT 1,
 status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','complete')),
 completed_at TIMESTAMPTZ, PRIMARY KEY(scope,bucket)
);
CREATE TABLE IF NOT EXISTS poc_v2_baselines (
 id BIGSERIAL PRIMARY KEY, scope TEXT NOT NULL, window_start TIMESTAMPTZ NOT NULL,
 window_end TIMESTAMPTZ NOT NULL, model JSONB NOT NULL, actor TEXT NOT NULL, note TEXT NOT NULL,
 valid BOOLEAN NOT NULL DEFAULT TRUE, invalidated_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS poc_v2_baseline_heads (
 scope TEXT PRIMARY KEY, active_baseline BIGINT NOT NULL REFERENCES poc_v2_baselines(id)
);
CREATE TABLE IF NOT EXISTS poc_v2_scores (
 scope TEXT NOT NULL, bucket TIMESTAMPTZ NOT NULL, bucket_revision BIGINT NOT NULL,
 baseline_id BIGINT REFERENCES poc_v2_baselines(id), status TEXT NOT NULL,
 qps DOUBLE PRECISION NOT NULL, err_5xx_ratio DOUBLE PRECISION NOT NULL,
 candidate_count BIGINT NOT NULL, score DOUBLE PRECISION, is_alert BOOLEAN,
 reasons JSONB NOT NULL, stale BOOLEAN NOT NULL DEFAULT FALSE,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(scope,bucket)
);
INSERT INTO poc_v2_buckets(scope,bucket)
 SELECT DISTINCT source,bucket FROM poc_v2_counts ON CONFLICT(scope,bucket) DO NOTHING;
CREATE TABLE IF NOT EXISTS poc_v2_rejections (
 batch_id BIGINT NOT NULL REFERENCES poc_v2_batches(id), reason TEXT NOT NULL,
 count BIGINT NOT NULL, PRIMARY KEY(batch_id,reason)
);
"""
ACCESS_RE = re.compile(r'^(\S+) \S+ \S+ \[[^\]]+\] "(\S+) (\S+)[^"]*" (\d{3}) (\S+)')
NUM_SEG = re.compile(r"^\d+$")
HEX_SEG = re.compile(r"^[0-9a-f]{16,}$", re.I)
LABEL = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*("(?:\\.|[^"\\])*")\s*')


def normalize(line):
    match = ACCESS_RE.match(line)
    if not match:
        return None
    _, method, url, status, _ = match.groups()
    segments = []
    for segment in url.split("?", 1)[0].split("/"):
        if not segment:
            continue
        segments.append("{D}" if NUM_SEG.fullmatch(segment) else
                        "{HASH}" if HEX_SEG.fullmatch(segment) else segment)
    return f"{method} /{'/'.join(segments)}", int(status)


def parse_labels(value):
    if not value.startswith("{") or not value.endswith("}"):
        raise ValueError("invalid stream labels")
    rest = value[1:-1]
    labels = {}
    while rest.strip():
        match = LABEL.match(rest)
        if not match:
            raise ValueError("invalid stream labels")
        key = match.group(1)
        if key in labels:
            raise ValueError("duplicate stream label")
        labels[key] = json.loads(match.group(2))
        rest = rest[match.end():]
        if rest.strip():
            if not rest.lstrip().startswith(","):
                raise ValueError("invalid stream labels")
            rest = rest.lstrip()[1:]
            if not rest.strip():
                raise ValueError("trailing label comma")
    return labels


def _varint_prefix(data):
    n = 0
    for i, byte in enumerate(data[:5]):
        n |= (byte & 127) << (7 * i)
        if not byte & 128:
            return n
    raise ValueError("invalid snappy length")


def decode_payload(raw, content_type, encoding=""):
    if len(raw) > MAX_BODY:
        raise ValueError("compressed body too large")
    media = content_type.split(";", 1)[0].strip().lower()
    if media == "application/x-protobuf":
        if encoding and encoding.lower() != "snappy":
            raise ValueError("unsupported protobuf content encoding")
        if _varint_prefix(raw) > MAX_DECODED:
            raise ValueError("decoded body too large")
        try:
            data = snappy.decompress(raw)
            msg = PushRequest.FromString(data)
        except (Exception, DecodeError) as exc:
            raise ValueError("invalid snappy/protobuf body") from exc
        streams = []
        for s in msg.streams:
            labels = parse_labels(s.labels)
            entries = []
            for entry in s.entries:
                if not entry.HasField("timestamp") or not 0 <= entry.timestamp.nanos < 1_000_000_000:
                    raise ValueError("invalid timestamp")
                ns = entry.timestamp.seconds * 1_000_000_000 + entry.timestamp.nanos
                entries.append([str(ns), entry.line, {v.name: v.value for v in entry.structured_metadata}])
            streams.append({"stream": labels, "values": entries})
        body = {"streams": streams}
    elif media == "application/json":
        if encoding.lower() == "gzip":
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
                    raw = gz.read(MAX_DECODED + 1)
            except (OSError, EOFError) as exc:
                raise ValueError("invalid gzip body") from exc
        elif encoding:
            raise ValueError("unsupported content encoding")
        if len(raw) > MAX_DECODED:
            raise ValueError("decoded body too large")
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("invalid JSON") from exc
    else:
        raise ValueError("unsupported content type")
    if not isinstance(body, dict) or not isinstance(body.get("streams"), list):
        raise ValueError("missing streams")
    entries_count = 0
    for stream in body["streams"]:
        if not isinstance(stream, dict) or not isinstance(stream.get("stream"), dict) or not isinstance(stream.get("values"), list):
            raise ValueError("invalid stream")
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in stream["stream"].items()):
            raise ValueError("invalid labels")
        for entry in stream["values"]:
            if not isinstance(entry, list) or len(entry) not in (2, 3) or not isinstance(entry[0], str) or not entry[0].isdigit() or not isinstance(entry[1], str):
                raise ValueError("invalid entry")
            if len(entry) == 3 and (not isinstance(entry[2], dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in entry[2].items())):
                raise ValueError("invalid metadata")
            # PostgreSQL and Python datetime have finite timestamp ranges.
            if int(entry[0]) > 253402300799999999999:
                raise ValueError("timestamp out of range")
            entries_count += 1
            if entries_count > MAX_ENTRIES:
                raise ValueError("too many entries")
    return body


def miner_new(state=None):
    cfg = TemplateMinerConfig()
    cfg.drain_sim_th = 0.8
    cfg.drain_depth = 4
    miner = TemplateMiner(config=cfg)
    if state:
        drain = jsonpickle.decode(state, keys=True)
        miner.drain.id_to_cluster = drain.id_to_cluster
        miner.drain.clusters_counter = drain.clusters_counter
        miner.drain.root_node = drain.root_node
    return miner


def lock_scope(cur, scope):
    cur.execute("INSERT INTO poc_v2_scope_heads(scope) VALUES (%s) ON CONFLICT DO NOTHING", (scope,))
    cur.execute("SELECT active_version FROM poc_v2_scope_heads WHERE scope=%s FOR UPDATE", (scope,))
    return cur.fetchone()[0]


def require_actor_note(actor, note):
    if not actor or not actor.strip() or not note or not note.strip():
        raise ValueError("actor and note are required")


def review_candidate(conn, candidate_id, decision, actor, note):
    require_actor_note(actor, note)
    if decision not in ("approve", "reject"):
        raise ValueError("invalid review decision")
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT scope FROM poc_v2_templates WHERE id=%s AND kind='candidate'", (candidate_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError("candidate not found")
            lock_scope(cur, row[0])
            cur.execute("INSERT INTO poc_v2_reviews(candidate_id,decision,actor,note) VALUES (%s,%s,%s,%s) RETURNING id", (candidate_id, decision, actor, note))
            return cur.fetchone()[0]


def publish_candidate(conn, candidate_id, actor, note):
    require_actor_note(actor, note)
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT scope,template FROM poc_v2_templates WHERE id=%s AND kind='candidate'", (candidate_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError("candidate not found")
            scope, template = row
            current = lock_scope(cur, scope)
            cur.execute("SELECT decision FROM poc_v2_reviews WHERE candidate_id=%s ORDER BY id DESC LIMIT 1", (candidate_id,))
            decision = cur.fetchone()
            if not decision or decision[0] != "approve":
                raise ValueError("candidate has not been approved or its latest review rejected it")
            templates = []
            if current:
                cur.execute("SELECT templates FROM poc_v2_versions WHERE id=%s AND scope=%s", (current, scope))
                templates = cur.fetchone()[0]
            if any(item["template"] == template for item in templates):
                raise ValueError("template already published")
            # Construct independently to avoid generalizing existing approved templates.
            probe = miner_new()
            probe.add_log_message(template)
            cur.execute("INSERT INTO poc_v2_templates(scope,kind,template) VALUES (%s,'approved',%s) ON CONFLICT(scope,kind,template) DO UPDATE SET template=EXCLUDED.template RETURNING id", (scope, template))
            approved_id = cur.fetchone()[0]
            templates = templates + [{"id": approved_id, "template": template}]
            cur.execute("""INSERT INTO poc_v2_versions(scope,parent_id,action,actor,note,normalization_version,templates)
                VALUES (%s,%s,'publish',%s,%s,'v1',%s) RETURNING id""", (scope, current, actor, note, Json(templates)))
            version = cur.fetchone()[0]
            cur.execute("UPDATE poc_v2_scope_heads SET active_version=%s WHERE scope=%s", (version, scope))
            return version


def rollback_version(conn, scope, target_version, actor, note):
    require_actor_note(actor, note)
    with conn:
        with conn.cursor() as cur:
            current = lock_scope(cur, scope)
            cur.execute("SELECT templates,normalization_version FROM poc_v2_versions WHERE id=%s AND scope=%s", (target_version, scope))
            target = cur.fetchone()
            if not target:
                raise ValueError("version not found for scope")
            cur.execute("""INSERT INTO poc_v2_versions(scope,parent_id,action,source_version,actor,note,normalization_version,templates)
                VALUES (%s,%s,'rollback',%s,%s,%s,%s,%s) RETURNING id""", (scope, current, target_version, actor, note, target[1], Json(target[0])))
            version = cur.fetchone()[0]
            cur.execute("UPDATE poc_v2_scope_heads SET active_version=%s WHERE scope=%s", (version, scope))
            return version


def approved_matchers(cur, scope):
    # One SELECT gives a consistent version and its complete immutable snapshot.
    cur.execute("""SELECT v.id,v.templates FROM poc_v2_scope_heads h
        JOIN poc_v2_versions v ON v.id=h.active_version AND v.scope=h.scope WHERE h.scope=%s""", (scope,))
    row = cur.fetchone()
    if not row:
        return 0, []
    version, templates = row
    result = []
    for item in templates:
        miner = miner_new()
        miner.add_log_message(item["template"])
        result.append((item["id"], miner))
    return version, result


def invalidate_scores_and_baselines(cur, touched):
    """Mark affected results invalid before acknowledging or committing late data."""
    for scope, bucket in touched:
        cur.execute("UPDATE poc_v2_scores SET stale=TRUE WHERE scope=%s AND bucket=%s", (scope, bucket))
        cur.execute("""UPDATE poc_v2_baselines SET valid=FALSE,invalidated_at=now()
            WHERE scope=%s AND valid=TRUE AND %s>=window_start AND %s<window_end RETURNING id""", (scope, bucket, bucket))
        invalid_ids = [r[0] for r in cur.fetchall()]
        if invalid_ids:
            cur.execute("UPDATE poc_v2_scores SET stale=TRUE WHERE baseline_id=ANY(%s)", (invalid_ids,))


def batch_event_buckets(body):
    touched = set()
    for stream in body["streams"]:
        scope = json.dumps(stream["stream"], sort_keys=True, separators=(",", ":"))
        for entry in stream["values"]:
            if normalize(entry[1]) is not None:
                bucket = datetime.fromtimestamp(int(entry[0]) // 1_000_000_000 // 60 * 60, timezone.utc)
                touched.add((scope, bucket))
    return sorted(touched)


def enqueue(conn, body):
    with conn:
        with conn.cursor() as cur:
            # serialize the bounded queue admission check across HTTP workers
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (WORKER_LOCK + 1,))
            cur.execute("SELECT count(*) FROM poc_v2_batches WHERE processed_at IS NULL")
            if cur.fetchone()[0] >= MAX_PENDING:
                raise OverflowError("queue full")
            cur.execute("INSERT INTO poc_v2_batches(payload) VALUES (%s) RETURNING id", (Json(body),))
            batch_id = cur.fetchone()[0]
            # Once accepted, delayed data must not leave an old score visible as current.
            invalidate_scores_and_baselines(cur, batch_event_buckets(body))
            return batch_id


def process_one(conn):
    """Aggregate exactly once per accepted queue row, including candidate state."""
    with conn:
        with conn.cursor() as cur:
            # Serialize ingestion, aggregation and scoring in this single-worker POC.
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (WORKER_LOCK + 1,))
            cur.execute("SELECT id,payload FROM poc_v2_batches WHERE processed_at IS NULL ORDER BY id LIMIT 1 FOR UPDATE")
            row = cur.fetchone()
            if row is None:
                return False
            batch_id, payload = row
            groups, rejects, models = Counter(), Counter(), {}
            for stream in payload["streams"]:
                # POC scope is the complete stream label set; no cross-stream template learning.
                scope = json.dumps(stream["stream"], sort_keys=True, separators=(",", ":"))
                if scope not in models:
                    cur.execute("SELECT state FROM poc_v2_candidate_state WHERE scope=%s", (scope,))
                    saved = cur.fetchone()
                    version, matchers = approved_matchers(cur, scope)
                    models[scope] = (miner_new(saved[0] if saved else None), version, matchers)
                candidate, version, approved = models[scope]
                for entry in stream["values"]:
                    parsed = normalize(entry[1])
                    if not parsed:
                        rejects["unparseable_access_log"] += 1
                        continue
                    text, status = parsed
                    tid = next((tid for tid, miner in approved if miner.match(text, full_search_strategy="fallback") is not None), None)
                    if tid is None:
                        template = candidate.add_log_message(text)["template_mined"]
                        cur.execute("INSERT INTO poc_v2_templates(scope,kind,template) VALUES (%s,'candidate',%s) ON CONFLICT(scope,kind,template) DO UPDATE SET template=EXCLUDED.template RETURNING id", (scope, template))
                        tid = cur.fetchone()[0]
                    bucket = datetime.fromtimestamp(int(entry[0]) // 1_000_000_000 // 60 * 60, timezone.utc)
                    groups[(tid, bucket, scope, version, "count")] += 1
                    if status >= 500:
                        groups[(tid, bucket, scope, version, "err5")] += 1
                    elif status >= 400:
                        groups[(tid, bucket, scope, version, "err4")] += 1
            for scope, (candidate, _, _) in models.items():
                state = jsonpickle.encode(candidate.drain, keys=True)
                cur.execute("INSERT INTO poc_v2_candidate_state(scope,state) VALUES (%s,%s) ON CONFLICT(scope) DO UPDATE SET state=EXCLUDED.state", (scope, state))
            rows = []
            for tid, bucket, scope, version, kind in groups:
                if kind == "count":
                    rows.append((tid, bucket, scope, groups[(tid,bucket,scope,version,"count")], groups[(tid,bucket,scope,version,"err4")], groups[(tid,bucket,scope,version,"err5")], version))
            if rows:
                execute_values(cur, """INSERT INTO poc_v2_counts(template_id,bucket,source,count,err_4xx,err_5xx,template_version) VALUES %s
                    ON CONFLICT(template_id,bucket,source,template_version) DO UPDATE SET
                    count=poc_v2_counts.count+EXCLUDED.count, err_4xx=poc_v2_counts.err_4xx+EXCLUDED.err_4xx,
                    err_5xx=poc_v2_counts.err_5xx+EXCLUDED.err_5xx, revision=poc_v2_counts.revision+1""", rows)
            touched = sorted({(r[2], r[1]) for r in rows})
            if touched:
                execute_values(cur, """INSERT INTO poc_v2_buckets(scope,bucket) VALUES %s
                    ON CONFLICT(scope,bucket) DO UPDATE SET revision=poc_v2_buckets.revision+1,
                    status='open', completed_at=NULL""", touched)
                invalidate_scores_and_baselines(cur, touched)
            if rejects:
                execute_values(cur, "INSERT INTO poc_v2_rejections(batch_id,reason,count) VALUES %s", [(batch_id, reason, count) for reason, count in rejects.items()])
            cur.execute("UPDATE poc_v2_batches SET processed_at=now(), parsed_count=%s, rejected_count=%s WHERE id=%s", (sum(r[3] for r in rows), sum(rejects.values()), batch_id))
    return True


def worker(stop):
    while not stop.is_set():
        try:
            with closing(psycopg2.connect(PG_DSN)) as conn:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_try_advisory_lock(%s)", (WORKER_LOCK,))
                    acquired = cur.fetchone()[0]
                if not acquired:
                    stop.wait(1)
                    continue
                conn.autocommit = False
                while not stop.is_set():
                    if not process_one(conn):
                        stop.wait(0.2)
        except Exception:
            LOG.exception("worker failed; transaction rolled back; retrying")
            stop.wait(2)


app = FastAPI()
stop_event = threading.Event()
worker_thread = None


@app.on_event("startup")
def startup():
    with closing(psycopg2.connect(PG_DSN)) as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA)
    global worker_thread
    stop_event.clear()
    worker_thread = threading.Thread(target=worker, args=(stop_event,), daemon=True)
    worker_thread.start()


@app.on_event("shutdown")
def shutdown():
    stop_event.set()
    if worker_thread is not None:
        worker_thread.join(timeout=5)


@app.post("/loki/api/v1/push")
async def loki_push(req: Request):
    body = bytearray()
    async for chunk in req.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY:
            raise HTTPException(413, "body too large")
    try:
        payload = decode_payload(bytes(body), req.headers.get("content-type", ""), req.headers.get("content-encoding", ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        def store():
            with closing(psycopg2.connect(PG_DSN)) as conn:
                enqueue(conn, payload)
        await asyncio.to_thread(store)
    except OverflowError as exc:
        raise HTTPException(503, "queue full") from exc
    except psycopg2.Error as exc:
        LOG.exception("enqueue failed")
        raise HTTPException(503, "queue unavailable") from exc
    return Response(status_code=204)


@app.get("/health")
def health():
    try:
        with closing(psycopg2.connect(PG_DSN, connect_timeout=2)) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM poc_v2_batches WHERE processed_at IS NULL")
                return {"pending_batches": cur.fetchone()[0]}
    except psycopg2.Error as exc:
        raise HTTPException(503, "queue unavailable") from exc
