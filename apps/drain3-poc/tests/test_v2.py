import gzip
import json
import os
import sys
from pathlib import Path

import psycopg2
import pytest
import snappy

sys.path.insert(0, str(Path(__file__).parents[1] / "sidecar"))
import app
import scorer_v2
from loki_proto import PushRequest

LINE = '127.0.0.1 - - [01/Jan/2026:00:00:00 +0000] "GET /api/users/123 HTTP/1.1" 200 10'
NS = "1767225600000000000"
LABELS = {"cluster": "test", "job": "nginx"}


def body(line=LINE, ns=NS):
    return {"streams": [{"stream": LABELS, "values": [[ns, line]]}]}


def test_json_gzip_and_validation():
    data = body()
    data["streams"][0]["values"][0].append({"trace_id": "x"})
    assert app.decode_payload(gzip.compress(json.dumps(data).encode()), "application/json", "gzip") == data
    with pytest.raises(ValueError):
        app.decode_payload(b"{}", "application/json")
    with pytest.raises(ValueError):
        app.decode_payload(json.dumps(body(ns="not-a-time")).encode(), "application/json")


def test_snappy_protobuf_and_metadata():
    msg = PushRequest()
    stream = msg.streams.add(labels='{cluster="test",job="nginx"}')
    e = stream.entries.add(line=LINE)
    e.timestamp.seconds = int(NS) // 1_000_000_000
    e.structured_metadata.add(name="trace_id", value="x")
    parsed = app.decode_payload(snappy.compress(msg.SerializeToString()), "application/x-protobuf")
    assert parsed["streams"][0]["stream"] == LABELS
    assert parsed["streams"][0]["values"] == [[NS, LINE, {"trace_id": "x"}]]
    with pytest.raises(ValueError):
        app.decode_payload(b"\xff", "application/x-protobuf")


def test_approved_match_is_not_trained_by_unknown():
    approved = app.miner_new()
    approved.add_log_message("GET /api/users/{D}")
    candidate = app.miner_new()
    before = json.dumps([c.get_template() for c in approved.drain.clusters])
    assert approved.match("GET /api/users/{D}") is not None
    assert approved.match("GET /wp-login.php") is None
    candidate.add_log_message("GET /wp-login.php")
    assert json.dumps([c.get_template() for c in approved.drain.clusters]) == before
    assert approved.match("GET /wp-login.php") is None


@pytest.fixture
def db():
    dsn = os.environ.get("POC_TEST_DSN")
    if not dsn:
        pytest.skip("POC_TEST_DSN not set")
    conn = psycopg2.connect(dsn)
    with conn:
        with conn.cursor() as cur:
            cur.execute(app.SCHEMA)
            cur.execute("TRUNCATE poc_v2_scores,poc_v2_baseline_heads,poc_v2_baselines,poc_v2_buckets,poc_v2_rejections,poc_v2_counts,poc_v2_reviews,poc_v2_scope_heads,poc_v2_versions,poc_v2_templates,poc_v2_candidate_state,poc_v2_batches RESTART IDENTITY CASCADE")
    yield conn
    conn.close()


def scalar(db, sql):
    with db.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def test_transaction_replay_and_event_time(db, monkeypatch):
    first = body()
    first["streams"][0]["values"].append([NS, "invalid line"])
    first_id = app.enqueue(db, first)
    original = app.execute_values
    def crash(*args, **kwargs):
        raise RuntimeError("simulate failure before commit")
    monkeypatch.setattr(app, "execute_values", crash)
    with pytest.raises(RuntimeError):
        app.process_one(db)
    assert scalar(db, "SELECT count(*) FROM poc_v2_counts") == 0
    assert scalar(db, "SELECT count(*) FROM poc_v2_batches WHERE processed_at IS NULL") == 1
    assert scalar(db, "SELECT count(*) FROM poc_v2_candidate_state") == 0
    monkeypatch.setattr(app, "execute_values", original)
    assert app.process_one(db)
    assert not app.process_one(db)
    assert scalar(db, "SELECT sum(count) FROM poc_v2_counts") == 1
    assert scalar(db, "SELECT sum(count) FROM poc_v2_rejections") == 1
    assert scalar(db, "SELECT extract(epoch from bucket)::bigint FROM poc_v2_counts") == int(NS) // 1_000_000_000
    # A sender retry is a distinct batch; duplicates are intentionally not hidden.
    assert app.enqueue(db, first) != first_id
    assert app.process_one(db)
    assert scalar(db, "SELECT sum(count) FROM poc_v2_counts") == 2


def test_approved_scope_and_candidate_isolation(db):
    scope = json.dumps(LABELS, sort_keys=True, separators=(",", ":"))
    with db:
        with db.cursor() as cur:
            cur.execute("INSERT INTO poc_v2_templates(scope,kind,template) VALUES (%s,'candidate',%s) RETURNING id", (scope, "GET /api/users/{D}"))
            candidate_id = cur.fetchone()[0]
    app.review_candidate(db, candidate_id, "approve", "tester", "reviewed")
    app.publish_candidate(db, candidate_id, "tester", "published")
    app.enqueue(db, body())
    unknown = LINE.replace("/api/users/123", "/wp-login.php")
    app.enqueue(db, body(unknown))
    assert app.process_one(db) and app.process_one(db)
    with db.cursor() as cur:
        cur.execute("SELECT kind, template FROM poc_v2_templates ORDER BY kind")
        assert cur.fetchall() == [("approved", "GET /api/users/{D}"), ("candidate", "GET /api/users/{D}"), ("candidate", "GET /wp-login.php")]
        cur.execute("SELECT t.kind,sum(c.count) FROM poc_v2_counts c JOIN poc_v2_templates t ON t.id=c.template_id GROUP BY t.kind ORDER BY t.kind")
        assert cur.fetchall() == [("approved", 1), ("candidate", 1)]


def test_http_success_only_after_enqueue_and_failure_is_retryable(monkeypatch):
    from fastapi.testclient import TestClient
    events = []
    class Connection:
        def close(self):
            events.append("close")
    monkeypatch.setattr(app.psycopg2, "connect", lambda *a, **k: Connection())
    monkeypatch.setattr(app, "enqueue", lambda conn, data: events.append("committed"))
    # No lifespan: test only the HTTP contract, not the background worker.
    client = TestClient(app.app)
    response = client.post("/loki/api/v1/push", json=body())
    assert response.status_code == 204
    assert events == ["committed", "close"]
    def unavailable(*args, **kwargs):
        raise psycopg2.OperationalError("unavailable")
    monkeypatch.setattr(app.psycopg2, "connect", unavailable)
    assert client.post("/loki/api/v1/push", json=body()).status_code == 503
    assert client.post("/loki/api/v1/push", content=b"bad", headers={"content-type": "application/json"}).status_code == 400


def test_http_protobuf_durable_queue_to_counts(db, monkeypatch):
    import time
    from fastapi.testclient import TestClient
    monkeypatch.setattr(app, "PG_DSN", os.environ["POC_TEST_DSN"])
    msg = PushRequest()
    stream = msg.streams.add(labels='{cluster="test",job="nginx"}')
    entry = stream.entries.add(line=LINE)
    entry.timestamp.seconds = int(NS) // 1_000_000_000
    with TestClient(app.app) as client:
        response = client.post("/loki/api/v1/push", content=snappy.compress(msg.SerializeToString()),
                               headers={"content-type": "application/x-protobuf"})
        assert response.status_code == 204
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if scalar(db, "SELECT count(*) FROM poc_v2_batches WHERE processed_at IS NOT NULL"):
                break
            time.sleep(.05)
        assert scalar(db, "SELECT count(*) FROM poc_v2_batches WHERE processed_at IS NOT NULL") == 1
        assert scalar(db, "SELECT sum(count) FROM poc_v2_counts") == 1


def test_failure_after_count_write_rolls_back_all_and_replays(db):
    app.enqueue(db, body())
    with db:
        with db.cursor() as cur:
            cur.execute("""CREATE OR REPLACE FUNCTION poc_test_fail() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected failure'; END $$""")
            cur.execute("CREATE TRIGGER poc_test_fail BEFORE UPDATE OF processed_at ON poc_v2_batches FOR EACH ROW EXECUTE FUNCTION poc_test_fail()")
    with pytest.raises(psycopg2.Error):
        app.process_one(db)
    assert scalar(db, "SELECT count(*) FROM poc_v2_counts") == 0
    assert scalar(db, "SELECT count(*) FROM poc_v2_templates") == 0
    assert scalar(db, "SELECT count(*) FROM poc_v2_candidate_state") == 0
    assert scalar(db, "SELECT count(*) FROM poc_v2_batches WHERE processed_at IS NULL") == 1
    with db:
        with db.cursor() as cur:
            cur.execute("DROP TRIGGER poc_test_fail ON poc_v2_batches")
    assert app.process_one(db)
    assert scalar(db, "SELECT sum(count) FROM poc_v2_counts") == 1
    app.enqueue(db, body())
    assert app.process_one(db)
    assert scalar(db, "SELECT revision FROM poc_v2_counts") == 2


def test_bounded_queue_rejects_before_ack_and_recovers(db, monkeypatch):
    monkeypatch.setattr(app, "MAX_PENDING", 1)
    app.enqueue(db, body())
    with pytest.raises(OverflowError):
        app.enqueue(db, body())
    assert scalar(db, "SELECT count(*) FROM poc_v2_batches") == 1
    assert app.process_one(db)
    app.enqueue(db, body())
    assert scalar(db, "SELECT count(*) FROM poc_v2_batches WHERE processed_at IS NULL") == 1


def test_real_alloy_loki_write_to_postgres(db):
    import time
    import uuid
    import requests
    url = os.environ.get("POC_ALLOY_PUSH_URL")
    if not url:
        pytest.skip("POC_ALLOY_PUSH_URL not set")
    job = "poc-alloy-" + uuid.uuid4().hex
    data = {"streams": [{"stream": {"job": job}, "values": [[NS, LINE.replace(" 200 ", " 503 ")]]}]}
    assert requests.post(url, json=data, timeout=5).status_code == 204
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with db.cursor() as cur:
            cur.execute("""SELECT COALESCE(sum(c.count),0),COALESCE(sum(c.err_5xx),0),
                           min(extract(epoch from c.bucket)) FROM poc_v2_counts c
                           JOIN poc_v2_templates t ON t.id=c.template_id WHERE t.scope::jsonb ->> 'job'=%s""", (job,))
            count, err5, bucket = cur.fetchone()
        if count:
            break
        time.sleep(.1)
    assert (count, err5, int(bucket)) == (1, 1, int(NS) // 1_000_000_000)


def test_review_publish_version_boundary_and_rollback(db):
    scope = json.dumps(LABELS, sort_keys=True, separators=(",", ":"))
    app.enqueue(db, body())
    assert app.process_one(db)
    with db.cursor() as cur:
        cur.execute("SELECT id FROM poc_v2_templates WHERE kind='candidate'")
        candidate1 = cur.fetchone()[0]
    with pytest.raises(ValueError, match="not been approved"):
        app.publish_candidate(db, candidate1, "tester", "unreviewed")
    app.review_candidate(db, candidate1, "approve", "reviewer", "reviewed template")
    v1 = app.publish_candidate(db, candidate1, "publisher", "initial version")
    app.enqueue(db, body())
    assert app.process_one(db)
    unknown = LINE.replace("/api/users/123", "/wp-login.php")
    app.enqueue(db, body(unknown))
    assert app.process_one(db)
    with db.cursor() as cur:
        cur.execute("SELECT id FROM poc_v2_templates WHERE kind='candidate' AND template='GET /wp-login.php'")
        candidate2 = cur.fetchone()[0]
    app.review_candidate(db, candidate2, "reject", "reviewer", "not reviewed as safe")
    with pytest.raises(ValueError, match="rejected"):
        app.publish_candidate(db, candidate2, "publisher", "must fail")
    app.review_candidate(db, candidate2, "approve", "reviewer", "POC only")
    v2 = app.publish_candidate(db, candidate2, "publisher", "second version")
    app.enqueue(db, body())
    assert app.process_one(db)
    v3 = app.rollback_version(db, scope, v1, "publisher", "restore first")
    app.enqueue(db, body(unknown))
    assert app.process_one(db)
    with db.cursor() as cur:
        cur.execute("SELECT action,parent_id,source_version FROM poc_v2_versions WHERE id=%s", (v3,))
        assert cur.fetchone() == ("rollback", v2, v1)
        cur.execute("SELECT template_version,sum(count) FROM poc_v2_counts GROUP BY template_version ORDER BY template_version")
        assert cur.fetchall() == [(0, 1), (v1, 2), (v2, 1), (v3, 1)]
        cur.execute("SELECT t.kind FROM poc_v2_counts c JOIN poc_v2_templates t ON t.id=c.template_id WHERE c.template_version=%s", (v3,))
        assert cur.fetchone()[0] == "candidate"
    with pytest.raises(ValueError, match="scope"):
        app.rollback_version(db, '{"other":"scope"}', v1, "publisher", "wrong scope")
    with pytest.raises(ValueError, match="already published"):
        app.publish_candidate(db, candidate1, "publisher", "duplicate")
    with pytest.raises(psycopg2.Error, match="immutable"):
        with db:
            with db.cursor() as cur:
                cur.execute("UPDATE poc_v2_versions SET note='changed' WHERE id=%s", (v1,))


def test_previous_counts_upgrade_keeps_existing_rows(db):
    with db:
        with db.cursor() as cur:
            cur.execute("ALTER TABLE poc_v2_counts DROP CONSTRAINT poc_v2_counts_version_key")
            cur.execute("ALTER TABLE poc_v2_counts DROP COLUMN template_version")
            cur.execute("ALTER TABLE poc_v2_counts ADD CONSTRAINT poc_v2_counts_pkey PRIMARY KEY(template_id,bucket,source)")
            cur.execute("INSERT INTO poc_v2_templates(scope,kind,template) VALUES ('old','approved','GET /old') RETURNING id")
            tid = cur.fetchone()[0]
            cur.execute("INSERT INTO poc_v2_counts(template_id,bucket,source,count,err_4xx,err_5xx) VALUES (%s,'2026-01-01T00:00:00Z','old',7,0,0)", (tid,))
            cur.execute(app.SCHEMA)
            cur.execute(app.SCHEMA)  # startup migration is idempotent
            cur.execute("SELECT count,template_version FROM poc_v2_counts")
            assert cur.fetchone() == (7, 0)
            assert app.approved_matchers(cur, "old") == (0, [])


def test_bucket_completion_late_correction_and_baseline_invalidation(db):
    from datetime import datetime, timedelta, timezone
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    scope = json.dumps(LABELS, sort_keys=True, separators=(",", ":"))
    def send(minute, count=1, line=LINE):
        ns = str(int((start + timedelta(minutes=minute)).timestamp()) * 1_000_000_000)
        batch = body(line, ns)
        batch["streams"][0]["values"] = [[ns, line] for _ in range(count)]
        app.enqueue(db, batch)
    send(-1)
    assert app.process_one(db)
    with db.cursor() as cur:
        cur.execute("SELECT id FROM poc_v2_templates WHERE kind='candidate'")
        candidate = cur.fetchone()[0]
    app.review_candidate(db, candidate, "approve", "reviewer", "local test")
    app.publish_candidate(db, candidate, "publisher", "test baseline")
    for minute in range(3):
        send(minute, 5)
        assert app.process_one(db)
    # Before the horizon passes, and while a pending batch exists, do not finalize.
    send(3, 50)
    assert scorer_v2.run_once(db, start + timedelta(minutes=5), 60)["blocked"] == "pending_batches"
    assert scalar(db, "SELECT count(*) FROM poc_v2_buckets WHERE status='complete'") == 0
    assert app.process_one(db)
    assert scorer_v2.run_once(db, start + timedelta(minutes=4), 60)["finalized"] == 4
    assert scalar(db, "SELECT count(*) FROM poc_v2_scores WHERE status='no_baseline'") == 4
    # A gap or incomplete bucket cannot silently be treated as zero traffic.
    with pytest.raises(ValueError, match="contiguous"):
        scorer_v2.approve_baseline(db, scope, start, start + timedelta(minutes=4), "reviewer", "bad window")
    baseline = scorer_v2.approve_baseline(db, scope, start, start + timedelta(minutes=3), "reviewer", "reviewed calm window")
    scorer_v2.run_once(db, start + timedelta(minutes=5), 60)
    with db.cursor() as cur:
        cur.execute("SELECT status,is_alert,qps,bucket_revision FROM poc_v2_scores WHERE scope=%s AND bucket=%s", (scope, start + timedelta(minutes=3)))
        status, alert, qps, rev = cur.fetchone()
        assert (status, alert, rev) == ("alert", True, 1)
        assert qps == pytest.approx(50/60)
    # Late non-baseline data immediately invalidates the old score, then is rescored.
    send(3, 5)
    with db.cursor() as cur:
        cur.execute("SELECT stale FROM poc_v2_scores WHERE scope=%s AND bucket=%s", (scope,start + timedelta(minutes=3)))
        assert cur.fetchone()[0] is True  # invalidated at durable enqueue, before processing
    assert app.process_one(db)
    with db.cursor() as cur:
        cur.execute("SELECT status,revision FROM poc_v2_buckets WHERE scope=%s AND bucket=%s", (scope,start + timedelta(minutes=3)))
        assert cur.fetchone() == ("open",2)
        cur.execute("SELECT stale FROM poc_v2_scores WHERE scope=%s AND bucket=%s", (scope,start + timedelta(minutes=3)))
        assert cur.fetchone()[0] is True
    scorer_v2.run_once(db, start + timedelta(minutes=6), 60)
    with db.cursor() as cur:
        cur.execute("SELECT bucket_revision,qps,stale FROM poc_v2_scores WHERE scope=%s AND bucket=%s", (scope,start + timedelta(minutes=3)))
        revision, qps, stale = cur.fetchone()
        assert revision == 2 and qps == pytest.approx(55/60) and not stale
    # Late events in the reviewed baseline invalidate it and every derived score.
    send(1, 1)
    with db.cursor() as cur:
        cur.execute("SELECT valid FROM poc_v2_baselines WHERE id=%s", (baseline,))
        assert cur.fetchone()[0] is False  # no stale approved baseline after enqueue ACK
    assert app.process_one(db)
    with db.cursor() as cur:
        cur.execute("SELECT valid FROM poc_v2_baselines WHERE id=%s", (baseline,))
        assert cur.fetchone()[0] is False
        cur.execute("SELECT bool_and(stale) FROM poc_v2_scores WHERE baseline_id=%s", (baseline,))
        assert cur.fetchone()[0] is True
    scorer_v2.run_once(db, start + timedelta(minutes=6), 60)
    with db.cursor() as cur:
        cur.execute("SELECT DISTINCT status,is_alert FROM poc_v2_scores WHERE baseline_id=%s", (baseline,))
        assert cur.fetchall() == [("baseline_invalid", None)]
    new_baseline = scorer_v2.approve_baseline(db, scope, start, start + timedelta(minutes=3), "reviewer", "review corrected data")
    assert new_baseline != baseline
    scorer_v2.run_once(db, start + timedelta(minutes=6), 60)
    with db.cursor() as cur:
        cur.execute("SELECT status,baseline_id FROM poc_v2_scores WHERE scope=%s AND bucket=%s", (scope,start + timedelta(minutes=3)))
        assert cur.fetchone() == ("alert",new_baseline)
    # A previously unseen path is a separate alert signal even at low volume.
    send(4, 1, LINE.replace("/api/users/123", "/wp-login.php"))
    assert app.process_one(db)
    scorer_v2.run_once(db, start + timedelta(minutes=7), 60)
    with db.cursor() as cur:
        cur.execute("SELECT status,candidate_count,score FROM poc_v2_scores WHERE scope=%s AND bucket=%s", (scope,start + timedelta(minutes=4)))
        assert cur.fetchone() == ("alert", 1, 1.0)


def test_candidate_traffic_cannot_be_approved_as_baseline(db):
    from datetime import datetime, timedelta, timezone
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    scope = json.dumps(LABELS, sort_keys=True, separators=(",", ":"))
    for minute in range(3):
        ns = str(int((start + timedelta(minutes=minute)).timestamp()) * 1_000_000_000)
        app.enqueue(db, body(ns=ns))
        assert app.process_one(db)
    scorer_v2.run_once(db, start + timedelta(minutes=6), 60)
    with pytest.raises(ValueError, match="candidate traffic"):
        scorer_v2.approve_baseline(db, scope, start, start + timedelta(minutes=3), "reviewer", "attempt")


def test_finalizer_waits_for_concurrent_enqueue_commit(db):
    import threading
    from datetime import datetime, timezone
    db.rollback()
    with db.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (app.WORKER_LOCK + 1,))
        from psycopg2.extras import Json
        cur.execute("INSERT INTO poc_v2_batches(payload) VALUES (%s)", (Json(body()),))
    started = threading.Event()
    done = threading.Event()
    result = []
    def finalize():
        from contextlib import closing
        with closing(psycopg2.connect(os.environ["POC_TEST_DSN"])) as other:
            started.set()
            result.append(scorer_v2.run_once(other, datetime(2026,1,1,1,tzinfo=timezone.utc), 60))
        done.set()
    thread = threading.Thread(target=finalize)
    thread.start()
    assert started.wait(3)
    try:
        assert not done.wait(.2)
    finally:
        db.commit()
    thread.join(3)
    assert done.is_set()
    assert result[0]["blocked"] == "pending_batches"
