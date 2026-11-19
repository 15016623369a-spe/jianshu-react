"""Exercise the policy release through a trusted local HTTPS gateway."""
from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import ssl
import time
from pathlib import Path
from urllib.parse import urlencode

DOCS = "/api/v2/policy-documents"
QUERY = "/api/v2/policy-investigation"
QUESTION = "华南电水壶 2026 年 10 月净营收是多少？"
EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "demo"


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
        connection = http.client.HTTPSConnection("localhost", port, context=trust, timeout=15)
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            data = response.read()
        finally:
            connection.close()
        statuses = (expect,) if isinstance(expect, int) else expect
        assert response.status in statuses, f"{method} {path}: HTTP {response.status}, expected {statuses}"
        if data and response.headers.get("Content-Type", "").startswith("application/json"):
            return json.loads(data)
        return None

    def net(user, doc_id, revision, amount):
        result = request(QUERY, method="POST", user=user, body={"question": QUESTION, "document_ids": [doc_id]})
        assert result["kind"] == "answer", result["reason"]
        assert result["actual"][0]["net_cents"] == amount
        evidence = result["actual"][0]["segments"][0]["documents"][0]
        assert f"/{doc_id}/v{revision}/" in evidence["file"]
        assert result["metrics"]["model_calls"] == 0
        return result["document_version"]

    # Compose waits for API/UI health; the TLS listener may still be opening.
    for attempt in range(20):
        try:
            request(DOCS, expect=401)
            break
        except OSError:
            if attempt == 19:
                raise
            time.sleep(0.5)

    if args.after_restart:
        docs = request(DOCS, user="alice")["documents"]
        assert len(docs) == 1, "smoke check must run against its disposable state volume"
        doc = docs[0]
        assert doc["version"] == 2
        net("alice", doc["id"], 2, 29480)
        request(f"{DOCS}/{doc['id']}/versions", user="bob", expect=404)
        print("PASS revisions and revoked ACL persisted across API restart")
        return

    def upload(path, filename):
        return request(path, method="POST", user="alice", expect=(200, 201),
                       headers={"Content-Type": "text/markdown; charset=utf-8"},
                       body=(EXAMPLES / filename).read_bytes())

    upload_query = urlencode({"filename": "policy-upload-v1.md", "source": "policy"})
    created = upload(f"{DOCS}?{upload_query}", "policy-upload-v1.md")
    doc_id = created["id"]
    document = f"{DOCS}/{doc_id}"
    version_before = net("alice", doc_id, 1, 29280)
    assert request(DOCS, user="bob", headers={"X-Rag-User": "alice"})["documents"] == []
    request(f"{document}/versions/1", user="bob", expect=404)
    request(QUERY, method="POST", user="bob", expect=404, body={"question": QUESTION, "document_ids": [doc_id]})

    request(f"{document}/readers", method="PUT", user="alice", body={"users": ["bob"]}, expect=(200, 204))
    net("bob", doc_id, 1, 29280)
    request(f"{document}/readers", method="PUT", user="bob", body={"users": []}, expect=404)

    revision_query = urlencode({"filename": "policy-upload-v2.md"})
    changed = upload(f"{document}/versions?{revision_query}", "policy-upload-v2.md")
    assert changed["id"] == doc_id and changed["version"] == 2
    assert request(f"{document}/versions/1", user="bob")["version"] == 1
    version_after = net("bob", doc_id, 2, 29480)
    assert version_after != version_before

    request(f"{document}/readers", method="PUT", user="alice", body={"users": []}, expect=(200, 204))
    request(f"{document}/versions", user="bob", expect=404)
    request(QUERY, method="POST", user="bob", expect=404, body={"question": QUESTION, "document_ids": [doc_id]})

    request(f"{document}/readers", method="PUT", user="alice", expect=403,
            body={"users": ["bob"]}, headers={"Origin": "https://foreign.invalid"})
    request(f"{DOCS}?{upload_query}", method="POST", user="alice", body=b"x" * 65537,
            headers={"Content-Type": "text/markdown"}, expect=413)
    # Verify the bundled sample remains independent of private releases.
    baseline = request(QUERY, method="POST", user="alice", body={"question": QUESTION})
    assert baseline["actual"][0]["net_cents"] == 29280
    print("PASS TLS, anonymous denial, forged identity, versioned uploads, share/read/revoke and upload limits")


if __name__ == "__main__":
    main()
