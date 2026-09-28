"""anon-risk 纯函数安全内核。

模块组成：

- :mod:`parser`      按规则/证据解析输入并显式声明列角色
- :mod:`hierarchy`   泛化层级的实例化、可验证性与包含关系检查
- :mod:`equivalence` 真实等价类计数、k-匿名 / l-多样性指标
- :mod:`optimizer`   穷举格点 + 保持真实计数的信息损失最小化
"""
