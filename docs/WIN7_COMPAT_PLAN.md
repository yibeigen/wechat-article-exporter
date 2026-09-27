<!-- 本文档是 Win7 兼容方案的唯一权威说明，任何 AI Agent / 开发者接手本项目请先读这里 -->
<!-- 目的：确保"同一份代码、两套构建"的策略不会因换对话窗口或换 AI 工具而丢失 -->

# Win7 兼容方案（Win7 Compat Plan）

> 更新时间：2026-09
> 背景：项目有一个活跃的 **Win7 用户**，大量需求来自他。本方案用于让 Win7 用户也能使用本软件。

## 一、结论先行

**做"Win7 专属桌面版"，代码层改动极小。** 核心策略是 **「同一份代码、两套构建」**：
不复制源码、不建新项目文件夹，只在现有项目内增加 Win7 专用的配置与打包脚本，
最终产出两个安装包分别发给 Win10/11 用户和 Win7 用户。

## 二、为什么必须"两套构建"（不能一套通吃）

新旧技术栈存在**硬性互斥**，操作系统层面无法合并：

| 组件 | Win10/11 版用 | Win7 版必须用 | 冲突原因 |
|---|---|---|---|
| Electron | 44（新版） | **22.x** | Electron 23+ 强制要求 Win10 1809+，44 在 Win7 上装不上 |
| Python | 3.10+ | **3.8.10** | Python 官方 3.9 起放弃 Win7 |
| Chromium(Playwright) | 最新 | **1.37.x 内置的 Chromium 109** | Chromium 110+ 不再支持 Win7 |

三者缺一不可，**不能只靠"最新版 + 兼容模式"**——这是系统级限制。

## 三、兼容性审计结论（已逐项检查）

| 检查项 | 结果 |
|---|---|
| 后端 Python 语法 | ✅ 无 `list[int]` 泛型、无 `match` 语句；唯一 3.9+ 语法是 `backend/app/scrapers/cto51.py` 两处 `removesuffix()`（需改为切片写法，行为等价） |
| Electron 主进程 desktop/main.js | ✅ 用了 `replaceAll`，Electron 22 的 Chromium 108 / Node 16.17 均支持；无 `node:` 新前缀模块 |
| Playwright 用法 | ✅ 全是基础 API（launch / new_context / new_page），1.37 通用 |
| 前端 app.html | ✅ 无 `Object.groupBy` / `toSorted` / `structuredClone` 等新语法，Chromium 108 全兼容 |
| 微信扫码登录 | ✅ 后端 sync_playwright，与系统无关 |

## 四、改造清单（实施时执行）

| 动作 | 文件 | 说明 |
|---|---|---|
| 🆕 新增 | `requirements-win7.txt` | 锁定 Python 3.8 兼容的依赖版本清单（fastapi/pydantic/httpx 等选最后支持 3.8 的版本） |
| 🆕 新增 | `scratch/make_win7_package.py` | Win7 版打包脚本（仿照现有 `scratch/make_local_package.py`，产物输出到 `dist/win7/`） |
| 🆕 新增 | `desktop/package.json` 的 Win7 构建配置 | 通过 electron-builder 指定 `electronVersion: 22.x`，产出 Win7 安装包 |
| ✏️ 修改 | `backend/app/scrapers/cto51.py` 两行 | `removesuffix()` → 切片写法，**行为完全等价**，Win10 上无任何变化 |
| ✏️ 修改 | `start.bat` 提示语 | 允许 "Python 3.8 或 3.10+" |

> 注意：日常开发默认跑 Win10/11 链（现有 `requirements.txt` + 现有打包脚本），
> Win7 相关文件全部是"新增在旁边"，不触碰现有构建流程与产物（产物隔离在 `dist/win7/`）。

## 五、测试策略

- **Win11 可测功能全流程**：旧技术栈向后兼容（Electron 22 / Python 3.8 / Chromium 109 均可在 Win11 运行）。
- **测不到的部分**：Win7 特有系统问题——VC++ 运行库缺失、旧字体、性能差异（老机器慢、内存小）。
- **发布前兜底**：在 Win7 虚拟机（VMware/VirtualBox 装 Win7 SP1）快速过一遍，或让 Win7 用户实际安装反馈。

## 六、风险与注意事项

1. **双栈测试成本**：以后每次改功能，需在 Win10/11 和 Win7 两套环境各测一遍（改的是同一份代码）。
2. **Electron 22 已 EOL**：有已知安全漏洞，官方停止维护——对个人工具可接受，不适合公开发布。
3. **Win7 用户首次安装较重**：要装 Python 3.8 + Chromium 109 内核（约 150MB）。
4. **依赖冻结**：Win7 版依赖锁死后，未来的依赖升级只服务 Win10/11 版——这是可控的取舍。
5. **动手前先 git commit 存档**：任何实施改动前必须先在 `main` 分支提交一次当前状态，保证可随时回退。

## 七、交付物

```
dist/
├── win10/   ← 正常版安装包（发给 Win10/11 用户）
└── win7/    ← Win7 版安装包（发给 Win7 用户）
```
