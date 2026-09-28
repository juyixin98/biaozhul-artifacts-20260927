"""非重叠替换规划服务 (non-overlapping replacement planning service).

模块分层（依赖方向自底向上，禁止反向依赖）：

    config      可调限制与运行编号
    errors      跨模块/HTTP 的错误类别契约
    schemas     Pydantic 数据契约（文本规范、规则、计划、诊断）
    engine      RE2 封装：成熟无回溯引擎，码点偏移，预算受控
    template    受限捕获模板：先验证后渲染
    textutil    码点偏移 <-> UTF-8 原始字节范围
    planning    多规则同轮优先级消解、计划绑定源摘要
    storage     SQLite 版本存储（源文本、规则、计划、计划版本、诊断事件）
    apply       流式应用计划 + 源摘要守卫
    api         FastAPI 路由，把错误契约映射为 HTTP 响应
"""

__all__ = ["__version__"]
__version__ = "1.0.0"
