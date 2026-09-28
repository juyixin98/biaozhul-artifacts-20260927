"""Composite-key MERGE decision engine.

Package layout (see README.md "Module relations" for the contract map):

* :mod:`merge_engine.errors`     - the four-category error hierarchy + codes
* :mod:`merge_engine.contract`   - request/spec dataclasses shared by every layer
* :mod:`merge_engine.predicate`  - safe whitelist expression compiler (SQL 3VL)
* :mod:`merge_engine.adapter`    - PyArrow / JSON rows -> canonical source rows
* :mod:`merge_engine.snapshot`   - pre-operation target snapshot + duplicate scan
* :mod:`merge_engine.planner`    - pure decision core: rows -> validated action plan
* :mod:`merge_engine.metadata`   - SQLite DDL, run/action/trace/snapshot DAO
* :mod:`merge_engine.executor`   - atomic plan execution against SQLite
* :mod:`merge_engine.runlog`     - replay-oriented JSONL run log
* :mod:`merge_engine.service`    - orchestration facade (validate / merge)
"""

__version__ = "1.0.0"
