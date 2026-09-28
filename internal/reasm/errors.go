package reasm

import (
	"errors"
	"fmt"

	"ipfragreasm/internal/netmodel"
)

// ErrorKind 是重组阶段对外暴露的失败类别枚举。
// 测试与 HTTP 层依据具体类别断言，绝不把异常折叠成“成功”。
type ErrorKind string

const (
	// KindUnalignedFragment：MF=1 的非末片载荷长度不是 8 字节整数倍。
	// 属于单片非法（后继片的偏移无法用 13 位字段表达），仅拒绝该片。
	KindUnalignedFragment ErrorKind = "unaligned_fragment"

	// KindOverlapGroupRejected：出现非“完全重复”的覆盖，整组拒绝（不复用旧数据）。
	KindOverlapGroupRejected ErrorKind = "overlap_group_rejected"

	// KindConflictingLastFragment：末片与已知末片冲突（末点不同或有片越过已宣告总长）。
	KindConflictingLastFragment ErrorKind = "conflicting_last_fragment"

	// KindDatagramTooLarge：重组终点超过配置上限（IPv4 硬上限 65535 字节）。
	KindDatagramTooLarge ErrorKind = "datagram_too_large"

	// KindGroupAlreadyTerminal：相同分组键的上一个组尚未回收，ID 过早复用。
	KindGroupAlreadyTerminal ErrorKind = "group_already_terminal"

	// KindGroupNotFound：活动组与终结留存中均无此键。
	KindGroupNotFound ErrorKind = "group_not_found"
)

// Error 携带重组失败类别、分组键与判定依据。
type Error struct {
	Kind   ErrorKind
	Key    netmodel.FragKey
	Detail string
}

func (e *Error) Error() string {
	return fmt.Sprintf("%s [%s]: %s", e.Kind, e.Key.String(), e.Detail)
}

// AsError 提取 *Error（包装错误也可识别）。
func AsError(err error) (*Error, bool) {
	var target *Error
	if errors.As(err, &target) {
		return target, true
	}
	return nil, false
}

// HTTPStatus 把失败类别映射为明确的 HTTP 状态码，避免统一 200/500。
func HTTPStatus(err error) int {
	if e, ok := AsError(err); ok {
		switch e.Kind {
		case KindUnalignedFragment:
			return 422
		case KindOverlapGroupRejected, KindConflictingLastFragment,
			KindDatagramTooLarge, KindGroupAlreadyTerminal:
			return 409
		case KindGroupNotFound:
			return 404
		}
	}
	var pe *netmodel.ParseError
	if errors.As(err, &pe) {
		return 400
	}
	return 500
}
