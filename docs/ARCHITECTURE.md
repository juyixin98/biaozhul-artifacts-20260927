# 架构与数据流

## 分层

```
            HTTP (FastAPI, api/main.py)
            鉴权 / 关联ID / 错误码映射 / pydantic 边界校验
                          │
                   service.RunService
        审计(成功/失败/不可达) + 日志上下文(run_id, correlation_id)
                          │
        ┌─────────────────┼──────────────────────────┐
        ▼                 ▼                          ▼
 kernel（纯函数）   security（crypto/view）    storage（SQLite）
 parser/hierarchy   HKDF派生/Fernet/HMAC       每运行加密文件
 equivalence        出站白名单                 只追加审计库
 optimizer
```

- **内核无 I/O**：输入普通 dict/list，输出 dataclass，可直接被脚本、
  Notebook 或其他服务复用（见 `scripts/kernel_demo.py`）。
- **安全视图是唯一出口**：`security/view.py` 显式枚举可离开进程的字段，
  原始 QI 元组在 `report_view` 内即被 HMAC 指纹替换；敏感值文本不输出，
  只输出“频次→类数”的直方图。
- **状态隔离**：一次运行 = 一个 `data/runs/<run_id>.db`。文件内整包数据是
  一整个 Fernet blob；`meta` 索引表只有行数、列名、深度等非敏感元数据。
  运行间不共享事务/临时表；删除一个文件不影响其他运行。

## 创建运行的数据流

1. `POST /runs`：pydantic 判别联合校验层级规则的基本形状。
2. `kernel.parser.parse_dataset`：列角色显式声明检查、行宽、NULL 规范化、
   逐列 NULL 计数证据。
3. `kernel.hierarchy.materialize`（每个 QI 列）：在**该数据实际值域**上
   实例化每层映射，并验证覆盖完整性与逐级包含。任何失败返回具体错误码，
   写 `VALIDATION_ERROR` 审计，且**不落运行文件**。
4. `storage.RunStore.create_run`：生成 `run_id`（128 位随机）与访问令牌
   （256 位随机），随机 16 字节 salt，HKDF 派生运行密钥，加密整包落盘，
   令牌仅存 SHA-256。

## evaluate / suggest 数据流

1. 令牌经 `hmac.compare_digest` 校验后解密数据包（解密失败=403）。
2. 重新解析与实例化层级（存储只保存声明，不缓存计算态）。
3. `apply_vector` 生成每行泛化 QI 键（NULL 全程保留）。
4. `equivalence.evaluate`：真实分组 → 类大小、非 NULL 敏感值分布、
   k/l 判定、HIGH/MEDIUM/LOW、检察官风险、DM/LM。
5. suggest：先评估全泛化向量做快速不可达判定；否则穷举
   `∏(depth_i+1)` 个向量，按 (DM, LM, 向量字典序) 选确定性最优，
   每 10% 记录一次进度日志。
6. 结果经白名单视图序列化；操作写入运行内 `operations` 表与中心审计库。

## 并发与事务

- SQLite 默认多线程连接（`check_same_thread=False`），审计写入即时 commit。
- 项目定位为本地/单机合成数据工具；高并发写场景应外置数据库（见限制）。
