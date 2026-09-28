package kernel

import (
	"crypto/rand"
	"encoding/hex"
)

// NewID 生成 128 位随机身份。每次任务转移（派生/认领）都获得唯一身份，
// 使“同一任务的重复确认”可以被准确识别，绝不会把两次确认计入两次结算。
//
// 形如 "t_<24 hex>"。前缀按身份类型区分，日志中即可辨别。
func NewID(prefix string) string {
	var b [12]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand 失败意味着运行环境已破坏，直接 panic 不产生静默坏身份。
		panic("kernel: cannot read crypto/rand: " + err.Error())
	}
	return prefix + "_" + hex.EncodeToString(b[:])
}
