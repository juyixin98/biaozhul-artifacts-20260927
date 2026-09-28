# 测试运行报告

- 日期：2026-09-28
- Go：go1.23.4 linux/amd64（纯 Go SQLite `modernc.org/sqlite v1.59.0`，无 cgo）
- 命令：`go vet ./...` 与 `go test -mod=vendor -race ./... -count=1`
- 结果：**vet 通过；全部包通过（含竞态检测）；无跳过项、无失败项。**

机器可读输出见同目录 `vet.txt` 与 `test-run.txt`。

## 覆盖矩阵

| 包 | 关键断言（具体结果/失败类别，而非"能调用"） |
|---|---|
| internal/fieldpath | 路径记法往返（含嵌套 key/set、字符串转义、数字/布尔 token）、非法路径报错、前缀判定、按 key/set token 查树 |
| internal/schema | 三种列表类型校验、map 必须带 key、非 map 不允许 key、点路径精确匹配 |
| internal/apperr | 分类/错误码文本、fmt 包装后 As 仍可分类、普通错误不分类 |
| internal/merge（golden A/B/C） | 对照 `testdata/threeway/*.md` 的**手工**三方合并结果逐字段断言 live 与所有权；冲突精确到路径+reason+owners；变更操作类型；参考答案不来自被测代码 |
| internal/store | 冲突零落盘（live/revision/history 均不变）、值与所有权同事务、历史含 from/to/manager/force/run_id、payload/资源上限→resource_exhausted、坏 JSON 输入→invalid_input、损坏 live→compute_failure、损坏所有权路径→internal |
| internal/reconcile | 前 2 次注入失败后恢复（调用次数、落地产物 revision 一致、last_error 清空）；永久失败不重试且 attempts=1、错误带 fatal: 前缀、无产物；可重试失败耗尽预算 attempts 精确等于 MaxAttempts；队列满→resource_exhausted/queue_full |
| internal/adapter | 文件 tmp+rename 原子写、无残留临时文件、故障适配器失败计数后恢复、永久故障不可重试、20 并发写均成功 |
| internal/server | 端到端 apply→409（体含 path/owners/reason/preview）→force→所有权/历史查询；404/400 各类 code；413；X-Run-Id 自动生成与客户端透传；冲突不写历史；协调异步到达 synced |

## 四类可区分故障（对应需求）

- 输入错误：`invalid_input/*` → HTTP 400（坏 JSON、缺 manager、未声明列表、set 重复值、map 缺键/重复键…）
- 状态冲突：`conflict/ownership_conflict` → HTTP 409，details 含全部冲突路径与原管理者
- 资源耗尽：`resource_exhausted/{payload_too_large,resource_limit,queue_full}` → 413/503
- 计算失败 / 存储故障：`compute_failure/live_bad_json`（引擎无法处理存量数据）与
  `internal/*`（sqlite/损坏所有权行）分开；适配器不可用另有 `unavailable` → 503

## 诊断与重放

- 每个请求与响应带 `X-Run-Id`（也可客户端指定以串联重试链）。
- 服务日志：`data/logs/fieldmerged.jsonl`（事件、run id、冲突、退避时长等）。
- 测试日志：`testlogs/TestThreeWay_*.jsonl` 记录每一步 run_id、apply 输入、
  引擎实际中间状态（conflicts/changes/pruned）与每条断言的通过情况及理由，
  可按 run_id 重放问题。

## 开发过程中真实发现并修复的问题（保留为记录）

1. 引擎最初把 keyed-list 的身份键本身登记为所有权字段——键已编码在路径中，
   修正为身份键不占所有权、不可被接管或单独删除。
2. 省略一个不属于自己的 set 元素曾被当作删除（违反"未提交≠删除"），修正为
   只回退自己（共）有的值。
3. 两个 golden 用例的手写预期第一次写错（把先建立字段的管理者记成了另一方；
   全量声明下管理者必须带上自己已有的元素）——由对照测试发现并更正，
   佐证了期望值确为独立推导而非从实现复制。
4. 可重试失败落库时 attempts 未递增（SQL CASE 分支笔误），导致重试预算
   判断失效；已修复并由重试耗尽测试锁定。
5. `json.RawMessage` 不能直接作为 database/sql Scan 目标，改为先扫 string。
