"""格式适配: 数据集目录布局、Parquet 行组读取、内嵌统计与声明统计。

数据集目录布局 (每个目录 = 一个数据集)::

    <dataset>/
      manifest.json   # 文件/行组/逻辑页切分与列逻辑类型
      claims.json     # *声明的* 页级与行组级统计 (可能缺失/错误/截断)
      data.parquet    # PyArrow 写入的真实数据 (可含多个行组)
      ground_truth.csv  # 独立真值 (测试夹具携带, 生产数据无此文件)

"逻辑页" 是行组内按 manifest 声明的连续行切片; 这样审计内核无需依赖
Parquet 编码页 (data page v1/v2) 的页统计暴露程度, 又能真实地校验
"页 -> 行组" 的聚合关系。数据读取始终来自 Parquet 本体。
"""
from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .logical import LogicalType, classify, is_nan, is_signed_zero
from .stats import ColumnStats

MANIFEST_NAME = "manifest.json"
CLAIMS_NAME = "claims.json"


# ---------------------------------------------------------------------------
# 结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ColumnInfo:
    name: str
    logical_type: LogicalType
    sensitive: bool = False


@dataclass(frozen=True)
class PageInfo:
    index: int
    row_count: int


@dataclass(frozen=True)
class RowGroupInfo:
    index: int
    row_count: int
    pages: tuple[PageInfo, ...]


@dataclass(frozen=True)
class FileInfo:
    file: str
    row_groups: tuple[RowGroupInfo, ...]


@dataclass
class Dataset:
    name: str
    root: Path
    columns: tuple[ColumnInfo, ...]
    files: tuple[FileInfo, ...]
    #: claims.json 解析结果; 缺失统计用 None 表达 ("无统计")
    claims: dict[str, Any]

    def column(self, name: str) -> ColumnInfo:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(f"数据集 {self.name} 中不存在列 {name!r}")

    @property
    def sensitive_columns(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.sensitive)

    # ---- claims 访问 ------------------------------------------------------
    def _file_claims(self, file_name: str) -> dict[str, Any] | None:
        for fc in self.claims.get("files", []):
            if fc.get("file") == file_name:
                return fc
        return None

    def _rg_claims(self, file_name: str, rg_index: int) -> dict[str, Any] | None:
        fc = self._file_claims(file_name)
        if fc is None:
            return None
        for rg in fc.get("row_groups", []):
            if rg.get("index") == rg_index:
                return rg
        return None

    def claimed_page_stats(
        self, file_name: str, rg_index: int, page_index: int, column: str
    ) -> ColumnStats | None:
        """声明的页级统计; 无 claims/页/列任何一层缺失即 None。"""
        rg = self._rg_claims(file_name, rg_index)
        if rg is None:
            return None
        for page in rg.get("pages", []):
            if page.get("index") == page_index:
                raw = page.get("stats", {}).get(column)
                return ColumnStats.from_json(raw) if raw else None
        return None

    def claimed_row_group_stats(
        self, file_name: str, rg_index: int, column: str
    ) -> ColumnStats | None:
        rg = self._rg_claims(file_name, rg_index)
        if rg is None:
            return None
        raw = rg.get("stats", {}).get(column)
        return ColumnStats.from_json(raw) if raw else None


# ---------------------------------------------------------------------------
# 加载与校验
# ---------------------------------------------------------------------------
def load_dataset(root: str | Path) -> Dataset:
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"数据集目录不存在: {root}")
    manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    if manifest.get("version") != 1:
        raise ValueError(f"不支持的 manifest 版本: {manifest.get('version')!r}")

    columns = tuple(
        ColumnInfo(
            name=c["name"],
            logical_type=LogicalType(c["logical_type"]),
            sensitive=bool(c.get("sensitive", False)),
        )
        for c in manifest["columns"]
    )
    files = tuple(_parse_file(f) for f in manifest["files"])
    _validate_layout(root, columns, files)

    claims_path = root / CLAIMS_NAME
    if claims_path.exists():
        claims = json.loads(claims_path.read_text(encoding="utf-8"))
        if claims.get("version", 1) != 1:
            raise ValueError(
                f"不支持的 claims 版本: {claims.get('version')!r}"
            )
    else:
        claims = {"version": 1, "files": []}

    return Dataset(
        name=manifest.get("name", root.name),
        root=root,
        columns=columns,
        files=files,
        claims=claims,
    )


def _parse_file(raw: dict[str, Any]) -> FileInfo:
    rgs = []
    for rg in raw["row_groups"]:
        pages = tuple(
            PageInfo(index=p["index"], row_count=int(p["row_count"]))
            for p in rg.get("pages", [])
        )
        rgs.append(
            RowGroupInfo(
                index=int(rg["index"]),
                row_count=int(rg["row_count"]),
                pages=pages,
            )
        )
    return FileInfo(file=raw["file"], row_groups=tuple(rgs))


def _validate_layout(
    root: Path, columns: tuple[ColumnInfo, ...], files: tuple[FileInfo, ...]
) -> None:
    names = [c.name for c in columns]
    if len(names) != len(set(names)):
        raise ValueError("manifest 列名重复")
    for fi in files:
        path = root / fi.file
        if not path.exists():
            raise FileNotFoundError(f"manifest 引用的数据文件缺失: {path}")
        pf = pq.ParquetFile(path)
        if len(fi.row_groups) != pf.num_row_groups:
            raise ValueError(
                f"{fi.file}: manifest 行组数 {len(fi.row_groups)} "
                f"!= Parquet 行组数 {pf.num_row_groups}"
            )
        arrow_schema = pf.schema_arrow
        for col in columns:
            if col.name not in arrow_schema.names:
                raise ValueError(f"{fi.file}: Parquet 中缺少列 {col.name}")
            physical = classify(arrow_schema.field(col.name).type)
            if physical is not col.logical_type:
                raise ValueError(
                    f"{fi.file}.{col.name}: manifest 逻辑类型 "
                    f"{col.logical_type.value} 与物理类型映射 "
                    f"{physical.value} 不一致"
                )
        for i, rg in enumerate(fi.row_groups):
            rg_meta = pf.metadata.row_group(i)
            if rg.row_count != rg_meta.num_rows:
                raise ValueError(
                    f"{fi.file} 行组 {rg.index}: manifest 行数 "
                    f"{rg.row_count} != Parquet {rg_meta.num_rows}"
                )
            page_total = sum(p.row_count for p in rg.pages)
            if page_total != rg.row_count:
                raise ValueError(
                    f"{fi.file} 行组 {rg.index}: 逻辑页行数和 "
                    f"{page_total} != 行组行数 {rg.row_count}"
                )
            indices = [p.index for p in rg.pages]
            if indices != list(range(len(rg.pages))):
                raise ValueError(
                    f"{fi.file} 行组 {rg.index}: 逻辑页 index 必须从 0 连续编号"
                )


# ---------------------------------------------------------------------------
# 数据读取 (事实来源)
# ---------------------------------------------------------------------------
def _parquet_file(ds: Dataset, file_name: str) -> pq.ParquetFile:
    return pq.ParquetFile(ds.root / file_name)


def _column_ordinal(pf: pq.ParquetFile, column: str) -> int:
    return pf.schema_arrow.names.index(column)


def read_row_group_values(
    ds: Dataset, file_name: str, rg_index: int, column: str
) -> list[Any]:
    """读取整行组的一列, 转为 Python 值 (NULL->None, NaN->float nan)。"""
    pf = _parquet_file(ds, file_name)
    table = pf.read_row_group(rg_index, columns=[column])
    return table.column(0).to_pylist()


def read_page_values(
    ds: Dataset,
    file_name: str,
    rg_index: int,
    page_index: int,
    column: str,
) -> list[Any]:
    """读取逻辑页 (行组内连续切片) 的一列。"""
    fi = _get_file(ds, file_name)
    rg = fi.row_groups[rg_index]
    start = sum(p.row_count for p in rg.pages[:page_index])
    end = start + rg.pages[page_index].row_count
    return read_row_group_values(ds, file_name, rg_index, column)[start:end]


def iter_file_pages(ds: Dataset, fi: FileInfo):
    for rg in fi.row_groups:
        for page in rg.pages:
            yield rg.index, page.index


def _get_file(ds: Dataset, file_name: str) -> FileInfo:
    for fi in ds.files:
        if fi.file == file_name:
            return fi
    raise KeyError(file_name)


# ---------------------------------------------------------------------------
# Parquet 内嵌行组统计 (第二个独立来源)
# ---------------------------------------------------------------------------
def embedded_row_group_stats(
    ds: Dataset, file_name: str, rg_index: int, column: str
) -> ColumnStats | None:
    """读取 Parquet footer 内嵌的行组统计; 无内嵌统计时返回 None。

    Parquet 内嵌统计无法表达截断标志、页级排序、有符号零区分,
    这些字段保持默认; 调用方只交叉校验其能表达的计数/端点。
    """
    col = ds.column(column)
    pf = _parquet_file(ds, file_name)
    ordinal = _column_ordinal(pf, column)
    chunk = pf.metadata.row_group(rg_index).column(ordinal)
    st = chunk.stats
    if st is None:
        return None
    out = ColumnStats(logical_type=col.logical_type)
    out.count = chunk.num_values + (st.null_count or 0)
    if st.has_null_count:
        out.null_count = st.null_count
    else:
        return None
    if st.has_min_max:
        out.min = _physical_to_py(st.min, col.logical_type)
        out.max = _physical_to_py(st.max, col.logical_type)
        # 写入端可能把 NaN 计入 min/max; 含 NaN 的端点交叉校验不可靠,
        # 交给审计内核按"内嵌无法判定"处理。
        if is_nan(out.min) or is_nan(out.max):
            out.min = None
            out.max = None
    else:
        return None
    return out


def _physical_to_py(value: Any, logical: LogicalType) -> Any:
    if value is None:
        return None
    scalar = pa.scalar(value)
    py = scalar.as_py()
    if logical is LogicalType.FLOAT:
        py = float(py)
        if is_signed_zero(py):
            # 规范化 +0.0/-0.0 -> +0.0: 内嵌来源不区分有符号零
            py = py + 0.0
    if isinstance(py, _dt.datetime):  # 时间戳不在支持类型内, 防御
        raise TypeError(f"不支持的端点类型: {type(py)}")
    return py
