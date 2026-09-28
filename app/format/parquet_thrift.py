"""Parquet Thrift metadata struct definitions (subset of parquet.thrift).

Field ids and numeric enum values follow
https://github.com/apache/parquet-format/blob/master/src/main/thrift/parquet.thrift
Only the fields needed to write/read uncompressed v1 data pages with the
restricted logical surface are described; unknown fields are skipped by the
compact reader (forward compatibility with files produced by PyArrow).
"""
from __future__ import annotations

from .thrift import (
    Field, ListOf, StructOf, StructSpec,
    T_BINARY, T_BOOLEAN_TRUE, T_I32, T_I64,
)

# --------------------------------------------------------------------------- enums
Type_BOOLEAN = 0
Type_INT32 = 1
Type_INT64 = 2
Type_INT96 = 3
Type_FLOAT = 4
Type_DOUBLE = 5
Type_BYTE_ARRAY = 6
Type_FIXED_LEN_BYTE_ARRAY = 7

# FieldRepetitionType
FieldRepetitionType_REQUIRED = 0
FieldRepetitionType_OPTIONAL = 1
FieldRepetitionType_REPEATED = 2

# Encoding
Encoding_PLAIN = 0
Encoding_RLE = 3
Encoding_BIT_PACKED = 4  # deprecated, v1 def/rep levels may still claim it
Encoding_RLE_DICTIONARY = 8

# CompressionCodec
CompressionCodec_UNCOMPRESSED = 0
CompressionCodec_SNAPPY = 1

# PageType
PageType_DATA_PAGE = 0
PageType_INDEX_PAGE = 1
PageType_DICTIONARY_PAGE = 2
PageType_DATA_PAGE_V2 = 3

# ConvertedType
ConvertedType_UTF8 = 0
ConvertedType_MAP = 1
ConvertedType_LIST = 3
ConvertedType_DECIMAL = 4
ConvertedType_DATE = 6
ConvertedType_TIMESTAMP_MILLIS = 12

# --------------------------------------------------------------------------- structs
Statistics = StructSpec("Statistics", (
    Field(1, "max", T_BINARY),
    Field(2, "min", T_BINARY),
    Field(3, "null_count", T_I64),
    Field(4, "max_value", T_BINARY),
    Field(5, "min_value", T_BINARY),
    Field(6, "is_max_value_exact", T_BOOLEAN_TRUE),
    Field(7, "is_min_value_exact", T_BOOLEAN_TRUE),
))

LogicalType = StructSpec("LogicalType", (
    Field(1, "STRING", StructOf(StructSpec("StringType", ()))),
    Field(2, "MAP", StructOf(StructSpec("MapType", ()))),
    Field(3, "LIST", StructOf(StructSpec("ListType", ()))),
    Field(4, "ENUM", StructOf(StructSpec("EnumType", ()))),
    Field(5, "DECIMAL", T_I32),
    Field(6, "DATE", StructOf(StructSpec("DateType", ()))),
    Field(7, "TIME", T_I32),
    Field(8, "TIMESTAMP", T_I32),
    Field(10, "INTEGER", T_I32),
    Field(11, "UNKNOWN", StructOf(StructSpec("NullType", ()))),
    Field(12, "JSON", StructOf(StructSpec("JsonType", ()))),
    Field(13, "BSON", StructOf(StructSpec("BsonType", ()))),
    Field(14, "UUID", StructOf(StructSpec("UUIDType", ()))),
    Field(16, "FLOAT16", StructOf(StructSpec("Float16Type", ()))),
))

SchemaElement = StructSpec("SchemaElement", (
    Field(1, "type", T_I32),
    Field(2, "type_length", T_I32),
    Field(3, "repetition_type", T_I32),
    Field(4, "name", T_BINARY),
    Field(5, "num_children", T_I32),
    Field(6, "converted_type", T_I32),
    Field(7, "scale", T_I32),
    Field(8, "precision", T_I32),
    Field(9, "field_id", T_I32),
    Field(10, "logicalType", StructOf(LogicalType)),
))

DataPageHeader = StructSpec("DataPageHeader", (
    Field(1, "num_values", T_I32),
    Field(2, "encoding", T_I32),
    Field(3, "definition_level_encoding", T_I32),
    Field(4, "repetition_level_encoding", T_I32),
    Field(5, "statistics", StructOf(Statistics)),
))

DataPageHeaderV2 = StructSpec("DataPageHeaderV2", (
    Field(1, "num_values", T_I32),
    Field(2, "num_nulls", T_I32),
    Field(3, "num_rows", T_I32),
    Field(4, "encoding", T_I32),
    Field(5, "definition_levels_byte_length", T_I32),
    Field(6, "repetition_levels_byte_length", T_I32),
    Field(7, "is_compressed", T_BOOLEAN_TRUE),
    Field(8, "statistics", StructOf(Statistics)),
))

DictionaryPageHeader = StructSpec("DictionaryPageHeader", (
    Field(1, "num_values", T_I32),
    Field(2, "encoding", T_I32),
    Field(3, "is_sorted", T_BOOLEAN_TRUE),
))

PageHeader = StructSpec("PageHeader", (
    Field(1, "type", T_I32),
    Field(2, "uncompressed_page_size", T_I32),
    Field(3, "compressed_page_size", T_I32),
    Field(4, "crc", T_I32),
    Field(5, "data_page_header", StructOf(DataPageHeader)),
    Field(7, "dictionary_page_header", StructOf(DictionaryPageHeader)),
    Field(8, "data_page_header_v2", StructOf(DataPageHeaderV2)),
))

KeyValue = StructSpec("KeyValue", (
    Field(1, "key", T_BINARY),
    Field(2, "value", T_BINARY),
))

SortingColumn = StructSpec("SortingColumn", (
    Field(1, "column_idx", T_I32),
    Field(2, "descending", T_BOOLEAN_TRUE),
    Field(3, "nulls_first", T_BOOLEAN_TRUE),
))

PageEncodingStats = StructSpec("PageEncodingStats", (
    Field(1, "page_type", T_I32),
    Field(2, "encoding", T_I32),
    Field(3, "count", T_I32),
))

ColumnMetaData = StructSpec("ColumnMetaData", (
    Field(1, "type", T_I32),
    Field(2, "encodings", ListOf(T_I32)),
    Field(3, "path_in_schema", ListOf(T_BINARY)),
    Field(4, "codec", T_I32),
    Field(5, "num_values", T_I64),
    Field(6, "total_uncompressed_size", T_I64),
    Field(7, "total_compressed_size", T_I64),
    Field(8, "key_value_metadata", ListOf(StructOf(KeyValue))),
    Field(9, "data_page_offset", T_I64),
    Field(10, "index_page_offset", T_I64),
    Field(11, "dictionary_page_offset", T_I64),
    Field(12, "statistics", StructOf(Statistics)),
    Field(13, "encoding_stats", ListOf(StructOf(PageEncodingStats))),
    Field(14, "bloom_filter_offset", T_I64),
))

ColumnChunk = StructSpec("ColumnChunk", (
    Field(1, "file_path", T_BINARY),
    Field(2, "file_offset", T_I64),
    Field(3, "meta_data", StructOf(ColumnMetaData)),
))

RowGroup = StructSpec("RowGroup", (
    Field(1, "columns", ListOf(StructOf(ColumnChunk))),
    Field(2, "total_byte_size", T_I64),
    Field(3, "num_rows", T_I64),
    Field(4, "sorting_columns", ListOf(StructOf(SortingColumn))),
    Field(5, "file_offset", T_I64),
    Field(6, "total_compressed_size", T_I64),
    Field(7, "ordinal", T_I32),
))

TypeDefinedOrder = StructSpec("TypeDefinedOrder", ())

ColumnOrder = StructSpec("ColumnOrder", (
    Field(1, "TYPE_ORDER", StructOf(TypeDefinedOrder)),
))

FileMetaData = StructSpec("FileMetaData", (
    Field(1, "version", T_I32),
    Field(2, "schema", ListOf(StructOf(SchemaElement))),
    Field(3, "num_rows", T_I64),
    Field(4, "row_groups", ListOf(StructOf(RowGroup))),
    Field(5, "key_value_metadata", ListOf(StructOf(KeyValue))),
    Field(6, "created_by", T_BINARY),
    Field(7, "column_orders", ListOf(StructOf(ColumnOrder))),
))
