# PRD：BlogDistiller 本地优先桌面客户端架构

> 版本：v1.0
> 创建时间：2026-09-24
> 适用项目：`e:\Tools\Web\下载各个平台`
> 目标读者：负责实现本架构的其他 AI Agent / 开发者

---

## 0. 会话背景与决策摘要（必读）

本 PRD 由当前会话总结得出。关键决策如下：

1. **放弃纯云端大任务处理**：现有「网页 + 浏览器扩展 + 云端服务器全链路处理」架构下，1894 篇知乎文章导出时，服务器进向流量超过 200GB、内存多次 OOM 被系统强杀，服务器磁盘一度只剩 8.4G/39G。
2. **选择客户端自治方案**：用户最终决定把 BlogDistiller 改造为以桌面客户端为主体的「本地优先架构」——抓取、清洗、排版、导出在用户本地电脑完成，云端只负责分发安装包、自动更新、扩展包下载。
3. **保留在线网站作为入口和 UI 载体**：用户希望界面继续使用现有 `frontend/app.html` 工作台，不重新开发客户端 UI。客户端壳子里加载线上页面即可。
4. **双版本发行**：先完成标准版（Win10+/Electron 44/Python 3.11），再补 Win7 兼容版（Win7/8.1/Electron 22 LTS/Python 3.9）。两套版本共用一套业务代码，只切换 Electron 与 Python 版本。
5. **UI 走线上、Python 后端走本地**：为避免每次小调整都发客户端包，客户端窗口加载 `doc.305758.xyz/app?client_mode=1`，本地只启动 Python 后端服务。只有 backend/app 抓取/清洗逻辑或客户端壳改动时才需要重新打包发版。

---

## 1. 架构名称

- **中文**：本地优先桌面客户端架构
- **英文**：Local-First Desktop Architecture
- **简称**：客户端自治模式

---

## 2. 背景与目标

### 2.1 当前痛点

现有架构（网页 + 浏览器扩展抓取源码 + 云端清洗导出）在实测中暴露出以下问题：

| 痛点 | 现象 | 根因 |
|---|---|---|
| 服务器流量爆 | 一周跑出 241GB，单次 1894 篇任务约 0.3GB 进出 | 大量网页源码/图片经过服务器 |
| 服务器内存爆 | 多次 OOM 被系统强杀 | 任务全文长期驻留内存，1894 篇接近极限 |
| 多人并发不可行 | 2~3 个千字级任务就会让服务器卡死 | 所有计算在云端堆叠 |
| 风控 | 知乎、微信等平台对服务器 IP 有风控 | 云端 IP 是机房/数据中心地址 |

### 2.2 目标

把重计算从云端搬到用户本地电脑，云端降级为**客户端分发与更新中心**。

---

## 3. 核心概念

| 组件 | 新职责 |
|---|---|
| **Electron 桌面客户端** | 主应用入口。壳中加载线上工作台 UI，后台启动本地 Python FastAPI 服务 |
| **本地 Python 后端** | 复用 `backend/app` 全套抓取/清洗/导出逻辑，监听 `127.0.0.1:8000` |
| **在线网站** | 降级为下载入口、帮助文档、轻量云端备用入口 |
| **云端服务器** | 只保留：客户端安装包下载、版本更新、浏览器扩展包分发 |

---

## 4. 用户使用流程

1. 用户打开浏览器访问 `doc.305758.xyz`
2. 官网推荐下载「BlogDistiller 桌面客户端」
3. 下载安装包（标准版或 Win7 版），双击安装
4. 首次启动：客户端自动检测本地 Python 环境，没有则静默创建 `.venv` 并安装依赖（复用 `start_desktop.bat` 逻辑，约 1-3 分钟）
5. 客户端窗口加载 `doc.305758.xyz/app?client_mode=1`
6. 用户界面与现在网页版完全一致：粘贴链接 → 检索 → 勾选 → 导出
7. 所有抓取/清洗在本地完成，产物 ZIP 自动保存到本地 `downloads/` 文件夹

---

## 5. 双版本发行策略

| 版本 | 目标系统 | Electron | Python | 构建顺序 |
|---|---|---|---|---|
| **标准版** | Windows 10 / 11（主流用户） | `^44.0.0` | 3.11 / 3.12 | **先做** |
| **Win7 兼容版** | Windows 7 / 8.1 | `^22.3.27`（最后一个支持 Win7 的 LTS） | 3.9.x（最高） | 标准版稳定后做 |

**关键约束：**

- 两套版本**共用同一套业务代码**（`main.js`、`preload.js`、`frontend/app.html`、`backend/app`），不允许为 Win7 fork 一套清洗逻辑。
- 通过构建脚本/CI 参数切换 Electron、Node.js、electron-builder、Python 版本。
- 客户端下载页根据 `navigator.userAgent` 自动推荐对应版本。

---

## 6. 云端服务边界

### 6.1 必须保留的云端接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/` | 官网首页，提供下载入口 |
| GET | `/app` | 云端备用工作台（给未安装客户端的用户临时使用，可选） |
| GET | `/api/client/version` | 返回最新客户端版本号、release notes、标准版下载 URL、Win7 版下载 URL |
| GET | `/api/client/download` | 返回标准版安装包 |
| GET | `/api/client/download/win7` | 返回 Win7 兼容版安装包 |
| GET | `/api/extension/download` | 浏览器扩展包（保留备用，知乎场景可能仍需要） |

### 6.2 需要本地化的原云端接口

`frontend/app.html` 中现有的以下 API 调用，在客户端模式下必须全部改为调用本地 `http://127.0.0.1:8000`：

- `/api/platforms`
- `/api/extract-links` 及 `/api/extract-links/progress`
- `/api/tasks` 系列（创建、状态、取消、暂停、恢复、events、decision、stop-and-export 等）
- `/api/download/{filename}`
- `/api/wechat/status`、`/api/wechat/set-auth`、`/api/wechat/generate-profile-url` 等认证类
- `/api/zhihu/status`、`/api/zhihu/sync-browser`、`/api/zhihu/qr-login`、`/api/zhihu/set-cookie` 等认证类
- `/api/weibo/status`、`/api/weibo/set-cookie`
- `/api/community-wall`

**注意**：`/api/relay/*` 本地模式下不再需要，因为抓取就在本地完成。但代码里可以保留接口复用逻辑，不强制删除。

---

## 7. 关键技术方案

### 7.1 Electron 客户端改造（`desktop/main.js`）

现有 `main.js` 是微信公众号导出工具，需扩展为通用 BlogDistiller 客户端。

#### 7.1.1 本地 Python 服务生命周期管理

- 启动时检查 `.venv/Scripts/python.exe` 是否存在
  - 不存在：执行首次初始化（复用 `start_desktop.bat` / `start.bat` 逻辑）
  - 若系统无 Python，弹出引导窗口提示下载安装
- 启动 `python run.py`，监听 `127.0.0.1:8000`
- 实现重试机制：本地服务若崩溃，自动尝试重启（最多 5 次，间隔递增）
- 退出客户端时，确保关闭 Python 子进程
- 提供一个状态 IPC：`get-local-service-status` 返回 `{ running: bool, port: number, url: string }`

#### 7.1.2 窗口与入口

- 主窗口加载 `https://doc.305758.xyz/app?client_mode=1`
- 保留微信/知乎扫码登录弹窗能力（需要时打开独立 BrowserWindow）
- 支持最小化到系统托盘
- 支持开机自启选项（默认关闭，用户可在托盘菜单开启）
- 窗口标题：BlogDistiller 文章导出助手

#### 7.1.3 自动更新

- 集成 `electron-updater`（`package.json` 已依赖）
- 启动时访问 `/api/client/version` 检查更新
- 发现更新后下载并提示用户重启
- 标准版和 Win7 版分别拉取各自安装包

#### 7.1.4 IPC 通道设计

新增以下 `ipcMain.handle` / `ipcMain.on`：

| 通道 | 方向 | 说明 |
|---|---|---|
| `start-local-service` | invoke → main | 手动启动本地服务 |
| `stop-local-service` | invoke → main | 手动停止本地服务 |
| `get-local-service-status` | invoke → main | 查询本地服务状态 |
| `open-external` | invoke → main | 用系统默认浏览器打开外部链接 |
| `reveal-file` | invoke → main | 导出完成后打开资源管理器并选中文件 |
| `get-downloads-path` | invoke → main | 返回默认下载目录 |
| `show-open-dialog` | invoke → main | 文件选择对话框 |
| `service-status-change` | main → renderer | 本地服务状态变化时推送 |
| `update-available` | main → renderer | 有新版本时推送 |
| `restart-app` | renderer → main | 重启应用（更新后） |

### 7.2 Preload 脚本（`desktop/preload.js`）

暴露安全的 `window.electronAPI`，前端 HTML 通过它判断是否在 Electron 环境：

```js
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  startLocalService: () => ipcRenderer.invoke('start-local-service'),
  stopLocalService: () => ipcRenderer.invoke('stop-local-service'),
  getLocalServiceStatus: () => ipcRenderer.invoke('get-local-service-status'),
  openExternal: (url) => ipcRenderer.invoke('open-external', url),
  revealFile: (path) => ipcRenderer.invoke('reveal-file', path),
  getDownloadsPath: () => ipcRenderer.invoke('get-downloads-path'),
  showOpenDialog: (options) => ipcRenderer.invoke('show-open-dialog', options),
  onServiceStatusChange: (callback) => ipcRenderer.on('service-status-change', (e, data) => callback(data)),
  onUpdateAvailable: (callback) => ipcRenderer.on('update-available', (e, data) => callback(data)),
  restartApp: () => ipcRenderer.send('restart-app'),
});
```

### 7.3 前端 `frontend/app.html` 改造

**核心改动：统一 API 路由。**

- 页面顶部检测运行环境：

```js
const isElectronClient = typeof window !== 'undefined' && !!window.electronAPI;
const apiBase = isElectronClient ? 'http://127.0.0.1:8000' : '';
const isClientMode = new URLSearchParams(location.search).get('client_mode') === '1' || isElectronClient;
```

- 将 `frontend/app.html` 中所有 `fetch('/api/...')` 改为 `fetch(apiBase + '/api/...')`
  - 经统计共有 32 处需要修改（详见第 8 节改动清单）
  - 下载链接同理处理
- 本地模式下的 UI 调整：
  - 隐藏「本地住宅 IP 直连」「云端抓取」等与服务器通道相关的选项
  - 顶部状态栏显示「本地客户端运行中 🟢」
  - 导出完成后显示「打开本地文件夹」按钮，调用 `electronAPI.revealFile(filePath)`
- 若检测到 `isClientMode` 但本地服务未启动，显示「正在启动本地服务…」占位，轮询 `electronAPI.getLocalServiceStatus()`

### 7.4 服务端改造（`backend/app/main.py`）

**大方向：云端瘦身。**

新增接口：

```python
class ClientVersionInfo(BaseModel):
    version: str
    release_notes: str
    standard_download_url: str
    win7_download_url: str
    mandatory: bool = False

@app.get("/api/client/version")
async def get_client_version():
    return {
        "version": "1.3.0",
        "release_notes": "支持本地优先架构，修复云端流量与内存问题",
        "standard_download_url": "/api/client/download",
        "win7_download_url": "/api/client/download/win7",
        "mandatory": False,
    }

@app.get("/api/client/download")
async def download_client_standard():
    file_path = BASE_DIR / "dist" / "BlogDistiller-Setup-x64.exe"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="标准版客户端安装包尚未上传")
    return FileResponse(file_path, filename="BlogDistiller-Setup-x64.exe", media_type="application/octet-stream")

@app.get("/api/client/download/win7")
async def download_client_win7():
    file_path = BASE_DIR / "dist" / "BlogDistiller-Win7-Setup-x64.exe"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Win7 兼容版安装包尚未上传")
    return FileResponse(file_path, filename="BlogDistiller-Win7-Setup-x64.exe", media_type="application/octet-stream")
```

原有抓取/任务相关接口建议**保留但转为云端备用模式**，不直接删除，避免网页端轻量用户突然不可用。

### 7.5 打包配置（`desktop/package.json`）

- `electron-builder` 已存在，产物配置 `nsis` + `zip`
- 新增 `extraResources`：把项目根目录的 `backend/`、`run.py`、`requirements.txt`、`start.bat` 拷贝到安装目录，供本地服务启动
- `productName` 改为通用名，例如：「BlogDistiller 文章导出助手」
- `appId` 保持不变：`com.yibeigen.blogdistiller`
- 自动更新配置指向 `/api/client/version`

### 7.6 首次启动初始化脚本

复用现有 `start_desktop.bat` 或 `start.bat`：

- 检测 `uv` 或 `python`
- 创建 `.venv`
- 安装 `requirements.txt`
- 安装 Playwright Chromium 内核
- 完成后启动 `run.py`

**注意**：Electron 主进程应直接调用脚本，而不是让用户看到命令行窗口。

---

## 8. 数据流

```
用户在 Electron 客户端内操作
        ↓
app.html (渲染层，加载线上地址)
        ↓ fetch(apiBase + '/api/extract-links')
本地 FastAPI (127.0.0.1:8000，由 run.py 启动)
        ↓ 调用 backend/app/scrapers/xxx.py
知乎 / 微信 / 微博 / CSDN / 简书 / 掘金 / 博客园 / 51CTO 等平台
        ↓ 返回网页 HTML
本地清洗模块 backend/app/cleaners
        ↓ 生成 md/html/pdf/docx/txt
本地导出模块 backend/app/exporters
        ↓ ZIP 保存到本地 downloads/
Electron 主进程通过 IPC 通知 UI
        ↓ 显示完成 → 用户点击「打开文件夹」
```

初始化/更新流：

```
客户端启动
        ↓
GET https://doc.305758.xyz/api/client/version
        ↓
云端返回版本信息 + 下载链接
        ↓
electron-updater 拉取对应安装包
        ↓
提示用户重启完成更新
```

---

## 9. 改动清单（按优先级）

| # | 优先级 | 文件 | 改动内容 | 估算 |
|---|--------|------|----------|------|
| 1 | P0 | `desktop/main.js` | 本地 Python 服务启动/停止/保活；托盘；开机自启；IPC 通道；窗口加载线上 app.html | 3h |
| 2 | P0 | `desktop/preload.js` | 暴露 `window.electronAPI`，让前端能检测客户端环境并调用本地能力 | 30min |
| 3 | P0 | `frontend/app.html` | 32 处 `fetch('/api/...')` 改为 `fetch(apiBase + '/api/...')`；本地模式 UI 调整 | 2h |
| 4 | P0 | `backend/app/main.py` | 新增 `/api/client/version`、 `/api/client/download`、 `/api/client/download/win7` | 30min |
| 5 | P1 | `desktop/package.json` | `extraResources` 打包 backend；自动更新配置；修改 productName | 1h |
| 6 | P1 | `start_desktop.bat` / `desktop/electron-first-run.js` | Electron 首次启动时静默初始化 Python 环境 | 1h |
| 7 | P1 | `frontend/index.html` 或云端首页 | 新增「下载桌面客户端」入口 + User-Agent 自动推荐版本 | 30min |
| 8 | P2 | Electron 自动更新完整链路 | 版本检查、下载、安装、重启 | 2h |

**总计：约 1-2 天工作量。**

`frontend/app.html` 中需要修改的 API 调用位置（供实现时核对）：

```
行 3007: /api/platforms
行 3234: /api/weibo/status
行 3258: /api/zhihu/status
行 3404: /api/wechat/search-biz
行 3575: /api/wechat/search-biz
行 3725: /api/extract-links/progress
行 3755: /api/extract-links
行 3779: /api/extract-links/progress
行 3799: /api/extract-links/progress
行 4412: /api/wechat/set-auth
行 4446: /api/zhihu/status
行 4471: /api/wechat/status
行 4509: /api/wechat/status
行 4579: /api/zhihu/sync-browser
行 4604: /api/zhihu/qr-login
行 4831: /api/relay/upload-status
行 4866: /api/relay/upload-chunk
行 4926: /api/zhihu/set-cookie
行 5384: /api/tasks
行 5415: /api/download/{filename}
行 5498: /api/tasks/{id}/{action}
行 5514: /api/tasks/{id}/stop-and-export
行 5534: /api/tasks/{id}/cancel
行 5578: /api/tasks/{id}/decision
行 5619: /api/tasks/{id}/events
行 5900: /api/tasks/{id}
行 5994: /api/tasks?client_id=...
行 6378: /api/community-wall
```

建议用 `replace_all` 或搜索替换方式统一处理：`fetch('/api/` → `fetch(apiBase + '/api/`，同时处理 `new EventSource('/api/` 等下载链接。

---

## 10. 更新策略

| 更新内容 | 需要更新哪里 |
|---|---|
| UI 文字、按钮颜色、流程提示 | 只需云端更新 `frontend/app.html`，客户端下次打开自动生效 |
| 抓取/清洗/导出逻辑（backend/app） | 需重新打包标准版 + Win7 版，通过自动更新推送 |
| 浏览器扩展包 | 只需云端更新 `/api/extension/download` 对应文件 |
| 客户端壳功能（托盘、开机自启、IPC） | 需重新打包，自动更新推送 |
| 云端首页下载链接 | 更新 `frontend/index.html` |

**核心原则**：

1. 客户端窗口必须加载线上地址 `https://doc.305758.xyz/app?client_mode=1`，不要把 `app.html` 打包进客户端。
2. 本地 Python 后端代码随客户端包一起分发，因此 backend 改动必须发版。
3. 标准版和 Win7 版更新节奏保持一致，但每次可以只发布有改动的版本。

---

## 11. 验收标准

1. 双击标准版安装包可完成安装；Win7 版在 Win7 虚拟机中可完成安装。
2. 首次启动自动完成 Python 环境初始化，用户只看到进度提示。
3. 打开客户端后界面与现有网页版一致。
4. 粘贴任意平台主页链接，能在客户端内完成文章列表提取。
5. 勾选文章导出后，产物 ZIP 保存在本地 `downloads/` 目录。
6. 过程中与 `doc.305758.xyz` 没有大流量交互（只有版本检查、页面静态资源加载）。
7. 关闭客户端时，本地 Python 进程一并退出。
8. 重启客户端后，可基于本地缓存继续先前未完成任务。
9. 自动更新触发后，能正常下载安装包并提示重启。

---

## 12. 风险与注意事项

1. **Python 环境体积**：首次安装若 embed Python 会很大（>100MB）。建议复用用户本机 Python，没有时再引导安装。
2. **杀毒软件误报**：Electron 客户端启动 Python 子进程可能触发 Windows Defender 误报，建议做代码签名。
3. **macOS 支持**：当前 `desktop/package.json` 仅配置 `win` target，mac 版本后续单独做。
4. **两个入口状态隔离**：用户可能同时在浏览器打开网页版和在客户端操作，需通过 `client_mode` 参数清晰区分。
5. **不要 fork 清洗逻辑**：本地模式必须复用 `backend/app` 现有代码，避免维护两套逻辑。
6. **Electron 22 与 44 的兼容性**：写 `main.js` 时避免使用 Electron 25+ 专属 API，为后续 Win7 版降低迁移成本。
7. **Win7 版 Python 版本锁定**：Win7 兼容版最高只能用 Python 3.9.x，需检查 backend 是否有 3.10+ 专属语法（如 `match...case`）。
8. **网络监听工具验证**：最终验收时建议用抓包工具确认客户端没有向云端传输大量网页源码。
