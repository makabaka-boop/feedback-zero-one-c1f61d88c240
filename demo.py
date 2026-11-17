#!/usr/bin/env python3
"""端到端演示: 初始化 -> 建单 -> 合并 -> 裁决 -> 校验 -> 交付 -> 幂等重试 -> 重启。

直接运行:  python3 demo.py
脚本会在临时目录建库、启动两次 HTTP 服务(第二次模拟重启), 全程打印关键结果。
"""

import json
import os
import tempfile
import threading
import urllib.error
import urllib.request

from merger.server import make_server
from merger.service import MergeService


def call(port, method, path, body=None, expect=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            status, payload = resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        status, payload = e.code, json.loads(e.read().decode("utf-8"))
    tag = "OK " if expect is None or status == expect else "!!!"
    print(f"  [{tag}] {method} {path} -> {status}")
    if expect is not None:
        assert status == expect, f"{method} {path}: 期望 {expect}, 实际 {status}: {payload}"
    return status, payload


def show(title, obj=None):
    print(f"\n=== {title} ===")
    if obj is not None:
        print(json.dumps(obj, ensure_ascii=False, indent=2))


def serve(db_path, init_data=None):
    svc = MergeService(db_path)
    if init_data is not None and not svc.is_initialized():
        svc.initialize(init_data)
    server = make_server(svc, "127.0.0.1", 0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return svc, server, port


# ---------------------------------------------------------------- 演示数据
TARGET = {
    "datasets": [
        {"id": "D1", "code": "DS-001", "version": "1.0", "title": "基线调查", "lead_record_id": "R2"},
        {"id": "D2", "code": "DS-002", "version": "2.0", "title": "随访数据", "lead_record_id": None},
        {"id": "D3", "code": "DS-003", "version": "1.0", "title": "旧数据(目标改)", "lead_record_id": None},
    ],
    "records": [
        {"id": "R1", "dataset_code": "DS-001", "dataset_version": "1.0", "subject": "S-01", "value": "9", "next_record_id": "R2"},
        {"id": "R2", "dataset_code": "DS-001", "dataset_version": "1.0", "subject": "S-02", "value": "20", "next_record_id": "R1"},
        {"id": "R3", "dataset_code": "DS-002", "dataset_version": "2.0", "subject": "S-09", "value": None, "next_record_id": None},
        {"id": "R4", "dataset_code": "DS-003", "dataset_version": "1.0", "subject": "S-10", "value": "x", "next_record_id": None},
        {"id": "R6", "dataset_code": "DS-002", "dataset_version": "2.0", "subject": "S-11", "value": "T", "next_record_id": None},
        {"id": "R7", "dataset_code": "DS-001", "dataset_version": "1.0", "subject": "S-04", "value": "目标侧", "next_record_id": None},
    ],
}
BASELINE = {
    "datasets": [
        {"id": "D1", "code": "DS-001", "version": "1.0", "title": "基线调查", "lead_record_id": None},
        {"id": "D2", "code": "DS-002", "version": "2.0", "title": "随访", "lead_record_id": None},
        {"id": "D3", "code": "DS-003", "version": "1.0", "title": "旧数据", "lead_record_id": None},
    ],
    "records": [
        {"id": "R1", "dataset_code": "DS-001", "dataset_version": "1.0", "subject": "S-01", "value": "5", "next_record_id": None},
        {"id": "R2", "dataset_code": "DS-001", "dataset_version": "1.0", "subject": "S-02", "value": "20", "next_record_id": None},
        {"id": "R3", "dataset_code": "DS-002", "dataset_version": "2.0", "subject": "S-09", "value": None, "next_record_id": None},
        {"id": "R4", "dataset_code": "DS-003", "dataset_version": "1.0", "subject": "S-10", "value": "x", "next_record_id": None},
    ],
}
SOURCE = {
    "datasets": [
        {"id": "D1", "code": "DS-001", "version": "1.0", "title": "基线调查(修订)", "lead_record_id": "R2"},
        {"id": "D2", "code": "DS-002", "version": "2.0", "title": "随访", "lead_record_id": None},
    ],
    "records": [
        {"id": "R1", "dataset_code": "DS-001", "dataset_version": "1.0", "subject": "S-01", "value": "7", "next_record_id": "R2"},
        {"id": "R2", "dataset_code": "DS-001", "dataset_version": "1.0", "subject": "S-02", "value": "20", "next_record_id": None},
        {"id": "R3", "dataset_code": "DS-002", "dataset_version": "2.0", "subject": "S-09", "value": None, "next_record_id": None},
        {"id": "R5", "dataset_code": "DS-001", "dataset_version": "1.0", "subject": "S-03", "value": "A", "next_record_id": None},
        {"id": "R7", "dataset_code": "DS-001", "dataset_version": "1.0", "subject": "S-04", "value": "来源侧", "next_record_id": None},
    ],
}


def main():
    tmp = tempfile.TemporaryDirectory()
    db = os.path.join(tmp.name, "demo.db")
    svc, server, port = serve(db, TARGET)
    print(f"服务已启动(临时端口 {port}), 数据库: {db}")

    show("1. 初始化后的目标数据(版本 1, 含循环引用 R1<->R2 与 D1->R2->D1)",
         call(port, "GET", "/target", expect=200)[1])

    _, order = call(port, "POST", "/orders", {"baseline": BASELINE, "source": SOURCE}, expect=201)
    oid = order["order_id"]
    show(f"2. 建单 {oid}: 三方合并产生 3 个冲突, 完整性待定", {
        "revision": order["revision"],
        "conflicts": [{k: c[k] for k in ("conflict_id", "kind", "table", "row_id", "field")} for c in order["conflicts"]],
        "integrity": order["integrity"],
        "R1候选(未决标记区别于空值与删除)": next(r for r in order["candidates"]["records"] if r["id"] == "R1"),
    })

    # 并存第二个审定单(稍后演示"目标已变化须另建单")
    _, order2 = call(port, "POST", "/orders", {"baseline": BASELINE, "source": SOURCE}, expect=201)
    oid2 = order2["order_id"]
    print(f"\n=== 并存第二个审定单 {oid2} ===")

    call(port, "POST", f"/orders/{oid}/actions",
         {"revision": 99, "actions": [{"type": "resolve_field", "conflict_id": "c2", "value": "8"}]}, expect=409)
    print("  ^ 旧修订号(99)修改被拒绝, 状态不变")

    _, v = call(port, "POST", f"/orders/{oid}/actions", {"revision": 1, "actions": [
        {"type": "resolve_row", "conflict_id": "c1", "keep": False},
        {"type": "resolve_field", "conflict_id": "c2", "value": "8"},
        {"type": "resolve_row", "conflict_id": "c3", "keep": True, "side": "target"},
    ]}, expect=200)
    show("3. 裁决全部冲突(删 D3 / R1.value 定为 8 / R7 保留目标侧), 修订号推进到 2",
         {"revision": v["revision"], "integrity": v["integrity"]})

    _, v = call(port, "POST", f"/orders/{oid}/actions", {"revision": 2, "actions": [
        {"type": "upsert_row", "table": "records", "values": {
            "id": "R5", "dataset_code": "DS-999", "dataset_version": "9.9",
            "subject": "S-03", "value": "A", "next_record_id": None}},
    ]}, expect=200)
    show("4. 暂存一个悬空引用(允许), 校验报告列出相关行", v["integrity"]["dangling_references"])

    _, v = call(port, "POST", f"/orders/{oid}/actions", {"revision": 3, "actions": [
        {"type": "upsert_row", "table": "records", "values": {
            "id": "R6", "dataset_code": "DS-002", "dataset_version": "2.0",
            "subject": "S-09", "value": "dup", "next_record_id": None}},
    ]}, expect=200)
    show("5. 再暂存一个重复组合键, 校验报告列出重复键组及相关行", v["integrity"]["duplicate_keys"])

    _, v = call(port, "POST", f"/orders/{oid}/actions", {"revision": 4, "actions": [
        {"type": "upsert_row", "table": "records", "values": {
            "id": "R5", "dataset_code": "DS-001", "dataset_version": "1.0",
            "subject": "S-03", "value": "A", "next_record_id": None}},
        {"type": "upsert_row", "table": "records", "values": {
            "id": "R6", "dataset_code": "DS-002", "dataset_version": "2.0",
            "subject": "S-11", "value": "T", "next_record_id": None}},
    ]}, expect=200)
    show("6. 管理员手工修正后重新校验通过(系统不自动删行或清空引用)",
         {"revision": v["revision"], "integrity": v["integrity"]})

    call(port, "POST", "/deliveries", {"request_id": "req-demo-1", "order_id": oid, "revision": 3}, expect=409)
    print("  ^ 旧修订交付被拒绝(不得检查一个修订却交付另一个)")

    _, d = call(port, "POST", "/deliveries", {"request_id": "req-demo-1", "order_id": oid, "revision": 5}, expect=201)
    show("7. 交付成功: 原子完成全部目标表替换 + 目标升版 + 回执保存", d["receipt"])

    _, replay = call(port, "POST", "/deliveries", {"request_id": "req-demo-1", "order_id": oid, "revision": 5}, expect=200)
    show("8. 相同交付请求重试 -> 返回原回执", {"idempotent_replay": replay["idempotent_replay"]})
    call(port, "POST", "/deliveries", {"request_id": "req-demo-1", "order_id": oid, "revision": 4}, expect=409)
    print("  ^ 同一 request_id 携带不同参数 -> 冲突拒绝")

    call(port, "POST", f"/orders/{oid}/actions",
         {"revision": 5, "actions": [{"type": "delete_row", "table": "records", "id": "R6"}]}, expect=409)
    print("  ^ 已交付单不可修改")

    _, v2 = call(port, "POST", f"/orders/{oid2}/actions", {"revision": 1, "actions": [
        {"type": "resolve_row", "conflict_id": "c1", "keep": False},
        {"type": "resolve_field", "conflict_id": "c2", "value": "8"},
        {"type": "resolve_row", "conflict_id": "c3", "keep": True, "side": "target"},
    ]}, expect=200)
    call(port, "POST", "/deliveries", {"request_id": "req-demo-2", "order_id": oid2, "revision": v2["revision"]}, expect=409)
    print(f"  ^ {oid2} 建单后目标已升版, 交付被拒绝(TARGET_CHANGED), 须另建单")

    show("9. 交付后的目标数据(版本 2, 完整数据)", call(port, "GET", "/target", expect=200)[1])

    # ---- 模拟重启 ----
    print("\n=== 模拟重启: 关闭服务, 用同一数据库文件重新启动 ===")
    server.shutdown()
    server.server_close()
    svc.close()
    svc, server, port = serve(db)
    _, replay = call(port, "POST", "/deliveries", {"request_id": "req-demo-1", "order_id": oid, "revision": 5}, expect=200)
    _, order_view = call(port, "GET", f"/orders/{oid}", expect=200)
    show("10. 重启后: 已确认修订、裁决与交付结果均保留", {
        "重试交付仍返回原回执": replay["receipt"]["delivered_at"] == d["receipt"]["delivered_at"],
        "审定单状态": order_view["status"],
        "修订号": order_view["revision"],
        "目标版本": call(port, "GET", "/target", expect=200)[1]["version"],
    })
    _, hist = call(port, "GET", f"/orders/{oid}/history", expect=200)
    print(f"  裁决历史共 {len(hist['history'])} 条, 全部保留")

    server.shutdown()
    server.server_close()
    svc.close()
    tmp.cleanup()
    print("\n演示完成。")


if __name__ == "__main__":
    main()
