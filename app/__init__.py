"""离线 RTP 抖动缓冲与播放计划后端。

模块划分::

    app.media      媒体解析层：RTP 报文解析/构造、合成夹具
    app.timekit    时间与信号内核：序号/时间戳展开、抖动与漂移估计
    app.core       抖动缓冲、播放计划、离线仿真、独立参考判定、场景
    app.jobs       作业状态：SQLite 持久化 + 后台执行器
    app.api        验证接口：FastAPI 路由与数据模型
"""

__version__ = "1.0.0"
