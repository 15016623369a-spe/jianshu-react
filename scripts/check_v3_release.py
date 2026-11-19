"""Exercise V3 history/export/retail/manual through a trusted HTTPS gateway."""
from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import ssl

A1 = "2027 年 1 月华南电水壶净营收是多少？如果都按 2026 年 10 月口径重算呢？"
INVESTIGATE = "/api/v3/investigations"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--after-restart", action="store_true")
    args = parser.parse_args()
    port = int(os.environ["RAG_SMOKE_TLS_PORT"])
    trust = ssl.create_default_context(cafile=os.environ["RAG_SMOKE_CA"])
    passwords = {name: os.environ[f"RAG_SMOKE_{name.upper()}"] for name in ("alice", "bob")}

    def request(path, *, user=None, method="GET", body=None, headers=None, expect=200):
        headers = {} if headers is None else dict(headers)
        if user:
            credentials = f"{user}:{passwords[user]}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode("ascii")
        if isinstance(body, dict):
            headers["Content-Type"] = "application/json"
            body = json.dumps(body, ensure_ascii=False).encode()
        connection = http.client.HTTPSConnection("localhost", port, context=trust, timeout=30)
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            data = response.read()
        finally:
            connection.close()
        statuses = (expect,) if isinstance(expect, int) else expect
        assert response.status in statuses, f"{method} {path}: HTTP {response.status}, expected {statuses}"
        if response.headers.get("Content-Type", "").startswith("application/json"):
            return json.loads(data) if data else None
        return data

    request(INVESTIGATE, expect=401)
    if args.after_restart:
        page = request(INVESTIGATE, user="alice")
        assert page["investigations"], "history must survive restart"
        policy = next(item for item in page["investigations"] if item.get("domain") == "policy")
        loaded = request(f"{INVESTIGATE}/{policy['investigation_id']}", user="alice")
        assert loaded["status"] == "completed"
        report = request(f"{INVESTIGATE}/{policy['investigation_id']}/report.md", user="alice")
        assert "见数调查记录" in report.decode()
        amounts = request(f"{INVESTIGATE}/{policy['investigation_id']}/amounts.csv", user="alice")
        assert amounts.startswith(b"\xef\xbb\xbf")
        assert request(f"{INVESTIGATE}/{policy['investigation_id']}", user="bob", expect=404)
        print("PASS v3 history/report/CSV and owner isolation survived API restart")
        return

    retail = request(INVESTIGATE, method="POST", user="alice", body={
        "domain": "retail", "question": "2026 年 10 月华南电水壶净营收是多少？",
    })
    assert retail["kind"] == "answer" and retail["actual"][0]["net_cents"] == 27400, retail
    policy = request(INVESTIGATE, method="POST", user="alice", body={"domain": "policy", "question": A1})
    assert policy["kind"] == "answer", policy
    assert policy["actual"][0]["net_cents"] == 21100
    assert policy["counterfactual"][0]["net_cents"] == 22000
    assert policy["single_period_delta_cents"] == -900
    manual = request(INVESTIGATE, method="POST", user="alice", body={
        "domain": "manual", "question": "怎样连接 Wi-Fi？", "version": 1,
    })
    assert manual["kind"] == "answer" and "蓝色" in manual["answer"], manual
    page = request(INVESTIGATE, user="alice")
    ids = {item["investigation_id"] for item in page["investigations"]}
    assert {retail["investigation_id"], policy["investigation_id"], manual["investigation_id"]} <= ids
    assert request(f"{INVESTIGATE}/{policy['investigation_id']}/report.md", user="alice").startswith(b"# ")
    assert request(f"{INVESTIGATE}/{policy['investigation_id']}/amounts.csv", user="alice").startswith(b"\xef\xbb\xbf")
    print("PASS v3 retail/LEDGER/A1/manual/history/report/CSV")


if __name__ == "__main__":
    main()
