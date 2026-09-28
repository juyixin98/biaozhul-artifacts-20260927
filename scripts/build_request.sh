#!/usr/bin/env bash
# 从网夹具 + 目标标识构造 /api/v1/reachability 请求体（只做 JSON 拼装，不调用被测核心）。
# 夹具里的 initial_marking 是按 places 顺序的数组，这里转成请求使用的 name->tokens 映射。
# 用法：scripts/build_request.sh fixtures/mutex.json '{"crit_a":1,"crit_b":1}'
set -euo pipefail
net_file="$1"
target="${2:-}"
if [[ -z "$target" ]]; then target='{}'; fi
jq -n --slurpfile n "$net_file" --argjson target "$target" '
  ($n[0]) as $root |
  (reduce range($root.places|length) as $i ({};
      .[$root.places[$i].name] = ($root.initial_marking[$i] // 0))
     | with_entries(select(.value != 0))) as $im |
  {
    net: {places: $root.places, transitions: $root.transitions},
    initial_marking: $im,
    target_marking: $target
  }'
