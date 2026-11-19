"""Grounded manual lookup in the investigation workspace."""
from __future__ import annotations

import httpx
import streamlit as st

from app.policy_library_ui import _document_request, document_label


def render_manual_investigation(api: str, headers: dict[str, str], session_id: str, registry: dict | None) -> None:
    st.caption("选择手册及版本，查询一个操作。回答引用该版本的正文。")
    use_uploads = st.radio(
        "手册来源", ["演示手册", "文档库手册"], horizontal=True, key=f"manual-source-{session_id}",
    ) == "文档库手册"
    docs = {item["id"]: item for item in registry["documents"]} if registry is not None else {}
    document_id = None
    latest = 2
    versions = [2, 1]
    if use_uploads:
        if registry is None:
            st.warning("暂时无法读取手册文档库。")
            return
        if not docs:
            st.info("可在「文档库」的「产品手册」上传 Markdown。")
            return
        document_id = st.selectbox("手册", list(docs), format_func=lambda key: document_label(docs[key]),
                                   key=f"manual-document-{session_id}")
        latest = docs[document_id]["version"]
        try:
            revisions = _document_request("GET", api, headers, f"/{document_id}/versions", manual=True)
            versions = sorted((item["version"] for item in revisions["versions"]), reverse=True)
        except (httpx.HTTPError, ValueError):
            st.warning("当前无法读取该手册，请重新选择。")
            return

    version = st.selectbox("手册版本", versions, format_func=lambda number: f"第 {number} 版",
                           key=f"manual-version-{session_id}-{document_id}")
    scope = session_id, document_id, version, latest
    result_key = "manual-investigation-result"
    with st.form(f"manual-investigation-{session_id}"):
        question = st.text_input("你想查什么操作？", "怎样连接 Wi-Fi？", max_chars=500,
                                 key=f"manual-question-{session_id}")
        submitted = st.form_submit_button("查阅手册", type="primary")
    if submitted:
        st.session_state.pop(result_key, None)
        if not question.strip():
            st.info("请输入要查的操作。")
            return
        try:
            response = httpx.post(
                api + "/api/v3/investigations", headers=headers, timeout=15, trust_env=False,
                json={"domain": "manual", "question": question.strip(), "document_id": document_id, "version": version},
            )
            response.raise_for_status()
            st.session_state[result_key] = scope, response.json()
        except (httpx.HTTPError, ValueError):
            st.warning("暂时无法读取该版本，请检查文档和访问权限。")
    saved_scope, result = st.session_state.get(result_key, (None, None))
    if saved_scope != scope or not result:
        return
    if result["kind"] != "answer":
        st.info(result["message"])
        return

    st.markdown("**手册原文**")
    st.text(result["answer"])
    with st.expander("查看出处"):
        for reference in result["evidence"]:
            st.text(reference["title"])
            st.text(f"{reference['file']} · 第 {reference['version']} 版 · "
                    f"第 {reference['line_start']}–{reference['line_end']} 行")
