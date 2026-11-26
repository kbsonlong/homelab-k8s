"""合成 nginx access log 流: 基线期 + 攻击期,按 Loki push 协议发给 sidecar."""
import datetime
import os
import random
import time

import requests

URL = os.getenv("SIDECAR_URL", "http://drain3-sidecar:9800") + "/loki/api/v1/push"
DURATION = int(os.getenv("DURATION", "240"))     # 总运行秒数
ATTACK_AT = int(os.getenv("ATTACK_AT", "60"))    # 第 N 秒开始注入攻击流量
RATE = int(os.getenv("RATE", "20"))              # 基线行/秒

BASE_PATHS = [
    "/api/v1/users", "/api/v1/orders", "/index.html",
    "/static/css/main.css", "/api/v1/products",
]
ATTACK_PATHS = [
    "/wp-login.php", "/admin/config.php", "/../../etc/passwd",
]


def make_line(path: str, status: int = 200) -> str:
    ip = f"10.0.{random.randint(0, 255)}.{random.randint(1, 254)}"
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%d/%b/%Y:%H:%M:%S %z")
    rt = round(random.uniform(0.01, 0.3), 3)
    return (f'{ip} - - [{ts}] "GET {path} HTTP/1.1" {status} '
            f'{random.randint(200, 5000)} {rt}')


def push(lines):
    now_ns = str(time.time_ns())
    payload = {"streams": [{"stream": {"job": "nginx-poc", "source": "generator"},
                            "values": [[now_ns, l] for l in lines]}]}
    r = requests.post(URL, json=payload, timeout=5)
    r.raise_for_status()


def main():
    start = time.time()
    attacked = False
    while time.time() - start < DURATION:
        batch = [make_line(random.choice(BASE_PATHS)) for _ in range(RATE)]
        elapsed = time.time() - start
        if elapsed >= ATTACK_AT and not attacked:
            attacked = True
            print(f">>> ATTACK WAVE start at {elapsed:.0f}s", flush=True)
        if attacked:
            # 攻击期: 新模板高频 + 少量 5xx
            batch.extend(make_line(random.choice(ATTACK_PATHS)) for _ in range(RATE * 2))
            if random.random() < 0.2:
                batch.append(make_line("/api/v1/orders", status=503))
        push(batch)
        time.sleep(1)
    print(f"generator done: {DURATION}s, attacked={attacked}", flush=True)


if __name__ == "__main__":
    main()
