"""执行内核：对一组文件版本与删除操作做纯函数式评估。

内核不碰 sqlite / 文件系统 / HTTP，输入全部由调用方组装，输出是
:class:`ScanReport`，可直接逐行复核。

可见性模型（与 Iceberg/Hudi 读时删除的共通子集）：

* 每个删除操作在注册时分配一个**全局单调序列号 seq**，它同时是
  该删除的可见性序列号。
* 每行带 ``insert_seq``：它进入本表的序列号。
* 等值删除命中条件（两者都满足才删除）::

      键值 NULL 规则命中（见 predicates.py）
      且 row.insert_seq <= op.seq      # 删除时该行已经存在

  即删除只能作用于"删除发生时已可见"的行；后插入的同键行天然在序列
  可见范围之外，必须保留（先删后插场景）。

* 位置删除命中条件：文件当前版本 == 操作绑定版本（内容身份一致）
  且行号在当前文件范围内。重写产生新版本后，旧操作一律失效——
  即使新文件里同一行号恰好还有另一行，也绝不复用旧行号。
"""
from __future__ import annotations

from typing import Callable, Mapping, Sequence

from . import models as m
from .predicates import predicate_has_null, row_key_has_null, row_matches

TraceFn = Callable[[dict], None]


def evaluate_table(
    files: Sequence[m.FileData],
    ops: Sequence[m.DeleteOp],
    key_columns: Sequence[str],
    bound_row_counts: Mapping[tuple[str, int], int] | None = None,
    survived_coords: frozenset[tuple[str, int, int]] | None = None,
    seq_horizon: int | None = None,
    trace: TraceFn | None = None,
) -> m.ScanReport:
    """对全部 live 文件应用全部删除操作，返回逐行结论。

    参数
    ----
    files:
        当前 live 文件版本（每个 file_id 至多一个）。
    ops:
        已注册的删除操作（含 seq）。
    key_columns:
        表主键列（等值删除的键空间）。
    bound_row_counts:
        仅供失效位置删除细分原因：(file_id, bound_version) -> 该版本行数。
    survived_coords:
        失效细分：(file_id, bound_version, row_number) 若作为父坐标
        出现在某个当前 live 重写产物的血缘中，说明该行幸存但换了内容身份。
    seq_horizon:
        扫描序列号水位（默认取 ops 最大 seq）。
    trace:
        可选回调，接收每个关键中间判断，供测试日志重放。
    """
    key_columns = tuple(key_columns)
    bound_row_counts = bound_row_counts or {}
    survived_coords = survived_coords or frozenset()

    position_ops = sorted((o for o in ops if o.kind == m.POSITION), key=lambda o: o.seq)
    equality_ops = sorted((o for o in ops if o.kind == m.EQUALITY), key=lambda o: o.seq)

    # 当前 live 版本号，用于识别"操作绑定的文件内容是否仍是当前内容"。
    live_versions: dict[str, int] = {f.file_id: f.version for f in files}

    # 有效的位置删除：(file_id, row_number) -> 最早 seq 的操作
    live_pos: dict[tuple[str, int], m.DeleteOp] = {}
    stale_pos: list[m.DeleteOp] = []
    for op in position_ops:
        assert op.file_id is not None and op.row_number is not None
        assert op.bound_version is not None
        if live_versions.get(op.file_id) == op.bound_version:
            live_pos.setdefault((op.file_id, op.row_number), op)
        else:
            stale_pos.append(op)

    def emit(event: dict) -> None:
        if trace is not None:
            trace(event)

    # ---- 逐行判定 ----
    verdicts: list[m.RowVerdict] = []
    equality_hits: dict[str, list[tuple[str, int]]] = {o.delete_id: [] for o in equality_ops}

    for f in sorted(files, key=lambda x: x.file_id):
        for rn, row in enumerate(f.rows):
            pos_op = live_pos.get((f.file_id, rn))
            verdict: m.RowVerdict | None = None

            # 1) 位置删除优先（绑定的就是这份内容、这个物理行号）
            if pos_op is not None:
                verdict = m.RowVerdict(
                    file_id=f.file_id, row_number=rn, insert_seq=row.insert_seq,
                    action=m.DELETE, reason=m.DEL_POSITION,
                    by_delete_id=pos_op.delete_id, by_seq=pos_op.seq,
                    values=dict(row.values),
                )

            # 2) 等值删除：按 seq 顺序找，记录最强的命中证据
            if verdict is None:
                first_inscope: m.DeleteOp | None = None
                first_outscope: m.DeleteOp | None = None
                for op in equality_ops:
                    if not row_matches(row, op.key_columns, op.key_values):
                        continue
                    # row_matches 已排除两侧 NULL；此处只剩值相等
                    if row.insert_seq <= op.seq:
                        first_inscope = op
                        break
                    if first_outscope is None:
                        first_outscope = op

                if first_inscope is not None:
                    verdict = m.RowVerdict(
                        file_id=f.file_id, row_number=rn, insert_seq=row.insert_seq,
                        action=m.DELETE, reason=m.DEL_EQUALITY,
                        by_delete_id=first_inscope.delete_id, by_seq=first_inscope.seq,
                        values=dict(row.values),
                    )
                    equality_hits[first_inscope.delete_id].append((f.file_id, rn))
                else:
                    keep_reason = _keep_reason(row, equality_ops, key_columns, first_outscope)
                    verdict = m.RowVerdict(
                        file_id=f.file_id, row_number=rn, insert_seq=row.insert_seq,
                        action=m.KEEP, reason=keep_reason,
                        by_delete_id=(first_outscope.delete_id if first_outscope else None),
                        by_seq=(first_outscope.seq if first_outscope else None),
                        values=dict(row.values),
                    )
                    if first_outscope is not None:
                        emit({
                            "stage": "out_of_scope_keep",
                            "file_id": f.file_id, "row_number": rn,
                            "insert_seq": row.insert_seq,
                            "delete_id": first_outscope.delete_id,
                            "delete_seq": first_outscope.seq,
                            "rule": "insert_seq > delete_seq，删除时该行尚不存在",
                        })

            emit({
                "stage": "row_verdict", "file_id": f.file_id,
                "row_number": rn, "insert_seq": row.insert_seq,
                "action": verdict.action, "reason": verdict.reason,
                "by_delete_id": verdict.by_delete_id, "by_seq": verdict.by_seq,
            })
            verdicts.append(verdict)

    # ---- 操作级评估 ----
    evaluations: list[m.OpEvaluation] = []
    for op in equality_ops:
        if op.has_null_predicate:
            # WHERE k = NULL 恒为 UNKNOWN：注册成功，但结构上不可能命中任何行
            status = m.OP_APPLIED_ZERO
            emit({"stage": "op_eval", "delete_id": op.delete_id,
                  "status": status, "rule": "谓词含 NULL，按三值逻辑不命中任何行"})
        else:
            hits = equality_hits[op.delete_id]
            status = m.OP_APPLIED if hits else m.OP_APPLIED_ZERO
            emit({"stage": "op_eval", "delete_id": op.delete_id, "status": status,
                  "matched": hits, "seq": op.seq})
        evaluations.append(m.OpEvaluation(
            delete_id=op.delete_id, kind=m.EQUALITY, seq=op.seq,
            status=status, matched_rows=equality_hits[op.delete_id],
        ))

    for op in position_ops:
        if op in stale_pos:
            status = _classify_stale(
                op, live_versions, bound_row_counts, survived_coords, emit,
            )
            evaluations.append(m.OpEvaluation(
                delete_id=op.delete_id, kind=m.POSITION, seq=op.seq, status=status,
            ))
        else:
            # 同一坐标上多个位置删除时，只有最早 seq 的操作者真正认领该行；
            # 其余操作结构有效但归因命中为 0（行已经记在更早的操作名下）。
            winner = live_pos.get((op.file_id, op.row_number))  # type: ignore[arg-type]
            if winner is op:
                evaluations.append(m.OpEvaluation(
                    delete_id=op.delete_id, kind=m.POSITION, seq=op.seq,
                    status=m.OP_APPLIED,
                    matched_rows=[(op.file_id, op.row_number)],  # type: ignore[arg-type]
                ))
            else:
                evaluations.append(m.OpEvaluation(
                    delete_id=op.delete_id, kind=m.POSITION, seq=op.seq,
                    status=m.OP_APPLIED_ZERO,
                ))

    # 稳定输出：操作按 seq；verdict 已按 file/row 有序
    evaluations.sort(key=lambda e: e.seq)
    horizon = seq_horizon if seq_horizon is not None else max((o.seq for o in ops), default=0)
    return m.ScanReport(
        table_id="",  # 由调用方补
        seq_horizon=horizon,
        verdicts=verdicts,
        op_evaluations=evaluations,
        files=dict(sorted(live_versions.items())),
    )


def _keep_reason(
    row: m.Row,
    equality_ops: Sequence[m.DeleteOp],
    key_columns: Sequence[str],
    outscope: m.DeleteOp | None,
) -> str:
    """决定保留行的具体依据。

    优先级（越界证据最具体，优先给出）：
    1. 越界保留：有键值相等的谓词但行晚于它插入；
    2. 行键 NULL：该行对所有谓词求值为 UNKNOWN，不可能被等值删除；
    3. 谓词全 NULL：行键非 NULL，且唯一的谓词都是 ``k = NULL``
       （恒为 UNKNOWN），行完全因此存活；
    4. no_match：存在取值不等的非 NULL 谓词（此时同批是否夹带 NULL
       谓词不影响该行——NULL 谓词对非 NULL 行本就不适用）。
    """
    if outscope is not None:
        return m.KEEP_INSCOPE_INSERT
    if not equality_ops:
        return m.KEEP_NO_MATCH
    if row_key_has_null(row, key_columns):
        return m.KEEP_NULL_ROW_BLOCKED
    if all(predicate_has_null(o.key_values) for o in equality_ops):
        return m.KEEP_NULL_KEY_BLOCKED
    return m.KEEP_NO_MATCH


def _classify_stale(
    op: m.DeleteOp,
    live_versions: Mapping[str, int],
    bound_row_counts: Mapping[tuple[str, int], int],
    survived_coords: frozenset[tuple[str, int, int]],
    emit: TraceFn,
) -> str:
    """失效位置删除的细分：

    * stale_row_already_removed：旧版本该行号曾存在，但该行未幸存进任何
      当前 live 重写产物（它当时已经被删除）；
    * stale_file_rewritten：该行作为父坐标幸存进了某个当前 live 重写产物
      （内容身份已换，旧行号随旧文件失效，绝不复用）；或旧文件元数据缺失；
    * stale_out_of_range：旧版本本来就没有该行号。
    """
    fid, rn, bv = op.file_id, op.row_number, op.bound_version
    old_count = bound_row_counts.get((fid, bv))  # type: ignore[arg-type]
    if old_count is not None and rn >= old_count:
        # 绑定的旧版本本来就没有该行号
        status = m.STALE_OUT_OF_RANGE
    elif (fid, bv, rn) not in survived_coords:
        # 绑定版本里该行曾存在，但从未作为幸存内容被任何重写携带：
        # 它在使文件退出 live 的重写之前就已经被删除
        status = m.STALE_ROW_REMOVED
    else:
        # 该行曾被重写携带，内容身份已经更替，旧行号绑定随之失效
        status = m.STALE_REWRITTEN
    emit({"stage": "stale_classify", "delete_id": op.delete_id, "file_id": fid,
          "row_number": rn, "bound_version": bv,
          "live_version": live_versions.get(fid), "status": status})
    return status
