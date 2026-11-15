"""HTTP boundary for authenticated policy document storage."""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.concurrency import run_in_threadpool

from app.hard_policy_data import DocumentSource
from app.policy_documents import MAX_DOCUMENT_BYTES, MAX_READERS
from app.upload_body import bounded_body

router = APIRouter(prefix="/api/v2/policy-documents", tags=["Policy documents"])
Filename = Annotated[str, Query(min_length=4, max_length=120)]


class Readers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    users: Annotated[list[Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.@-]{1,64}$")]], Field(max_length=MAX_READERS)]


async def _upload(request: Request, update, *args):
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type not in {"", "text/markdown", "text/plain", "application/octet-stream"}:
        raise HTTPException(415, "请上传 UTF-8 Markdown 文件正文。")
    slots = request.app.state.upload_slots
    if slots.locked():
        raise HTTPException(503, "文档上传请求较多，请稍后重试。")
    async with slots:
        data = await bounded_body(request, MAX_DOCUMENT_BYTES, size_error="Markdown 文件超过 64 KiB")
        return await run_in_threadpool(update, request.state.owner, *args, data)


@router.get("")
def list_documents(
    request: Request, limit: Annotated[int, Query(ge=1, le=200)] = 200,
    offset: Annotated[int, Query(ge=0, le=2_147_483_647)] = 0,
):
    documents = request.app.state.policy_documents.list_visible(request.state.owner, limit=limit + 1, offset=offset)
    return {
        "documents": documents[:limit],
        "sharing_enabled": bool(request.state.owner),
        "next_offset": offset + limit if len(documents) > limit else None,
    }


@router.post("", status_code=201)
async def upload_document(request: Request, filename: Filename, source: DocumentSource):
    return await _upload(request, request.app.state.policy_documents.create, filename, source)


@router.post("/{document_id}/versions")
async def upload_version(document_id: UUID, request: Request, filename: Filename):
    return await _upload(request, request.app.state.policy_documents.revise, document_id, filename)


@router.get("/{document_id}/versions")
def list_versions(document_id: UUID, request: Request):
    return {"versions": request.app.state.policy_documents.versions(request.state.owner, document_id)}


@router.get("/{document_id}/versions/{version}")
def view_version(document_id: UUID, version: Annotated[int, Path(ge=1, le=2_147_483_647)], request: Request):
    return request.app.state.policy_documents.version(request.state.owner, document_id, version)


@router.put("/{document_id}/readers")
def update_readers(document_id: UUID, body: Readers, request: Request):
    if not request.state.owner:
        raise HTTPException(403, "按账号共享仅在经过身份验证的部署中可用。")
    return {"readers": request.app.state.policy_documents.replace_readers(request.state.owner, document_id, body.users)}
