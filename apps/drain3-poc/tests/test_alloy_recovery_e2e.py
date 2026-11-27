"""Opt-in local Docker end-to-end test. Creates and stops only uniquely named test containers.

Run with POC_E2E_DOCKER=1 and POC_E2E_IMAGE=<locally built POC image>.
This exercises actual Alloy delivery and intentionally interrupts its isolated PG.
"""
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest
import requests

pytestmark = pytest.mark.skipif(os.getenv("POC_E2E_DOCKER") != "1", reason="opt-in isolated Docker E2E")


def docker(*args, timeout=60):
    result = subprocess.run(["rtk", "proxy", "docker", *args], text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise AssertionError("docker command failed: " + " ".join(args[:2]) + "\n" + result.stderr[-2000:])
    return (result.stdout + result.stderr).strip() if args[0] == "logs" else result.stdout.strip()


def eventually(label, check, timeout=40):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = check()
            if last:
                return last
        except (psycopg2.Error, requests.RequestException, AssertionError) as exc:
            last = repr(exc)
        time.sleep(.2)
    raise AssertionError(f"{label} timed out: {last}")


def test_real_alloy_complete_flow_and_restart_recovery():
    image = os.environ.get("POC_E2E_IMAGE", "drain3-v2-e2e-test-local:latest")
    suffix = uuid.uuid4().hex[:10]
    pg, api, scorer, alloy = ("drain3-e2e-" + part + "-" + suffix for part in ("pg", "api", "score", "alloy"))
    started = []
    job = "e2e-" + suffix
    scope = json.dumps({"job": job}, sort_keys=True, separators=(",", ":"))
    origin = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(second=0, microsecond=0)
    dbport = None

    def port(container, internal):
        return int(docker("port", container, str(internal)).splitlines()[0].rsplit(":", 1)[1])

    def query(sql, args=()):
        with psycopg2.connect(host="127.0.0.1", port=dbport, user="postgres", password="poc-only", dbname="drain3_test", connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return cur.fetchall()

    def sql_first(sql, args=()):
        rows = query(sql, args)
        return rows[0] if rows else None

    try:
        docker("run", "-d", "--name", pg, "--label", "drain3-e2e=local", "-e", "POSTGRES_PASSWORD=poc-only", "-e", "POSTGRES_DB=drain3_test", "-p", "127.0.0.1::5432", "postgres:16-alpine")
        started.append(pg)
        dbport = port(pg, 5432)
        eventually("PostgreSQL startup", lambda: sql_first("SELECT 1"))
        dsn = "host=postgres dbname=drain3_test user=postgres password=poc-only connect_timeout=2"
        docker("run", "-d", "--name", api, "--label", "drain3-e2e=local", "--link", pg + ":postgres", "-e", "PG_DSN=" + dsn, image)
        started.append(api)
        eventually("V2 schema", lambda: sql_first("SELECT to_regclass('poc_v2_scores')")[0])
        docker("run", "-d", "--name", scorer, "--label", "drain3-e2e=local", "--link", pg + ":postgres", "-e", "PG_DSN=" + dsn, "-e", "ALLOWED_LATENESS_SECONDS=0", "-e", "SCORER_INTERVAL_SECONDS=1", image, "python", "/app/scorer_v2.py", "run")
        started.append(scorer)
        docker("create", "--name", alloy, "--label", "drain3-e2e=local", "--link", api + ":sidecar", "-p", "127.0.0.1::9999", "grafana/alloy:latest")
        started.append(alloy)
        docker("cp", str(Path(__file__).with_name("alloy-smoke.alloy")), alloy + ":/etc/alloy/config.alloy")
        docker("start", alloy)
        alloyport = port(alloy, 9999)
        url = f"http://127.0.0.1:{alloyport}/loki/api/v1/push"
        eventually("Alloy input ready", lambda: requests.get(f"http://127.0.0.1:{alloyport}/ready", timeout=2).status_code == 200)

        def send(minute, count, path="/api/users/123", status=200):
            bucket = origin + timedelta(minutes=minute)
            ns = str(int(bucket.timestamp()) * 1_000_000_000)
            line = f'127.0.0.1 - - [01/Jan/2026:00:00:00 +0000] "GET {path} HTTP/1.1" {status} 10'
            payload = {"streams": [{"stream": {"job": job}, "values": [[ns, line] for _ in range(count)]}]}
            response = requests.post(url, json=payload, timeout=5)
            assert response.status_code == 204, response.text
            return bucket

        def count_at(bucket):
            return sql_first("SELECT COALESCE(sum(count),0) FROM poc_v2_counts WHERE source=%s AND bucket=%s", (scope, bucket))[0]

        def score_at(bucket):
            return sql_first("SELECT status,score,is_alert,stale,bucket_revision,baseline_id,candidate_count FROM poc_v2_scores WHERE scope=%s AND bucket=%s", (scope, bucket))

        print("phase: learn candidate through Alloy")
        boot = send(-1, 1)
        eventually("bootstrap event", lambda: count_at(boot) == 1)
        candidate = sql_first("SELECT id FROM poc_v2_templates WHERE scope=%s AND kind='candidate' AND template='GET /api/users/{D}'", (scope,))[0]
        docker("exec", api, "python", "/app/template_admin.py", "review", str(candidate), "approve", "--actor", "e2e", "--note", "local test")
        docker("exec", api, "python", "/app/template_admin.py", "publish", str(candidate), "--actor", "e2e", "--note", "local test")

        print("phase: complete and approve baseline")
        for minute in range(3):
            send(minute, 10)
        def baseline_ready():
            row = sql_first("""SELECT count(*),COALESCE(sum(c.count),0)
                FROM poc_v2_buckets b JOIN poc_v2_counts c ON c.source=b.scope AND c.bucket=b.bucket
                WHERE b.scope=%s AND b.bucket >= %s AND b.bucket < %s AND b.status='complete'""", (scope, origin, origin + timedelta(minutes=3)))
            return row and row == (3, 30)
        eventually("complete baseline buckets", baseline_ready)
        baseline = int(docker("exec", scorer, "python", "/app/scorer_v2.py", "approve-baseline", scope, origin.isoformat(), (origin + timedelta(minutes=3)).isoformat(), "--actor", "e2e", "--note", "reviewed test window"))
        eventually("baseline scoring", lambda: score_at(origin) and score_at(origin)[0] == "ok" and score_at(origin)[5] == baseline)

        print("phase: surge and late candidate correction")
        attack = send(3, 80)
        eventually("surge alert", lambda: score_at(attack) and score_at(attack)[0] == "alert" and score_at(attack)[2] is True)
        first_rev = score_at(attack)[4]
        send(3, 1, "/wp-login.php")
        eventually("late candidate alert", lambda: score_at(attack) and score_at(attack)[0] == "alert" and not score_at(attack)[3] and score_at(attack)[4] > first_rev and score_at(attack)[6] == 1 and count_at(attack) == 81)
        assert sql_first("SELECT count(*) FROM poc_v2_templates WHERE scope=%s AND kind='approved' AND template='GET /wp-login.php'", (scope,))[0] == 0

        print("phase: late baseline data invalidates and is re-reviewed")
        send(1, 1)
        eventually("baseline invalidated", lambda: sql_first("SELECT valid FROM poc_v2_baselines WHERE id=%s", (baseline,))[0] is False)
        eventually("invalid score", lambda: score_at(attack) and score_at(attack)[0] == "baseline_invalid" and not score_at(attack)[3])
        eventually("corrected baseline bucket", lambda: count_at(origin + timedelta(minutes=1)) == 11 and sql_first("SELECT status FROM poc_v2_buckets WHERE scope=%s AND bucket=%s", (scope, origin + timedelta(minutes=1)))[0] == "complete")
        new_baseline = int(docker("exec", scorer, "python", "/app/scorer_v2.py", "approve-baseline", scope, origin.isoformat(), (origin + timedelta(minutes=3)).isoformat(), "--actor", "e2e", "--note", "review corrected data"))
        assert new_baseline != baseline
        eventually("rescored after review", lambda: score_at(attack) and score_at(attack)[0] == "alert" and score_at(attack)[5] == new_baseline)

        print("phase: sidecar and scorer process restart")
        docker("restart", "-t", "2", api, scorer, timeout=30)
        restarted = send(4, 10)
        eventually("processing after process restart", lambda: count_at(restarted) == 10 and score_at(restarted) and score_at(restarted)[5] == new_baseline and score_at(restarted)[0] == "ok")
        assert count_at(attack) == 81

        print("phase: PostgreSQL outage, Alloy retry, recovery")
        previous_503s = docker("logs", api).count(' 503 ')
        docker("stop", "-t", "2", pg, timeout=20)
        outage_started = time.monotonic()
        during_outage = send(5, 7)
        eventually("sidecar rejects while PostgreSQL is down", lambda: docker("logs", api).count(' 503 ') > previous_503s, timeout=15)
        docker("start", pg)
        dbport = port(pg, 5432)
        eventually("PostgreSQL restart", lambda: sql_first("SELECT 1"))
        outage_seconds = time.monotonic() - outage_started
        eventually("Alloy resend and scoring after PostgreSQL outage", lambda: count_at(during_outage) == 7 and score_at(during_outage) and score_at(during_outage)[5] == new_baseline and score_at(during_outage)[0] in ("ok", "alert"), timeout=60)
        eventually("queue drained after outage", lambda: sql_first("SELECT count(*) FROM poc_v2_batches WHERE processed_at IS NULL")[0] == 0)
        time.sleep(3)  # observe late retry duplicates after the queue first drains
        assert count_at(during_outage) == 7
        print(f"PostgreSQL unavailable for approximately {outage_seconds:.1f}s; 7/7 records observed after recovery")
        assert count_at(attack) == 81
        assert sql_first("SELECT valid FROM poc_v2_baselines WHERE id=%s", (new_baseline,))[0] is True
        print("phase: 5xx ratio alert after recovery")
        errors = send(6, 10, status=503)
        def error_alert():
            row = sql_first("SELECT status,is_alert,err_5xx_ratio,baseline_id FROM poc_v2_scores WHERE scope=%s AND bucket=%s AND NOT stale", (scope, errors))
            return row and row == ("alert", True, 1.0, new_baseline)
        eventually("5xx ratio alert", error_alert)
        print("all phases passed")
    finally:
        for container in reversed(started):
            subprocess.run(["rtk", "proxy", "docker", "stop", "-t", "2", container], capture_output=True, text=True, timeout=20)
