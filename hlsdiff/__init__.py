"""hlsdiff — 本地 HLS 媒体播放列表版本对比与播放计划服务。

模块职责:
- models      数据模型(分段、播放列表、报告)
- errors      解析/对比失败类别
- parser      M3U8 媒体播放列表解析(非加密分段媒体)
- timeline    时间与信号内核:媒体序号、discontinuity 序号、时间线分别维护
- compare     相邻版本对比:窗口前移 / 内容撤回 / 冲突 / 结束后追加
- plan        可下载计划与连续播放边界
- store       SQLite 快照与作业状态
- api         FastAPI 验证接口
"""

__version__ = "0.1.0"
