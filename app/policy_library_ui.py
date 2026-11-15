"""Policy document revisions and access management in the investigation workspace."""
from __future__ import annotations

import re

import httpx
import streamlit as st

_PATH = "/api/v2/policy-documents"
_SOURCES = {"policy": "政策", "bulletin": "公告"}
_MAX_DOCUMENT_BYTES = 64 * 1024
_ACCEPTED_TYPES = ("md", "markdown")
_EXAMPLE = """# 售后政策
## 华南电水壶退款口径

文号：policy-custom-kettle-202611。

华南地区的家电类商品适用本页规则，具体限于电水壶。自2026年11月1日起生效，至2026年12月1日前有效。

使用原订单退款列，每笔最多扣除 5 元。"""
_ERRORS = {
    400: "请检查上传文件、正文格式或账号名称。",
    403: "当前身份没有修改共享权限。",
    404: "找不到文档，或当前账号已没有访问权限。",
    409: "版本或文档数量已达上限，请核对当前文档状态。",
    413: "Markdown 文档不得超过 64 KiB。",
    415: "请上传 UTF-8 编码的 Markdown 文档。",
    422: "请使用 .md 或 .markdown 文件，并添加以「## 」开头的章节标题。",
}


def _document_request(method: str, api: str, headers: dict[str, str], path: str = "", **kwargs) -> dict:
    response = httpx.request(method, api + _PATH + path, headers=headers, timeout=8, trust_env=False, **kwargs)
    response.raise_for_status()
    return response.json()


def _report_error(error: Exception, default: str) -> None:
    if isinstance(error, httpx.HTTPStatusError):
        st.error(_ERRORS.get(error.response.status_code, default))
    else:
        st.error(default)


def read_policy_library(api: str, headers: dict[str, str]) -> dict | None:
    try:
        return _document_request("GET", api, headers)
    except (httpx.HTTPError, ValueError):
        return None


def document_label(document: dict) -> str:
    access = "我的文档" if document["owned"] else "共享给我"
    return (f"{document['title']} · {document['filename']} · {_SOURCES[document['source']]} "
            f"· 第 {document['version']} 版 · {access}")


def _send_file(api: str, headers: dict[str, str], upload, path: str = "", **params) -> dict | None:
    if upload is None:
        st.info("请先选择 Markdown 文件。")
        return None
    if upload.size > _MAX_DOCUMENT_BYTES:
        st.error(_ERRORS[413])
        return None
    try:
        return _document_request(
            "POST", api, {**headers, "Content-Type": "text/markdown"}, path,
            params={"filename": upload.name, **params}, content=upload.getvalue(),
        )
    except (httpx.HTTPError, ValueError) as exc:
        _report_error(exc, "文档暂时无法保存，请稍后重试。")
        return None


def render_policy_library(
    api: str, headers: dict[str, str], session_id: str, registry: dict | None,
) -> None:
    if registry is None:
        st.warning("暂时无法读取文档库。")
        return

    st.caption("上传和更新 UTF-8 Markdown，每版最多 64 KiB。调查时可选择最新可读版本，旧版本在此保留。")
    with st.expander("查看可用于调查的正文格式"):
        st.code(_EXAMPLE, language="markdown", wrap_lines=True)

    selection_key = f"policy-library-document-{session_id}"
    if message := st.session_state.pop("policy-library-feedback", None):
        st.success(message)
    with st.form(f"policy-library-upload-{session_id}", clear_on_submit=True, border=False):
        source = st.radio("文档类型", list(_SOURCES), format_func=_SOURCES.__getitem__, horizontal=True)
        upload = st.file_uploader("新 Markdown 文档", type=_ACCEPTED_TYPES)
        create = st.form_submit_button("上传文档", type="primary")
    if create:
        created = _send_file(api, headers, upload, source=source)
        if created:
            st.session_state[selection_key] = created["id"]
            st.session_state["policy-library-feedback"] = f"已上传第 {created['version']} 版。"
            st.rerun()

    documents = {item["id"]: item for item in registry["documents"]}
    if not documents:
        st.info("文档库为空。上传后可在「自然语言调查」选择文档。")
        return
    if st.session_state.get(selection_key) not in documents:
        st.session_state.pop(selection_key, None)
    document_id = st.selectbox("查看文档", list(documents), format_func=lambda key: document_label(documents[key]),
                               key=selection_key)
    selected = documents[document_id]
    path = f"/{document_id}"
    try:
        versions = _document_request("GET", api, headers, path + "/versions")["versions"]
        version_key = f"policy-library-version-{document_id}"
        if pending := st.session_state.pop("policy-library-version-next", None):
            if pending[0] == document_id:
                st.session_state[version_key] = pending[1]
        numbers = sorted((item["version"] for item in versions), reverse=True)
        version = st.selectbox("查看版本", numbers, format_func=lambda number: f"第 {number} 版", key=version_key)
        current = _document_request("GET", api, headers, f"{path}/versions/{version}")
    except (httpx.HTTPError, ValueError) as exc:
        _report_error(exc, "暂时无法读取文档版本。")
        return

    st.text(current["filename"])
    st.code(current["content"], language="markdown", line_numbers=True, wrap_lines=True)
    if not selected["owned"]:
        return

    with st.expander("上传新版本"):
        with st.form(f"policy-library-revision-{document_id}", clear_on_submit=True, border=False):
            revision = st.file_uploader("新版本 Markdown 文档", type=_ACCEPTED_TYPES)
            save = st.form_submit_button("保存新版本", type="primary")
        if save:
            revised = _send_file(api, headers, revision, path + "/versions")
            if revised:
                st.session_state["policy-library-version-next"] = (document_id, revised["version"])
                st.session_state["policy-library-feedback"] = f"已保存第 {revised['version']} 版。"
                st.rerun()
    if not registry["sharing_enabled"]:
        st.caption("本机模式下文档仅此环境可见。通过已登录的部署入口可设置读者。")
        return

    with st.expander("读者权限"):
        with st.form(f"policy-library-readers-{document_id}", border=False):
            readers = st.text_area(
                "读者账号", value="\n".join(selected["readers"]), max_chars=1500,
                help="账号用逗号或换行分隔。读者可查看历史版本及用于调查。清空后保存即可撤销共享。",
            )
            share = st.form_submit_button("保存读者权限")
        if share:
            accounts = list(dict.fromkeys(re.split(r"[\s，,]+", readers.strip()))) if readers.strip() else []
            try:
                _document_request("PUT", api, headers, path + "/readers", json={"users": accounts})
            except (httpx.HTTPError, ValueError) as exc:
                _report_error(exc, "暂时无法保存读者权限。")
            else:
                st.session_state["policy-library-feedback"] = "读者权限已保存。"
                st.rerun()
