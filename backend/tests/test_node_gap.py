"""节点采集缺口视图验收测试：交接重叠、迟到回执、补采清单与失联重派。

场景对应需求：设应采 16 行，A 提交 1 至 8，B 提交 7 至 12，视图查到
12 行及两份原回执；B 领取 13 至 16 后收到 A 对 13、14 的迟到回执；B 再
尝试交付原领取区间、替补节点请求派单；核对只有 15、16 仍需采集，原领用
进展和真正已归集的记录都能查到；B 不可用后只重派仍未采的数据。
"""
from __future__ import annotations

from app.services import service_for

A_NODE = "ROBO-0001"
B_NODE = "ROBO-0002"
STANDBY_NODE = "ROBO-0003"
EXECUTION = "RUN-ACE-1"


def _svc():
    return service_for("node_gap")


def _new_execution(svc, execution: str = EXECUTION, expected: str = "1-16"):
    entry, missing = svc.create_entry({"执行编号": execution, "应采区间": expected})
    assert not missing
    return entry


class TestHandoverOverlapScenario:
    """同一执行从 A 交给 B 后回执重叠、迟到确认与失联重派的完整链路。"""

    def test_full_scenario(self):
        svc = _svc()
        _new_execution(svc)

        # A 提交 1 至 8，B 提交 7 至 12（交接后两边回执区间重叠）
        r1 = svc.confirm_receipt(EXECUTION, A_NODE, "1-8")
        assert r1["ok"] and r1["new_rows"] == list(range(1, 9))
        r2 = svc.confirm_receipt(EXECUTION, B_NODE, "7-12")
        assert r2["ok"] and r2["new_rows"] == [9, 10, 11, 12]
        assert r2["duplicate_rows"] == [7, 8]

        # 视图查到 12 行及两份原回执，重叠依据指向先确认的 A
        view = svc.gap_view(EXECUTION)
        assert view["collected"]["count"] == 12
        assert view["collected"]["ranges"] == "1-12"
        assert len(view["receipts"]) == 2
        assert [o["row"] for o in view["overlaps"]] == [7, 8]
        for overlap in view["overlaps"]:
            assert overlap["covered_by"] == [
                r1["receipt"]["回执编号"], r2["receipt"]["回执编号"],
            ]
            assert overlap["credited_node"] == A_NODE
        # 节点实际贡献：A=1-8，B=9-12（重复行只累加一次）
        assert view["contributions"][A_NODE]["ranges"] == "1-8"
        assert view["contributions"][A_NODE]["count"] == 8
        assert view["contributions"][B_NODE]["ranges"] == "9-12"
        assert view["contributions"][B_NODE]["count"] == 4
        assert view["uncollected"]["ranges"] == "13-16"

        # 剩余区间写入补采清单，B 领取 13 至 16，任务队列兑现领取
        sup = svc.refresh_supplementary(EXECUTION)
        assert sup["ok"] and sup["ranges"] == "13-16"
        dispatched = svc.request_dispatch(EXECUTION, B_NODE)
        assert dispatched["ok"]
        claim_b = dispatched["claim"]["领取编号"]
        assert dispatched["claim"]["当前区间"] == "13-16"
        view = svc.gap_view(EXECUTION)
        assert view["claimed"]["ranges"] == "13-16"
        assert view["dispatchable"]["count"] == 0  # 已领取不混入可重派
        queue_svc = service_for("task_queue")
        task = [t for t in queue_svc.list_entries(size=1000)[0]
                if t["队列编号"] == f"TASK-{claim_b}"][0]
        assert task["占用节点"] == B_NODE and task["status"] == "已派发"

        # A 对 13、14 的迟到回执：有效行只累加一次，B 的领取收窄写回视图
        r3 = svc.confirm_receipt(EXECUTION, A_NODE, "13-14")
        assert r3["ok"] and r3["is_late"]
        assert r3["new_rows"] == [13, 14]
        assert r3["narrowed_claims"] == [claim_b]
        view = svc.gap_view(EXECUTION)
        claim_view = [c for c in view["claims"] if c["领取编号"] == claim_b][0]
        assert claim_view["当前区间"] == "15-16"
        assert claim_view["原领取区间"] == "13-16"  # 原领取区间仍可查
        assert any("收窄" in line and r3["receipt"]["回执编号"] in line
                   for line in claim_view["变更记录"])
        assert view["collected"]["ranges"] == "1-14"
        assert view["uncollected"]["ranges"] == "15-16"

        # 重复提交只累加一次有效行
        dup = svc.confirm_receipt(EXECUTION, A_NODE, "1-8")
        assert dup["ok"] and dup["new_rows"] == []
        assert dup["duplicate_rows"] == list(range(1, 9))
        assert svc.gap_view(EXECUTION)["collected"]["count"] == 14

        # B 尝试交付原领取区间：13、14 已归集，不能再次进入归集，整批退回
        rejected = svc.deliver_claim(claim_b, "13-16")
        assert not rejected["ok"]
        assert rejected["already_collected"] == "13-14"
        assert rejected["outside_claim"] == "13-14"
        view = svc.gap_view(EXECUTION)
        assert view["collected"]["ranges"] == "1-14"
        assert view["uncollected"]["ranges"] == "15-16"
        claim_view = [c for c in view["claims"] if c["领取编号"] == claim_b][0]
        assert any("交付被拒" in line for line in claim_view["变更记录"])

        # B 仍持有 15-16 时替补节点请求派单：已领取范围不混入，无单可派
        denied = svc.request_dispatch(EXECUTION, STANDBY_NODE)
        assert not denied["ok"]

        # B 失联：只把仍未采的 15-16 退回可重派，节点被摘除、任务被丢弃
        lost = svc.node_unavailable(EXECUTION, B_NODE)
        assert lost["ok"] and lost["returned_ranges"] == "15-16"
        node_svc = service_for("robot_node")
        node_b = [n for n in node_svc.list_entries(size=1000)[0]
                  if n["节点编号"] == B_NODE][0]
        assert node_b["status"] == "离线"
        task = [t for t in queue_svc.list_entries(size=1000)[0]
                if t["队列编号"] == f"TASK-{claim_b}"][0]
        assert task["status"] == "已丢弃"
        view = svc.gap_view(EXECUTION)
        assert view["dispatchable"]["ranges"] == "15-16"
        assert view["status"] == "需重派"

        # 值班员按原清单重派：替补只领到仍未采的 15-16，且能追到原领取
        redispatched = svc.request_dispatch(EXECUTION, STANDBY_NODE)
        assert redispatched["ok"]
        claim_c = redispatched["claim"]["领取编号"]
        assert redispatched["claim"]["当前区间"] == "15-16"
        assert redispatched["claim"]["上一领取"] == claim_b

        # 核对：只有 15、16 仍需采集；原领用进展与真正已归集的记录都能查到
        view = svc.gap_view(EXECUTION)
        assert view["uncollected"]["ranges"] == "15-16"
        assert view["collected"]["ranges"] == "1-14"
        assert view["contributions"][A_NODE]["ranges"] == "1-8,13-14"
        assert view["contributions"][B_NODE]["ranges"] == "9-12"
        receipt_nodes = {r["回执编号"]: r["节点编号"] for r in view["receipts"]}
        assert receipt_nodes[r3["receipt"]["回执编号"]] == A_NODE  # 迟到回执原上传节点
        history = [c for c in view["claims"] if c["领取编号"] == claim_b][0]["变更记录"]
        assert [line.split("] ", 1)[1].split("：")[0] for line in history] == [
            "领取13-16（清单SUP-0002）", "收窄", "交付被拒", "撤销",
        ]

        # 任务队列兑现替补领到的范围：交付 15-16 后缺口清零
        done = svc.deliver_claim(claim_c, "15-16")
        assert done["ok"]
        view = svc.gap_view(EXECUTION)
        assert view["uncollected"]["count"] == 0
        assert view["collected"]["count"] == 16
        assert view["status"] == "已补齐"
        task = [t for t in queue_svc.list_entries(size=1000)[0]
                if t["队列编号"] == f"TASK-{claim_c}"][0]
        assert task["status"] == "已消费"


class TestGapViewRules:
    """缺口视图的独立规则：重复提交、迟到回执、通用动作。"""

    def test_late_receipt_cannot_recollect_rows(self):
        svc = _svc()
        _new_execution(svc)
        svc.confirm_receipt(EXECUTION, A_NODE, "1-8")
        svc.refresh_supplementary(EXECUTION)
        svc.request_dispatch(EXECUTION, B_NODE)  # B 领取 9-16
        # A 迟到确认 5-10：5-8 已归集不重复计入，9-10 进入归集并收窄 B
        late = svc.confirm_receipt(EXECUTION, A_NODE, "5-10")
        assert late["is_late"]
        assert late["new_rows"] == [9, 10]
        assert late["duplicate_rows"] == [5, 6, 7, 8]
        view = svc.gap_view(EXECUTION)
        assert view["collected"]["ranges"] == "1-10"
        claim_b = [c for c in view["claims"] if c["节点编号"] == B_NODE][0]
        assert claim_b["当前区间"] == "11-16"

    def test_duplicate_submission_accumulates_once(self):
        svc = _svc()
        _new_execution(svc)
        first = svc.confirm_receipt(EXECUTION, A_NODE, "1-8")
        again = svc.confirm_receipt(EXECUTION, A_NODE, "1-8")
        assert first["new_rows"] == list(range(1, 9))
        assert again["new_rows"] == []
        assert again["duplicate_rows"] == list(range(1, 9))
        view = svc.gap_view(EXECUTION)
        assert view["collected"]["count"] == 8
        assert view["contributions"][A_NODE]["count"] == 8

    def test_generic_actions_on_view_row(self):
        svc = _svc()
        entry = _new_execution(svc)
        svc.confirm_receipt(EXECUTION, A_NODE, "1-8")
        # 工作台动作「生成补采清单」
        row, message = svc.run_action(entry["id"], "生成补采清单")
        assert row is not None and "9-16" in message
        assert row["status"] == "补采中"
        # 工作台动作「核对补齐」：缺口未清时报告剩余区间
        row, message = svc.run_action(entry["id"], "核对补齐")
        assert "9-16" in message
        svc.confirm_receipt(EXECUTION, B_NODE, "9-16")
        row, message = svc.run_action(entry["id"], "核对补齐")
        assert row["status"] == "已补齐"

    def test_dispatch_requires_supplementary_items(self):
        svc = _svc()
        _new_execution(svc)
        result = svc.request_dispatch(EXECUTION, A_NODE)
        assert not result["ok"]
        assert "补采清单" in result["reason"]

    def test_seed_execution_shows_twelve_rows_and_two_receipts(self):
        """种子执行 RUN-5001：A 交 1-8、B 交 7-12，视图即见 12 行与两份原回执。"""
        svc = _svc()
        view = svc.gap_view("RUN-5001")
        assert view["collected"]["count"] == 12
        assert view["collected"]["ranges"] == "1-12"
        assert len(view["receipts"]) == 2
        assert [o["row"] for o in view["overlaps"]] == [7, 8]
        assert view["uncollected"]["ranges"] == "13-16"
