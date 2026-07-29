# 重构计划：移除多语言、模型测试、主题切换、命名空间

## 影响范围

| 文件 | 修改量 |
|------|--------|
| `app.py` | ~165 行（命名空间路由/DB/API） + ~57行（模型测试路由） |
| `templates/landing.html` | ~530 行（i18n+主题） |
| `templates/index.html` | ~200 行（i18n+主题+scope+模型测试CSS） |

## 1. app.py 改动

### 移除命名空间
- `get_db_path(scope)` → 去掉 scope 参数，永远用 `DB_PATH`
- `get_db(scope)` → 去掉 scope 参数，连主库
- `ensure_schema()` → 去掉 `scope` 列（新的表不再包含 scope），去掉 `scope_models` 表名中的 `scope_` 前缀
- `_log_usage(scope, ...)` → 去掉 scope 参数
- `_handle_stream(scope, ...)` → 去掉 scope 参数
- `_proxy_passthrough(scope, ...)` → 去掉 scope 参数
- 路由 `"POST /v1/chat/completions"` 和 `"POST /<scope>/v1/chat/completions"` → 合为一个 `/v1/chat/completions`
- 路由 `"POST /v1"` 和 `"POST /<scope>/v1"` → 合为一个 `/v1`
- `/api/usage` 去掉 scope 过滤参数
- `/api/recent` 去掉 scope 过滤参数
- **删除** `/api/scopes` 路由
- `/api/scope/<scope>/models/*` → 改为 `/api/models*`，去掉 scope 层级
- `dashboard()` 去掉 `/<scope>` 路由，只保留 `/dashboard`
- `/api/db/download` 和 `/api/db/upload` 去掉 scope 参数

### 移除模型测试
- **删除** `@app.route("/api/test-model", ...)` 整个函数

## 2. landing.html 改动

### 移除多语言 (~250行 JS + ~15行 HTML + RTL CSS)
- 删除整个 `i18n` 对象（6种语言，~250行）
- 删除 `langNames` 映射
- 删除语言下拉菜单 HTML
- 删除 `toggleLangMenu()`, `setLang()`, `updateTexts()`, `updateLangDisplay()`
- 删除外部点击关闭语言菜单
- 删除 `init()` 中 lang 相关代码
- 删除所有 `data-i18n` 属性，保留中文文本
- 删除 `data-lang` 属性
- 删除 RTL CSS（`[dir="rtl"]` 相关）
- 删除 `.lang-dropdown`, `.lang-menu` CSS

### 移除主题切换 (~90行 CSS + ~20行 JS)
- 删除 `[data-theme="dark"]` CSS 变量块（保留 :root 亮色主题）
- 删除主题切换按钮 HTML
- 删除 `toggleTheme()`, `updateThemeIcon()` 函数
- 删除 `init()` 中 theme 相关代码

## 3. index.html 改动

### 移除多语言 (~28行 I18N + ~50行调用)
- 删除整个 `I18N` 对象
- 删除 `applyLang()`, `toggleLang()`
- 删除 `initPrefs()` 中 lang 相关
- 删除所有 `data-i18n` 属性，保留中文文本
- 删除 `renderRecent()`, `renderChart()`, `renderModelsChart()`, `fetchAll()` 中的 lang 参数和 I18N 引用
- 删除 `weekdayLabel()` 函数（直接用中文星期）
- 删除语言切换按钮

### 移除主题切换 (~45行 CSS + ~50行 JS)
- 删除 `[data-theme="light"]` CSS 变量块（保留 dark 默认）
- 删除 `applyTheme()`, `toggleTheme()` 函数
- 删除 `initPrefs()` 中 theme 相关
- 删除 `chartColors()` 函数（用固定的暗色值）
- 删除 `renderRecent()`, `renderModelsChart()` 中的 theme 判断
- 删除主题切换按钮

### 移除命名空间引用
- 删除 `CURRENT_SCOPE` 变量
- 删除 API 调用中 `params.scope = CURRENT_SCOPE`
- 删除 `downloadDb()`, `uploadDb()` 中的 scope 逻辑

### 移除模型测试 CSS (~6行)
- 删除 `.btn-test`, `.test-result` 相关样式

## 执行顺序

1. `app.py` — 核心后端改动（命名空间 + 模型测试）
2. `landing.html` — 页面重构
3. `index.html` — 页面重构
4. 验证：`python main.py --worker` 测试各页面和 API
