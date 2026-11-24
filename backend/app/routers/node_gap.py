"""节点采集缺口视图专用接口：回执确认、补采清单、派单、交付与失联重派。

通用资源路由的动作接口不带业务参数，缺口视图的领域操作需要携带节点与
来源区间，这里提供薄路由；全部业务判断落在 NodeGapService，路由不做判断。
路径均为 /api/node_gap/<操作>/... 三段式，不与通用资源路由冲突。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import service_for
from app.services.node_gap import NodeGapService

router = APIRouter(prefix="/api/node_gap", tags=["节点采集缺口视图"])


def _service() -> NodeGapService:
    service = service_for("node_gap")
    if service is None:  # pragma: no cover - 服务已在注册表登记
        raise HTTPException(status_code=404, detail="节点采集缺口视图服务未注册")
    return service  # type: ignore[return-value]


class ReceiptPayload(BaseModel):
    execution: str
    node: str
    ranges: str


class ExecutionPayload(BaseModel):
    execution: str


class DispatchPayload(BaseModel):
    execution: str
    node: str


class DeliverPayload(BaseModel):
    claim: str
    ranges: str


@router.get("/view/{execution}", response_model=dict)
def gap_view(execution: str) -> dict:
    """按执行查看节点贡献、重叠依据、剩余缺口与领取/回执台账。"""
    view = _service().gap_view(execution)
    if view is None:
        raise HTTPException(status_code=404, detail=f"采集执行 {execution} 未登记缺口视图")
    return view


@router.post("/receipts/confirm", response_model=dict)
def confirm_receipt(payload: ReceiptPayload) -> dict:
    """确认归集回执：去重累加、迟到判定、收窄他节点领取并写回视图。"""
    result = _service().confirm_receipt(payload.execution, payload.node, payload.ranges)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("reason", "回执确认失败"))
    return result


@router.post("/supplementary/refresh", response_model=dict)
def refresh_supplementary(payload: ExecutionPayload) -> dict:
    """把视图算出的剩余区间写入补采清单。"""
    result = _service().refresh_supplementary(payload.execution)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("reason", "补采清单生成失败"))
    return result


@router.post("/dispatch/request", response_model=dict)
def request_dispatch(payload: DispatchPayload) -> dict:
    """节点请求派单：按原清单派发，已领取与可重派范围不混。"""
    result = _service().request_dispatch(payload.execution, payload.node)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("reason", "派单失败"))
    return result


@router.post("/claims/deliver", response_model=dict)
def deliver_claim(payload: DeliverPayload) -> dict:
    """节点按当前领取交付来源区间，重复或越界行整批退回。"""
    result = _service().deliver_claim(payload.claim, payload.ranges)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("reason", "交付失败"))
    return result


@router.post("/nodes/unavailable", response_model=dict)
def node_unavailable(payload: DispatchPayload) -> dict:
    """节点失联：撤销其进行中领取，未采区间退回可重派。"""
    result = _service().node_unavailable(payload.execution, payload.node)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("reason", "处理失败"))
    return result
