# 微信公众号桌面客户端（公号三刀逆向与自研落地全景架构规范）

## 1. 逆向核心成果梳理 (已保存在 `docs/sanji_source/`)
已完成对 `sanji-1.2.1-setup.exe` 的完整解包与主进程 JS 混淆代码的自动化解码（生成文件：`docs/sanji_source/out/main/index_decoded.js`）。

### 1.1 微信凭证截获四步链路 (CaptureRunner & InterceptProxy)
1. **自签 CA 证书生成**：
   - 依赖 `node-forge` 在本地即时生成根证书 `ca.key.pem` 与 `ca.cert.pem`。
2. **静默注入受信任证书库**：
   - 调用系统原生命令 `certutil.exe -user -addstore Root <certPath>`（Mac 对应 `/usr/bin/security add-trusted-cert -r trustRoot`）。
3. **系统局部代理拦截**：
   - 开启本地代理端口（如 `127.0.0.1:8899`），仅对 `mp.weixin.qq.com` 流量进行定向拦截嗅探。
4. **微信流量嗅探与凭证提取**：
   - 用户在电脑微信点开文章时，嗅探到请求头并提取：
     - `uin`：微信用户身份唯一标识
     - `key`：微信客户端签发的阅读通行私钥
     - `pass_ticket`：微信票据
     - `appmsg_token`：文章会话 Token
     - `rawCookie`：微信阅读会话 Cookie
   - 存入本地 SQLite / 文件存储，凭证有效期约 30 分钟。

### 1.2 文章抓取与导出引擎 (FetchRunner & ExportRunner)
- **历史群发文章流**：走 `https://mp.weixin.qq.com/mp/profile_ext?action=getmsg` 接口按消息流分页拉取。
- **专栏/合集**：走 `https://mp.weixin.qq.com/mp/appmsgalbum` 接口免凭证并发读取。
- **本地多格式转换**：内置 `turndown`（Markdown）、`docx`（Word）、`write-excel-file`（Excel），本地渲染 HTML/PDF。

### 1.3 商业化与架构
- **0 边际成本**：抓取、解析、存储 100% 消耗用户本地电脑算力和家庭宽带。
- **云端服务唯一职责**：极轻量 API 负责校验用户的 VIP 激活码与每日配额上报。

---

## 2. 我们的双轨产品形态

### 轨道 A：Web 网页在线版（SaaS 模式，免安装）
- **适用场景**：全网多平台抓取（CSDN、知乎、微博、掘金、博客园、51CTO）+ 微信合集/专辑一键打包 + 批量文章链接导出。
- **状态**：已部署至生产服务器 `https://doc.305758.xyz/app`，运行正常。

### 轨道 B：桌面客户端版（BlogDistiller Desktop Client，EXE/DMG）
- **适用场景**：零门槛截获电脑微信凭证，全自动拉取任意微信公众号全部历史文章，支持激活码离线/在线授权。
- **源码参考**：`docs/sanji_source/`。
