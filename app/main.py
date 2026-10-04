"""条款重排校稿台 —— FastAPI 后端。

引用模型：
- 导入原文中的引用写作 ``[[数字]]``，校验通过后绑定到条款的稳定 id（``c<原编号>``），
  正文内部统一存为 ``[[c<原编号>]]``。
- 编辑保存时，文本中的 ``[[数字]]`` 按“保存当刻”的显示编号绑定到对应稳定 id，
  因此之后无论怎样重排，引用都不会转指占用旧号的条款。
- 被引用条款被删除后，其 id 仍留在引用处，预览解析为失效引用（[[原X?]]），
  修复前导出接口返回 409，前端禁止下载。
- 合并两条条款时，按当前显示顺序用全角分号连接正文；合并结果沿用较前条款的稳定 id，
  较后条款删除，其 id 在所有正文中（含两条待合并正文）统一改写为较前条款 id。
  其他引用保持不动；已失效引用（id 已不存在于草稿）不会因合并恢复，仍保持失效。
- 拆分一条条款时，在指定分界处把已保存正文切成前后两段：分界不能切开引用标记，
  两段都不能只含空白，其余字符原样保留。前段沿用原稳定 id，后段使用前端分配的、
  本草稿从未用过的全新 id（被删除或合并移除的 id 不复用，已有失效引用不会因此复活）；
  指向被拆条款的全部引用（含两段中的自引用）按请求统一改指前段或后段，其余引用不动；
  新条款在顺序表中紧跟前段，其余条款相对顺序不变。
- 复制一组条款时，按当前显示顺序为所选条款生成可独立编辑的副本，整组插在所选
  最后一条之后；原条款的正文、身份、相对顺序与引用均不变。副本正文中指向组内
  条款的引用（含自引用）改指相应副本，指向组外条款的引用保留原目标，已失效引用
  保持失效。副本身份高于调用方给出的已用身份上界（与拆分共用同一上界，被删除或
  合并移除的身份不复用，已有失效引用不会因此复活），接口返回更新后的上界。

原稿对照（``POST /api/diff``）：
- 入参为两份沿用现有草稿结构的文档：成功导入后不可变的原稿基线（baseline）与
  当前已保存草稿（current）；接口只返回对照结果，不回退、不修改任何一份草稿。
- 按稳定 id 识别条款，标注新增（added）、删除（deleted）、正文改动（body_changed）、
  移动（moved），标签可并存。正文按内部引用绑定比较：仅显示编号变化不算正文改动。
- 移动只计基线与现稿中都仍存在、且彼此先后颠倒的条款；删除或拆分插入造成的
  编号平移不算移动。合并记保留身份的正文改动与另一身份删除；拆分记前段正文改动、
  后段新增；复制记各副本（新身份）的新增，原条款不变。
- 正文未变但某条引用由基线中的有效引用变成现稿失效引用时，另列引用来源条款与位置。
- 现存项按当前顺序排列，删除项随后按基线顺序排列；没有任何变化时 has_changes 为假。

谁引用本条（``POST /api/referrers``）：
- 入参沿用现有草稿结构（``order_ids`` / ``clauses``）加目标稳定身份（``target_id``），
  目标不存在则 422 拒绝；接口只返回查询结果，不改正文、顺序、基线或引用绑定。
- 只在"目标仍在草稿内"的有效引用上遍历（失效引用不成边）；反向 BFS 给出每个来源
  到目标的最短引用链：长度 1 的边来源为直接引用者，距离 ≥2 的为间接引用者，目标
  本身不列入；重复引用与循环不会造成重复或死循环。
- 等长链按整条链的当前编号序列取字典序最小；结果按来源的当前顺序排列。

勾选条款摘录（``POST /api/excerpt``）：
- 入参沿用现有草稿结构（``order_ids`` / ``clauses``）加所选稳定身份（``selected_ids``）；
  未选择、重复选择或身份不存在均 422 拒绝；接口只读，不改草稿、基线、身份上界或引用绑定。
- 收录范围为所选条款及其直接、间接引用（沿"目标仍在草稿内"的有效引用可达）的全部现存
  条款；自引用、循环与重复引用只收录一次。摘录按草稿当前顺序从 1 连续编号，正文中的
  引用改用摘录内编号，其余正文字符不变。
- 收录范围内存在失效引用（目标已不在草稿）时整份拒绝（409），列出来源稳定身份与缺失
  目标；范围外的失效引用不阻止摘录，也不影响整稿导出的既有规则。
- 返回摘录文本及各条的稳定身份、草稿编号（当前显示编号）与摘录编号。

校稿存档（``GET /api/archive`` / ``PUT /api/archive``）：
- 服务端用 SQLite 保存**一个共享槽**的完整快照：不可变原稿基线、当前顺序、内部正文
  （``order_ids`` / ``clauses``，内部形态 ``[[c3]]``）与已用身份上界（``id_counter``）；
  不含勾选、编辑中正文或拆分选择等临时状态。数据库文件自动创建（表不存在时自动建表），
  容器经数据卷持久化，刷新页面或重建容器后均可恢复继续校订。
- 读取返回版本与快照：空槽版本为 0、快照为 null。
- 保存携带最近读取或保存成功得到的版本：仅与槽内当前版本一致时整份写入并加 1；
  不一致返回 409（附当前版本），旧存档保留。版本检查与写入在同一事务内完成。
- 快照沿用草稿校验：基线与现稿均须通过既有状态校验，身份上界须覆盖基线、现稿及
  失效引用中出现的全部身份号（恢复后继续拆分 / 复制不会复用旧身份）；
  非法快照（422）或写入失败（500）时旧存档一律保留。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import deque
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

MIN_LINES = 2
MAX_LINES = 40

# 匹配任意 [[...]]（内容不含方括号）；数字引用与内部 id 引用均用它识别
MARKER_RE = re.compile(r"\[\[([^\[\]]*)\]\]")
OPEN_RE = re.compile(r"(?=\[\[)")
CLOSE_RE = re.compile(r"(?=\]\])")
# 规范的引用/编号数字：十进制正整数，不允许前导零（0 单独成案处理）
NUM_TOKEN_RE = re.compile(r"^(0|[1-9][0-9]*)$")
# 稳定 id：c + 导入时的原始编号
ID_RE = re.compile(r"^c(\d+)$")

app = FastAPI(title="条款重排校稿台")


# --------------------------------------------------------------------------- #
# 请求模型
# --------------------------------------------------------------------------- #
class ImportIn(BaseModel):
    text: str


class ClauseIn(BaseModel):
    id: str
    body: str  # 内部形态：[[c3]] 形式的引用


class StateIn(BaseModel):
    order_ids: list[str]
    clauses: list[ClauseIn]


class SaveIn(BaseModel):
    order_ids: list[str]
    clauses: list[ClauseIn]
    edit_id: str
    text: str  # 编辑器中的正文（数字引用）


class MergeIn(BaseModel):
    order_ids: list[str]
    clauses: list[ClauseIn]
    keep_id: str  # 合并后沿用的身份（当前显示顺序中较前的一条）
    drop_id: str  # 被并入并移除的一条（当前显示顺序中较后的一条）


class SplitIn(BaseModel):
    order_ids: list[str]
    clauses: list[ClauseIn]
    split_id: str  # 被拆分的条款（前段沿用其身份）
    offset: int  # 分界：已保存正文中的字符位置（按 Unicode 码位计）
    new_id: str  # 后段的新身份（本草稿从未使用过的 id）
    refs_to: str  # 原指向被拆条款的引用落到 "front"（前段）还是 "back"（后段）


class DiffIn(BaseModel):
    # 成功导入后保留的不可变原稿基线；两份文档均沿用现有草稿结构
    baseline: StateIn
    current: StateIn


class DuplicateIn(BaseModel):
    order_ids: list[str]
    clauses: list[ClauseIn]
    selected_ids: list[str]  # 要复制的条款身份；按当前显示顺序复制整组
    next_id: int  # 已用身份上界：副本新身份从 next_id + 1 起依次分配


class ReferrersIn(StateIn):
    target_id: str  # 被查询的条款稳定身份；不存在则拒绝（422）


class ExcerptIn(StateIn):
    selected_ids: list[str]  # 要收入摘录的条款身份；自动并入其直接、间接引用的全部现存条款


class ArchiveSnapshotIn(BaseModel):
    # 共享槽的完整快照：不可变原稿基线 + 当前草稿（顺序与内部正文）+ 已用身份上界；
    # 不含勾选、编辑中正文或拆分选择等临时状态
    baseline: StateIn
    order_ids: list[str]
    clauses: list[ClauseIn]
    id_counter: int  # 已用身份上界：须覆盖基线、现稿及失效引用中的身份号


class ArchivePutIn(BaseModel):
    version: int  # 最近读取或保存成功得到的版本；空槽为 0
    snapshot: ArchiveSnapshotIn


# --------------------------------------------------------------------------- #
# 通用解析
# --------------------------------------------------------------------------- #
def _stray_opens(text: str) -> list[int]:
    """找出没有构成合法 ``[[...]]`` 的 ``[[`` 位置（未闭合/嵌套等）。"""
    return [m.start() for m in OPEN_RE.finditer(text) if not MARKER_RE.match(text, m.start())]


def _stray_closes(text: str) -> list[int]:
    """找出不属于任何合法 ``[[...]]`` 的 ``]]`` 位置。"""
    close_starts = {m.end() - 2 for m in MARKER_RE.finditer(text)}
    return [m.start() for m in CLOSE_RE.finditer(text) if m.start() not in close_starts]


def scan_raw_refs(body: str) -> tuple[list[int], list[str]]:
    """扫描导入/编辑器正文中的显式引用。

    仅接受 ``[[数字]]``；其余 ``[[...]]`` 与游离的 ``[[`` 一律算格式错误。
    """
    refs: list[int] = []
    errors: list[str] = []
    for m in MARKER_RE.finditer(body):
        token = m.group(1)
        if NUM_TOKEN_RE.match(token):
            refs.append(int(token))
        else:
            errors.append(f"引用格式错误：{m.group(0)}（须写作 [[数字]]，中间不能有空格或其他字符）")
    if _stray_opens(body):
        errors.append("存在未正确闭合的 [[ 标记（引用须完整写作 [[数字]]）")
    if _stray_closes(body):
        errors.append("存在多余的 ]] 标记（引用须完整写作 [[数字]]）")
    return refs, errors


def parse_import(text: str) -> tuple[list[dict], list[str]]:
    """解析整份导入文本；任何错误都导致整份拒收。"""
    errors: list[str] = []
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    # 仅容忍粘贴时常见的结尾空行；中间/开头空行仍拒收
    while lines and lines[-1].strip() == "":
        lines.pop()

    n = len(lines)
    if not (MIN_LINES <= n <= MAX_LINES):
        return [], [f"行数需为 {MIN_LINES}～{MAX_LINES} 行“编号|正文”，当前为 {n} 行"]

    parsed: list[tuple[int, str, list[int], int]] = []
    for i, line in enumerate(lines, 1):
        if line.strip() == "":
            errors.append(f"第 {i} 行为空行，整份文本须为连续的编号条款")
            continue
        if "|" not in line:
            errors.append(f"第 {i} 行缺少分隔符“|”，格式须为“编号|正文”")
            continue
        head, body = line.split("|", 1)
        head = head.strip()
        if not head.isdigit():
            errors.append(f"第 {i} 行编号“{head or '（空）'}”不是正整数")
            continue
        num = int(head)
        if head != str(num):
            errors.append(f"第 {i} 行编号“{head}”含前导零，编号须写作 {num}")
        if body.strip() == "":
            errors.append(f"第 {i} 行正文为空")
        refs, ref_errors = scan_raw_refs(body)
        for err in ref_errors:
            errors.append(f"第 {i} 行：{err}")
        parsed.append((num, body, refs, i))

    # 重复编号
    seen: dict[int, list[int]] = {}
    for num, _, _, line_no in parsed:
        seen.setdefault(num, []).append(line_no)
    for num, idxs in seen.items():
        if len(idxs) > 1:
            errors.append(f"编号重复：{num} 同时出现在第 {'、'.join(map(str, idxs))} 行")

    got = {num for num, _, _, _ in parsed}
    expected = set(range(1, n + 1))
    for missing in sorted(expected - got):
        errors.append(f"编号未从 1 连续排列：缺少编号 {missing}")
    for extra in sorted(got - expected):
        errors.append(f"编号越界：{extra}（应分布在 1～{n}）")

    if errors:
        return [], errors

    # 引用目标检查（此时编号集合恰为 1..n）
    for num, body, refs, line_no in parsed:
        for ref in refs:
            if not (1 <= ref <= n):
                errors.append(f"第 {line_no} 行引用无目标：[[{ref}]]（有效编号为 1～{n}）")
    if errors:
        return [], errors

    parsed.sort(key=lambda item: item[0])
    clauses: list[dict] = []
    for num, body, refs, _ in parsed:
        internal = MARKER_RE.sub(lambda m: f"[[c{int(m.group(1))}]]", body)
        clauses.append(
            {
                "id": f"c{num}",
                "original": num,
                "body": internal,
                "ref_ids": [f"c{r}" for r in refs],
            }
        )
    return clauses, []


def _validate_state(order_ids: list[str], clauses: list[ClauseIn]) -> dict[str, ClauseIn]:
    """校验前端回传的文档状态，返回 id -> ClauseIn。"""
    by_id: dict[str, ClauseIn] = {}
    for clause in clauses:
        if not ID_RE.match(clause.id):
            raise HTTPException(422, detail={"errors": [f"内部条款 id 非法：{clause.id}"]})
        if clause.id in by_id:
            raise HTTPException(422, detail={"errors": [f"条款 {clause.id} 重复出现"]})
        by_id[clause.id] = clause
    if len(order_ids) != len(by_id) or set(order_ids) != set(by_id.keys()):
        raise HTTPException(422, detail={"errors": ["顺序列表与条款集合不一致"]})
    for clause in clauses:
        if clause.body.strip() == "":
            raise HTTPException(422, detail={"errors": [f"原第 {clause.id[1:]} 条正文为空"]})
        if "\n" in clause.body:
            raise HTTPException(422, detail={"errors": [f"原第 {clause.id[1:]} 条不是单行条款"]})
        for m in MARKER_RE.finditer(clause.body):
            if not ID_RE.match(m.group(1)):
                raise HTTPException(
                    422,
                    detail={"errors": [f"原第 {clause.id[1:]} 条内部引用形态损坏：{m.group(0)}"]},
                )
        if _stray_opens(clause.body) or _stray_closes(clause.body):
            raise HTTPException(422, detail={"errors": [f"原第 {clause.id[1:]} 条存在未闭合标记"]})
    return by_id


def resolve_body(body: str, pos_by_id: dict[str, int]) -> tuple[str, list[dict]]:
    """把内部正文解析为“当前显示编号”文本，并收集失效引用。"""
    out: list[str] = []
    broken: list[dict] = []
    pos = 0
    for m in MARKER_RE.finditer(body):
        out.append(body[pos : m.start()])
        token = m.group(1)
        if token in pos_by_id:
            out.append(f"[[{pos_by_id[token]}]]")
        else:
            original = int(ID_RE.match(token).group(1))  # 形态已在 _validate_state 保证
            broken.append({"ref_id": token, "original": original})
            out.append(f"[[原{original}?]]")
        pos = m.end()
    out.append(body[pos:])
    return "".join(out), broken


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
@app.post("/api/import")
def import_doc(payload: ImportIn):
    clauses, errors = parse_import(payload.text)
    if errors:
        raise HTTPException(422, detail={"errors": errors})
    return {"clauses": clauses}


@app.post("/api/save")
def save_clause(payload: SaveIn):
    """保存对某条正文的编辑：数字引用按**保存当刻**显示编号绑定。"""
    by_id = _validate_state(payload.order_ids, payload.clauses)
    if payload.edit_id not in by_id:
        raise HTTPException(422, detail={"errors": ["被编辑的条款不存在"]})

    text = payload.text.replace("\r\n", "\n").replace("\r", "\n")
    errors: list[str] = []
    if text.strip() == "":
        errors.append("正文不能为空")
    if "\n" in text:
        errors.append("每条条款只能为单行，请去掉换行")
    refs, ref_errors = scan_raw_refs(text)
    errors.extend(ref_errors)

    total = len(payload.order_ids)
    for ref in refs:
        if not (1 <= ref <= total):
            errors.append(f"引用无目标：[[{ref}]]（当前显示编号范围为 1～{total}）")
    if errors:
        raise HTTPException(422, detail={"errors": errors})

    def bind(m: re.Match) -> str:
        target_id = payload.order_ids[int(m.group(1)) - 1]
        return f"[[{target_id}]]"

    internal = MARKER_RE.sub(bind, text)
    return {"id": payload.edit_id, "body": internal}


@app.post("/api/merge")
def merge_clauses(payload: MergeIn):
    """合并两条条款。

    - ``keep_id`` 必须是当前显示顺序中较前的一条，合并结果占据它的位置并沿用其 id；
      ``drop_id`` 为较后一条，合并后移除；其余条款相对顺序不变。
    - 合并正文为 ``较前正文；较后正文``（全角分号“；”连接）。
    - 所有指向 ``drop_id`` 的引用（含两条待合并正文中的引用）改指 ``keep_id``；
      其他引用不动；已失效引用（目标不在草稿内）保持失效。
    """
    by_id = _validate_state(payload.order_ids, payload.clauses)
    errors: list[str] = []
    if payload.keep_id not in by_id:
        errors.append("合并目标条款不存在")
    if payload.drop_id not in by_id:
        errors.append("被合并条款不存在")
    if payload.keep_id == payload.drop_id and payload.keep_id in by_id:
        errors.append("必须选择两条不同的条款")
    if errors:
        raise HTTPException(422, detail={"errors": errors})

    keep_pos = payload.order_ids.index(payload.keep_id)
    drop_pos = payload.order_ids.index(payload.drop_id)
    if keep_pos > drop_pos:
        raise HTTPException(
            422,
            detail={
                "errors": [
                    f"合并方向有误：原第 {payload.keep_id[1:]} 条当前在第 {keep_pos + 1} 位，"
                    f"原第 {payload.drop_id[1:]} 条在第 {drop_pos + 1} 位，"
                    "须按当前显示顺序选择较前与较后的两条"
                ]
            },
        )

    def repoint(m: re.Match) -> str:
        return f"[[{payload.keep_id if m.group(1) == payload.drop_id else m.group(1)}]]"

    merged: list[dict] = []
    for clause in payload.clauses:
        body = clause.body
        if clause.id == payload.keep_id:
            body = f"{body}；{by_id[payload.drop_id].body}"
        if clause.id == payload.drop_id:
            continue
        merged.append({"id": clause.id, "body": MARKER_RE.sub(repoint, body)})

    new_order = [cid for cid in payload.order_ids if cid != payload.drop_id]
    return {"order_ids": new_order, "clauses": merged}


@app.post("/api/split")
def split_clause(payload: SplitIn):
    """拆分一条条款。

    - 在 ``offset`` 处把已保存正文切成前段 / 后段；分界不能切开引用标记，
      两段都不能只含空白，其余字符原样保留。
    - 前段沿用 ``split_id``，后段使用 ``new_id``；``new_id`` 不得与现有条款重复，
      也不得出现在任何正文的引用中（被删除/合并移除的身份不能复用，
      已有失效引用不能因此复活）。新条款在顺序表中紧跟前段，其余顺序不变。
    - 指向 ``split_id`` 的全部引用（含两段中的自引用）统一改指 ``refs_to``
      所选段；其余引用保持不动；已失效引用保持失效。
    """
    by_id = _validate_state(payload.order_ids, payload.clauses)
    errors: list[str] = []
    if payload.split_id not in by_id:
        errors.append("被拆分的条款不存在")
    if not ID_RE.match(payload.new_id):
        errors.append(f"后段新身份 id 非法：{payload.new_id}")
    elif payload.new_id in by_id:
        errors.append(f"后段新身份 {payload.new_id} 已被现有条款占用")
    if payload.refs_to not in ("front", "back"):
        errors.append("引用落点须为 front（前段）或 back（后段）")
    if len(payload.order_ids) + 1 > MAX_LINES:
        errors.append(
            f"拆分后共 {len(payload.order_ids) + 1} 条，超过上限 {MAX_LINES} 条，不能拆分"
        )
    if errors:
        raise HTTPException(422, detail={"errors": errors})

    # 新身份不得出现在任何正文的引用中：避免复用已删除/已合并移除的身份、复活失效引用
    for clause in payload.clauses:
        for m in MARKER_RE.finditer(clause.body):
            if m.group(1) == payload.new_id:
                raise HTTPException(
                    422,
                    detail={
                        "errors": [
                            f"后段新身份 {payload.new_id} 与既有引用冲突"
                            "（该身份在本草稿中已被使用过，不能复用）"
                        ]
                    },
                )

    body = by_id[payload.split_id].body
    offset = payload.offset
    if not (0 <= offset <= len(body)):
        raise HTTPException(
            422,
            detail={"errors": [f"分界位置 {offset} 超出正文范围（0～{len(body)}）"]},
        )
    for m in MARKER_RE.finditer(body):
        if m.start() < offset < m.end():
            raise HTTPException(
                422,
                detail={"errors": [f"分界不能切开引用标记 {m.group(0)}"]},
            )
    front, back = body[:offset], body[offset:]
    if front.strip() == "" or back.strip() == "":
        raise HTTPException(422, detail={"errors": ["拆分后的两段都不能只含空白字符"]})

    def repoint(m: re.Match) -> str:
        if payload.refs_to == "back" and m.group(1) == payload.split_id:
            return f"[[{payload.new_id}]]"
        return m.group(0)

    split: list[dict] = []
    for clause in payload.clauses:
        if clause.id == payload.split_id:
            split.append({"id": clause.id, "body": MARKER_RE.sub(repoint, front)})
            split.append({"id": payload.new_id, "body": MARKER_RE.sub(repoint, back)})
        else:
            split.append({"id": clause.id, "body": MARKER_RE.sub(repoint, clause.body)})

    new_order: list[str] = []
    for cid in payload.order_ids:
        new_order.append(cid)
        if cid == payload.split_id:
            new_order.append(payload.new_id)
    return {"order_ids": new_order, "clauses": split}


@app.post("/api/duplicate")
def duplicate_clauses(payload: DuplicateIn):
    """把所选一组条款复制为可独立编辑的副本。

    - 按当前显示顺序复制整组（与所选身份的传入顺序无关），副本整组插在所选
      最后一条之后；原条款的正文、身份、相对顺序与引用均不变。
    - 副本正文中指向组内条款的引用（含自引用）改指相应副本；指向组外条款的
      引用保留原目标；已失效引用（目标不在草稿内）保持失效。
    - 副本身份从 ``next_id + 1`` 起依次分配（与拆分共用同一身份上界）：
      不得与现有条款重复，也不得出现在任何正文的引用中（被删除/合并移除的
      身份不能复用，已有失效引用不能因此复活）。返回更新后的上界。
    """
    by_id = _validate_state(payload.order_ids, payload.clauses)
    errors: list[str] = []
    if not payload.selected_ids:
        errors.append("未选择要复制的条款")
    seen: set[str] = set()
    for sel in payload.selected_ids:
        if sel in seen:
            errors.append(f"所选条款重复：{sel}")
        seen.add(sel)
    for sel in payload.selected_ids:
        if sel not in by_id:
            errors.append(f"所选条款不存在：{sel}")
    if payload.next_id < 0:
        errors.append("身份上界非法：须为不小于 0 的整数")
    total = len(payload.order_ids) + len(payload.selected_ids)
    if total > MAX_LINES:
        errors.append(f"复制后共 {total} 条，超过上限 {MAX_LINES} 条，不能复制")
    if errors:
        raise HTTPException(422, detail={"errors": errors})

    # 按当前显示顺序确定复制顺序、身份分配与插入位置
    selected = [cid for cid in payload.order_ids if cid in seen]
    new_ids = [f"c{payload.next_id + i + 1}" for i in range(len(selected))]

    # 新身份不得与现有条款重复，也不得出现在任何正文的引用中：
    # 避免复用已删除/已合并移除的身份、复活失效引用（上界过期时拒绝）
    conflicts: list[str] = []
    for new_id in new_ids:
        if new_id in by_id:
            conflicts.append(f"副本新身份 {new_id} 已被现有条款占用（身份上界已过期）")
    fresh = set(new_ids)
    for clause in payload.clauses:
        for m in MARKER_RE.finditer(clause.body):
            if m.group(1) in fresh:
                conflicts.append(
                    f"副本新身份 {m.group(1)} 与既有引用冲突"
                    "（该身份在本草稿中已被使用过，不能复用）"
                )
    if conflicts:
        raise HTTPException(422, detail={"errors": conflicts})

    mapping = dict(zip(selected, new_ids))

    def repoint(m: re.Match) -> str:
        target = mapping.get(m.group(1))
        return f"[[{target}]]" if target else m.group(0)

    copies = [
        {"id": new_id, "body": MARKER_RE.sub(repoint, by_id[old_id].body)}
        for old_id, new_id in zip(selected, new_ids)
    ]
    last_selected = selected[-1]

    clauses: list[dict] = []
    for clause in payload.clauses:
        clauses.append({"id": clause.id, "body": clause.body})
        if clause.id == last_selected:
            clauses.extend(copies)

    new_order: list[str] = []
    for cid in payload.order_ids:
        new_order.append(cid)
        if cid == last_selected:
            new_order.extend(new_ids)

    return {
        "order_ids": new_order,
        "clauses": clauses,
        "next_id": payload.next_id + len(new_ids),
    }


@app.post("/api/preview")
def preview(payload: StateIn):
    by_id = _validate_state(payload.order_ids, payload.clauses)
    pos_by_id = {clause_id: i + 1 for i, clause_id in enumerate(payload.order_ids)}
    items = []
    has_broken = False
    for display, clause_id in enumerate(payload.order_ids, 1):
        clause = by_id[clause_id]
        text, broken = resolve_body(clause.body, pos_by_id)
        has_broken = has_broken or bool(broken)
        items.append(
            {
                "id": clause_id,
                "original": int(clause_id[1:]),
                "display": display,
                "text": text,
                "broken": broken,
            }
        )
    return {"items": items, "has_broken": has_broken}


@app.post("/api/export")
def export_doc(payload: StateIn):
    """修复所有失效引用后才返回纯文本；否则 409。"""
    by_id = _validate_state(payload.order_ids, payload.clauses)
    pos_by_id = {clause_id: i + 1 for i, clause_id in enumerate(payload.order_ids)}
    lines = []
    sources: list[str] = []
    for display, clause_id in enumerate(payload.order_ids, 1):
        clause = by_id[clause_id]
        text, broken = resolve_body(clause.body, pos_by_id)
        if broken:
            originals = "、".join(f"[[原{b['original']}?]]" for b in broken)
            sources.append(f"第 {display} 条（原第 {clause_id[1:]} 条）含失效引用 {originals}")
        lines.append(f"{display}|{text}")
    if sources:
        raise HTTPException(
            409,
            detail={"errors": ["存在失效引用，修复前禁止下载", *sources]},
        )
    return {"text": "\n".join(lines)}


# --------------------------------------------------------------------------- #
# 原稿对照
# --------------------------------------------------------------------------- #
def _moved_ids(base_order: list[str], cur_order: list[str]) -> set[str]:
    """找出发生“移动”的稳定身份。

    只看基线与现稿中都仍存在的身份：某条款若与任一其他共存条款的先后关系
    相对基线发生颠倒（现稿中在它之前的某条原在其后，或在它之后的某条原在其前），
    即为移动的参与方。删除 / 拆分插入造成的纯编号平移不改变共存条款的相对先后，
    不会被标记。
    """
    base_rank = {clause_id: i for i, clause_id in enumerate(base_order)}
    common = [clause_id for clause_id in cur_order if clause_id in base_rank]
    ranks = [base_rank[clause_id] for clause_id in common]
    n = len(ranks)
    moved: set[str] = set()
    if n < 2:
        return moved
    pref_max = [0] * n
    suff_min = [0] * n
    pref_max[0] = ranks[0]
    for i in range(1, n):
        pref_max[i] = max(pref_max[i - 1], ranks[i])
    suff_min[-1] = ranks[-1]
    for i in range(n - 2, -1, -1):
        suff_min[i] = min(suff_min[i + 1], ranks[i])
    for i, rank in enumerate(ranks):
        inverted_before = i > 0 and pref_max[i - 1] > rank
        inverted_after = i < n - 1 and suff_min[i + 1] < rank
        if inverted_before or inverted_after:
            moved.add(common[i])
    return moved


@app.post("/api/diff")
def diff_doc(payload: DiffIn):
    """对照不可变原稿基线与当前已保存草稿，只返回对照结果、不回退任何草稿。"""
    base = payload.baseline
    cur = payload.current
    base_by = _validate_state(base.order_ids, base.clauses)
    cur_by = _validate_state(cur.order_ids, cur.clauses)
    base_pos = {clause_id: i + 1 for i, clause_id in enumerate(base.order_ids)}
    cur_pos = {clause_id: i + 1 for i, clause_id in enumerate(cur.order_ids)}

    moved = _moved_ids(base.order_ids, cur.order_ids)
    base_set = set(base.order_ids)

    def entry(
        clause_id: str,
        tags: list[str],
        base_text: str | None,
        cur_text: str | None,
    ) -> dict:
        return {
            "id": clause_id,
            "original": int(clause_id[1:]),
            "baseline_no": base_pos.get(clause_id),
            "current_no": cur_pos.get(clause_id),
            "baseline_text": base_text,
            "current_text": cur_text,
            "tags": tags,
        }

    items: list[dict] = []
    # 现存项按当前顺序排列
    for clause_id in cur.order_ids:
        cur_text, _ = resolve_body(cur_by[clause_id].body, cur_pos)
        if clause_id not in base_set:
            items.append(entry(clause_id, ["added"], None, cur_text))
            continue
        base_text, _ = resolve_body(base_by[clause_id].body, base_pos)
        tags: list[str] = []
        if base_by[clause_id].body != cur_by[clause_id].body:
            tags.append("body_changed")  # 按内部引用绑定比较；仅显示编号变化不改正文
        if clause_id in moved:
            tags.append("moved")
        if tags:
            items.append(entry(clause_id, tags, base_text, cur_text))

    # 删除项随后按基线原顺序排列（合并记保留身份改动、另一身份删除）
    for clause_id in base.order_ids:
        if clause_id in cur_pos:
            continue
        base_text, _ = resolve_body(base_by[clause_id].body, base_pos)
        items.append(entry(clause_id, ["deleted"], base_text, None))

    # 正文未变、但引用由基线有效变为现稿失效：另列来源条款及位置（1 起，按 Unicode 码位）
    invalid_refs: list[dict] = []
    for clause_id in cur.order_ids:
        if clause_id not in base_set:
            continue
        base_body = base_by[clause_id].body
        cur_body = cur_by[clause_id].body
        if base_body != cur_body:
            continue
        cur_text, _ = resolve_body(cur_body, cur_pos)
        base_text, _ = resolve_body(base_body, base_pos)
        for m in MARKER_RE.finditer(cur_body):
            ref_id = m.group(1)
            if ref_id in base_pos and ref_id not in cur_pos:
                invalid_refs.append(
                    {
                        "source_id": clause_id,
                        "source_original": int(clause_id[1:]),
                        "baseline_no": base_pos[clause_id],
                        "current_no": cur_pos[clause_id],
                        "ref_id": ref_id,
                        "ref_original": int(ID_RE.match(ref_id).group(1)),
                        "position": m.start() + 1,
                        "position_end": m.end() + 1,
                        "baseline_text": base_text,
                        "current_text": cur_text,
                    }
                )

    return {"items": items, "invalid_refs": invalid_refs, "has_changes": bool(items or invalid_refs)}


# --------------------------------------------------------------------------- #
# 谁引用本条
# --------------------------------------------------------------------------- #
def _referrer_chains(
    order_ids: list[str], by_id: dict[str, ClauseIn], target_id: str
) -> dict[str, list[str]]:
    """在有效引用图上求每条现存条款到 ``target_id`` 的最短引用链。

    - 只把"目标仍在草稿内"的引用算作边（失效引用不参与遍历）；同一条款正文中的
      重复引用只构成一条边；自环允许（导入即允许自引用），但目标本身不作为来源。
    - 反向 BFS 给出最短距离；距离为 1 的来源即直接引用者，距离 ≥2 即仅经多级
      引用可达的间接引用者。环只在首次访问时扩展，重复边 / 循环不会造成重复或死循环。
    - 等长链按整条链的当前编号序列取字典序最小：从某来源出发的各条等长链只在
      第二跳（它直接引用的那条条款）上即可分出唯一胜负（当前编号互不相同），
      因此从"距离恰好少 1 的直接引用目标"中取当前编号最小者，再接该目标的最优链。
    """
    pos_by_id = {clause_id: i for i, clause_id in enumerate(order_ids)}
    valid = set(order_ids)

    # 正向 / 反向邻接（去重：重复引用只构成一条边）
    graph: dict[str, set[str]] = {clause_id: set() for clause_id in order_ids}
    reverse: dict[str, list[str]] = {clause_id: [] for clause_id in order_ids}
    for clause_id in order_ids:
        targets = {
            m.group(1)
            for m in MARKER_RE.finditer(by_id[clause_id].body)
            if m.group(1) in valid
        }
        graph[clause_id] = targets
        for target in targets:
            reverse[target].append(clause_id)

    # 反向 BFS：每个节点到目标的最短边数（自环 / 循环不会造成重复或死循环）
    dist: dict[str, int] = {target_id: 0}
    queue: deque[str] = deque([target_id])
    while queue:
        node = queue.popleft()
        for source in reverse.get(node, ()):
            if source not in dist:
                dist[source] = dist[node] + 1
                queue.append(source)

    # 按距离从小到大，为每个节点选取字典序最小的等长最短链
    best: dict[str, list[str]] = {target_id: [target_id]}
    for distance in range(1, max(dist.values(), default=0) + 1):
        nodes = sorted(
            (clause_id for clause_id, d in dist.items() if d == distance),
            key=lambda cid: pos_by_id[cid],
        )
        for clause_id in nodes:
            # 沿本条款的正向边，选距离恰好少 1（最短）且当前编号最小的下一跳
            next_hop = min(
                (
                    target
                    for target in graph[clause_id]
                    if dist.get(target) == distance - 1
                ),
                key=lambda cid: pos_by_id[cid],
            )
            best[clause_id] = [clause_id] + best[next_hop]
    return best


def _referrer_entry(chain: list[str], pos_by_id: dict[str, int]) -> dict:
    return {
        "id": chain[0],
        "original": int(chain[0][1:]),
        "current_no": pos_by_id[chain[0]] + 1,
        # 从来源到目标的最短引用链：当前编号序列
        "chain": [pos_by_id[clause_id] + 1 for clause_id in chain],
        "chain_ids": list(chain),
    }


@app.post("/api/referrers")
def referrers(payload: ReferrersIn):
    """返回目标条款的直接 / 间接引用者；只读取草稿，不改正文、顺序或引用绑定。"""
    by_id = _validate_state(payload.order_ids, payload.clauses)
    if payload.target_id not in by_id:
        raise HTTPException(422, detail={"errors": [f"目标条款不存在：{payload.target_id}"]})

    best = _referrer_chains(payload.order_ids, by_id, payload.target_id)
    pos_by_id = {clause_id: i for i, clause_id in enumerate(payload.order_ids)}

    direct: list[dict] = []
    indirect: list[dict] = []
    # 结果按来源当前顺序排列（目标本身距离为 0，不列入）
    for clause_id in payload.order_ids:
        chain = best.get(clause_id)
        if chain is None or len(chain) < 2:
            continue
        entry = _referrer_entry(chain, pos_by_id)
        (direct if len(chain) == 2 else indirect).append(entry)

    target_no = pos_by_id[payload.target_id] + 1
    return {
        "target_id": payload.target_id,
        "target_original": int(payload.target_id[1:]),
        "target_no": target_no,
        "direct": direct,
        "indirect": indirect,
    }


# --------------------------------------------------------------------------- #
# 勾选条款摘录
# --------------------------------------------------------------------------- #
@app.post("/api/excerpt")
def excerpt_doc(payload: ExcerptIn):
    """把所选条款及其直接、间接引用的全部现存条款导出为独立编号的摘录。

    - 收录范围：``selected_ids`` 及沿有效引用（目标仍在草稿内）可达的全部条款；
      自引用、循环与重复引用只收录一次。只读接口，不改草稿、基线、身份上界或引用绑定。
    - 摘录按草稿当前顺序从 1 连续编号；正文中的引用改用摘录内编号，其余字符不变。
    - 收录范围内存在失效引用（目标已不在草稿）时整份拒绝（409），列出来源稳定身份
      与缺失目标；范围外的失效引用不阻止摘录，也不影响整稿导出的既有规则。
    """
    by_id = _validate_state(payload.order_ids, payload.clauses)
    errors: list[str] = []
    if not payload.selected_ids:
        errors.append("未选择要导出摘录的条款")
    seen: set[str] = set()
    for sel in payload.selected_ids:
        if sel in seen:
            errors.append(f"所选条款重复：{sel}")
        seen.add(sel)
    for sel in payload.selected_ids:
        if sel not in by_id:
            errors.append(f"所选条款不存在：{sel}")
    if errors:
        raise HTTPException(422, detail={"errors": errors})

    # 收录闭包：沿有效引用遍历（失效引用的目标不在草稿内，无法跟随，随后单独检查）；
    # included 集合保证自引用、循环与重复引用只收录一次
    included: set[str] = set()
    queue: deque[str] = deque(payload.selected_ids)
    while queue:
        clause_id = queue.popleft()
        if clause_id in included:
            continue
        included.add(clause_id)
        for m in MARKER_RE.finditer(by_id[clause_id].body):
            target = m.group(1)
            if target in by_id and target not in included:
                queue.append(target)

    pos_by_id = {clause_id: i + 1 for i, clause_id in enumerate(payload.order_ids)}

    # 收录范围内的失效引用：整份拒绝，列出来源稳定身份与缺失目标
    broken: list[dict] = []
    for clause_id in payload.order_ids:
        if clause_id not in included:
            continue
        for m in MARKER_RE.finditer(by_id[clause_id].body):
            target = m.group(1)
            if target not in by_id:
                broken.append(
                    {
                        "source_id": clause_id,
                        "source_no": pos_by_id[clause_id],
                        "ref_id": target,
                        "ref_original": int(ID_RE.match(target).group(1)),  # 形态已由 _validate_state 保证
                    }
                )
    if broken:
        sources = [
            f"来源 {b['source_id']}（草稿第 {b['source_no']} 条）含失效引用 "
            f"[[原{b['ref_original']}?]]，缺失目标 {b['ref_id']}"
            for b in broken
        ]
        raise HTTPException(
            409,
            detail={
                "errors": ["摘录范围内存在失效引用，整份摘录拒绝导出，请先修复：", *sources],
                "broken": broken,
            },
        )

    # 按草稿当前顺序从 1 连续编号；引用改用摘录内编号，其余正文字符不变
    ordered = [clause_id for clause_id in payload.order_ids if clause_id in included]
    excerpt_no = {clause_id: i + 1 for i, clause_id in enumerate(ordered)}

    def renumber(m: re.Match) -> str:
        # 收录条款的每个引用：有效引用目标必在闭包内，失效引用已在上面整份拒绝
        return f"[[{excerpt_no[m.group(1)]}]]"

    items: list[dict] = []
    lines: list[str] = []
    for clause_id in ordered:
        no = excerpt_no[clause_id]
        lines.append(f"{no}|{MARKER_RE.sub(renumber, by_id[clause_id].body)}")
        items.append(
            {
                "id": clause_id,
                "original": int(clause_id[1:]),
                "draft_no": pos_by_id[clause_id],
                "excerpt_no": no,
            }
        )
    return {"text": "\n".join(lines), "items": items}


# --------------------------------------------------------------------------- #
# 校稿存档（SQLite 单槽持久化）
# --------------------------------------------------------------------------- #
# 数据库路径：默认工作目录下 data/archive.db（容器内为 /app/data，由 Compose 数据卷持久化）；
# 可用环境变量 ARCHIVE_DB_PATH 覆盖（测试借此隔离）
DB_PATH = Path(os.environ.get("ARCHIVE_DB_PATH", "data/archive.db"))


def _connect() -> sqlite3.Connection:
    """打开存档数据库连接；目录与表不存在时自动创建（空库即空槽）。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS archive (
            slot INTEGER PRIMARY KEY CHECK (slot = 1),
            version INTEGER NOT NULL,
            snapshot TEXT NOT NULL
        )
        """
    )
    return conn


def _validate_archive_snapshot(snap: ArchiveSnapshotIn) -> None:
    """校验存档快照：基线与现稿沿用草稿校验，身份上界覆盖全部已用身份号。"""
    _validate_state(snap.baseline.order_ids, snap.baseline.clauses)
    _validate_state(snap.order_ids, snap.clauses)
    if snap.id_counter < 0:
        raise HTTPException(422, detail={"errors": ["身份上界非法：须为不小于 0 的整数"]})
    # 已用身份号 = 基线与现稿的条款身份 + 两份正文中全部引用（含失效引用）的身份；
    # 上界必须覆盖它们，恢复后继续拆分 / 复制才不会复用旧身份、复活失效引用
    used = 0
    for clause_id in [
        *snap.order_ids,
        *snap.baseline.order_ids,
        *(c.id for c in snap.clauses),
        *(c.id for c in snap.baseline.clauses),
    ]:
        used = max(used, int(clause_id[1:]))  # 形态已由 _validate_state 保证
    for clause in [*snap.clauses, *snap.baseline.clauses]:
        for m in MARKER_RE.finditer(clause.body):
            used = max(used, int(m.group(1)[1:]))
    if snap.id_counter < used:
        raise HTTPException(
            422,
            detail={
                "errors": [
                    f"身份上界 {snap.id_counter} 未覆盖草稿已用身份号 {used}"
                    "（基线、现稿及失效引用中的身份均须覆盖）"
                ]
            },
        )


@app.get("/api/archive")
def read_archive():
    """读取共享槽存档：返回版本与完整快照；空槽版本为 0、快照为 null。"""
    try:
        conn = _connect()
        try:
            row = conn.execute("SELECT version, snapshot FROM archive WHERE slot = 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise HTTPException(500, detail={"errors": [f"读取存档失败：{exc}"]})
    if row is None:
        return {"version": 0, "snapshot": None}
    return {"version": row[0], "snapshot": json.loads(row[1])}


@app.put("/api/archive")
def write_archive(payload: ArchivePutIn):
    """整份写入共享槽存档（乐观并发）。

    - 快照先校验：基线与现稿沿用草稿校验，身份上界须覆盖基线、现稿及失效引用中的
      身份号；非法快照 422，旧存档保留。
    - 仅当所携版本与槽内当前版本一致时整份写入并加 1；不一致返回 409（附当前版本），
      旧存档保留。版本检查与写入在同一事务（BEGIN IMMEDIATE）内完成，并发保存只有
      一个能推进版本。
    - 写入失败（如磁盘错误）回滚事务，旧存档保留。
    """
    if payload.version < 0:
        raise HTTPException(422, detail={"errors": ["版本号非法：须为不小于 0 的整数"]})
    _validate_archive_snapshot(payload.snapshot)
    snapshot_json = json.dumps(payload.snapshot.model_dump(), ensure_ascii=False)
    try:
        conn = _connect()
    except sqlite3.Error as exc:
        raise HTTPException(500, detail={"errors": [f"打开存档数据库失败，旧存档已保留：{exc}"]})
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT version FROM archive WHERE slot = 1").fetchone()
        current = row[0] if row else 0
        if current != payload.version:
            conn.rollback()
            raise HTTPException(
                409,
                detail={
                    "errors": [
                        f"存档版本冲突：服务端当前版本为 {current}，本次保存基于版本 "
                        f"{payload.version}；旧存档已保留，请读取最新版本后再保存"
                    ],
                    "version": current,
                },
            )
        conn.execute(
            "INSERT INTO archive (slot, version, snapshot) VALUES (1, ?, ?) "
            "ON CONFLICT(slot) DO UPDATE SET version = excluded.version, snapshot = excluded.snapshot",
            (current + 1, snapshot_json),
        )
        conn.commit()
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(500, detail={"errors": [f"写入存档失败，旧存档已保留：{exc}"]})
    finally:
        conn.close()
    return {"version": current + 1}


# --------------------------------------------------------------------------- #
# 静态页面
# --------------------------------------------------------------------------- #
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
