// Package procmanager 把模拟进程管理器适配为控制器端口 controller.ProcessManager。
package procmanager

import (
	"rollingdeploy/internal/controller"
	"rollingdeploy/internal/procman"
)

// Adapter 适配 *procman.Manager。
type Adapter struct{ M *procman.Manager }

// New 创建适配器。
func New(m *procman.Manager) *Adapter { return &Adapter{M: m} }

// Start 委托给模拟器。
func (a *Adapter) Start(appName, version string) (string, error) {
	return a.M.Start(appName, version)
}

// Status 把模拟器状态映射为中立端口类型。
func (a *Adapter) Status(procID string) (controller.ProcStatus, error) {
	st, err := a.M.Status(procID)
	return controller.ProcStatus{Missing: st.Missing, Ready: st.Ready}, err
}

// Stop 委托给模拟器。
func (a *Adapter) Stop(procID string) error { return a.M.Stop(procID) }
