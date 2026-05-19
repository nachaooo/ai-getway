# 重要规则

## 禁止启动网关服务

调试/修改 AI Gateway 时，**绝对不要启动网关服务** (`python app.py`)。
网关在端口 5000 运行时，openocde 的所有 provider 都指向 `localhost:5000`，
启动网关会导致 opencode 的所有 AI 请求被阻塞/中断。

### 正确的测试方式

- 写单元测试脚本，不依赖运行中的网关
- 或者使用非 5000 端口 (`PORT=5230 python app.py`)
- 直接在 Python 中 import app 模块调用内部函数
