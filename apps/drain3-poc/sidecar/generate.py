"""合成 nginx access log 流: 多相位场景,按 Loki push 协议发给 sidecar.

相位(全部相对启动时刻, env 可选, 0/不设 = 禁用):
- calm:        RATE 行/秒 基线路径 (冷启动基线来源)
- LEGIT_SURGE_AT 秒后: 合法变更 — 总量 x SURGE_MULT + 新端点 LEGIT_PATH 上线
  (未确认时 scorer 应持续告警; 人工确认新模板 + approve_refit 后静默)
- RAMP_AT 秒后: 慢速爬坡攻击 — 对已有端点 /api/v1/orders 每秒多发
  (elapsed-RAMP_AT)*RAMP_SLOPE 行 (candidate 会吸收, stable 冻结基线负责拦截)
"""
import datetime
import os
import random
import time

import requests

URL = os.getenv("SIDECAR_URL", "http://drain3-sidecar:9800") + "/loki/api/v1/push"
DURATION = int(os.getenv("DURATION", "240"))
RATE = int(os.getenv("RATE", "20"))
ATTACK_AT = int(os.getenv("ATTACK_AT", "0") or 0)       # Stage 2 兼容: 洪水攻击波
LEGIT_SURGE_AT = int(os.getenv("LEGIT_SURGE_AT", "0") or 0)
SURGE_MULT = float(os.getenv("SURGE_MULT", "2.0"))
LEGIT_PATH = os.getenv("LEGIT_PATH", "/api/v2/checkout")
RAMP_AT = int(os.getenv("RAMP_AT", "0") or 0)
RAMP_SLOPE = float(os.getenv("RAMP_SLOPE", "0.15"))     # 每秒新增行数(线性爬坡)

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
    surge_on = attacked = ramp_on = False
    while time.time() - start < DURATION:
        el = time.time() - start
        batch = [make_line(random.choice(BASE_PATHS)) for _ in range(RATE)]

        if LEGIT_SURGE_AT and el >= LEGIT_SURGE_AT:
            if not surge_on:
                surge_on = True
                print(f">>> LEGIT SURGE at {el:.0f}s (x{SURGE_MULT} + {LEGIT_PATH})", flush=True)
            batch += [make_line(random.choice(BASE_PATHS))
                      for _ in range(int(RATE * SURGE_MULT) - RATE)]
            batch += [make_line(LEGIT_PATH) for _ in range(max(2, RATE // 5))]

        if RAMP_AT and el >= RAMP_AT:
            if not ramp_on:
                ramp_on = True
                print(f">>> SLOW RAMP attack at {el:.0f}s (slope={RAMP_SLOPE}/s)", flush=True)
            batch += [make_line("/api/v1/orders")
                      for _ in range(int((el - RAMP_AT) * RAMP_SLOPE))]

        if ATTACK_AT and el >= ATTACK_AT:
            if not attacked:
                attacked = True
                print(f">>> ATTACK WAVE start at {el:.0f}s", flush=True)
            batch += [make_line(random.choice(ATTACK_PATHS)) for _ in range(RATE * 2)]
            if random.random() < 0.2:
                batch.append(make_line("/api/v1/orders", status=503))

        push(batch)
        time.sleep(1)
    print(f"generator done: {DURATION}s surge={surge_on} ramp={ramp_on} attack={attacked}",
          flush=True)


if __name__ == "__main__":
    main()
