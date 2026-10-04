import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def import_text(text):
    return client.post("/api/import", json={"text": text})


GOOD = "\n".join(
    [
        "1|甲方在七日内交付设备",
        "2|乙方按 [[1]] 验收后付款",
        "3|争议解决见 [[4]]，违约参照 [[2]]",
        "4|争议提交仲裁",
    ]
)


def test_import_ok_binds_stable_ids():
    r = import_text(GOOD)
    assert r.status_code == 200, r.text
    clauses = r.json()["clauses"]
    assert [c["id"] for c in clauses] == ["c1", "c2", "c3", "c4"]
    assert clauses[1]["body"] == "乙方按 [[c1]] 验收后付款"
    assert clauses[2]["body"] == "争议解决见 [[c4]]，违约参照 [[c2]]"


@pytest.mark.parametrize(
    "text",
    [
        "1|只有一行",  # 少于 2 行
        "1-无分隔符\n2|正常条款",
        "a|编号非数字\n2|正文",
        "0|编号须从 1 开始\n2|正文",
        "01|编号含前导零\n2|正文",
        "1|正文为空编号重复\n1|另一条",
        "1|第一条\n3|缺编号2",
        "1|引用越界 [[9]]\n2|正常",
        "1|标记带空格 [[ 2 ]]\n2|正常",
        "1|未闭合 [[2\n2|正常",
        "1|多余右括号 [[2]]]\n2|正常",
        "1|前导零 [[01]]\n2|正常",
        "1|引用零号 [[0]]\n2|正常",
        "1|空内容标记 [[]]\n2|正常",
        "1|\n2|空正文",
        "1|中间空行\n\n3|第三行",
    ],
)
def test_import_rejects_bad_inputs(text):
    r = import_text(text)
    assert r.status_code == 422
    assert r.json()["detail"]["errors"]


def test_line_count_limit():
    text = "\n".join(f"{i}|条款{i}" for i in range(1, 42))  # 41 行
    r = import_text(text)
    assert r.status_code == 422


def test_preview_and_reorder_keeps_binding():
    d = import_text(GOOD).json()
    ids = [c["id"] for c in d["clauses"]]
    bodies = [{"id": c["id"], "body": c["body"]} for c in d["clauses"]]

    # 把原第 4 条移到第 1 位：原第 3 条引用 [[4]] 应显示为 [[1]]，而不是转指新占 4 号位的条款
    order = ["c4", "c1", "c2", "c3"]
    r = client.post("/api/preview", json={"order_ids": order, "clauses": bodies})
    items = {it["id"]: it for it in r.json()["items"]}
    assert items["c3"]["text"].startswith("争议解决见 [[1]]，违约参照 [[3]]")
    assert items["c4"]["display"] == 1


def test_edit_save_binds_by_current_display_number():
    d = import_text(GOOD).json()
    bodies = [{"id": c["id"], "body": c["body"]} for c in d["clauses"]]
    # 当前顺序 c4,c1,c2,c3 下，第 1 位是原 4 条
    order = ["c4", "c1", "c2", "c3"]
    r = client.post(
        "/api/save",
        json={
            "order_ids": order,
            "clauses": bodies,
            "edit_id": "c2",
            "text": "改为引用当前第 1 条 [[1]]",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["body"] == "改为引用当前第 1 条 [[c4]]"

    # 再重排：引用仍锁定 c4（原条款），不会转指占旧号的条款
    saved = [b for b in bodies if b["id"] != "c2"] + [{"id": "c2", "body": "改为引用当前第 1 条 [[c4]]"}]
    r = client.post(
        "/api/preview",
        json={"order_ids": ["c1", "c2", "c3", "c4"], "clauses": saved},
    )
    items = {it["id"]: it for it in r.json()["items"]}
    assert items["c2"]["text"] == "改为引用当前第 1 条 [[4]]"


def test_save_rejects_dangling_ref():
    d = import_text(GOOD).json()
    bodies = [{"id": c["id"], "body": c["body"]} for c in d["clauses"]]
    r = client.post(
        "/api/save",
        json={"order_ids": ids4(), "clauses": bodies, "edit_id": "c2", "text": "引用不存在 [[5]]"},
    )
    assert r.status_code == 422


def ids4():
    return ["c1", "c2", "c3", "c4"]


def test_delete_referenced_clause_marks_broken_and_blocks_export():
    d = import_text(GOOD).json()
    # 删除被引用的 c4（c3 引用它）
    order = ["c1", "c2", "c3"]
    bodies = [{"id": c["id"], "body": c["body"]} for c in d["clauses"] if c["id"] != "c4"]

    r = client.post("/api/preview", json={"order_ids": order, "clauses": bodies})
    preview = r.json()
    assert preview["has_broken"] is True
    c3 = next(it for it in preview["items"] if it["id"] == "c3")
    assert "[[原4?]]" in c3["text"]
    assert c3["broken"] == [{"ref_id": "c4", "original": 4}]

    # 修复前导出 409
    r = client.post("/api/export", json={"order_ids": order, "clauses": bodies})
    assert r.status_code == 409

    # 修复：编辑 c3，改引当前第 1 条（c1）
    r = client.post(
        "/api/save",
        json={"order_ids": order, "clauses": bodies, "edit_id": "c3", "text": "争议解决改为 [[1]]"},
    )
    assert r.status_code == 200
    fixed = [{"id": b["id"], "body": b["body"]} for b in bodies if b["id"] != "c3"]
    fixed.append({"id": "c3", "body": r.json()["body"]})

    r = client.post("/api/preview", json={"order_ids": order, "clauses": fixed})
    assert r.json()["has_broken"] is False

    r = client.post("/api/export", json={"order_ids": order, "clauses": fixed})
    assert r.status_code == 200
    lines = r.json()["text"].split("\n")
    assert lines == [
        "1|甲方在七日内交付设备",
        "2|乙方按 [[1]] 验收后付款",
        "3|争议解决改为 [[1]]",
    ]


def test_self_reference_is_allowed():
    r = import_text("1|本条说明自身 [[1]]\n2|普通条款")
    assert r.status_code == 200


def test_single_line_only_enforced_on_edit():
    d = import_text(GOOD).json()
    bodies = [{"id": c["id"], "body": c["body"]} for c in d["clauses"]]
    r = client.post(
        "/api/save",
        json={"order_ids": ids4(), "clauses": bodies, "edit_id": "c2", "text": "第一行\n第二行"},
    )
    assert r.status_code == 422
    assert "单行" in r.json()["detail"]["errors"][0]


def test_pipe_only_split_on_first_one():
    r = import_text("1|正文里|可以有竖线\n2|引用 [[1]]")
    assert r.status_code == 200
    assert r.json()["clauses"][0]["body"] == "正文里|可以有竖线"


# --------------------------------------------------------------------------- #
# 合并
# --------------------------------------------------------------------------- #
def _bodies(doc):
    return [{"id": c["id"], "body": c["body"]} for c in doc["clauses"]]


def test_merge_joins_with_fullwidth_semicolon_and_repoints_refs():
    bodies = _bodies(import_text(GOOD).json())
    # 合并原 1（保留）与原 2（并入）
    r = client.post(
        "/api/merge",
        json={"order_ids": ids4(), "clauses": bodies, "keep_id": "c1", "drop_id": "c2"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["order_ids"] == ["c1", "c3", "c4"]
    merged = {c["id"]: c["body"] for c in data["clauses"]}
    # 全角分号连接；c2 正文里指向 c1 的引用保持/并入 c1
    assert merged["c1"] == "甲方在七日内交付设备；乙方按 [[c1]] 验收后付款"
    # 原来指向 c2 的引用（c3）改指合并结果 c1；其余引用不动
    assert merged["c3"] == "争议解决见 [[c4]]，违约参照 [[c1]]"
    assert "c2" not in merged

    # 预览 / 导出立即对应同一份草稿
    r = client.post("/api/preview", json={"order_ids": data["order_ids"], "clauses": data["clauses"]})
    items = {it["id"]: it for it in r.json()["items"]}
    assert r.json()["has_broken"] is False
    assert items["c1"]["display"] == 1
    assert items["c1"]["text"] == "甲方在七日内交付设备；乙方按 [[1]] 验收后付款"
    assert items["c3"]["text"] == "争议解决见 [[3]]，违约参照 [[1]]"

    r = client.post("/api/export", json={"order_ids": data["order_ids"], "clauses": data["clauses"]})
    assert r.status_code == 200
    assert r.json()["text"].split("\n") == [
        "1|甲方在七日内交付设备；乙方按 [[1]] 验收后付款",
        "2|争议解决见 [[3]]，违约参照 [[1]]",
        "3|争议提交仲裁",
    ]


def test_merge_in_reordered_doc_uses_display_order():
    # 当前显示顺序 c4(1), c1(2), c2(3), c3(4)：合并显示较前的 c1 与较后的 c3
    order = ["c4", "c1", "c2", "c3"]
    bodies = _bodies(import_text(GOOD).json())
    r = client.post(
        "/api/merge",
        json={"order_ids": order, "clauses": bodies, "keep_id": "c1", "drop_id": "c3"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["order_ids"] == ["c4", "c1", "c2"]  # 其余条款相对顺序不变
    merged = {c["id"]: c["body"] for c in data["clauses"]}
    # c3 正文中 [[c4]] 与 [[c2]] 都不是被移除身份，保持不变（c2 并未被合并）
    assert merged["c1"] == "甲方在七日内交付设备；争议解决见 [[c4]]，违约参照 [[c2]]"
    assert merged["c2"] == "乙方按 [[c1]] 验收后付款"  # c2 对 c1 的引用保持

    r = client.post("/api/preview", json={"order_ids": data["order_ids"], "clauses": data["clauses"]})
    items = {it["id"]: it for it in r.json()["items"]}
    assert items["c1"]["text"] == "甲方在七日内交付设备；争议解决见 [[1]]，违约参照 [[3]]"
    assert items["c2"]["text"] == "乙方按 [[2]] 验收后付款"
    assert items["c2"]["display"] == 3


def test_merge_keeps_already_broken_refs_broken():
    doc = import_text(GOOD).json()
    # 先删 c4：c3 中 [[c4]] 失效
    order = ["c1", "c2", "c3"]
    bodies = [b for b in _bodies(doc) if b["id"] != "c4"]

    # 再合并 c2（保留）+ c3（并入）
    r = client.post(
        "/api/merge",
        json={"order_ids": order, "clauses": bodies, "keep_id": "c2", "drop_id": "c3"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    merged = {c["id"]: c["body"] for c in data["clauses"]}
    # [[c2]] 改指保留方 c2（自引用），[[c4]] 保持失效，不会因合并恢复
    assert merged["c2"] == "乙方按 [[c1]] 验收后付款；争议解决见 [[c4]]，违约参照 [[c2]]"

    r = client.post("/api/preview", json={"order_ids": data["order_ids"], "clauses": data["clauses"]})
    preview = r.json()
    assert preview["has_broken"] is True
    c2 = next(it for it in preview["items"] if it["id"] == "c2")
    assert "[[原4?]]" in c2["text"]

    r = client.post("/api/export", json={"order_ids": data["order_ids"], "clauses": data["clauses"]})
    assert r.status_code == 409


def test_merge_rejects_reversed_same_and_missing():
    bodies = _bodies(import_text(GOOD).json())
    # 方向反了：c2 在 c1 之后，却要求保留 c2
    r = client.post(
        "/api/merge",
        json={"order_ids": ids4(), "clauses": bodies, "keep_id": "c2", "drop_id": "c1"},
    )
    assert r.status_code == 422

    # 同一条款不能与自己合并
    r = client.post(
        "/api/merge",
        json={"order_ids": ids4(), "clauses": bodies, "keep_id": "c1", "drop_id": "c1"},
    )
    assert r.status_code == 422

    # 条款不存在
    r = client.post(
        "/api/merge",
        json={"order_ids": ids4(), "clauses": bodies, "keep_id": "c1", "drop_id": "c9"},
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# 拆分
# --------------------------------------------------------------------------- #
def _split(order, bodies, split_id, offset, new_id, refs_to="front"):
    return client.post(
        "/api/split",
        json={
            "order_ids": order,
            "clauses": bodies,
            "split_id": split_id,
            "offset": offset,
            "new_id": new_id,
            "refs_to": refs_to,
        },
    )


def test_split_front_keeps_identity_and_refs_stay():
    bodies = _bodies(import_text(GOOD).json())
    body = next(b["body"] for b in bodies if b["id"] == "c2")  # 乙方按 [[c1]] 验收后付款
    offset = body.index("验") + 1
    r = _split(ids4(), bodies, "c2", offset, "c5", refs_to="front")
    assert r.status_code == 200, r.text
    data = r.json()
    # 新条款紧跟前段，其余顺序不变
    assert data["order_ids"] == ["c1", "c2", "c5", "c3", "c4"]
    by = {c["id"]: c["body"] for c in data["clauses"]}
    # 前段沿用原身份，后段获得新身份；两段字符原样保留
    assert by["c2"] == "乙方按 [[c1]] 验"
    assert by["c5"] == "收后付款"
    assert by["c2"] + by["c5"] == body
    # 引用落前段：c3 中指向 c2 的引用不变；两段内的原有引用保持绑定
    assert by["c3"] == "争议解决见 [[c4]]，违约参照 [[c2]]"

    # 预览 / 导出立即对应同一份草稿
    r = client.post("/api/preview", json={"order_ids": data["order_ids"], "clauses": data["clauses"]})
    items = {it["id"]: it for it in r.json()["items"]}
    assert r.json()["has_broken"] is False
    assert items["c2"]["display"] == 2
    assert items["c5"]["display"] == 3
    assert items["c2"]["text"] == "乙方按 [[1]] 验"
    assert items["c3"]["text"] == "争议解决见 [[5]]，违约参照 [[2]]"

    r = client.post("/api/export", json={"order_ids": data["order_ids"], "clauses": data["clauses"]})
    assert r.status_code == 200
    assert r.json()["text"].split("\n") == [
        "1|甲方在七日内交付设备",
        "2|乙方按 [[1]] 验",
        "3|收后付款",
        "4|争议解决见 [[5]]，违约参照 [[2]]",
        "5|争议提交仲裁",
    ]


def test_split_refs_to_back_repoints_all_including_self_refs():
    bodies = _bodies(import_text(GOOD).json())
    # 先给 c2 加一个自引用（当前第 2 位就是 c2）
    r = client.post(
        "/api/save",
        json={
            "order_ids": ids4(),
            "clauses": bodies,
            "edit_id": "c2",
            "text": "乙方按 [[1]] 验收后付款，并见 [[2]] 末款",
        },
    )
    assert r.status_code == 200
    bodies = [b if b["id"] != "c2" else {"id": "c2", "body": r.json()["body"]} for b in bodies]
    body = next(b["body"] for b in bodies if b["id"] == "c2")
    offset = body.index("，")  # 自引用 [[c2]] 落在后段
    r = _split(ids4(), bodies, "c2", offset, "c5", refs_to="back")
    assert r.status_code == 200, r.text
    by = {c["id"]: c["body"] for c in r.json()["clauses"]}
    # c3 原来指向 c2 的引用统一改指后段 c5
    assert by["c3"] == "争议解决见 [[c4]]，违约参照 [[c5]]"
    # 后段中的自引用同样改指 c5；前段其余引用（[[c1]]）保持绑定
    assert by["c2"] == "乙方按 [[c1]] 验收后付款"
    assert by["c5"] == "，并见 [[c5]] 末款"


def test_split_rejects_cutting_marker_but_allows_hugging_it():
    bodies = _bodies(import_text(GOOD).json())
    body = next(b["body"] for b in bodies if b["id"] == "c2")  # 乙方按 [[c1]] 验收后付款
    start = body.index("[[c1]]")
    end = start + len("[[c1]]")
    # 分界落在标记内部：拒绝
    for off in (start + 1, start + 3, end - 1):
        r = _split(ids4(), bodies, "c2", off, "c5")
        assert r.status_code == 422, off
    # 紧贴标记两侧：允许，标记完整归入某一段
    r = _split(ids4(), bodies, "c2", start, "c5")
    assert r.status_code == 200, r.text
    by = {c["id"]: c["body"] for c in r.json()["clauses"]}
    assert by["c2"] == "乙方按 "
    assert by["c5"] == "[[c1]] 验收后付款"
    r = _split(ids4(), bodies, "c2", end, "c5")
    assert r.status_code == 200, r.text
    by = {c["id"]: c["body"] for c in r.json()["clauses"]}
    assert by["c2"] == "乙方按 [[c1]]"
    assert by["c5"] == " 验收后付款"


def test_split_rejects_whitespace_only_segment():
    bodies = _bodies(import_text(GOOD).json())
    body = next(b["body"] for b in bodies if b["id"] == "c1")  # 甲方在七日内交付设备
    # 分界在首尾：有一段为空
    for off in (0, len(body)):
        r = _split(ids4(), bodies, "c1", off, "c5")
        assert r.status_code == 422, off
    # 分界越界
    for off in (-1, len(body) + 1):
        r = _split(ids4(), bodies, "c1", off, "c5")
        assert r.status_code == 422, off
    # 正文以空白开头时，落在空白段内的分界会让前段只含空白
    doc = import_text("1| 甲乙\n2|丙 [[1]]").json()
    bodies2 = _bodies(doc)
    r = _split(["c1", "c2"], bodies2, "c1", 1, "c3")
    assert r.status_code == 422
    # 空白之后的分界合法：前段字符（含空白）原样保留
    r = _split(["c1", "c2"], bodies2, "c1", 2, "c3")
    assert r.status_code == 200, r.text
    by = {c["id"]: c["body"] for c in r.json()["clauses"]}
    assert by["c1"] == " 甲"
    assert by["c3"] == "乙"


def test_split_count_limit_40():
    text = "\n".join(f"{i}|条款{i}" for i in range(1, 41))  # 40 条
    bodies = _bodies(import_text(text).json())
    order = [f"c{i}" for i in range(1, 41)]
    r = _split(order, bodies, "c1", 2, "c41")
    assert r.status_code == 422
    # 39 条时拆分为第 40 条：允许
    r = _split(order[:-1], bodies[:-1], "c1", 2, "c40")
    assert r.status_code == 200, r.text
    assert r.json()["order_ids"] == ["c1", "c40"] + [f"c{i}" for i in range(2, 40)]


def test_split_new_id_must_be_fresh_and_not_revive_broken_refs():
    bodies = _bodies(import_text(GOOD).json())
    # 与现有条款身份冲突
    r = _split(ids4(), bodies, "c2", 2, "c3")
    assert r.status_code == 422
    # 与被拆条款自身冲突
    r = _split(ids4(), bodies, "c2", 2, "c2")
    assert r.status_code == 422
    # 非法 id 形态 / 非法落点
    r = _split(ids4(), bodies, "c2", 2, "x5")
    assert r.status_code == 422
    r = _split(ids4(), bodies, "c2", 2, "c5", refs_to="middle")
    assert r.status_code == 422
    # 被拆条款不存在
    r = _split(ids4(), bodies, "c9", 2, "c5")
    assert r.status_code == 422

    # 删除 c4 后，c3 中 [[c4]] 失效；新身份不得复用 c4（否则失效引用会复活）
    order = ["c1", "c2", "c3"]
    bodies3 = [b for b in bodies if b["id"] != "c4"]
    r = _split(order, bodies3, "c2", 2, "c4")
    assert r.status_code == 422
    # 用全新身份则成功，且已有失效引用保持失效
    r = _split(order, bodies3, "c2", 2, "c5")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["order_ids"] == ["c1", "c2", "c5", "c3"]
    r = client.post("/api/preview", json={"order_ids": data["order_ids"], "clauses": data["clauses"]})
    assert r.json()["has_broken"] is True
    r = client.post("/api/export", json={"order_ids": data["order_ids"], "clauses": data["clauses"]})
    assert r.status_code == 409


def test_split_keeps_broken_refs_broken_in_segments():
    bodies = _bodies(import_text(GOOD).json())
    # 删除 c4：c3 中 [[c4]] 失效
    order = ["c1", "c2", "c3"]
    bodies3 = [b for b in bodies if b["id"] != "c4"]
    body = next(b["body"] for b in bodies3 if b["id"] == "c3")  # 争议解决见 [[c4]]，违约参照 [[c2]]
    offset = body.index("，")
    r = _split(order, bodies3, "c3", offset, "c5", refs_to="front")
    assert r.status_code == 200, r.text
    by = {c["id"]: c["body"] for c in r.json()["clauses"]}
    # 失效标记完整留在前段，仍保持失效；后段引用保持绑定
    assert by["c3"] == "争议解决见 [[c4]]"
    assert by["c5"] == "，违约参照 [[c2]]"
    r = client.post("/api/preview", json={"order_ids": r.json()["order_ids"], "clauses": r.json()["clauses"]})
    preview = r.json()
    assert preview["has_broken"] is True
    c3 = next(it for it in preview["items"] if it["id"] == "c3")
    assert c3["broken"] == [{"ref_id": "c4", "original": 4}]
    assert "[[原4?]]" in c3["text"]


def test_split_in_reordered_doc_inserts_right_after_front():
    bodies = _bodies(import_text(GOOD).json())
    order = ["c4", "c1", "c2", "c3"]
    r = _split(order, bodies, "c1", 3, "c5", refs_to="front")
    assert r.status_code == 200, r.text
    data = r.json()
    # 新条款紧跟前段，其余条款相对顺序不变
    assert data["order_ids"] == ["c4", "c1", "c5", "c2", "c3"]
    r = client.post("/api/preview", json={"order_ids": data["order_ids"], "clauses": data["clauses"]})
    items = {it["id"]: it for it in r.json()["items"]}
    assert items["c5"]["display"] == 3
    # c2 对 c1 的引用按新显示位置解析，绑定不变
    assert items["c2"]["text"] == "乙方按 [[2]] 验收后付款"


def test_split_then_merge_and_merge_then_split():
    bodies = _bodies(import_text(GOOD).json())
    # 拆分后可以继续合并：拆 c2（引用落后段）→ 再合并前段 c2 与后段 c5
    r = _split(ids4(), bodies, "c2", 2, "c5", refs_to="back")
    assert r.status_code == 200, r.text
    data = r.json()
    by = {c["id"]: c["body"] for c in data["clauses"]}
    assert by["c3"] == "争议解决见 [[c4]]，违约参照 [[c5]]"  # 指向 c2 的引用已落后段
    r = client.post(
        "/api/merge",
        json={"order_ids": data["order_ids"], "clauses": data["clauses"], "keep_id": "c2", "drop_id": "c5"},
    )
    assert r.status_code == 200, r.text
    by = {c["id"]: c["body"] for c in r.json()["clauses"]}
    assert by["c2"] == "乙方；按 [[c1]] 验收后付款"
    assert by["c3"] == "争议解决见 [[c4]]，违约参照 [[c2]]"  # 合并后引用改指合并结果

    # 合并后可以继续拆分：合并 c1+c2 → 再拆合并结果
    bodies = _bodies(import_text(GOOD).json())
    r = client.post(
        "/api/merge",
        json={"order_ids": ids4(), "clauses": bodies, "keep_id": "c1", "drop_id": "c2"},
    )
    data = r.json()
    body = next(c["body"] for c in data["clauses"] if c["id"] == "c1")
    offset = body.index("；") + 1
    r = _split(data["order_ids"], data["clauses"], "c1", offset, "c6", refs_to="front")
    assert r.status_code == 200, r.text
    assert r.json()["order_ids"] == ["c1", "c6", "c3", "c4"]
    by = {c["id"]: c["body"] for c in r.json()["clauses"]}
    assert by["c1"] == "甲方在七日内交付设备；"
    # 后段中指向被拆条款（前段）的引用保持绑定
    assert by["c6"] == "乙方按 [[c1]] 验收后付款"


# --------------------------------------------------------------------------- #
# 复制
# --------------------------------------------------------------------------- #
def _dup(order, bodies, selected, next_id):
    return client.post(
        "/api/duplicate",
        json={
            "order_ids": order,
            "clauses": bodies,
            "selected_ids": selected,
            "next_id": next_id,
        },
    )


def test_duplicate_group_copies_in_display_order_and_repoints_internal_refs():
    bodies = _bodies(import_text(GOOD).json())
    # 选择 c2、c3（当前顺序即显示顺序）：整组副本插在所选最后一条 c3 之后
    r = _dup(ids4(), bodies, ["c2", "c3"], 4)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["order_ids"] == ["c1", "c2", "c3", "c5", "c6", "c4"]
    assert data["next_id"] == 6  # 上界随副本数量推进
    by = {c["id"]: c["body"] for c in data["clauses"]}
    # 原条款正文、身份、相对顺序与引用均不变
    assert by["c2"] == "乙方按 [[c1]] 验收后付款"
    assert by["c3"] == "争议解决见 [[c4]]，违约参照 [[c2]]"
    # 副本 c5（复制 c2）：指向组外 c1 的引用保留原目标
    assert by["c5"] == "乙方按 [[c1]] 验收后付款"
    # 副本 c6（复制 c3）：指向组内 c2 的引用改指相应副本 c5；组外 c4 保留
    assert by["c6"] == "争议解决见 [[c4]]，违约参照 [[c5]]"

    # 预览 / 导出立即对应同一份草稿
    r = client.post("/api/preview", json={"order_ids": data["order_ids"], "clauses": data["clauses"]})
    items = {it["id"]: it for it in r.json()["items"]}
    assert r.json()["has_broken"] is False
    assert items["c5"]["display"] == 4
    assert items["c6"]["text"] == "争议解决见 [[6]]，违约参照 [[4]]"
    r = client.post("/api/export", json={"order_ids": data["order_ids"], "clauses": data["clauses"]})
    assert r.status_code == 200


def test_duplicate_self_reference_repoints_to_copy():
    doc = import_text("1|本条说明自身 [[1]]\n2|普通条款").json()
    bodies = _bodies(doc)
    r = _dup(["c1", "c2"], bodies, ["c1"], 2)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["order_ids"] == ["c1", "c3", "c2"]
    assert data["next_id"] == 3
    by = {c["id"]: c["body"] for c in data["clauses"]}
    assert by["c1"] == "本条说明自身 [[c1]]"  # 原条款不变
    assert by["c3"] == "本条说明自身 [[c3]]"  # 副本的自引用改指副本自身
    r = client.post("/api/preview", json={"order_ids": data["order_ids"], "clauses": data["clauses"]})
    items = {it["id"]: it for it in r.json()["items"]}
    assert items["c3"]["text"] == "本条说明自身 [[2]]"


def test_duplicate_keeps_broken_refs_broken():
    bodies = _bodies(import_text(GOOD).json())
    # 删除 c4：c3 中 [[c4]] 失效；复制 c3 后副本中的失效引用仍失效
    order = ["c1", "c2", "c3"]
    bodies3 = [b for b in bodies if b["id"] != "c4"]
    r = _dup(order, bodies3, ["c3"], 4)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["order_ids"] == ["c1", "c2", "c3", "c5"]
    by = {c["id"]: c["body"] for c in data["clauses"]}
    assert by["c5"] == "争议解决见 [[c4]]，违约参照 [[c2]]"
    r = client.post("/api/preview", json={"order_ids": data["order_ids"], "clauses": data["clauses"]})
    preview = r.json()
    assert preview["has_broken"] is True
    c5 = next(it for it in preview["items"] if it["id"] == "c5")
    assert c5["broken"] == [{"ref_id": "c4", "original": 4}]
    assert "[[原4?]]" in c5["text"]
    r = client.post("/api/export", json={"order_ids": data["order_ids"], "clauses": data["clauses"]})
    assert r.status_code == 409


def test_duplicate_in_reordered_doc_inserts_after_last_selected_in_display_order():
    bodies = _bodies(import_text(GOOD).json())
    order = ["c4", "c1", "c2", "c3"]
    # 传入顺序与显示顺序无关：按显示顺序 c1（第 2 位）、c3（第 4 位）分配身份
    r = _dup(order, bodies, ["c3", "c1"], 4)
    assert r.status_code == 200, r.text
    data = r.json()
    # 整组插在所选最后一条（显示顺序中的 c3）之后，其余条款相对顺序不变
    assert data["order_ids"] == ["c4", "c1", "c2", "c3", "c5", "c6"]
    by = {c["id"]: c["body"] for c in data["clauses"]}
    assert by["c5"] == "甲方在七日内交付设备"  # c5 复制显示顺序中较前的 c1
    assert by["c6"] == "争议解决见 [[c4]]，违约参照 [[c2]]"  # 组外引用保留原目标
    r = client.post("/api/preview", json={"order_ids": data["order_ids"], "clauses": data["clauses"]})
    items = {it["id"]: it for it in r.json()["items"]}
    assert items["c5"]["display"] == 5
    assert items["c6"]["display"] == 6
    assert items["c6"]["text"] == "争议解决见 [[1]]，违约参照 [[3]]"


def test_duplicate_rejects_empty_duplicate_and_missing_selection():
    bodies = _bodies(import_text(GOOD).json())
    # 未选择
    r = _dup(ids4(), bodies, [], 4)
    assert r.status_code == 422
    # 选择重复
    r = _dup(ids4(), bodies, ["c1", "c1"], 4)
    assert r.status_code == 422
    # 所选不存在
    r = _dup(ids4(), bodies, ["c9"], 4)
    assert r.status_code == 422
    # 非法上界
    r = _dup(ids4(), bodies, ["c1"], -1)
    assert r.status_code == 422
    # 失败不改草稿：原状态仍可正常预览
    r = client.post("/api/preview", json={"order_ids": ids4(), "clauses": bodies})
    assert r.status_code == 200
    assert len(r.json()["items"]) == 4


def test_duplicate_count_limit_40():
    text = "\n".join(f"{i}|条款{i}" for i in range(1, 41))  # 40 条
    bodies = _bodies(import_text(text).json())
    order = [f"c{i}" for i in range(1, 41)]
    # 40 条时复制 1 条即超上限：拒绝
    r = _dup(order, bodies, ["c1"], 40)
    assert r.status_code == 422
    # 39 条时复制 1 条达 40 条：允许
    r = _dup(order[:-1], bodies[:-1], ["c1"], 39)
    assert r.status_code == 200, r.text
    assert r.json()["order_ids"] == ["c1", "c40"] + [f"c{i}" for i in range(2, 40)]
    assert r.json()["next_id"] == 40
    # 39 条时复制 2 条达 41 条：拒绝
    r = _dup(order[:-1], bodies[:-1], ["c1", "c2"], 39)
    assert r.status_code == 422


def test_duplicate_new_ids_must_be_fresh_and_not_revive_broken_refs():
    bodies = _bodies(import_text(GOOD).json())
    # 上界过期：分配到的身份与现有条款冲突
    r = _dup(ids4(), bodies, ["c1"], 2)
    assert r.status_code == 422
    # 删除 c4 后，c3 中 [[c4]] 失效；上界过期导致分配到 c4 会复活失效引用：拒绝
    order = ["c1", "c2", "c3"]
    bodies3 = [b for b in bodies if b["id"] != "c4"]
    r = _dup(order, bodies3, ["c1"], 3)
    assert r.status_code == 422
    # 用真正高于已用上界的身份则成功，失效引用保持失效
    r = _dup(order, bodies3, ["c1"], 4)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["order_ids"] == ["c1", "c5", "c2", "c3"]
    assert data["next_id"] == 5
    r = client.post("/api/preview", json={"order_ids": data["order_ids"], "clauses": data["clauses"]})
    assert r.json()["has_broken"] is True


def test_duplicate_shares_upper_bound_with_split():
    bodies = _bodies(import_text(GOOD).json())
    # 复制推进上界到 5：拆分须用更高的新身份，且不能复用已占用的 c5
    r = _dup(ids4(), bodies, ["c1"], 4)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["next_id"] == 5
    r = _split(data["order_ids"], data["clauses"], "c2", 2, "c5")
    assert r.status_code == 422  # c5 已被副本占用
    r = _split(data["order_ids"], data["clauses"], "c2", 2, "c6")
    assert r.status_code == 200, r.text
    # 拆分推进上界后，复制同样从更高身份起分配
    data = r.json()
    r = _dup(data["order_ids"], data["clauses"], ["c1"], 6)
    assert r.status_code == 200, r.text
    assert "c7" in r.json()["order_ids"]
    assert r.json()["next_id"] == 7


def test_diff_duplicate_marks_copies_added():
    doc = import_text(GOOD).json()
    base = _state(doc)
    r = _dup(base["order_ids"], base["clauses"], ["c2"], 4)
    assert r.status_code == 200, r.text
    cur = {"order_ids": r.json()["order_ids"], "clauses": r.json()["clauses"]}
    data = _diff(base, cur).json()
    # 复制记副本（新身份）的新增，原条款不变；插入造成的平移不算移动
    assert [(it["id"], it["tags"]) for it in data["items"]] == [("c5", ["added"])]
    c5 = data["items"][0]
    assert c5["baseline_no"] is None and c5["current_no"] == 3
    assert c5["current_text"] == "乙方按 [[1]] 验收后付款"
    assert data["invalid_refs"] == []


# --------------------------------------------------------------------------- #
# 原稿对照 /api/diff
# --------------------------------------------------------------------------- #
def _state(doc, order=None):
    return {
        "order_ids": order or [c["id"] for c in doc["clauses"]],
        "clauses": [{"id": c["id"], "body": c["body"]} for c in doc["clauses"]],
    }


def _diff(baseline, current):
    return client.post("/api/diff", json={"baseline": baseline, "current": current})


def test_diff_no_changes():
    doc = import_text(GOOD).json()
    base = _state(doc)
    r = _diff(base, base)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["has_changes"] is False
    assert data["items"] == []
    assert data["invalid_refs"] == []


def test_diff_reorder_marks_moved_and_tolerates_display_number_change():
    doc = import_text(GOOD).json()
    base = _state(doc)
    # 仅重排（c4 提到首位）：全部条款相对原稿颠倒，标 moved；
    # 正文内部绑定未变，仅显示编号变化，不标 body_changed
    cur = _state(doc, ["c4", "c1", "c2", "c3"])
    data = _diff(base, cur).json()
    by = {it["id"]: it for it in data["items"]}
    assert set(by) == {"c1", "c2", "c3", "c4"}
    for item in by.values():
        assert item["tags"] == ["moved"]
    assert by["c4"]["baseline_no"] == 4 and by["c4"]["current_no"] == 1
    # 两版正文按各自编号解析：绑定同一条款，显示编号不同
    assert by["c2"]["baseline_text"] == "乙方按 [[1]] 验收后付款"
    assert by["c2"]["current_text"] == "乙方按 [[2]] 验收后付款"
    assert data["invalid_refs"] == []


def test_diff_adjacent_swap_marks_both_participants():
    doc = import_text(GOOD).json()
    base = _state(doc)
    data = _diff(base, _state(doc, ["c2", "c1", "c3", "c4"])).json()
    by = {it["id"]: it for it in data["items"]}
    assert by["c1"]["tags"] == ["moved"]
    assert by["c2"]["tags"] == ["moved"]
    assert "c3" not in by and "c4" not in by  # 未参与颠倒的条款不出现


def test_diff_delete_shifts_numbers_but_is_not_move_and_lists_broken_ref_source():
    doc = import_text(GOOD).json()
    base = _state(doc)
    # 删除 c2：c1/c3/c4 编号平移但相对先后不变，不算移动；c3 中 [[c2]] 由有效变失效
    cur = {
        "order_ids": ["c1", "c3", "c4"],
        "clauses": [b for b in base["clauses"] if b["id"] != "c2"],
    }
    data = _diff(base, cur).json()
    assert [(it["id"], it["tags"]) for it in data["items"]] == [("c2", ["deleted"])]
    deleted = data["items"][0]
    assert deleted["baseline_no"] == 2 and deleted["current_no"] is None
    assert deleted["baseline_text"] == "乙方按 [[1]] 验收后付款"
    assert deleted["current_text"] is None
    # 现存项按当前顺序在前，删除项在最后：此处仅删除项
    assert len(data["invalid_refs"]) == 1
    rec = data["invalid_refs"][0]
    assert rec["source_id"] == "c3"
    assert rec["ref_id"] == "c2" and rec["ref_original"] == 2
    assert rec["baseline_no"] == 3 and rec["current_no"] == 2
    assert rec["position"] == 19 and rec["position_end"] == 25  # 1 起、按 Unicode 码位
    assert rec["baseline_text"] == "争议解决见 [[4]]，违约参照 [[2]]"
    # c4 仍存在（现处第 3 位），[[c4]] 仅显示编号变为 [[3]]；只有被删的 c2 变失效
    assert rec["current_text"] == "争议解决见 [[3]]，违约参照 [[原2?]]"
    assert data["has_changes"] is True


def test_diff_split_is_front_body_change_and_back_added_without_move():
    doc = import_text(GOOD).json()
    base = _state(doc)
    r = client.post(
        "/api/split",
        json={**base, "split_id": "c2", "offset": 2, "new_id": "c5", "refs_to": "front"},
    )
    cur = {"order_ids": r.json()["order_ids"], "clauses": r.json()["clauses"]}
    data = _diff(base, cur).json()
    # 顺序：现存项（c2 改动、c5 新增）按当前顺序；插入造成的平移不算移动
    assert [(it["id"], it["tags"]) for it in data["items"]] == [
        ("c2", ["body_changed"]),
        ("c5", ["added"]),
    ]
    c2 = next(it for it in data["items"] if it["id"] == "c2")
    assert c2["baseline_text"] == "乙方按 [[1]] 验收后付款"
    assert c2["current_text"] == "乙方"
    c5 = next(it for it in data["items"] if it["id"] == "c5")
    assert c5["baseline_no"] is None and c5["current_no"] == 3
    assert c5["baseline_text"] is None
    assert c5["current_text"] == "按 [[1]] 验收后付款"
    assert data["invalid_refs"] == []


def test_diff_merge_is_kept_body_change_and_drop_deleted():
    doc = import_text(GOOD).json()
    base = _state(doc)
    r = client.post(
        "/api/merge",
        json={**base, "keep_id": "c1", "drop_id": "c2"},
    )
    cur = {"order_ids": r.json()["order_ids"], "clauses": r.json()["clauses"]}
    data = _diff(base, cur).json()
    # 现存项按当前顺序（c1 改动、c3 因引用重写而改动），删除项 c2 在最后
    assert [(it["id"], it["tags"]) for it in data["items"]] == [
        ("c1", ["body_changed"]),
        ("c3", ["body_changed"]),
        ("c2", ["deleted"]),
    ]
    c1 = next(it for it in data["items"] if it["id"] == "c1")
    assert c1["baseline_text"] == "甲方在七日内交付设备"
    assert c1["current_text"] == "甲方在七日内交付设备；乙方按 [[1]] 验收后付款"


def test_diff_body_change_and_move_tags_coexist():
    doc = import_text(GOOD).json()
    base = _state(doc)
    bodies = [dict(b) for b in base["clauses"]]
    bodies[0]["body"] = "甲方改为三十日内交付设备"
    cur = {"order_ids": ["c2", "c3", "c4", "c1"], "clauses": bodies}
    data = _diff(base, cur).json()
    c1 = next(it for it in data["items"] if it["id"] == "c1")
    assert set(c1["tags"]) == {"body_changed", "moved"}


def test_diff_edit_body_change_is_comparison_by_ref_binding():
    doc = import_text(GOOD).json()
    base = _state(doc)
    # 顺序改变、c3 正文内部绑定完全一致（只是显示编号变了）：不标 body_changed
    reordered = _state(doc, ["c4", "c1", "c2", "c3"])
    data = _diff(base, reordered).json()
    c3 = next(it for it in data["items"] if it["id"] == "c3")
    assert "body_changed" not in c3["tags"]
    # 真正改正文（内部绑定变化）才标 body_changed
    bodies = [dict(b) for b in base["clauses"]]
    bodies[2]["body"] = "争议解决改为 [[c1]]"
    data = _diff(base, {"order_ids": base["order_ids"], "clauses": bodies}).json()
    c3 = next(it for it in data["items"] if it["id"] == "c3")
    assert c3["tags"] == ["body_changed"]
    assert c3["current_text"] == "争议解决改为 [[1]]"


def test_diff_invalid_ref_only_listed_when_body_unchanged():
    doc = import_text(GOOD).json()
    base = _state(doc)
    # 删 c4 后又编辑 c3 修复引用：正文已变，标 body_changed，不再列入引用失效
    after_delete = {
        "order_ids": ["c1", "c2", "c3"],
        "clauses": [b for b in base["clauses"] if b["id"] != "c4"],
    }
    r = client.post(
        "/api/save",
        json={**after_delete, "edit_id": "c3", "text": "争议解决改为 [[1]]"},
    )
    fixed = [
        {"id": b["id"], "body": r.json()["body"] if b["id"] == "c3" else b["body"]}
        for b in after_delete["clauses"]
    ]
    data = _diff(base, {"order_ids": after_delete["order_ids"], "clauses": fixed}).json()
    c3 = next(it for it in data["items"] if it["id"] == "c3")
    assert c3["tags"] == ["body_changed"]
    assert data["invalid_refs"] == []


def test_diff_already_broken_in_baseline_is_not_relisted():
    doc = import_text(GOOD).json()
    base_full = _state(doc)
    # 基线本身就是删除 c4 后的状态（c3 含失效引用）：现稿相同，应“无变更”
    broken = {
        "order_ids": ["c1", "c2", "c3"],
        "clauses": [b for b in base_full["clauses"] if b["id"] != "c4"],
    }
    data = _diff(broken, broken).json()
    assert data["has_changes"] is False
    assert data["items"] == [] and data["invalid_refs"] == []


def test_diff_validates_both_documents():
    doc = import_text(GOOD).json()
    base = _state(doc)
    # 基线损坏：顺序列表与条款集合不一致 → 422
    r = _diff({"order_ids": ["c1"], "clauses": base["clauses"]}, base)
    assert r.status_code == 422
    # 现稿损坏：内部引用形态损坏 → 422
    bad_cur = {
        "order_ids": base["order_ids"],
        "clauses": [
            {"id": b["id"], "body": b["body"].replace("[[c1]]", "[[1]]") if b["id"] == "c2" else b["body"]}
            for b in base["clauses"]
        ],
    }
    r = _diff(base, bad_cur)
    assert r.status_code == 422


def test_diff_does_not_modify_input_documents():
    doc = import_text(GOOD).json()
    base = _state(doc)
    cur = _state(doc, ["c4", "c1", "c2", "c3"])
    import copy
    payload = {"baseline": copy.deepcopy(base), "current": copy.deepcopy(cur)}
    r = client.post("/api/diff", json=payload)
    assert r.status_code == 200
    # 接口只返回对照，不回退任何草稿
    assert payload["baseline"] == base
    assert payload["current"] == cur


# --------------------------------------------------------------------------- #
# 谁引用本条 /api/referrers
# --------------------------------------------------------------------------- #
def _referrers(state, target_id):
    return client.post("/api/referrers", json={**state, "target_id": target_id})


def test_referrers_lists_direct_and_indirect_sorted_by_current_order():
    doc = import_text(
        "\n".join(
            [
                "1|目标条款",
                "2|直接引用 [[1]]",
                "3|间接引用 [[2]]",
                "4|更间接 [[3]]",
                "5|无人问津",
            ]
        )
    ).json()
    state = _state(doc)
    data = _referrers(state, "c1").json()
    assert data["target_id"] == "c1" and data["target_no"] == 1
    # 直接引用者仅 c2；c3、c4 只经多级引用可达，为间接引用者；按来源当前顺序排列
    assert [it["id"] for it in data["direct"]] == ["c2"]
    assert [it["id"] for it in data["indirect"]] == ["c3", "c4"]
    # 附稳定身份、当前编号与从来源到目标的最短引用链
    assert data["direct"][0] == {
        "id": "c2",
        "original": 2,
        "current_no": 2,
        "chain": [2, 1],
        "chain_ids": ["c2", "c1"],
    }
    assert [it["chain"] for it in data["indirect"]] == [[3, 2, 1], [4, 3, 2, 1]]
    assert [it["chain_ids"] for it in data["indirect"]] == [
        ["c3", "c2", "c1"],
        ["c4", "c3", "c2", "c1"],
    ]


def test_referrers_target_itself_is_not_listed_even_with_self_reference():
    doc = import_text("1|目标并自述 [[1]]\n2|引用 [[1]]").json()
    data = _referrers(_state(doc), "c1").json()
    assert [it["id"] for it in data["direct"]] == ["c2"]
    assert data["indirect"] == []
    # 查询无引用的条款：明确空结果
    doc2 = import_text("1|第一条\n2|第二条").json()
    data = _referrers(_state(doc2), "c1").json()
    assert data["direct"] == [] and data["indirect"] == []
    assert data["target_id"] == "c1" and data["target_no"] == 1


def test_referrers_results_order_follow_current_display_order():
    doc = import_text(
        "\n".join(
            [
                "1|目标",
                "2|直引甲 [[1]]",
                "3|直引乙 [[1]]",
                "4|间引 [[3]]",
            ]
        )
    ).json()
    # 重排为 c1, c4, c3, c2：直接引用者按当前顺序为 c3（第3位）、c2（第4位）
    state = _state(doc, ["c1", "c4", "c3", "c2"])
    data = _referrers(state, "c1").json()
    assert [it["id"] for it in data["direct"]] == ["c3", "c2"]
    assert [it["current_no"] for it in data["direct"]] == [3, 4]
    indirect = data["indirect"]
    assert [it["id"] for it in indirect] == ["c4"]
    assert indirect[0]["chain"] == [2, 3, 1]
    assert indirect[0]["chain_ids"] == ["c4", "c3", "c1"]


def test_referrers_tie_breaks_equal_length_chains_by_number_sequence():
    # c2、c3 都直接引用 c1；c4 同时引用 c2 与 c3，故 c4→c1 有两条等长链：
    # [4,2,1] 与 [4,3,1]，按整条链当前编号序列取字典序最小 → 经当前第 2 位的 c2
    doc = import_text(
        "\n".join(
            [
                "1|目标",
                "2|直引甲 [[1]]",
                "3|直引乙 [[1]]",
                "4|间引 [[2]][[3]]",
            ]
        )
    ).json()
    data = _referrers(_state(doc), "c1").json()
    c4 = next(it for it in data["indirect"] if it["id"] == "c4")
    assert c4["chain"] == [4, 2, 1]
    # 重排后 c3 在 c2 之前：字典序最小变为经当前第 2 位的 c3
    state = _state(doc, ["c1", "c3", "c2", "c4"])
    data = _referrers(state, "c1").json()
    c4 = next(it for it in data["indirect"] if it["id"] == "c4")
    assert c4["chain"] == [4, 2, 1]  # 当前编号：c4=4 → c3=2 → c1=1
    assert c4["chain_ids"] == ["c4", "c3", "c1"]


def test_referrers_tie_break_at_deeper_level():
    # 两条同距离的第二层前驱位置相同级时，更深元素也要按编号序比较：
    # c2、c3 直引 c1；c4 引 c2、c5 引 c3；c6 同时引 c4、c5。
    # c6→c1 等长链 [6,4,2,1] 与 [6,5,3,1] → 取经当前编号较小的 c4
    doc = import_text(
        "\n".join(
            [
                "1|目标",
                "2|[[1]]",
                "3|[[1]]",
                "4|[[2]]",
                "5|[[3]]",
                "6|[[4]][[5]]",
            ]
        )
    ).json()
    data = _referrers(_state(doc), "c1").json()
    c6 = next(it for it in data["indirect"] if it["id"] == "c6")
    assert c6["chain"] == [6, 4, 2, 1]


def test_referrers_duplicate_edges_and_cycles_do_not_duplicate_or_loop():
    # c2 正文多次引用 c1（重复引用只构成一条边、来源只列一次）；
    # c1↔c2 与 c2→c3→c2 构成循环，遍历时不得重复或死循环
    doc = import_text(
        "\n".join(
            [
                "1|回引 [[2]]",
                "2|重复 [[1]][[1]]，又见 [[3]]",
                "3|再回 [[2]]",
            ]
        )
    ).json()
    data = _referrers(_state(doc), "c1").json()
    assert [it["id"] for it in data["direct"]] == ["c2"]
    assert [it["id"] for it in data["indirect"]] == ["c3"]
    # 最短链而非绕环长链
    assert data["indirect"][0]["chain"] == [3, 2, 1]


def test_referrers_broken_refs_do_not_participate():
    doc = import_text(GOOD).json()
    # 删除 c4：c3 中 [[c4]] 失效；c3 仍通过 [[c2]] 间接可达 c1
    state = {
        "order_ids": ["c1", "c2", "c3"],
        "clauses": [b for b in _bodies(doc) if b["id"] != "c4"],
    }
    # 失效引用目标 c4 已不存在：无法查询
    r = _referrers(state, "c4")
    assert r.status_code == 422
    # 查询现存 c2：c3 中指向 c2 的有效引用仍算来源，[[c4]] 失效边不参与
    data = _referrers(state, "c2").json()
    assert [it["id"] for it in data["direct"]] == ["c3"]
    assert data["indirect"] == []
    # 没有任何现存条款经失效引用成为来源
    data = _referrers(state, "c1").json()
    assert [it["id"] for it in data["direct"]] == ["c2"]
    assert [it["id"] for it in data["indirect"]] == ["c3"]


def test_referrers_rejects_unknown_target_and_does_not_mutate_draft():
    doc = import_text(GOOD).json()
    state = _state(doc)
    r = _referrers(state, "c9")
    assert r.status_code == 422
    assert r.json()["detail"]["errors"]
    # 草稿损坏同样 422
    r = client.post(
        "/api/referrers",
        json={"order_ids": ["c1"], "clauses": state["clauses"], "target_id": "c1"},
    )
    assert r.status_code == 422
    # 查询不改正文、顺序或引用绑定
    import copy
    payload = copy.deepcopy(state)
    r = _referrers(payload, "c1")
    assert r.status_code == 200
    assert payload == state


def test_referrers_shorter_path_wins_over_longer_one():
    # c3 既直引 c1 又经 c2 间接引 c1：应归为直接引用者，链取最短 [3,1]
    doc = import_text(
        "\n".join(
            [
                "1|目标",
                "2|[[1]]",
                "3|直引 [[1]] 又间引 [[2]]",
            ]
        )
    ).json()
    data = _referrers(_state(doc), "c1").json()
    assert [it["id"] for it in data["direct"]] == ["c2", "c3"]
    assert data["indirect"] == []
    c3 = next(it for it in data["direct"] if it["id"] == "c3")
    assert c3["chain"] == [3, 1]


# --------------------------------------------------------------------------- #
# 勾选条款摘录 /api/excerpt
# --------------------------------------------------------------------------- #
def _excerpt(state, selected):
    return client.post("/api/excerpt", json={**state, "selected_ids": selected})


def test_excerpt_includes_transitive_refs_and_renumbers():
    # 勾选 c4：沿引用闭包收入 c4→c3→c2，跳过未被引用的 c1；
    # 摘录从 1 连续编号，引用改用摘录内编号，其余字符不变
    doc = import_text(
        "\n".join(
            [
                "1|合同附件清单",
                "2|甲方在七日内交付设备",
                "3|乙方按 [[2]] 验收后付款",
                "4|争议解决见 [[3]]",
            ]
        )
    ).json()
    r = _excerpt(_state(doc), ["c4"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["text"].split("\n") == [
        "1|甲方在七日内交付设备",
        "2|乙方按 [[1]] 验收后付款",
        "3|争议解决见 [[2]]",
    ]
    # 各条附稳定身份、草稿编号（当前显示编号）与摘录编号
    assert data["items"] == [
        {"id": "c2", "original": 2, "draft_no": 2, "excerpt_no": 1},
        {"id": "c3", "original": 3, "draft_no": 3, "excerpt_no": 2},
        {"id": "c4", "original": 4, "draft_no": 4, "excerpt_no": 3},
    ]


def test_excerpt_follows_current_display_order_after_reorder():
    bodies = _bodies(import_text(GOOD).json())
    order = ["c4", "c1", "c2", "c3"]
    # 勾选 c3：闭包为全部四条；摘录按当前显示顺序编号，引用改用摘录内编号
    r = _excerpt({"order_ids": order, "clauses": bodies}, ["c3"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["text"].split("\n") == [
        "1|争议提交仲裁",
        "2|甲方在七日内交付设备",
        "3|乙方按 [[2]] 验收后付款",
        "4|争议解决见 [[1]]，违约参照 [[3]]",
    ]
    assert [(it["id"], it["draft_no"], it["excerpt_no"]) for it in data["items"]] == [
        ("c4", 1, 1),
        ("c1", 2, 2),
        ("c2", 3, 3),
        ("c3", 4, 4),
    ]


def test_excerpt_self_cycle_and_duplicate_refs_collected_once():
    # 自引用、c1↔c2 与 c2→c3→c2 循环、重复引用：每条只收录一次，遍历不死循环
    doc = import_text(
        "\n".join(
            [
                "1|回引 [[2]]",
                "2|重复 [[1]][[1]]，又见 [[3]]",
                "3|再回 [[2]]",
            ]
        )
    ).json()
    r = _excerpt(_state(doc), ["c2"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert [it["id"] for it in data["items"]] == ["c1", "c2", "c3"]
    assert data["text"].split("\n") == [
        "1|回引 [[2]]",
        "2|重复 [[1]][[1]]，又见 [[3]]",
        "3|再回 [[2]]",
    ]
    # 自引用条款单独成摘录：[[1]] 指向自身，摘录内编号仍为 1
    doc2 = import_text("1|本条说明自身 [[1]]\n2|普通条款").json()
    data = _excerpt(_state(doc2), ["c1"]).json()
    assert data["text"] == "1|本条说明自身 [[1]]"
    assert [it["id"] for it in data["items"]] == ["c1"]


def test_excerpt_selection_order_is_irrelevant():
    bodies = _bodies(import_text(GOOD).json())
    # 传入顺序与收录顺序无关：一律按草稿当前顺序排列
    r = _excerpt({"order_ids": ids4(), "clauses": bodies}, ["c3", "c1"])
    assert r.status_code == 200, r.text
    assert [it["id"] for it in r.json()["items"]] == ["c1", "c2", "c3", "c4"]


def test_excerpt_broken_ref_in_scope_rejects_whole_excerpt():
    doc = import_text(GOOD).json()
    # 删除 c4：c3 中 [[c4]] 失效；勾选 c3 时闭包含 c3/c2/c1，失效引用在收录范围内 → 整份 409
    state = {
        "order_ids": ["c1", "c2", "c3"],
        "clauses": [b for b in _bodies(doc) if b["id"] != "c4"],
    }
    r = _excerpt(state, ["c3"])
    assert r.status_code == 409
    detail = r.json()["detail"]
    # 列出来源稳定身份与缺失目标
    assert detail["broken"] == [
        {"source_id": "c3", "source_no": 3, "ref_id": "c4", "ref_original": 4}
    ]
    assert any("c3" in m and "c4" in m for m in detail["errors"])


def test_excerpt_broken_ref_out_of_scope_does_not_block_and_export_stays_blocked():
    doc = import_text(GOOD).json()
    # 删除 c4 后勾选 c2：闭包仅 {c2, c1}，c3 的失效引用在范围外 → 不阻止摘录
    state = {
        "order_ids": ["c1", "c2", "c3"],
        "clauses": [b for b in _bodies(doc) if b["id"] != "c4"],
    }
    r = _excerpt(state, ["c2"])
    assert r.status_code == 200, r.text
    assert r.json()["text"].split("\n") == [
        "1|甲方在七日内交付设备",
        "2|乙方按 [[1]] 验收后付款",
    ]
    # 范围外的失效引用不解除原整稿下载限制：整稿导出仍 409
    r = client.post("/api/export", json=state)
    assert r.status_code == 409


def test_excerpt_rejects_empty_duplicate_and_missing_selection():
    state = _state(import_text(GOOD).json())
    # 未选择
    r = _excerpt(state, [])
    assert r.status_code == 422
    # 重复选择
    r = _excerpt(state, ["c1", "c1"])
    assert r.status_code == 422
    # 身份不存在
    r = _excerpt(state, ["c9"])
    assert r.status_code == 422
    # 草稿结构损坏
    r = client.post(
        "/api/excerpt",
        json={"order_ids": ["c1"], "clauses": state["clauses"], "selected_ids": ["c1"]},
    )
    assert r.status_code == 422


def test_excerpt_does_not_modify_draft():
    doc = import_text(GOOD).json()
    state = _state(doc)
    import copy
    payload = copy.deepcopy({**state, "selected_ids": ["c3"]})
    r = client.post("/api/excerpt", json=payload)
    assert r.status_code == 200
    # 只读接口：草稿、顺序与引用绑定均不被改动
    assert payload == {**state, "selected_ids": ["c3"]}


# --------------------------------------------------------------------------- #
# 校稿存档 /api/archive
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolated_archive_db(tmp_path, monkeypatch):
    """每个测试使用独立的临时存档数据库，互不干扰（不改生产默认路径）。"""
    monkeypatch.setattr("app.main.DB_PATH", tmp_path / "archive.db")


def _archive_snapshot(order, bodies, id_counter=4, baseline=None):
    return {
        "baseline": baseline or {"order_ids": list(order), "clauses": bodies},
        "order_ids": list(order),
        "clauses": bodies,
        "id_counter": id_counter,
    }


def test_archive_empty_slot_reads_version_zero():
    r = client.get("/api/archive")
    assert r.status_code == 200
    assert r.json() == {"version": 0, "snapshot": None}


def test_archive_save_then_read_roundtrip_and_version_bumps():
    bodies = _bodies(import_text(GOOD).json())
    snap = _archive_snapshot(ids4(), bodies)
    # 空槽保存：携带版本 0，整份写入后版本加 1
    r = client.put("/api/archive", json={"version": 0, "snapshot": snap})
    assert r.status_code == 200, r.text
    assert r.json() == {"version": 1}
    # 读取返回版本与完整快照（基线、顺序、内部正文与身份上界一致）
    r = client.get("/api/archive")
    assert r.status_code == 200
    assert r.json() == {"version": 1, "snapshot": snap}
    # 携带最近保存得到的版本再次保存：整份写入并再加 1
    snap2 = _archive_snapshot(["c4", "c1", "c2", "c3"], bodies)
    r = client.put("/api/archive", json={"version": 1, "snapshot": snap2})
    assert r.status_code == 200
    assert r.json() == {"version": 2}
    r = client.get("/api/archive")
    assert r.json()["version"] == 2
    assert r.json()["snapshot"]["order_ids"] == ["c4", "c1", "c2", "c3"]


def test_archive_conflict_returns_409_and_keeps_old_snapshot():
    bodies = _bodies(import_text(GOOD).json())
    snap = _archive_snapshot(ids4(), bodies)
    assert client.put("/api/archive", json={"version": 0, "snapshot": snap}).status_code == 200
    # 基于过期版本保存：409 并附服务端当前版本，旧存档保留
    snap2 = _archive_snapshot(["c4", "c1", "c2", "c3"], bodies)
    r = client.put("/api/archive", json={"version": 0, "snapshot": snap2})
    assert r.status_code == 409
    assert r.json()["detail"]["version"] == 1
    assert r.json()["detail"]["errors"]
    r = client.get("/api/archive")
    assert r.json()["version"] == 1
    assert r.json()["snapshot"]["order_ids"] == ids4()
    # 携带读取到的最新版本再保存：成功推进
    r = client.put("/api/archive", json={"version": 1, "snapshot": snap2})
    assert r.status_code == 200
    assert r.json() == {"version": 2}


def test_archive_rejects_invalid_snapshot_and_keeps_old():
    bodies = _bodies(import_text(GOOD).json())
    snap = _archive_snapshot(ids4(), bodies)
    assert client.put("/api/archive", json={"version": 0, "snapshot": snap}).status_code == 200
    # 现稿损坏：顺序列表与条款集合不一致（沿用草稿校验）
    bad = _archive_snapshot(["c1", "c2"], bodies)
    r = client.put("/api/archive", json={"version": 1, "snapshot": bad})
    assert r.status_code == 422
    # 基线损坏：内部引用形态非法
    bad_base = _archive_snapshot(
        ids4(),
        bodies,
        baseline={
            "order_ids": ids4(),
            "clauses": [
                {"id": b["id"], "body": b["body"].replace("[[c1]]", "[[1]]") if b["id"] == "c2" else b["body"]}
                for b in bodies
            ],
        },
    )
    r = client.put("/api/archive", json={"version": 1, "snapshot": bad_base})
    assert r.status_code == 422
    # 非法版本号
    r = client.put("/api/archive", json={"version": -1, "snapshot": snap})
    assert r.status_code == 422
    # 非法快照与非法版本均未写入：旧存档保留
    r = client.get("/api/archive")
    assert r.json() == {"version": 1, "snapshot": snap}


def test_archive_id_counter_must_cover_baseline_current_and_broken_ref_ids():
    doc = import_text(GOOD).json()
    bodies = _bodies(doc)
    # 现稿删除 c4（c3 正文含失效引用 [[c4]]），基线仍是完整四条：上界须覆盖到 4
    cur_order = ["c1", "c2", "c3"]
    cur_bodies = [b for b in bodies if b["id"] != "c4"]
    snap = _archive_snapshot(
        cur_order,
        cur_bodies,
        id_counter=3,
        baseline={"order_ids": ids4(), "clauses": bodies},
    )
    r = client.put("/api/archive", json={"version": 0, "snapshot": snap})
    assert r.status_code == 422
    assert "上界" in r.json()["detail"]["errors"][0]
    assert client.get("/api/archive").json() == {"version": 0, "snapshot": None}
    # 上界覆盖后保存成功：失效引用与身份上界随快照保留，恢复后旧身份不复用
    snap["id_counter"] = 4
    r = client.put("/api/archive", json={"version": 0, "snapshot": snap})
    assert r.status_code == 200
    got = client.get("/api/archive").json()["snapshot"]
    assert got["id_counter"] == 4
    assert got["clauses"][2]["body"] == "争议解决见 [[c4]]，违约参照 [[c2]]"
    # 恢复出的快照可直接用于既有接口：失效引用如实呈现、导出仍 409
    r = client.post("/api/preview", json={"order_ids": got["order_ids"], "clauses": got["clauses"]})
    assert r.json()["has_broken"] is True
    r = client.post("/api/export", json={"order_ids": got["order_ids"], "clauses": got["clauses"]})
    assert r.status_code == 409
    # 拆分新身份须高于恢复的上界：复用旧身份（c4）被拒，c5 成功
    r = _split(got["order_ids"], got["clauses"], "c2", 2, "c4")
    assert r.status_code == 422
    r = _split(got["order_ids"], got["clauses"], "c2", 2, "c5")
    assert r.status_code == 200


def test_archive_snapshot_with_advanced_counter_roundtrips():
    bodies = _bodies(import_text(GOOD).json())
    # 复制推进上界到 5 后保存：快照保留新身份与上界
    r = _dup(ids4(), bodies, ["c1"], 4)
    assert r.status_code == 200
    data = r.json()
    snap = _archive_snapshot(data["order_ids"], data["clauses"], id_counter=data["next_id"])
    r = client.put("/api/archive", json={"version": 0, "snapshot": snap})
    assert r.status_code == 200
    got = client.get("/api/archive").json()["snapshot"]
    assert got == snap
    # 上界低于现稿身份（c5）时拒绝
    snap_low = _archive_snapshot(data["order_ids"], data["clauses"], id_counter=4)
    r = client.put("/api/archive", json={"version": 1, "snapshot": snap_low})
    assert r.status_code == 422

