"""节点采集缺口视图服务：按执行汇总节点贡献、重叠依据与剩余缺口。

机器人节点原本只能看到当前负载；本模块把同一采集执行下各节点确认的
归集回执与每次接手（领取）的来源范围关联起来，按执行算出每个节点的
实际贡献、回执之间的重叠依据和剩余缺口区间：

- 重复提交只累加一次有效行，迟到回执不能让同一来源行再次进入归集；
- 视图算出的剩余区间写入补采清单，由任务队列兑现节点领到的范围；
- 回执覆盖他节点领取时收窄或撤销该领取，并把变更写回视图；
- 已领取的范围和可重新分配的范围分开核算，值班员按原清单继续派发也不混；
- 节点失联后只把仍未采集的区间退回可重派，归属变更后仍能追到原上传节点。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.ranges import (
    format_ranges,
    parse_ranges,
    ranges_to_rows,
    rows_to_ranges,
)
from app.services.base import BaseModuleService
from app.store import store

RECEIPT_TABLE = "node_gap_receipt"
CLAIM_TABLE = "node_gap_claim"
ITEM_TABLE = "node_gap_item"


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _next_id(table: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(table)), default=0) + 1


def _next_code(table: str, field: str, prefix: str) -> str:
    """按表内现有编号生成下一个编号，store 重置后仍然确定。"""
    highest = 0
    for row in store.rows(table):
        code = str(row.get(field, ""))
        if code.startswith(prefix):
            try:
                highest = max(highest, int(code[len(prefix):]))
            except ValueError:
                continue
    return f"{prefix}{highest + 1:04d}"


class NodeGapService(BaseModuleService):
    MODULE = "node_gap"
    ENTITY = "缺口视图"
    LIST_FIELDS = [
        "执行编号", "应采区间", "已归集行数", "缺口区间",
        "已领取区间", "可重派区间", "参与节点", "最近变更",
    ]
    REQUIRED_FIELDS = ["执行编号", "应采区间"]
    STATUS_ORDER = ["待补采", "补采中", "需重派", "已补齐"]
    ACTION_RULES = {"生成补采清单": "补采中", "核对补齐": "已补齐"}

    # ── 登记与通用动作 ────────────────────────────────────────

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        execution = str(values.get("执行编号", "")).strip()
        if execution and self._gap_row(execution) is not None:
            return None, ["执行编号（同一执行已登记缺口视图）"]
        try:
            expected = parse_ranges(str(values.get("应采区间", "")))
        except (ValueError, TypeError):
            return None, ["应采区间（格式应为 1-16 或 1-8,13-14）"]
        if not expected:
            return None, ["应采区间（不能为空）"]
        entry, missing = super().create_entry(values)
        if missing:
            return None, missing
        entry["应采区间"] = format_ranges(expected)
        entry["已归集行数"] = "0"
        entry["缺口区间"] = entry["应采区间"]
        entry["已领取区间"] = "-"
        entry["可重派区间"] = entry["应采区间"]
        entry["参与节点"] = "-"
        entry["最近变更"] = "执行登记"
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action == "生成补采清单":
            result = self.refresh_supplementary(entry["执行编号"])
            if not result["ok"]:
                return None, result["reason"]
            return self.get_entry(entry_id), (
                f"补采清单第{result['generation']}批已写入：{result['ranges']}"
            )
        if action == "核对补齐":
            self._sync_view(entry)
            if entry["缺口区间"] == "-":
                return entry, "缺口已补齐，无待采集行"
            return entry, (
                f"仍有缺口 {entry['缺口区间']}，可重派 {entry['可重派区间']}"
            )
        return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

    # ── 表访问 ────────────────────────────────────────────────

    def _gap_row(self, execution: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if row.get("执行编号") == execution:
                return row
        return None

    def _receipts(self, execution: str) -> list[dict[str, Any]]:
        return [r for r in store.rows(RECEIPT_TABLE) if r.get("执行编号") == execution]

    def _claims(self, execution: str) -> list[dict[str, Any]]:
        return [c for c in store.rows(CLAIM_TABLE) if c.get("执行编号") == execution]

    def _items(self, execution: str) -> list[dict[str, Any]]:
        return [i for i in store.rows(ITEM_TABLE) if i.get("执行编号") == execution]

    def _active_claims(self, execution: str) -> list[dict[str, Any]]:
        return [c for c in self._claims(execution) if c.get("status") == "进行中"]

    def _find_claim(self, claim_code: str) -> dict[str, Any] | None:
        for claim in store.rows(CLAIM_TABLE):
            if claim.get("领取编号") == claim_code:
                return claim
        return None

    def _collected_rows(self, execution: str) -> set[int]:
        """已归集的来源行：各回执有效区间的并集，重复提交只累加一次。"""
        rows: set[int] = set()
        for receipt in self._receipts(execution):
            rows |= ranges_to_rows(parse_ranges(receipt.get("有效区间", "-")))
        return rows

    def _active_claim_rows(self, execution: str, exclude_node: str | None = None) -> set[int]:
        rows: set[int] = set()
        for claim in self._active_claims(execution):
            if exclude_node is not None and claim.get("节点编号") == exclude_node:
                continue
            rows |= ranges_to_rows(parse_ranges(claim.get("当前区间", "-")))
        return rows

    def _uncollected_rows(self, execution: str) -> set[int]:
        gap = self._gap_row(execution)
        expected = ranges_to_rows(parse_ranges(gap["应采区间"])) if gap else set()
        return expected - self._collected_rows(execution)

    def _dispatchable_rows(self, execution: str) -> set[int]:
        """可重新分配的行：未归集且不在任何进行中领取内。"""
        return self._uncollected_rows(execution) - self._active_claim_rows(execution)

    @staticmethod
    def _append_claim_log(claim: dict[str, Any], line: str) -> None:
        """追加领取变更记录；整体替换字段值，避免原地改动种子数据。"""
        log = claim.get("变更记录", "")
        claim["变更记录"] = (log + f"\n[{_now_iso()}] {line}").strip()

    # ── 归集回执 ──────────────────────────────────────────────

    def confirm_receipt(
        self,
        execution: str,
        node: str,
        ranges_text: str,
        linked_claim: str = "",
    ) -> dict[str, Any]:
        """确认一张归集回执：去重累加、迟到判定、收窄他节点领取并写回视图。"""
        gap = self._gap_row(execution)
        if gap is None:
            return {"ok": False, "reason": f"采集执行 {execution} 未登记缺口视图"}
        try:
            ranges = parse_ranges(ranges_text)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "reason": f"来源区间无法解析：{exc}"}
        expected = ranges_to_rows(parse_ranges(gap["应采区间"]))
        rows = ranges_to_rows(ranges) & expected
        if not rows:
            return {"ok": False, "reason": "来源区间不在应采范围内"}
        collected = self._collected_rows(execution)
        new_rows = rows - collected
        duplicate_rows = rows & collected
        # 迟到判定：回执到达时，来源行已在他节点的进行中领取内
        late_rows = rows & self._active_claim_rows(execution, exclude_node=node)
        code = _next_code(RECEIPT_TABLE, "回执编号", "RCV-")
        receipt = {
            "id": _next_id(RECEIPT_TABLE),
            "status": "已确认",
            "pending": False,
            "abnormal": bool(late_rows),
            "回执编号": code,
            "执行编号": execution,
            "节点编号": node,
            "来源区间": format_ranges(ranges),
            "有效区间": format_ranges(rows_to_ranges(new_rows)),
            "重复区间": format_ranges(rows_to_ranges(duplicate_rows)),
            "有效行数": str(len(new_rows)),
            "重复行数": str(len(duplicate_rows)),
            "是否迟到": "是" if late_rows else "否",
            "确认顺序": str(len(self._receipts(execution)) + 1),
            "关联领取": linked_claim,
            "确认时间": _now_iso(),
        }
        store.rows(RECEIPT_TABLE).append(receipt)
        # 回执覆盖他节点领取 → 收窄或撤销，变更写回视图
        narrowed = []
        for claim in self._active_claims(execution):
            if claim.get("节点编号") == node:
                continue
            claim_rows = ranges_to_rows(parse_ranges(claim.get("当前区间", "-")))
            hit = claim_rows & new_rows
            if not hit:
                continue
            remaining = rows_to_ranges(claim_rows - hit)
            claim["当前区间"] = format_ranges(remaining)
            self._append_claim_log(
                claim,
                f"收窄：区间{format_ranges(rows_to_ranges(hit))}被回执{code}覆盖，"
                f"剩余{format_ranges(remaining)}",
            )
            if not remaining:
                claim["status"] = "已撤销"
                claim["pending"] = False
                self._append_claim_log(claim, f"撤销：领取范围被回执{code}全部覆盖")
            narrowed.append(claim["领取编号"])
        gap["最近变更"] = (
            f"回执{code}确认：新增{receipt['有效区间']}"
            + (f"，重复{receipt['重复区间']}" if duplicate_rows else "")
            + ("，迟到回执" if late_rows else "")
        )
        self._sync_view(gap)
        return {
            "ok": True,
            "receipt": receipt,
            "new_rows": sorted(new_rows),
            "duplicate_rows": sorted(duplicate_rows),
            "is_late": bool(late_rows),
            "narrowed_claims": narrowed,
        }

    # ── 补采清单与任务队列 ────────────────────────────────────

    def refresh_supplementary(self, execution: str) -> dict[str, Any]:
        """把视图算出的剩余区间写入补采清单，旧批次未派发项标记已取代。"""
        gap = self._gap_row(execution)
        if gap is None:
            return {"ok": False, "reason": f"采集执行 {execution} 未登记缺口视图"}
        dispatchable = self._dispatchable_rows(execution)
        if not dispatchable:
            return {"ok": False, "reason": "没有可写入补采清单的剩余区间"}
        generation = max(
            (int(item.get("批次", "0")) for item in self._items(execution)), default=0
        ) + 1
        for item in self._items(execution):
            if item.get("status") == "待派发":
                item["status"] = "已取代"
                item["pending"] = False
        codes = []
        for rng in rows_to_ranges(dispatchable):
            code = _next_code(ITEM_TABLE, "清单编号", "SUP-")
            store.rows(ITEM_TABLE).append({
                "id": _next_id(ITEM_TABLE),
                "status": "待派发",
                "pending": True,
                "abnormal": False,
                "清单编号": code,
                "执行编号": execution,
                "区间": format_ranges([rng]),
                "批次": str(generation),
            })
            codes.append(code)
        ranges_text = format_ranges(rows_to_ranges(dispatchable))
        gap["最近变更"] = f"补采清单第{generation}批写入：{ranges_text}"
        self._sync_view(gap)
        return {"ok": True, "generation": generation, "items": codes, "ranges": ranges_text}

    def request_dispatch(self, execution: str, node: str) -> dict[str, Any]:
        """节点请求派单：按原清单派发，已领取与已归集的范围不混入。"""
        gap = self._gap_row(execution)
        if gap is None:
            return {"ok": False, "reason": f"采集执行 {execution} 未登记缺口视图"}
        open_items = [i for i in self._items(execution) if i.get("status") == "待派发"]
        if not open_items:
            return {"ok": False, "reason": "补采清单没有待派发项，请先生成补采清单"}
        item_rows: set[int] = set()
        for item in open_items:
            item_rows |= ranges_to_rows(parse_ranges(item.get("区间", "-")))
        available = (
            item_rows
            - self._collected_rows(execution)
            - self._active_claim_rows(execution)
        )
        if not available:
            return {"ok": False, "reason": "清单剩余区间均已归集或被他节点领取，无可派发范围"}
        ranges = rows_to_ranges(available)
        code = _next_code(CLAIM_TABLE, "领取编号", "CLM-")
        # 归属变更可追溯：找到这些来源行上一张领取
        previous = ""
        for claim in sorted(self._claims(execution), key=lambda c: int(c.get("id", 0))):
            held = ranges_to_rows(parse_ranges(claim.get("原领取区间", "-")))
            if held & available:
                previous = claim["领取编号"]
        source_items = "、".join(
            item["清单编号"] for item in open_items
            if ranges_to_rows(parse_ranges(item.get("区间", "-"))) & available
        )
        claim = {
            "id": _next_id(CLAIM_TABLE),
            "status": "进行中",
            "pending": True,
            "abnormal": False,
            "领取编号": code,
            "执行编号": execution,
            "节点编号": node,
            "原领取区间": format_ranges(ranges),
            "当前区间": format_ranges(ranges),
            "来源清单": source_items,
            "上一领取": previous,
            "变更记录": f"[{_now_iso()}] 领取{format_ranges(ranges)}（清单{source_items}）",
        }
        store.rows(CLAIM_TABLE).append(claim)
        self._queue_task_for_claim(claim)
        gap["最近变更"] = f"节点{node}领取{format_ranges(ranges)}"
        self._sync_view(gap)
        return {"ok": True, "claim": claim}

    def deliver_claim(self, claim_code: str, ranges_text: str) -> dict[str, Any]:
        """节点按领取交付来源区间；含已归集或已收窄出去的行则整批退回。"""
        claim = self._find_claim(claim_code)
        if claim is None:
            return {"ok": False, "reason": f"领取 {claim_code} 不存在"}
        if claim.get("status") != "进行中":
            return {"ok": False, "reason": f"领取 {claim_code} 状态为{claim.get('status')}，不能交付"}
        execution = claim["执行编号"]
        try:
            rows = ranges_to_rows(parse_ranges(ranges_text))
        except (ValueError, TypeError) as exc:
            return {"ok": False, "reason": f"交付区间无法解析：{exc}"}
        claim_rows = ranges_to_rows(parse_ranges(claim.get("当前区间", "-")))
        outside = rows - claim_rows
        already = rows & self._collected_rows(execution)
        if outside or already:
            # 迟到回执已覆盖的行不能再次进入归集，整批退回让节点按当前领取重交
            self._append_claim_log(
                claim,
                f"交付被拒：已归集{format_ranges(rows_to_ranges(already))}，"
                f"超出当前领取{format_ranges(rows_to_ranges(outside))}",
            )
            gap = self._gap_row(execution)
            gap["最近变更"] = f"领取{claim_code}交付被拒，已归集行不重复计入"
            self._sync_view(gap)
            return {
                "ok": False,
                "reason": "交付区间含已归集或已不在当前领取内的行，已整批退回",
                "already_collected": format_ranges(rows_to_ranges(already)),
                "outside_claim": format_ranges(rows_to_ranges(outside)),
            }
        result = self.confirm_receipt(
            execution, claim["节点编号"], ranges_text, linked_claim=claim_code
        )
        if not result["ok"]:
            return result
        claim["status"] = "已兑现"
        claim["pending"] = False
        self._append_claim_log(claim, f"兑现：回执{result['receipt']['回执编号']}")
        self._settle_queue_task(claim, "回收任务")
        gap = self._gap_row(execution)
        gap["最近变更"] = f"领取{claim_code}兑现，回执{result['receipt']['回执编号']}"
        self._sync_view(gap)
        return {"ok": True, "receipt": result["receipt"], "claim": claim}

    # ── 节点失联与重派 ────────────────────────────────────────

    def node_unavailable(self, execution: str, node: str) -> dict[str, Any]:
        """节点失联：摘除节点、撤销其进行中领取，未采区间退回可重派。"""
        gap = self._gap_row(execution)
        if gap is None:
            return {"ok": False, "reason": f"采集执行 {execution} 未登记缺口视图"}
        from app.services import service_for

        node_svc = service_for("robot_node")
        if node_svc is not None:
            for entry in node_svc.list_entries(size=10000)[0]:
                if entry.get("节点编号") == node and entry.get("status") != "离线":
                    node_svc.run_action(entry["id"], "摘除节点")
        revoked = []
        returned: set[int] = set()
        for claim in self._active_claims(execution):
            if claim.get("节点编号") != node:
                continue
            claim["status"] = "已撤销"
            claim["pending"] = False
            claim["abnormal"] = True
            self._append_claim_log(
                claim, f"撤销：节点{node}失联，区间{claim['当前区间']}退回可重派"
            )
            returned |= ranges_to_rows(parse_ranges(claim.get("当前区间", "-")))
            revoked.append(claim["领取编号"])
            self._settle_queue_task(claim, "丢弃任务")
        if not revoked:
            return {"ok": False, "reason": f"节点{node}在执行{execution}没有进行中的领取"}
        returned_text = format_ranges(rows_to_ranges(returned))
        gap["最近变更"] = f"节点{node}失联，撤销{'、'.join(revoked)}，退回{returned_text}"
        self._sync_view(gap)
        return {"ok": True, "revoked": revoked, "returned_ranges": returned_text}

    # ── 任务队列兑现 ──────────────────────────────────────────

    @staticmethod
    def _queue_task_for_claim(claim: dict[str, Any]) -> None:
        """为领取生成排队任务并派发到节点，由任务队列兑现领到的范围。"""
        from app.services import service_for

        queue_svc = service_for("task_queue")
        if queue_svc is None:
            return
        task, missing = queue_svc.create_entry({
            "队列编号": f"TASK-{claim['领取编号']}",
            "关联模板": claim["执行编号"],
            "优先级": "high",
            "期望开始": _now_iso(),
            "重试次数": "0",
            "占用节点": "",
            "等待秒数": "0",
            "入队时间": _now_iso(),
        })
        if missing:
            return
        queue_svc.dispatch_to_node(task["id"], claim["节点编号"])

    @staticmethod
    def _settle_queue_task(claim: dict[str, Any], action: str) -> None:
        """兑现或撤销领取时同步结算对应的排队任务。"""
        from app.services import service_for

        queue_svc = service_for("task_queue")
        if queue_svc is None:
            return
        for task in queue_svc.list_entries(size=10000)[0]:
            if task.get("队列编号") == f"TASK-{claim['领取编号']}":
                queue_svc.run_action(task["id"], action)

    # ── 视图汇总 ──────────────────────────────────────────────

    def _sync_view(self, gap: dict[str, Any]) -> None:
        """把回执与领取的最新状态写回视图行（含撤销或收窄的范围）。"""
        execution = gap["执行编号"]
        expected = ranges_to_rows(parse_ranges(gap["应采区间"]))
        collected = self._collected_rows(execution) & expected
        uncollected = expected - collected
        active = self._active_claim_rows(execution)
        dispatchable = uncollected - active
        gap["已归集行数"] = str(len(collected))
        gap["缺口区间"] = format_ranges(rows_to_ranges(uncollected))
        gap["已领取区间"] = format_ranges(rows_to_ranges(active & uncollected))
        gap["可重派区间"] = format_ranges(rows_to_ranges(dispatchable))
        nodes: list[str] = []
        for row in self._receipts(execution) + self._claims(execution):
            node = row.get("节点编号", "")
            if node and node not in nodes:
                nodes.append(node)
        gap["参与节点"] = "、".join(nodes) if nodes else "-"
        if not uncollected:
            status = "已补齐"
        else:
            # 已撤销领取退回的行仍未被其他领取覆盖 → 需重派
            returned_pending = any(
                (ranges_to_rows(parse_ranges(claim.get("当前区间", "-")))
                 & uncollected) - active
                for claim in self._claims(execution)
                if claim.get("status") == "已撤销"
            )
            if returned_pending:
                status = "需重派"
            elif active or collected or self._items(execution):
                status = "补采中"
            else:
                status = "待补采"
        gap["status"] = status
        gap["pending"] = status != "已补齐"
        gap["abnormal"] = status == "需重派"

    def gap_view(self, execution: str) -> dict[str, Any] | None:
        """按执行汇总节点实际贡献、重叠依据、剩余缺口与领取/回执台账。"""
        gap = self._gap_row(execution)
        if gap is None:
            return None
        self._sync_view(gap)
        receipts = sorted(self._receipts(execution), key=lambda r: int(r.get("确认顺序", "0")))
        claims = sorted(self._claims(execution), key=lambda c: int(c.get("id", 0)))
        items = sorted(self._items(execution), key=lambda i: int(i.get("id", 0)))
        expected = ranges_to_rows(parse_ranges(gap["应采区间"]))
        collected = self._collected_rows(execution) & expected
        uncollected = expected - collected
        active = self._active_claim_rows(execution)
        dispatchable = uncollected - active

        # 节点实际贡献：各节点回执中真正进入归集的有效行
        contributions: dict[str, dict[str, Any]] = {}
        for receipt in receipts:
            slot = contributions.setdefault(receipt["节点编号"], {"rows": set(), "receipts": []})
            slot["rows"] |= ranges_to_rows(parse_ranges(receipt["有效区间"]))
            slot["receipts"].append(receipt["回执编号"])

        # 重叠依据：同一来源行被多张回执覆盖时，先确认者计入归集
        coverage: dict[int, list[dict[str, Any]]] = {}
        for receipt in receipts:
            for row in ranges_to_rows(parse_ranges(receipt["来源区间"])):
                coverage.setdefault(row, []).append(receipt)
        overlaps = []
        for row in sorted(coverage):
            covering = coverage[row]
            if len(covering) < 2:
                continue
            credited = next(
                (r for r in covering
                 if row in ranges_to_rows(parse_ranges(r["有效区间"]))),
                None,
            )
            overlaps.append({
                "row": row,
                "covered_by": [r["回执编号"] for r in covering],
                "credited_receipt": credited["回执编号"] if credited else "",
                "credited_node": credited["节点编号"] if credited else "",
                "basis": "先确认的回执计入归集，后续回执记为重复行",
            })

        return {
            "执行编号": execution,
            "应采区间": gap["应采区间"],
            "应采行数": len(expected),
            "collected": {
                "ranges": format_ranges(rows_to_ranges(collected)),
                "count": len(collected),
            },
            "uncollected": {
                "ranges": format_ranges(rows_to_ranges(uncollected)),
                "count": len(uncollected),
            },
            "claimed": {
                "ranges": format_ranges(rows_to_ranges(active & uncollected)),
                "count": len(active & uncollected),
            },
            "dispatchable": {
                "ranges": format_ranges(rows_to_ranges(dispatchable)),
                "count": len(dispatchable),
            },
            "contributions": {
                node: {
                    "ranges": format_ranges(rows_to_ranges(data["rows"])),
                    "count": len(data["rows"]),
                    "receipts": data["receipts"],
                }
                for node, data in contributions.items()
            },
            "overlaps": overlaps,
            "receipts": [
                {
                    "回执编号": r["回执编号"],
                    "节点编号": r["节点编号"],
                    "来源区间": r["来源区间"],
                    "有效区间": r["有效区间"],
                    "重复区间": r["重复区间"],
                    "是否迟到": r["是否迟到"],
                    "确认顺序": r["确认顺序"],
                    "关联领取": r["关联领取"],
                }
                for r in receipts
            ],
            "claims": [
                {
                    "领取编号": c["领取编号"],
                    "节点编号": c["节点编号"],
                    "原领取区间": c["原领取区间"],
                    "当前区间": c["当前区间"],
                    "status": c["status"],
                    "上一领取": c["上一领取"],
                    "来源清单": c["来源清单"],
                    "变更记录": [line for line in c["变更记录"].split("\n") if line.strip()],
                }
                for c in claims
            ],
            "supplementary_items": [
                {
                    "清单编号": i["清单编号"],
                    "区间": i["区间"],
                    "批次": i["批次"],
                    "status": i["status"],
                }
                for i in items
            ],
            "status": gap["status"],
        }
