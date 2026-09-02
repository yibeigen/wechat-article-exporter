process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";

const { app, BrowserWindow, ipcMain, dialog, shell } = require("electron");
const path = require("path");
const fs = require("fs");
const http = require("http");
const https = require("https");
const net = require("net");
const tls = require("tls");
const url = require("url");
const os = require("os");
const { exec, execFile } = require("child_process");
const forge = require("node-forge");
const docx = require("docx");
const writeXlsxFile = require("write-excel-file/node");
const { imageSize } = require("image-size");
const { parseHTML } = require("linkedom");
const TurndownService = require("turndown");
const turndownService = new TurndownService({
    headingStyle: "atx",
    codeBlockStyle: "fenced",
    emDelimiter: "*"
});
const zlib = require("zlib");
const DEFAULT_TARGET = "mp.weixin.qq.com";
const DATA_DIR = path.join(os.homedir(), ".blogdistiller_data");
const CERTS_DIR = path.join(DATA_DIR, "certs");
const CACHE_DIR = path.join(DATA_DIR, "cache");
const DEFAULT_EXPORT_DIR = path.join(os.homedir(), "Downloads", "BlogDistiller文章导出");
const AUTH_FILE = path.join(DATA_DIR, "auth.json");

function decodeResponseBody(buffer, encoding) {
    if (!buffer || buffer.length === 0) return "";
    try {
        const enc = (encoding || "").toLowerCase().trim();
        if (enc === "gzip") {
            return zlib.gunzipSync(buffer).toString("utf8");
        } else if (enc === "deflate") {
            return zlib.inflateSync(buffer).toString("utf8");
        } else if (enc === "br") {
            return zlib.brotliDecompressSync(buffer).toString("utf8");
        }
    } catch(e) {
        // 解压异常降级为 utf8 字符串
    }
    return buffer.toString("utf8");
}

process.on("uncaughtException", (err) => console.error("[UncaughtException]", err));
process.on("unhandledRejection", (err) => console.error("[UnhandledRejection]", err));

if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
if (!fs.existsSync(CERTS_DIR)) fs.mkdirSync(CERTS_DIR, { recursive: true });
if (!fs.existsSync(CACHE_DIR)) fs.mkdirSync(CACHE_DIR, { recursive: true });
if (!fs.existsSync(DEFAULT_EXPORT_DIR)) fs.mkdirSync(DEFAULT_EXPORT_DIR, { recursive: true });

let mainWindow = null;
let proxyInstance = null;
let mpLoginWindow = null;

// 微信凭证状态 (支持客户端嗅探凭证 + 官方公众平台直连 Token)
let wechatAuth = {
    captured: false,
    uin: "",
    key: "",
    pass_ticket: "",
    appmsg_token: "",
    wap_sid2: "",
    biz: "",
    captured_at: null,
    mpToken: "",
    mpCookie: "",
    mpConnected: false
};

const MP_SESSION_PATH = path.join(DATA_DIR, "wechat_session.json");
if (fs.existsSync(MP_SESSION_PATH)) {
    try {
        const mpSaved = JSON.parse(fs.readFileSync(MP_SESSION_PATH, "utf8"));
        if (mpSaved && mpSaved.token && mpSaved.cookie) {
            wechatAuth.mpToken = mpSaved.token;
            wechatAuth.mpCookie = mpSaved.cookie;
            wechatAuth.mpConnected = true;
            console.log("[BlogDistiller] 微信公众平台官方通道已恢复, Token:", wechatAuth.mpToken);
        }
    } catch(e) {}
}

// 待自动抓取的公众号目标
let pendingAutoFetchTarget = null;

// =========================================================================
// 1. 本地断点缓存管理 (ArticleCacheManager)
// =========================================================================
class ArticleCacheManager {
    static getCacheFilePath(biz) {
        const safeBiz = (biz || "default").replace(/[^a-zA-Z0-9_-]/g, "");
        return path.join(CACHE_DIR, `articles_${safeBiz}.json`);
    }

    static loadCache(biz) {
        try {
            const filePath = this.getCacheFilePath(biz);
            if (fs.existsSync(filePath)) {
                return JSON.parse(fs.readFileSync(filePath, "utf8")) || {};
            }
        } catch(e) {}
        return {};
    }

    static saveArticle(biz, articleUrl, articleData) {
        try {
            const filePath = this.getCacheFilePath(biz);
            const cache = this.loadCache(biz);
            cache[articleUrl] = {
                ...articleData,
                cached_at: new Date().toISOString()
            };
            fs.writeFileSync(filePath, JSON.stringify(cache, null, 2), "utf8");
        } catch(e) {}
    }
}

function fetchHtmlDirect(targetUrl) {
    return new Promise((resolve) => {
        try {
            const u = new URL(targetUrl);
            const client = u.protocol === "http:" ? http : https;
            const req = client.get(targetUrl, {
                headers: {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
                },
                rejectUnauthorized: false,
                timeout: 8000
            }, (res) => {
                if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
                    return fetchHtmlDirect(res.headers.location).then(resolve);
                }
                let chunks = [];
                res.on("data", c => chunks.push(c));
                res.on("end", () => {
                    const encoding = (res.headers["content-encoding"] || "").toLowerCase();
                    const bodyStr = decodeResponseBody(Buffer.concat(chunks), encoding);
                    resolve(bodyStr);
                });
            });
            req.on("error", () => resolve(""));
            req.on("timeout", () => { req.destroy(); resolve(""); });
        } catch(e) {
            resolve("");
        }
    });
}

// =========================================================================
// 2. 证书管理 CertStore (100% 对齐原版三刀)
// =========================================================================
class CertStore {
    constructor(baseDir) {
        this.baseDir = baseDir;
        this.caKeyFile = path.join(baseDir, "ca.key.pem");
        this.caCertFile = path.join(baseDir, "ca.cert.pem");
        this.cache = new Map();
        this.initCA();
        this.leafKeys = forge.pki.rsa.generateKeyPair(2048);
    }

    randomSerial() {
        const bytes = forge.random.getBytesSync(16);
        let hex = forge.util.bytesToHex(bytes);
        const first = parseInt(hex.slice(0, 2), 16) & 0x7f;
        return first.toString(16).padStart(2, "0") + hex.slice(2);
    }

    initCA() {
        if (fs.existsSync(this.caKeyFile) && fs.existsSync(this.caCertFile)) {
            try {
                this.caKey = forge.pki.privateKeyFromPem(fs.readFileSync(this.caKeyFile, "utf8"));
                this.caCert = forge.pki.certificateFromPem(fs.readFileSync(this.caCertFile, "utf8"));
                return;
            } catch (e) {}
        }

        const keys = forge.pki.rsa.generateKeyPair(2048);
        this.caKey = keys.privateKey;
        this.caCert = forge.pki.createCertificate();
        this.caCert.publicKey = keys.publicKey;
        this.caCert.serialNumber = this.randomSerial();
        this.caCert.validity.notBefore = new Date(Date.now() - 24 * 3600 * 1000);
        this.caCert.validity.notAfter = new Date(Date.now() + 10 * 365 * 24 * 3600 * 1000);

        const attrs = [
            { name: "commonName", value: "BlogDistiller Local Credential Helper CA" },
            { name: "organizationName", value: "BlogDistiller" }
        ];
        this.caCert.setSubject(attrs);
        this.caCert.setIssuer(attrs);
        this.caCert.setExtensions([
            { name: "basicConstraints", cA: true, critical: true },
            { name: "keyUsage", critical: true, keyCertSign: true, cRLSign: true }
        ]);
        this.caCert.sign(this.caKey, forge.md.sha256.create());

        fs.writeFileSync(this.caKeyFile, forge.pki.privateKeyToPem(this.caKey));
        fs.writeFileSync(this.caCertFile, forge.pki.certificateToPem(this.caCert));
    }

    leafFor(hostname) {
        if (this.cache.has(hostname)) return this.cache.get(hostname);

        const cert = forge.pki.createCertificate();
        cert.publicKey = this.leafKeys.publicKey;
        cert.serialNumber = this.randomSerial();
        cert.validity.notBefore = new Date(Date.now() - 24 * 3600 * 1000);
        cert.validity.notAfter = new Date(Date.now() + 365 * 24 * 3600 * 1000);

        cert.setSubject([{ name: "commonName", value: hostname }]);
        cert.setIssuer(this.caCert.subject.attributes);
        cert.setExtensions([
            { name: "basicConstraints", cA: false, critical: true },
            { name: "keyUsage", critical: true, digitalSignature: true, keyEncipherment: true },
            { name: "extKeyUsage", serverAuth: true },
            { name: "subjectAltName", altNames: [{ type: 2, value: hostname }] }
        ]);
        cert.sign(this.caKey, forge.md.sha256.create());

        const pair = {
            keyPem: forge.pki.privateKeyToPem(this.leafKeys.privateKey),
            certPem: forge.pki.certificateToPem(cert)
        };
        this.cache.set(hostname, pair);
        return pair;
    }
}

// =========================================================================
// 3. 原版三刀 InterceptProxy 中间人代理引擎
// =========================================================================
class InterceptProxy {
    constructor(certStore, onCaptured) {
        this.certStore = certStore;
        this.onCaptured = onCaptured;
        this.target = DEFAULT_TARGET;
        this.sockets = new Set();

        this.intercept = http.createServer((req, res) => {
            this.onDecryptedRequest(req, res);
        });

        this.tls = tls.createServer({
            SNICallback: (serverName, cb) => {
                try {
                    const host = serverName || this.target;
                    const pair = this.certStore.leafFor(host);
                    const ctx = tls.createSecureContext({
                        key: pair.keyPem,
                        cert: pair.certPem,
                        ca: fs.readFileSync(this.certStore.caCertFile, "utf8")
                    });
                    cb(null, ctx);
                } catch (err) {
                    cb(err);
                }
            }
        });

        this.tls.on("secureConnection", (secureSocket) => {
            this.sockets.add(secureSocket);
            secureSocket.on("close", () => this.sockets.delete(secureSocket));
            this.intercept.emit("connection", secureSocket);
        });

        this.outer = http.createServer((req, res) => {
            if (req.url === "/proxy.pac" || req.url === "/") {
                const pac = `function FindProxyForURL(url, host) {\n  if (host === '${this.target}') return 'PROXY 127.0.0.1:${this.port}; DIRECT';\n  return 'DIRECT';\n}`;
                res.writeHead(200, { "Content-Type": "application/x-ns-proxy-autoconfig" });
                res.end(pac);
                return;
            }
            res.writeHead(404);
            res.end();
        });

        this.outer.on("connect", (req, socket, head) => {
            this.sockets.add(socket);
            socket.on("close", () => this.sockets.delete(socket));

            const parts = (req.url || "").split(":");
            const host = parts[0];
            const port = parseInt(parts[1] || "443", 10);

            if (host === this.target || host.endsWith("weixin.qq.com") || host.endsWith("qq.com")) {
                socket.write("HTTP/1.1 200 Connection Established\r\n\r\n", () => {
                    this.tls.emit("connection", socket);
                    if (head && head.length) socket.unshift(head);
                });
                return;
            }

            this.tunnelPassthrough(socket, host, port, head);
        });
    }

    tunnelPassthrough(socket, host, port, head) {
        const remote = net.connect(port, host, () => {
            socket.write("HTTP/1.1 200 Connection Established\r\n\r\n");
            if (head && head.length) remote.write(head);
            remote.pipe(socket);
            socket.pipe(remote);
        });
        remote.on("error", () => socket.destroy());
    }

    onDecryptedRequest(req, res) {
        const rawCookie = req.headers["cookie"] || "";
        const reqUrl = req.url || "";
        const referer = req.headers["referer"] || "";
        
        let bodyChunks = [];
        req.on("data", chunk => bodyChunks.push(chunk));
        req.on("end", () => {
            const bodyBuffer = Buffer.concat(bodyChunks);
            const bodyStr = bodyBuffer.toString("utf8");

            this.parseAndFire(reqUrl, rawCookie, [], bodyStr, referer);

            const targetHost = req.headers["host"] || this.target;
            const forwardHeaders = { ...req.headers };
            delete forwardHeaders["proxy-connection"];
            forwardHeaders["host"] = targetHost;
            if (req.method === "POST" || req.method === "PUT") {
                forwardHeaders["content-length"] = bodyBuffer.length;
            }

            const forwardReq = https.request(`https://${targetHost}${reqUrl}`, {
                method: req.method,
                headers: forwardHeaders,
                rejectUnauthorized: false
            }, (forwardRes) => {
                const setCookies = forwardRes.headers["set-cookie"] || [];
                this.parseAndFire(reqUrl, rawCookie, setCookies, "", referer);
                
                res.writeHead(forwardRes.statusCode, forwardRes.headers);

                let resChunks = [];
                forwardRes.on("data", (chunk) => {
                    resChunks.push(chunk);
                    try { res.write(chunk); } catch(e) {}
                });
                forwardRes.on("end", () => {
                    try { res.end(); } catch(e) {}
                    try {
                        const encoding = (forwardRes.headers["content-encoding"] || "").toLowerCase();
                        const resBodyStr = decodeResponseBody(Buffer.concat(resChunks), encoding);
                        parseResponseArticles(reqUrl, resBodyStr);
                    } catch(e) {}
                });
                forwardRes.on("error", () => {
                    try { res.end(); } catch(e) {}
                });
            });

            forwardReq.on("error", () => {
                try { res.end(); } catch(e) {}
            });

            if (bodyBuffer.length > 0) {
                forwardReq.write(bodyBuffer);
            }
            forwardReq.end();
        });
    }

    parseAndFire(reqUrl, cookieHeader, setCookies = [], bodyStr = "", refererHeader = "") {
        try {
            const u = new URL(reqUrl, `https://${this.target}`);
            let uin = u.searchParams.get("uin") || "";
            let key = u.searchParams.get("key") || "";
            let pass_ticket = u.searchParams.get("pass_ticket") || "";
            let appmsg_token = u.searchParams.get("appmsg_token") || "";
            let biz = u.searchParams.get("__biz") || "";

            if (bodyStr) {
                try {
                    const bodyParams = new URLSearchParams(bodyStr);
                    if (!uin) uin = bodyParams.get("uin") || "";
                    if (!key) key = bodyParams.get("key") || "";
                    if (!pass_ticket) pass_ticket = bodyParams.get("pass_ticket") || "";
                    if (!appmsg_token) appmsg_token = bodyParams.get("appmsg_token") || "";
                    if (!biz) biz = bodyParams.get("__biz") || "";
                } catch(e) {}
            }

            if (!biz && refererHeader) {
                const refMatch = refererHeader.match(/__biz=([^&#]+)/);
                if (refMatch && isValidBiz(decodeURIComponent(refMatch[1]))) {
                    biz = decodeURIComponent(refMatch[1]);
                }
            }

            let wap_sid2 = "";
            const cookieStr = [cookieHeader, ...setCookies].join("; ");
            const sidMatch = cookieStr.match(/wap_sid2=([^;]+)/);
            if (sidMatch) wap_sid2 = sidMatch[1];
            const ptMatch = cookieStr.match(/pass_ticket=([^;]+)/);
            if (ptMatch && !pass_ticket) pass_ticket = ptMatch[1];
            const uinMatch = cookieStr.match(/wxuin=([^;]+)/);
            if (uinMatch && !uin) uin = uinMatch[1];

            if (key || pass_ticket || wap_sid2 || appmsg_token || biz) {
                this.onCaptured({ uin, key, pass_ticket, appmsg_token, wap_sid2, biz });
            }
        } catch(e) {}
    }

    listen(port = 8899) {
        return new Promise((resolve) => {
            const onError = (err) => {
                if (err.code === "EADDRINUSE") {
                    console.warn(`[BlogDistiller] 端口 ${port} 占用，转用动态备用端口...`);
                    this.outer.listen(0, "127.0.0.1", () => {
                        const addr = this.outer.address();
                        this.port = typeof addr === "object" && addr ? addr.port : 8899;
                        resolve(this.port);
                    });
                }
            };
            this.outer.once("error", onError);
            this.outer.listen(port, "127.0.0.1", () => {
                this.outer.removeListener("error", onError);
                const addr = this.outer.address();
                this.port = typeof addr === "object" && addr ? addr.port : port;
                console.log(`[BlogDistiller] 代理服务就绪，稳定监听 127.0.0.1:${this.port}`);
                resolve(this.port);
            });
        });
    }

    close() {
        for (const s of this.sockets) s.destroy();
        this.sockets.clear();
        try { this.outer.close(); } catch(e){}
        try { this.tls.close(); } catch(e){}
        try { this.intercept.close(); } catch(e){}
    }
}

// =========================================================================
// 4. Windows 系统代理配置与 WinINet 广播
// =========================================================================
const INET_KEY = "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings";
const REFRESH_SCRIPT = "$s='[DllImport(\"wininet.dll\",SetLastError=true)] public static extern bool InternetSetOption(IntPtr h,int o,IntPtr b,int l);';$t=Add-Type -MemberDefinition $s -Name W -Namespace I -PassThru;$t::InternetSetOption([IntPtr]::Zero,39,[IntPtr]::Zero,0)|Out-Null;$t::InternetSetOption([IntPtr]::Zero,37,[IntPtr]::Zero,0)|Out-Null";
const REFRESH_ENCODED = Buffer.from(REFRESH_SCRIPT, "utf16le").toString("base64");

function applyWindowsPac(port, enable) {
    if (process.platform !== "win32") return;
    const refreshCmd = `powershell -NoProfile -NonInteractive -EncodedCommand ${REFRESH_ENCODED}`;

    if (enable) {
        // 关键：彻底清除任何遗留的 AutoConfigURL，确保 Windows 流量 100% 走 ProxyServer 127.0.0.1:port
        const cmd = `reg delete "${INET_KEY}" /v AutoConfigURL /f 2>nul & reg add "${INET_KEY}" /v ProxyEnable /t REG_DWORD /d 1 /f & reg add "${INET_KEY}" /v ProxyServer /t REG_SZ /d "127.0.0.1:${port}" /f`;
        exec(cmd, () => {
            exec(refreshCmd, () => {
                console.log(`[BlogDistiller] Windows 系统代理已强制激活: 127.0.0.1:${port}`);
            });
        });
    } else {
        const cmd = `reg delete "${INET_KEY}" /v AutoConfigURL /f 2>nul & reg add "${INET_KEY}" /v ProxyEnable /t REG_DWORD /d 0 /f`;
        exec(cmd, () => {
            exec(refreshCmd);
        });
    }
}

function installCaToTrustStore(certFile) {
    if (process.platform !== "win32") return;
    exec(`certutil.exe -user -addstore -f Root "${certFile}"`, (err) => {
        if (!err) console.log("[BlogDistiller] CA 根证书已成功信任");
    });
}

// =========================================================================
// 5. 业务控制流与多页文章抓取
// =========================================================================
function isValidBiz(bizStr) {
    if (!bizStr || typeof bizStr !== "string") return false;
    const clean = bizStr.trim();
    if (clean.includes("${") || clean.includes("window.") || clean === "undefined" || clean.length < 8) {
        return false;
    }
    return /^[A-Za-z0-9+/=]+$/.test(clean);
}

function extractWechatBiz(targetUrl, html, fallbackBiz = "") {
    // 1. Check targetUrl
    let urlMatch = targetUrl.match(/__biz=([^&#]+)/);
    if (urlMatch && isValidBiz(decodeURIComponent(urlMatch[1]).replace(/&amp;/g, "&"))) {
        return decodeURIComponent(urlMatch[1]).replace(/&amp;/g, "&");
    }

    // 2. Check HTML for valid biz patterns (reportOpt, var biz, etc.)
    const patterns = [
        /var\s+biz\s*=\s*"([^"]+)"/i,
        /var\s+appuin\s*=\s*"([^"]+)"/i,
        /biz:\s*"([A-Za-z0-9+/=]{10,})"/i,
        /biz:\s*'([A-Za-z0-9+/=]{10,})'/i,
        /__biz=([A-Za-z0-9+/=]{10,})/
    ];

    for (const pat of patterns) {
        const m = html.match(pat);
        if (m && isValidBiz(m[1])) {
            return m[1];
        }
    }

    // 3. Fallback to proxy captured wechatAuth.biz
    if (fallbackBiz && isValidBiz(fallbackBiz)) {
        return fallbackBiz;
    }

    return "";
}

function sendDebugLog(text, level = "info") {
    const time = new Date().toLocaleTimeString();
    console.log(`[Diagnostic] ${time} [${level.toUpperCase()}] ${text}`);
    if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send("wechat:debug-log", { text, level, time });
    }
}

function parseResponseArticles(reqUrl, resBodyStr) {
    if (!resBodyStr) return;

    sendDebugLog(`[微信嗅探] 收到响应: ${reqUrl.slice(0, 80)} (${resBodyStr.length} 字符)`, "info");

    // 尝试从页面或响应中提取公众号名称
    let detectedAuthor = wechatAuth.author || "微信公众号";
    const nickMatch = resBodyStr.match(/var\s+nickname\s*=\s*"([^"]+)"/i) || resBodyStr.match(/<a[^>]*id="js_name"[^>]*>([\s\S]*?)<\/a>/i);
    if (nickMatch && nickMatch[1].trim()) {
        detectedAuthor = unescapeWechatText(nickMatch[1].replace(/<[^>]+>/g, ""));
        wechatAuth.author = detectedAuthor;
    }

    // 1. profile_ext?action=getmsg (分页历史文章列表)
    if (reqUrl.includes("profile_ext") && reqUrl.includes("action=getmsg")) {
        try {
            const data = JSON.parse(resBodyStr);
            if (data.general_msg_list) {
                const listObj = typeof data.general_msg_list === "string" ? JSON.parse(data.general_msg_list) : data.general_msg_list;
                const msgList = listObj.list || [];
                const extracted = [];
                for (const item of msgList) {
                    const comm = item.comm_msg_info || {};
                    const appInfo = item.app_msg_ext_info;
                    if (!appInfo) continue;
                    const create_time = comm.datetime ? new Date(comm.datetime * 1000).toISOString().split("T")[0] : "";
                    if (appInfo.title && appInfo.content_url) {
                        const cleanUrl = appInfo.content_url.replace(/&amp;/g, "&");
                        extracted.push({
                            id: `art_${comm.id || Date.now()}_0`,
                            title: appInfo.title.replace(/<[^>]+>/g, "").trim(),
                            author: detectedAuthor,
                            url: cleanUrl.startsWith("http") ? cleanUrl : `https://mp.weixin.qq.com${cleanUrl}`,
                            create_time,
                            digest: appInfo.digest || "",
                            cover: appInfo.cover || "",
                            is_original: appInfo.copyright_stat === 11 || appInfo.copyright_stat === 1,
                            biz: wechatAuth.biz,
                            status: "pending",
                            fail_reason: ""
                        });
                    }
                }
                if (extracted.length > 0) {
                    sendDebugLog(`[微信嗅探] 实时捕获微信历史文章流: 成功提取 ${extracted.length} 篇！`, "success");
                    if (mainWindow && !mainWindow.isDestroyed()) {
                        mainWindow.webContents.send("wechat:stream-articles", { articles: extracted, author: detectedAuthor });
                    }
                }
            }
        } catch(e) {}
    }

    // 2. profile_ext?action=home 或 authorpage (主页首屏历史文章)
    if (reqUrl.includes("profile_ext") || reqUrl.includes("authorpage") || reqUrl.includes("homepage")) {
        try {
            const msgListMatch = resBodyStr.match(/var\s+msgList\s*=\s*'([^']+)'/i) || resBodyStr.match(/var\s+msgList\s*=\s*({[\s\S]*?});/i);
            if (msgListMatch) {
                const raw = msgListMatch[1].replace(/\\x26quot;/g, '"').replace(/&quot;/g, '"');
                const listObj = JSON.parse(raw);
                const msgList = listObj.list || [];
                const extracted = [];
                for (const item of msgList) {
                    const comm = item.comm_msg_info || {};
                    const appInfo = item.app_msg_ext_info;
                    if (!appInfo) continue;
                    const create_time = comm.datetime ? new Date(comm.datetime * 1000).toISOString().split("T")[0] : "";
                    if (appInfo.title && appInfo.content_url) {
                        const cleanUrl = appInfo.content_url.replace(/&amp;/g, "&");
                        extracted.push({
                            id: `art_${comm.id || Date.now()}_0`,
                            title: appInfo.title.replace(/<[^>]+>/g, "").trim(),
                            author: detectedAuthor,
                            url: cleanUrl.startsWith("http") ? cleanUrl : `https://mp.weixin.qq.com${cleanUrl}`,
                            create_time,
                            digest: appInfo.digest || "",
                            cover: appInfo.cover || "",
                            is_original: appInfo.copyright_stat === 11 || appInfo.copyright_stat === 1,
                            biz: wechatAuth.biz,
                            status: "pending",
                            fail_reason: ""
                        });
                    }
                }
                if (extracted.length > 0) {
                    sendDebugLog(`[微信嗅探] 实时捕获主页首屏文章: 成功提取 ${extracted.length} 篇！`, "success");
                    if (mainWindow && !mainWindow.isDestroyed()) {
                        mainWindow.webContents.send("wechat:stream-articles", { articles: extracted, author: detectedAuthor });
                    }
                }
            }
        } catch(e) {}
    }

    // 3. 实时捕获单篇文章页面 (/s/ 或 /s?)
    if (reqUrl.startsWith("/s/") || reqUrl.startsWith("/s?")) {
        try {
            const parsed = parseSingleArticleFromHtml(resBodyStr, `https://mp.weixin.qq.com${reqUrl}`);
            if (parsed.title && parsed.title !== "未知标题" && !parsed.title.includes("环境异常")) {
                sendDebugLog(`[微信嗅探] 实时捕获正在阅读的推文: 《${parsed.title.slice(0, 22)}...》 (作者: 【${parsed.author}】)`, "success");
                if (mainWindow && !mainWindow.isDestroyed()) {
                    mainWindow.webContents.send("wechat:stream-articles", { articles: [parsed], author: parsed.author });
                }
            }
        } catch(e) {}
    }

    // 4. 通用微信文章流遍历扫描 (覆盖搜一搜、合集、推荐、主页历史流等所有页面)
    try {
        const urlPattern = /(?:https?:)?\/\/mp\.weixin\.qq\.com\/s(?:\/|(?:\?[^"'\s<>]*))/g;
        let match;
        const autoExtracted = [];
        const seenUrls = new Set();
        while ((match = urlPattern.exec(resBodyStr)) !== null) {
            let rawUrl = match[0];
            if (rawUrl.startsWith("//")) rawUrl = "https:" + rawUrl;
            const cleanUrl = rawUrl.replace(/&amp;/g, "&").replace(/\\x26/g, "&");
            if (!seenUrls.has(cleanUrl)) {
                seenUrls.add(cleanUrl);
                const startPos = Math.max(0, match.index - 400);
                const endPos = Math.min(resBodyStr.length, match.index + 400);
                const snippet = resBodyStr.slice(startPos, endPos);
                
                const titleMatch = snippet.match(/"title"\s*:\s*"([^"]+)"/) ||
                                   snippet.match(/title="([^"]+)"/) ||
                                   snippet.match(/<h[1-4][^>]*>([\s\S]*?)<\/h[1-4]>/) ||
                                   snippet.match(/<a[^>]+>([\s\S]*?)<\/a>/);
                
                let title = titleMatch ? unescapeWechatText(titleMatch[1].replace(/<[^>]+>/g, "")) : "";
                if (title && title.length > 2 && !title.includes("weixin.qq.com") && !title.includes("JavaScript")) {
                    autoExtracted.push({
                        id: `art_${Date.now()}_${autoExtracted.length}`,
                        title,
                        author: detectedAuthor,
                        url: cleanUrl,
                        create_time: "",
                        digest: "",
                        cover: "",
                        is_original: true,
                        biz: wechatAuth.biz || "",
                        status: "pending",
                        fail_reason: ""
                    });
                }
            }
        }
        if (autoExtracted.length > 0) {
            sendDebugLog(`[微信嗅探] 🌟 深度扫描自动提取到 ${autoExtracted.length} 篇推文！`, "success");
            if (mainWindow && !mainWindow.isDestroyed()) {
                mainWindow.webContents.send("wechat:stream-articles", { articles: autoExtracted, author: detectedAuthor });
            }
        }
    } catch(e) {}
}

let lastLoggedKey = "";
function handleCapturedAuth(data) {
    const isNewKey = data.key && data.key !== wechatAuth.key;
    wechatAuth = {
        captured: true,
        uin: data.uin || wechatAuth.uin,
        key: data.key || wechatAuth.key,
        pass_ticket: data.pass_ticket || wechatAuth.pass_ticket,
        appmsg_token: data.appmsg_token || wechatAuth.appmsg_token,
        wap_sid2: data.wap_sid2 || wechatAuth.wap_sid2,
        biz: (data.biz && isValidBiz(data.biz)) ? data.biz : wechatAuth.biz,
        captured_at: new Date().toLocaleTimeString()
    };
    try {
        fs.writeFileSync(AUTH_FILE, JSON.stringify(wechatAuth, null, 2), "utf8");
    } catch(e) {}

    if (isNewKey && wechatAuth.key !== lastLoggedKey) {
        lastLoggedKey = wechatAuth.key;
        sendDebugLog(`[通信状态] 成功截获并更新微信最新会话凭证 (uin: ${wechatAuth.uin || "已具备"}, key: ${wechatAuth.key ? wechatAuth.key.slice(0, 8) + "..." : "已具备"})`, "success");
    }
    if (mainWindow) {
        mainWindow.webContents.send("wechat:status-change", wechatAuth);
    }

    // 自动触发全量文章拉取
    if (pendingAutoFetchTarget && (wechatAuth.key || wechatAuth.pass_ticket || wechatAuth.wap_sid2)) {
        const targetInfo = pendingAutoFetchTarget;
        pendingAutoFetchTarget = null;
        sendDebugLog(`[自动就绪] 凭证已就绪，立即自动触发对【${targetInfo.author || "公众号"}】的全量历史文章拉取！`, "info");
        if (mainWindow) {
            mainWindow.webContents.send("wechat:auto-trigger-search", targetInfo);
        }
    }
}

function fetchPageHtml(targetUrl, maxRedirects = 5) {
    return new Promise((resolve, reject) => {
        if (maxRedirects <= 0) return reject(new Error("链接重定向次数过多"));
        const u = new URL(targetUrl);
        const mod = u.protocol === "http:" ? http : https;
        const options = {
            hostname: u.hostname,
            path: u.pathname + u.search,
            headers: {
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat",
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            }
        };

        const cookieArr = [];
        if (wechatAuth.wap_sid2) cookieArr.push(`wap_sid2=${wechatAuth.wap_sid2}`);
        if (wechatAuth.pass_ticket) cookieArr.push(`pass_ticket=${wechatAuth.pass_ticket}`);
        if (wechatAuth.uin) cookieArr.push(`wxuin=${wechatAuth.uin}`);
        if (cookieArr.length > 0) options.headers["cookie"] = cookieArr.join("; ");

        mod.get(options, (res) => {
            if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
                let loc = res.headers.location;
                if (!loc.startsWith("http")) {
                    loc = `${u.protocol}//${u.hostname}${loc.startsWith("/") ? "" : "/"}${loc}`;
                }
                return resolve(fetchPageHtml(loc, maxRedirects - 1));
            }

            let data = "";
            res.on("data", chunk => data += chunk);
            res.on("end", () => resolve(data));
        }).on("error", reject);
    });
}

function unescapeWechatText(text) {
    if (!text) return "";
    return text
        .replace(/\\x26quot;/g, '"')
        .replace(/\\x26amp;/g, '&')
        .replace(/\\x26lt;/g, '<')
        .replace(/\\x26gt;/g, '>')
        .replace(/\\x26nbsp;/g, ' ')
        .replace(/\\x0a/g, ' ')
        .replace(/\\x0d/g, '')
        .replace(/\\n/g, ' ')
        .replace(/\\r/g, '')
        .replace(/&quot;/g, '"')
        .replace(/&amp;/g, '&')
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .replace(/&nbsp;/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
}

function parseSingleArticleFromHtml(html, targetUrl) {
    let title = "未知标题";
    let author = "微信公众号";
    let create_time = new Date().toISOString().split("T")[0];
    let digest = "";

    const titleMatch = html.match(/<meta\s+property="og:title"\s+content="([^"]*)"/i) ||
                       html.match(/<h1[^>]*class="[^"]*activity-title[^"]*"[^>]*>([\s\S]*?)<\/h1>/i) ||
                       html.match(/<h1[^>]*class="[^"]*rich_media_title[^"]*"[^>]*>([\s\S]*?)<\/h1>/i) ||
                       html.match(/var\s+msg_title\s*=\s*"([^"]*)";/i) ||
                       html.match(/title:\s*"([^"]*)"/i);
    if (titleMatch) {
        title = unescapeWechatText(titleMatch[1].replace(/<[^>]+>/g, ""));
    }

    // 优先提取公众号主体全称 (例如：异环工坊)
    const accountNameMatch = html.match(/<a[^>]*id="js_name"[^>]*>([\s\S]*?)<\/a>/i) ||
                             html.match(/<strong[^>]*class="[^"]*profile_nickname[^"]*"[^>]*>([\s\S]*?)<\/strong>/i) ||
                             html.match(/<span[^>]*class="[^"]*profile_nickname[^"]*"[^>]*>([\s\S]*?)<\/span>/i) ||
                             html.match(/<a[^>]*class="[^"]*rich_media_meta_nickname[^"]*"[^>]*>([\s\S]*?)<\/a>/i) ||
                             html.match(/var\s+nickname\s*=\s*htmlDecode\("([^"]*)"\)/i) ||
                             html.match(/var\s+nickname\s*=\s*"([^"]*)";/i) ||
                             html.match(/var\s+user_name\s*=\s*"([^"]*)";/i) ||
                             html.match(/nickname:\s*"([^"]*)"/i) ||
                             html.match(/<meta\s+property="og:site_name"\s+content="([^"]*)"/i);
    if (accountNameMatch && accountNameMatch[1].trim() && !accountNameMatch[1].includes("miniprogram") && accountNameMatch[1] !== "微信公众平台") {
        author = unescapeWechatText(accountNameMatch[1].replace(/<[^>]+>/g, ""));
    } else {
        const authorMatch = html.match(/<meta\s+property="og:article:author"\s+content="([^"]*)"/i) ||
                            html.match(/<span[^>]*id="js_author_name"[^>]*>([\s\S]*?)<\/span>/i);
        if (authorMatch && authorMatch[1].trim() && authorMatch[1] !== "微信公众平台") {
            author = unescapeWechatText(authorMatch[1].replace(/<[^>]+>/g, ""));
        } else if (wechatAuth.author && wechatAuth.author !== "微信公众号") {
            author = wechatAuth.author;
        }
    }

    const biz = extractWechatBiz(targetUrl, html, wechatAuth.biz);

    const digestMatch = html.match(/<meta\s+property="og:description"\s+content="([^"]*)"/i);
    if (digestMatch) digest = unescapeWechatText(digestMatch[1]);

    const dateMatch = html.match(/var\s+ct\s*=\s*"(\d+)";/i) || html.match(/createTime\s*=\s*'(\d+)'/i);
    if (dateMatch) {
        const ts = parseInt(dateMatch[1], 10);
        if (ts > 0) create_time = new Date(ts * 1000).toISOString().split("T")[0];
    }

    return {
        id: `art_${Date.now()}_0`,
        title,
        author,
        url: targetUrl,
        create_time,
        digest,
        is_original: true,
        biz,
        status: "pending",
        fail_reason: ""
    };
}

function formatWechatUin(uin) {
    if (!uin) return "";
    const str = uin.toString().trim();
    if (/^\d+$/.test(str)) {
        return Buffer.from(str).toString("base64");
    }
    return str;
}

// 多页并发/翻页抓取全部历史文章
async function fetchWechatHistoryArticles(biz, author = "微信公众号", maxArticles = 0, progressCb = null) {
    if (!biz || !isValidBiz(biz)) {
        throw new Error("公众号 biz 标识无效");
    }

    let offset = 0;
    const count = 10;
    let articles = [];
    let hasMore = true;
    let rateLimitRetries = 0;
    let lastErrorText = "";

    const safeUin = formatWechatUin(wechatAuth.uin);

    sendDebugLog(`[历史翻页] 开始向微信请求【${author}】(biz: ${biz}) 的历史文章列表...`, "info");
    sendDebugLog(`[凭证参数] uin=${safeUin || "缺省"}, key=${wechatAuth.key ? wechatAuth.key.slice(0, 8) + "..." : "缺省"}, pass_ticket=${wechatAuth.pass_ticket ? "已具备" : "缺省"}, wap_sid2=${wechatAuth.wap_sid2 ? "已具备" : "缺省"}`, "info");

    while (hasMore) {
        const queryParams = new URLSearchParams({
            action: "getmsg",
            __biz: biz,
            f: "json",
            offset: offset.toString(),
            count: count.toString(),
            is_ok: "1",
            scene: "124",
            uin: safeUin,
            key: wechatAuth.key || "",
            pass_ticket: wechatAuth.pass_ticket || "",
            appmsg_token: wechatAuth.appmsg_token || "",
            wxtoken: "777",
            x5: "0"
        });

        const apiUrl = `https://mp.weixin.qq.com/mp/profile_ext?${queryParams.toString()}`;
        const headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat",
            "accept": "application/json, text/javascript, */*; q=0.01",
            "x-requested-with": "XMLHttpRequest",
            "referer": `https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=${biz}&scene=124#wechat_redirect`
        };

        const cookieArr = [];
        if (wechatAuth.wap_sid2) cookieArr.push(`wap_sid2=${wechatAuth.wap_sid2}`);
        if (wechatAuth.pass_ticket) cookieArr.push(`pass_ticket=${wechatAuth.pass_ticket}`);
        if (safeUin) {
            cookieArr.push(`wxuin=${safeUin}`);
            cookieArr.push(`uin=${safeUin}`);
        }
        if (wechatAuth.key) cookieArr.push(`key=${wechatAuth.key}`);
        if (cookieArr.length > 0) headers["cookie"] = cookieArr.join("; ");

        const resData = await new Promise((resolve, reject) => {
            const req = https.get(apiUrl, { headers }, (res) => {
                let text = "";
                res.on("data", chunk => text += chunk);
                res.on("end", () => {
                    try {
                        resolve(JSON.parse(text));
                    } catch(e) {
                        resolve({ ret: -1, errmsg: text });
                    }
                });
            });
            req.on("error", reject);
            req.setTimeout(12000, () => req.destroy(new Error("网络请求超时")));
        });

        sendDebugLog(`[接口返回] 偏移量 offset=${offset}, 微信响应: ret=${resData.ret}, errmsg="${resData.errmsg || "ok"}"`, resData.ret === 0 ? "success" : "warn");

        if (resData.ret === 0 && resData.general_msg_list) {
            rateLimitRetries = 0;
            const listObj = typeof resData.general_msg_list === "string" ? JSON.parse(resData.general_msg_list) : resData.general_msg_list;
            const msgList = listObj.list || [];

            for (const item of msgList) {
                const comm = item.comm_msg_info || {};
                const appInfo = item.app_msg_ext_info;
                if (!appInfo) continue;

                const create_time = comm.datetime ? new Date(comm.datetime * 1000).toISOString().split("T")[0] : "";
                
                // 头条文章
                if (appInfo.title && appInfo.content_url) {
                    const cleanUrl = appInfo.content_url.replace(/&amp;/g, "&");
                    articles.push({
                        id: `art_${comm.id || Date.now()}_0`,
                        title: appInfo.title.replace(/<[^>]+>/g, "").trim(),
                        author,
                        url: cleanUrl.startsWith("http") ? cleanUrl : `https://mp.weixin.qq.com${cleanUrl}`,
                        create_time,
                        digest: appInfo.digest || "",
                        cover: appInfo.cover || "",
                        is_original: appInfo.copyright_stat === 11 || appInfo.copyright_stat === 1,
                        biz,
                        status: "pending",
                        fail_reason: ""
                    });
                }

                // 次条与多图文
                if (appInfo.multi_app_msg_item_list && Array.isArray(appInfo.multi_app_msg_item_list)) {
                    for (let subIdx = 0; subIdx < appInfo.multi_app_msg_item_list.length; subIdx++) {
                        const sub = appInfo.multi_app_msg_item_list[subIdx];
                        if (sub.title && sub.content_url) {
                            const cleanSubUrl = sub.content_url.replace(/&amp;/g, "&");
                            articles.push({
                                id: `art_${comm.id || Date.now()}_${subIdx + 1}`,
                                title: sub.title.replace(/<[^>]+>/g, "").trim(),
                                author,
                                url: cleanSubUrl.startsWith("http") ? cleanSubUrl : `https://mp.weixin.qq.com${cleanSubUrl}`,
                                create_time,
                                digest: sub.digest || "",
                                cover: sub.cover || "",
                                is_original: sub.copyright_stat === 11 || sub.copyright_stat === 1,
                                biz,
                                status: "pending",
                                fail_reason: ""
                            });
                        }
                    }
                }
            }

            if (progressCb) progressCb(`已快速索引 ${articles.length} 篇文章目录...`, articles.length, maxArticles || articles.length, [...articles]);

            if (maxArticles > 0 && articles.length >= maxArticles) {
                articles = articles.slice(0, maxArticles);
                break;
            }

            const canContinue = resData.can_msg_continue == 1 || resData.can_msg_continue === true || (msgList && msgList.length >= count);
            if (canContinue && msgList.length > 0) {
                if (resData.next_offset !== undefined && resData.next_offset !== null && Number(resData.next_offset) > offset) {
                    offset = Number(resData.next_offset);
                } else {
                    offset += count;
                }
                sendDebugLog(`[多页拉取] 正在自动翻页 (offset=${offset})，已索引 ${articles.length} 篇...`, "info");
                // 拟人化随机安全延迟 (600ms~1000ms)，完美模拟正常微信滑动浏览，坚决杜绝风控
                const sleepTime = 600 + Math.floor(Math.random() * 400);
                await new Promise(r => setTimeout(r, sleepTime));
            } else {
                hasMore = false;
            }
        } else if (resData.ret === -3) {
            lastErrorText = "微信会话已过期 (ret=-3, no session)。请在电脑微信中打开任意一篇公众号推文以激活最新会话凭证。";
            break;
        } else if (resData.ret === -6) {
            if (offset > 0 && rateLimitRetries < 2) {
                rateLimitRetries++;
                sendDebugLog(`[频控保护] 正在翻页中遭遇短时限流，等待 2 秒继续拉取...`, "warn");
                if (progressCb) progressCb(`翻页遇到限流，稍候继续...`, articles.length, maxArticles || 0, [...articles]);
                await new Promise(r => setTimeout(r, 2000));
                continue;
            } else {
                lastErrorText = "微信安全频控限制 (ret=-6)。请稍后重试，或在电脑微信中打开该公众号主页。";
                break;
            }
        } else {
            lastErrorText = `微信接口返回异常 (ret=${resData.ret}, ${resData.errmsg || 'unknown error'})`;
            break;
        }
    }

    sendDebugLog(`[检索结束] 成功索引到 ${articles.length} 篇历史文章。`, articles.length > 0 ? "success" : "warn");

    // 检查本地是否有断点缓存
    const cachedMap = ArticleCacheManager.loadCache(biz);
    articles = articles.map(a => {
        if (cachedMap[a.url] && cachedMap[a.url].content_markdown) {
            return { ...a, status: "completed" };
        }
        return a;
    });

    return {
        articles,
        author,
        biz,
        total: articles.length,
        lastError: lastErrorText
    };
}

// 微信公众平台官方后台快速扫码登录窗口 (官方渠道，零风控秒级全量历史拉取)
function openMpLoginWindow() {
    if (mpLoginWindow && !mpLoginWindow.isDestroyed()) {
        mpLoginWindow.focus();
        return;
    }

    mpLoginWindow = new BrowserWindow({
        width: 1020,
        height: 760,
        title: "微信公众平台官方快速扫码连接 (mp.weixin.qq.com)",
        autoHideMenuBar: true,
        webPreferences: {
            nodeIntegration: false,
            contextIsolation: true
        }
    });

    mpLoginWindow.loadURL("https://mp.weixin.qq.com/");

    const checkNavigation = async (targetUrl) => {
        if (!targetUrl) return;
        const tokenMatch = targetUrl.match(/[?&]token=([^&#]+)/);
        if (tokenMatch) {
            const token = tokenMatch[1];
            const cookies = await mpLoginWindow.webContents.session.cookies.get({ domain: "mp.weixin.qq.com" });
            const cookieStr = cookies.map(c => `${c.name}=${c.value}`).join("; ");
            
            wechatAuth.mpToken = token;
            wechatAuth.mpCookie = cookieStr;
            wechatAuth.mpConnected = true;
            
            fs.writeFileSync(path.join(DATA_DIR, "wechat_session.json"), JSON.stringify({
                cookie: cookieStr,
                token: token,
                updated_at: new Date().toISOString()
            }, null, 2), "utf8");

            sendDebugLog(`[官方通道] 微信公众平台官方通道连接成功 (Token: ${token})，支持任意公众号全量历史一键拉取！`, "success");
            
            if (mainWindow && !mainWindow.isDestroyed()) {
                mainWindow.webContents.send("wechat:mp-status-change", { connected: true, token });
            }

            setTimeout(() => {
                if (mpLoginWindow && !mpLoginWindow.isDestroyed()) {
                    mpLoginWindow.close();
                }
            }, 1200);
        }
    };

    mpLoginWindow.webContents.on("did-navigate", (_, url) => checkNavigation(url));
    mpLoginWindow.webContents.on("did-navigate-in-page", (_, url) => checkNavigation(url));

    mpLoginWindow.on("closed", () => {
        mpLoginWindow = null;
    });
}

// 官方公众平台 search_biz + appmsg 接口拉取全部历史文章 (100% 官方通道，无 ret=-6)
async function fetchArticlesViaMpOfficial(targetName, maxArticles = 0, progressCb = null) {
    const token = wechatAuth.mpToken;
    const cookie = wechatAuth.mpCookie;
    if (!token || !cookie) {
        throw new Error("缺少微信公众平台官方凭证，请先点击顶部【微信官方扫码连接】！");
    }

    sendDebugLog(`[官方直连] 正在通过公众平台官方接口检索公众号【${targetName}】...`, "info");
    if (progressCb) progressCb(`正在通过官方后台检索【${targetName}】全部历史...`, 0, 0);

    // 1. 搜索目标公众号获取 fakeid 与全称
    const searchUrl = `https://mp.weixin.qq.com/cgi-bin/searchbiz?action=search_biz&begin=0&count=5&query=${encodeURIComponent(targetName)}&token=${token}&lang=zh_CN&f=json&ajax=1`;
    const headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Cookie": cookie,
        "Referer": `https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit_v2&action=edit&isNew=1&type=77&createType=0&token=${token}&lang=zh_CN`
    };

    const searchRes = await new Promise((resolve, reject) => {
        https.get(searchUrl, { headers }, (res) => {
            let data = "";
            res.on("data", c => data += c);
            res.on("end", () => {
                try { resolve(JSON.parse(data)); } catch(e) { resolve({ base_resp: { ret: -1 }, list: [] }); }
            });
        }).on("error", reject);
    });

    const bizList = searchRes.list || [];
    if (bizList.length === 0) {
        throw new Error(`未在微信平台搜索到公众号【${targetName}】，请核对公众号名称！`);
    }

    const fakeid = bizList[0].fakeid;
    const nickname = bizList[0].nickname || targetName;
    sendDebugLog(`[官方直连] 成功匹配目标公众号: 【${nickname}】(fakeid: ${fakeid})`, "success");

    // 2. 分页遍历全部历史文章
    let begin = 0;
    const count = 20;
    let articles = [];
    let totalCount = 0;

    while (true) {
        const listUrl = `https://mp.weixin.qq.com/cgi-bin/appmsg?action=list_ex&begin=${begin}&count=${count}&fakeid=${fakeid}&type=9&query=&token=${token}&lang=zh_CN&f=json&ajax=1`;
        const listRes = await new Promise((resolve, reject) => {
            https.get(listUrl, { headers }, (res) => {
                let data = "";
                res.on("data", c => data += c);
                res.on("end", () => {
                    try { resolve(JSON.parse(data)); } catch(e) { resolve({ app_msg_list: [] }); }
                });
            }).on("error", reject);
        });

        const appMsgList = listRes.app_msg_list || [];
        if (appMsgList.length === 0) break;

        for (const msg of appMsgList) {
            const cleanUrl = (msg.link || "").replace(/&amp;/g, "&");
            if (cleanUrl) {
                articles.push({
                    id: String(msg.aid || cleanUrl),
                    title: unescapeWechatText(msg.title || "微信推文"),
                    author: nickname,
                    url: cleanUrl.startsWith("http") ? cleanUrl : `https://mp.weixin.qq.com${cleanUrl}`,
                    create_time: msg.create_time ? new Date(msg.create_time * 1000).toISOString().split("T")[0] : "",
                    digest: msg.digest || "",
                    cover: msg.cover || "",
                    is_original: true,
                    biz: fakeid,
                    status: "pending",
                    fail_reason: ""
                });
            }
            if (maxArticles > 0 && articles.length >= maxArticles) break;
        }

        totalCount = listRes.app_msg_cnt || articles.length;
        sendDebugLog(`[官方直连] 已全量拉取 ${articles.length} / ${totalCount} 篇历史推文...`, "info");
        if (progressCb) progressCb(`已拉取 ${articles.length} / ${totalCount} 篇文章目录...`, articles.length, totalCount, [...articles]);

        if (maxArticles > 0 && articles.length >= maxArticles) break;
        begin += count;
        if (begin >= totalCount) break;

        await new Promise(r => setTimeout(r, 400));
    }

    sendDebugLog(`[官方直连] 成功全量获取【${nickname}】全部 ${articles.length} 篇历史文章！`, "success");
    return { author: nickname, articles, biz: fakeid };
}

function escapeHtml(str) {
    if (!str) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function cleanWechatArticleHtml(rawHtml) {
    if (!rawHtml) return { html: "", markdown: "", status: "failed", failReason: "正文为空" };

    if (rawHtml.includes("weui-msg") && rawHtml.includes("该内容已被发布者删除")) {
        return { html: "", markdown: "", status: "failed", failReason: "该内容已被作者删除" };
    }
    if (rawHtml.includes("weui-msg") && rawHtml.includes("由用户投诉并经平台审核")) {
        return { html: "", markdown: "", status: "failed", failReason: "此内容因违规无法查看" };
    }

    try {
        const { document } = parseHTML(rawHtml);

        // 彻底移除所有 script, style, iframe, noscript, svg, button, input, form 以及各类广告容器
        const uselessSelectors = [
            "script", "style", "iframe", "noscript", "svg", "button", "input", "form",
            "#js_pc_qr_code", ".qr_code_pc_outer", ".reward_area", "#js_sponsor_ad_area",
            ".like_comment_wording", ".rich_media_area_extra", "#js_bottom_share_area",
            ".article-banner", ".advertisement", ".wx-qrcode", ".qr-code"
        ];
        uselessSelectors.forEach(sel => {
            document.querySelectorAll(sel).forEach(el => el.remove());
        });

        // 提取正文容器 (#js_content 或 .rich_media_content)
        const contentEl = document.getElementById("js_content") || document.querySelector(".rich_media_content");
        const targetEl = contentEl || document.body;

        // 还原图片真实地址
        targetEl.querySelectorAll("img").forEach(img => {
            const actualSrc = img.getAttribute("data-src") || img.getAttribute("data-original") || img.getAttribute("src") || "";
            if (actualSrc && actualSrc.startsWith("http")) {
                img.setAttribute("src", actualSrc);
            } else if (!actualSrc) {
                img.remove();
            }
        });

        // 移除微信反爬样式 (visibility: hidden / opacity: 0)
        targetEl.querySelectorAll("[style]").forEach(el => {
            let st = el.getAttribute("style") || "";
            st = st.replace(/visibility\s*:\s*hidden\s*;?/gi, "").replace(/opacity\s*:\s*0\s*;?/gi, "");
            el.setAttribute("style", st);
        });

        const cleanHtml = targetEl.innerHTML.trim();
        let cleanMarkdown = turndownService.turndown(cleanHtml);

        // 二次过滤掉残留的任何 JS 监控脚本代码片段
        if (cleanMarkdown.includes("window.logs =") || cleanMarkdown.includes("navigator.userAgent") || cleanMarkdown.includes("BadJs")) {
            cleanMarkdown = cleanMarkdown.replace(/try\{[\s\S]*?\}\s*catch\(e\)\{\}/gi, "")
                                         .replace(/var ua=[\s\S]*?;\s*/gi, "")
                                         .replace(/window\.logs[\s\S]*?;\s*/gi, "")
                                         .replace(/BadJs[\s\S]*?;\s*/gi, "");
        }

        return {
            html: cleanHtml,
            markdown: cleanMarkdown.trim() || "正文解析完成",
            status: "completed",
            failReason: ""
        };
    } catch(e) {
        return { html: "", markdown: "", status: "failed", failReason: "解析异常: " + e.message };
    }
}

async function extractArticleFullContent(articleUrl) {
    try {
        const rawHtml = await fetchPageHtml(articleUrl);
        return cleanWechatArticleHtml(rawHtml);
    } catch(e) {
        return { html: "", markdown: "", status: "failed", failReason: e.message || "网络请求超时" };
    }
}

// 渲染 PDF 助手
async function generatePdfFromHtml(htmlContent, pdfPath) {
    const saveDir = path.dirname(pdfPath);
    const tempHtmlPath = path.join(saveDir, `._temp_pdf_render_${Date.now()}.html`);
    fs.writeFileSync(tempHtmlPath, htmlContent, "utf8");

    return new Promise((resolve, reject) => {
        let isDone = false;
        let pdfWin = new BrowserWindow({
            show: false,
            webPreferences: {
                nodeIntegration: false,
                contextIsolation: true,
                webSecurity: false
            }
        });

        const cleanup = () => {
            if (pdfWin && !pdfWin.isDestroyed()) {
                try { pdfWin.destroy(); } catch(e) {}
            }
            pdfWin = null;
            try {
                if (fs.existsSync(tempHtmlPath)) fs.unlinkSync(tempHtmlPath);
            } catch(e) {}
        };

        // 超时保护 (最长等待 90 秒)
        const timer = setTimeout(() => {
            if (!isDone) {
                isDone = true;
                cleanup();
                reject(new Error("PDF 矢量渲染超时"));
            }
        }, 90000);

        pdfWin.webContents.on("did-finish-load", async () => {
            if (isDone) return;
            try {
                // 等待页面内所有图片 100% 彻底解码与绘制就绪
                await pdfWin.webContents.executeJavaScript(`
                    new Promise((resolve) => {
                        const imgs = Array.from(document.images);
                        if (imgs.length === 0) return resolve();
                        let remaining = imgs.length;
                        const checkOne = () => {
                            remaining--;
                            if (remaining <= 0) resolve();
                        };
                        imgs.forEach(img => {
                            if (img.complete) {
                                checkOne();
                            } else {
                                img.onload = checkOne;
                                img.onerror = checkOne;
                            }
                        });
                        setTimeout(resolve, 8000);
                    });
                `);

                // 额外给予 800ms 字体排版稳定缓冲
                await new Promise(r => setTimeout(r, 800));

                const pdfBuffer = await pdfWin.webContents.printToPDF({
                    printBackground: true,
                    pageSize: "A4",
                    preferCSSPageSize: true
                });
                fs.writeFileSync(pdfPath, pdfBuffer);
                isDone = true;
                clearTimeout(timer);
                cleanup();
                resolve(pdfPath);
            } catch(e) {
                if (!isDone) {
                    isDone = true;
                    clearTimeout(timer);
                    cleanup();
                    reject(e);
                }
            }
        });

        pdfWin.webContents.on("did-fail-load", (err) => {
            if (!isDone) {
                isDone = true;
                clearTimeout(timer);
                cleanup();
                reject(err);
            }
        });

        pdfWin.loadFile(tempHtmlPath);
    });
}

// 构建对齐网页端 100% 一模一样的优雅 HTML 离线电子书模板
function buildPremiumHtmlDocument(author, fullArticles, isPdf = false) {
    const nowStr = new Date().toLocaleString();
    const coverTitle = author.includes("公众号") ? `【${escapeHtml(author)}文章合集】` : `【${escapeHtml(author)}公众号合集】`;
    const coverSub = `共 ${fullArticles.length} 篇文章 · 微信公众号`;

    const tocItems = fullArticles.map((art, idx) => `
        <a href="#art-${idx + 1}" class="toc-link" onclick="highlightToc(this)">
            <span class="toc-num">${String(idx + 1).padStart(2, '0')}</span>
            <span class="toc-text">${escapeHtml(art.title)}</span>
        </a>
    `).join("");

    const articleSections = fullArticles.map((art, idx) => `
        <article id="art-${idx + 1}" class="article-card">
            <header class="article-header">
                <div class="article-meta-badge">微信公众号 · 第 ${idx + 1} 篇</div>
                <h2 class="article-title">${escapeHtml(art.title)}</h2>
                <div class="article-meta">
                    <span>👤 ${escapeHtml(author)}</span>
                    <span>🕒 ${escapeHtml(art.create_time || '未知时间')}</span>
                    ${art.url ? `<span>🔗 <a href="${art.url}" target="_blank" rel="noopener">查看原文</a></span>` : ''}
                </div>
            </header>
            <div class="article-content markdown-body">
                ${art.content_html || '<p style="color:#ef4444;">正文内容为空或抓取异常</p>'}
            </div>
            <div class="article-footer-watermark">
                <span>📖 本文档由【微信公众号：艺杯羹】整理排版 · 仅供个人离线学习与学术交流</span>
            </div>
        </article>
    `).join("");

    return `<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="referrer" content="no-referrer">
    <title>${coverTitle} - BlogDistiller 离线电子书</title>
    <style>
        /* ==============================================
           自适应双主题系统 (默认浅色阅读排版，网页端同款)
           ============================================== */
        :root[data-theme="light"] {
            --bg-base: #f8fafc;
            --sidebar-bg: #f1f5f9;
            --card-bg: #ffffff;
            --card-border: #e2e8f0;
            --text-main: #0f172a;
            --text-secondary: #334155;
            --text-muted: #64748b;
            --accent: #0284c7;
            --accent-subtle: rgba(2, 132, 199, 0.08);
            --code-bg: #f1f5f9;
            --code-border: #e2e8f0;
            --shadow-card: 0 4px 16px rgba(0, 0, 0, 0.04), 0 1px 3px rgba(0, 0, 0, 0.03);
            --cover-gradient: linear-gradient(145deg, #ffffff, #f8fafc);
        }

        :root[data-theme="dark"] {
            --bg-base: #0b0f19;
            --sidebar-bg: #111827;
            --card-bg: #182234;
            --card-border: rgba(255, 255, 255, 0.08);
            --text-main: #f8fafc;
            --text-secondary: #cbd5e1;
            --text-muted: #94a3b8;
            --accent: #38bdf8;
            --accent-subtle: rgba(56, 189, 248, 0.15);
            --code-bg: #0b1120;
            --code-border: #1e293b;
            --shadow-card: 0 10px 25px -5px rgba(0, 0, 0, 0.5);
            --cover-gradient: linear-gradient(145deg, #182234, #111827);
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
            background-color: var(--bg-base);
            color: var(--text-main);
            display: flex;
            height: 100vh;
            overflow: hidden;
            line-height: 1.75;
            transition: background-color 0.2s, color 0.2s;
        }

        /* 侧边栏 */
        #sidebar {
            width: 330px;
            background: var(--sidebar-bg);
            border-right: 1px solid var(--card-border);
            display: flex;
            flex-direction: column;
            flex-shrink: 0;
            z-index: 10;
            transition: background-color 0.2s, border-color 0.2s;
        }

        .sidebar-header {
            padding: 18px 20px;
            border-bottom: 1px solid var(--card-border);
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .author-title {
            font-size: 1.05rem;
            font-weight: 800;
            color: var(--text-main);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .author-sub {
            font-size: 0.78rem;
            color: var(--text-muted);
            margin-top: 2px;
        }

        .theme-toggle-btn {
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            color: var(--text-main);
            padding: 6px 10px;
            border-radius: 6px;
            font-size: 0.76rem;
            font-weight: 600;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 4px;
            transition: all 0.15s;
            flex-shrink: 0;
        }
        .theme-toggle-btn:hover {
            border-color: var(--accent);
            color: var(--accent);
        }

        .search-box {
            padding: 12px 18px;
            border-bottom: 1px solid var(--card-border);
        }
        .search-box input {
            width: 100%;
            padding: 8px 12px;
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 8px;
            color: var(--text-main);
            font-size: 0.84rem;
            outline: none;
            transition: border-color 0.15s;
        }
        .search-box input:focus {
            border-color: var(--accent);
        }

        .toc-list {
            flex: 1;
            overflow-y: auto;
            padding: 10px;
        }
        .toc-link {
            display: flex;
            align-items: center;
            padding: 8px 12px;
            color: var(--text-secondary);
            text-decoration: none;
            font-size: 0.85rem;
            border-radius: 6px;
            margin-bottom: 3px;
            transition: all 0.15s;
        }
        .toc-link:hover, .toc-link.active {
            background: var(--accent-subtle);
            color: var(--accent);
            font-weight: 600;
        }
        .toc-num {
            font-size: 0.75rem;
            font-family: Consolas, monospace;
            opacity: 0.6;
            margin-right: 8px;
            min-width: 22px;
        }
        .toc-text {
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        /* 主阅读内容区域 */
        #main {
            flex: 1;
            overflow-y: auto;
            padding: 40px 48px;
            scroll-behavior: smooth;
        }
        .main-container {
            max-width: 860px;
            margin: 0 auto;
        }

        .cover-card {
            background: var(--cover-gradient);
            border: 1px solid var(--card-border);
            border-radius: 14px;
            padding: 36px 40px;
            margin-bottom: 36px;
            box-shadow: var(--shadow-card);
        }
        .cover-card h1 {
            font-size: 1.85rem;
            font-weight: 800;
            margin-bottom: 12px;
            color: var(--text-main);
        }

        .article-card {
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 14px;
            padding: 36px 40px;
            margin-bottom: 36px;
            box-shadow: var(--shadow-card);
            transition: background-color 0.2s, border-color 0.2s;
        }
        .article-meta-badge {
            display: inline-block;
            font-size: 0.76rem;
            font-weight: 700;
            padding: 3px 10px;
            background: var(--accent-subtle);
            color: var(--accent);
            border-radius: 6px;
            margin-bottom: 12px;
        }
        .article-title {
            font-size: 1.55rem;
            font-weight: 800;
            line-height: 1.35;
            margin-bottom: 12px;
            color: var(--text-main);
        }
        .article-meta {
            display: flex;
            gap: 16px;
            font-size: 0.84rem;
            color: var(--text-muted);
            padding-bottom: 16px;
            border-bottom: 1px solid var(--card-border);
            margin-bottom: 24px;
            flex-wrap: wrap;
        }
        .article-meta a {
            color: var(--accent);
            text-decoration: none;
        }
        .article-meta a:hover {
            text-decoration: underline;
        }

        /* Markdown 正文排版 */
        .markdown-body {
            font-size: 1.02rem;
            line-height: 1.8;
            color: var(--text-secondary);
        }
        .markdown-body p { margin-bottom: 16px; }
        .markdown-body h1, .markdown-body h2, .markdown-body h3 {
            color: var(--text-main);
            font-weight: 700;
            margin-top: 28px;
            margin-bottom: 14px;
        }
        .markdown-body h2 {
            font-size: 1.35rem;
            border-bottom: 1px solid var(--card-border);
            padding-bottom: 6px;
        }
        .markdown-body h3 { font-size: 1.15rem; }
        .markdown-body code {
            background: var(--code-bg);
            border: 1px solid var(--code-border);
            padding: 2px 6px;
            border-radius: 4px;
            font-family: Consolas, monospace;
            font-size: 0.9em;
            color: #e11d48;
        }
        .markdown-body pre {
            background: var(--code-bg);
            border: 1px solid var(--code-border);
            padding: 16px 20px;
            border-radius: 8px;
            overflow-x: auto;
            margin-bottom: 18px;
        }
        .markdown-body pre code {
            background: none;
            border: none;
            padding: 0;
            color: var(--text-main);
        }
        .markdown-body img {
            max-width: 100%;
            height: auto;
            border-radius: 8px;
            margin: 16px auto;
            display: block;
            box-shadow: 0 2px 8px rgba(0,0,0,0.06);
        }
        .markdown-body blockquote {
            border-left: 4px solid var(--accent);
            padding: 8px 16px;
            background: var(--accent-subtle);
            border-radius: 0 8px 8px 0;
            color: var(--text-secondary);
            margin-bottom: 16px;
        }
        .article-footer-watermark {
            margin-top: 24px;
            padding-top: 12px;
            border-top: 1px dashed var(--card-border);
            font-size: 0.85rem;
            color: var(--text-muted);
            text-align: center;
            font-style: italic;
        }
        .disclaimer-badge {
            margin-top: 12px;
            padding: 10px 14px;
            background: var(--accent-subtle);
            border-left: 3px solid var(--accent);
            border-radius: 4px;
            font-size: 0.82rem;
            color: var(--text-secondary);
            line-height: 1.5;
            text-align: left;
        }
        .sidebar-brand-tag {
            font-size: 0.78rem;
            color: var(--accent);
            background: var(--accent-subtle);
            padding: 2px 6px;
            border-radius: 4px;
            display: inline-block;
            margin-top: 4px;
            font-weight: 500;
        }

        @media (max-width: 768px) {
            body { flex-direction: column; }
        @page {
            size: A4;
            margin: 16mm 14mm 16mm 14mm;
        }

        @media print {
            body { height: auto !important; overflow: visible !important; display: block !important; background: #ffffff !important; color: #1e293b !important; }
            #sidebar { display: none !important; }
            #main { padding: 0 !important; max-width: 100% !important; overflow: visible !important; }
            .cover-card { page-break-after: always; min-height: 85vh; display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center; border: none; box-shadow: none; padding: 40px 20px; }
            .cover-card h1 { font-size: 24pt; color: #0f172a; margin-bottom: 16px; }
            .article-card { page-break-before: always; page-break-after: always; box-shadow: none; border: none; padding: 20px 0; }
            .article-header { border-bottom: 2px solid #e2e8f0; padding-bottom: 12px; margin-bottom: 20px; }
            .article-title { font-size: 18pt; color: #0f172a; line-height: 1.35; margin-bottom: 8px; }
            .markdown-body img { max-width: 88% !important; height: auto !important; margin: 16px auto !important; display: block !important; border-radius: 6px !important; box-shadow: 0 2px 6px rgba(0,0,0,0.08) !important; }
            .markdown-body p { margin-bottom: 12px; text-align: justify; line-height: 1.75; }
        }
    </style>
</head>
<body>
    ${isPdf ? '' : `
    <div id="sidebar">
        <div class="sidebar-header">
            <div>
                <div class="author-title">${escapeHtml(author)}</div>
                <div class="author-sub">${coverSub}</div>
                <div class="sidebar-brand-tag">📖 公众号：艺杯羹</div>
            </div>
            <button class="theme-toggle-btn" onclick="toggleTheme()">
                <span id="themeIcon">🌙 暗黑</span>
            </button>
        </div>
        <div class="search-box">
            <input type="text" id="searchInput" placeholder="搜索目录..." oninput="filterToc()">
        </div>
        <div class="toc-list" id="tocList">
            ${tocItems}
        </div>
    </div>
    `}
    <div id="main">
        <div class="main-container">
            <div class="cover-card">
                <h1>${coverTitle}</h1>
                <p style="color: var(--text-muted); margin-bottom: 12px; font-size: 0.95rem;">
                    排版整理：微信公众号【艺杯羹】 · 导出时间：${nowStr} · 文章总数：共计 ${fullArticles.length} 篇
                </p>
                <div class="disclaimer-badge">
                    【免责声明】本文档内容均摘取自公开网络免费内容，排版整理：【微信公众号：艺杯羹】。仅供个人离线学习、学术研究与知识归档使用，严禁用于任何商业营利用途。原文知识产权归原作者及原发布平台所有。
                </div>
            </div>
            ${articleSections}
        </div>
    </div>
    <script>
        function initTheme() {
            const saved = localStorage.getItem('bd_doc_theme') || 'light';
            document.documentElement.setAttribute('data-theme', saved);
            updateThemeBtn(saved);
        }

        function toggleTheme() {
            const current = document.documentElement.getAttribute('data-theme') || 'light';
            const next = current === 'dark' ? 'light' : 'dark';
            document.documentElement.setAttribute('data-theme', next);
            localStorage.setItem('bd_doc_theme', next);
            updateThemeBtn(next);
        }

        function updateThemeBtn(theme) {
            const btnText = document.getElementById('themeIcon');
            if (btnText) {
                btnText.innerText = theme === 'dark' ? '☀️ 浅色' : '🌙 暗黑';
            }
        }

        function filterToc() {
            const val = document.getElementById('searchInput').value.toLowerCase();
            const links = document.querySelectorAll('.toc-link');
            links.forEach(link => {
                const text = link.querySelector('.toc-text').innerText.toLowerCase();
                link.style.display = text.includes(val) ? 'flex' : 'none';
            });
        }

        function highlightToc(el) {
            document.querySelectorAll('.toc-link').forEach(l => l.classList.remove('active'));
            el.classList.add('active');
        }

        initTheme();
    </script>
</body>
</html>`;
}

// 本地下载微信高清图片助手 (带防盗链 Referer、自动重定向与 3 次重试支持)
async function downloadImageLocal(imgUrl, saveFilePath) {
    if (!imgUrl || !imgUrl.startsWith("http")) return false;
    if (fs.existsSync(saveFilePath) && fs.statSync(saveFilePath).size > 200) return true;

    for (let retry = 0; retry < 3; retry++) {
        try {
            const success = await new Promise((resolve) => {
                const parsed = new URL(imgUrl);
                const client = parsed.protocol === "https:" ? https : http;
                const req = client.get(imgUrl, {
                    headers: {
                        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 MicroMessenger/7.0.20",
                        "referer": "https://mp.weixin.qq.com/",
                        "accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
                    },
                    timeout: 10000
                }, (res) => {
                    // 处理 301/302 重定向
                    if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
                        return downloadImageLocal(res.headers.location, saveFilePath).then(resolve);
                    }
                    if (res.statusCode >= 200 && res.statusCode < 300) {
                        const chunks = [];
                        res.on("data", chunk => chunks.push(chunk));
                        res.on("end", () => {
                            try {
                                const buf = Buffer.concat(chunks);
                                if (buf.length > 100) {
                                    fs.writeFileSync(saveFilePath, buf);
                                    resolve(true);
                                } else {
                                    resolve(false);
                                }
                            } catch(e) { resolve(false); }
                        });
                    } else {
                        resolve(false);
                    }
                });
                req.on("error", () => resolve(false));
                req.on("timeout", () => { req.destroy(); resolve(false); });
            });

            if (success) return true;
        } catch(e) {}
        await new Promise(r => setTimeout(r, 200));
    }
    return false;
}

async function exportArticlesLocal(exportOptions, progressCb, statusCb) {
    const { articles, author, formats, outputDir, biz } = exportOptions;
    const baseDir = outputDir || DEFAULT_EXPORT_DIR;
    
    // 自动以当前公众号名称创建专属合集子文件夹，避免文件混乱
    const todayStr = new Date().toISOString().split("T")[0];
    const subFolderName = `【${author}】文章合集_${todayStr}`;
    const saveDir = path.join(baseDir, subFolderName);
    if (!fs.existsSync(saveDir)) fs.mkdirSync(saveDir, { recursive: true });

    // 创建本地真实离线图片目录
    const imagesDir = path.join(saveDir, "images");
    if (!fs.existsSync(imagesDir)) fs.mkdirSync(imagesDir, { recursive: true });

    const prefix = `【${author}】微信公众号文章合集`;
    const results = [];
    const fullArticles = [];
    const cachedMap = biz ? ArticleCacheManager.loadCache(biz) : {};

    sendDebugLog(`[全量导出] 开始导出选中的 ${articles.length} 篇文章到专属合集目录: ${saveDir}，导出格式: ${formats.join(", ").toUpperCase()}`, "info");

    for (let i = 0; i < articles.length; i++) {
        const art = articles[i];
        
        // 检查断点续传缓存
        let { html, markdown, status, failReason } = { html: "", markdown: "", status: "pending", failReason: "" };
        if (cachedMap[art.url] && cachedMap[art.url].content_markdown) {
            html = cachedMap[art.url].content_html;
            markdown = cachedMap[art.url].content_markdown;
            
            // 自动检测并清洗旧缓存中的微信 JS 监控代码乱码
            if (markdown.includes("navigator.userAgent") || markdown.includes("window.logs") || markdown.includes("BadJs") || html.includes("<script")) {
                const cleaned = cleanWechatArticleHtml(html);
                html = cleaned.html;
                markdown = cleaned.markdown;
                if (biz) {
                    ArticleCacheManager.saveArticle(biz, art.url, {
                        title: art.title,
                        content_html: html,
                        content_markdown: markdown,
                        create_time: art.create_time,
                        author: art.author
                    });
                }
            }

            status = "completed";
            if (statusCb) statusCb({ index: i, id: art.id, status: "completed", failReason: "" });
        } else {
            if (statusCb) statusCb({ index: i, id: art.id, status: "downloading", failReason: "" });
            if (progressCb) progressCb(`正在全量抓取第 ${i + 1}/${articles.length} 篇: ${art.title.slice(0, 18)}...`, i + 1, articles.length);
            
            const res = await extractArticleFullContent(art.url);
            html = res.html;
            markdown = res.markdown;
            status = res.status;
            failReason = res.failReason;

            if (status === "completed" && biz) {
                ArticleCacheManager.saveArticle(biz, art.url, {
                    title: art.title,
                    content_html: html,
                    content_markdown: markdown,
                    create_time: art.create_time,
                    author: art.author
                });
            }

            if (statusCb) statusCb({ index: i, id: art.id, status, failReason });
            await new Promise(r => setTimeout(r, 40));
        }

        fullArticles.push({
            ...art,
            content_html: html || `<p style="color:#ef4444;">${failReason || '抓取失败'}</p>`,
            content_markdown: markdown || `> ⚠️ **抓取失败**：${failReason || '文章已被删除或违规'}`,
            single_markdown: markdown || `> ⚠️ **抓取失败**：${failReason || '文章已被删除或违规'}`,
            export_status: status,
            fail_reason: failReason
        });
    }

    // 收集所有文章内的全部配图，并进行真正的并发下载与完整等待
    const imageDownloadQueue = [];
    for (let i = 0; i < fullArticles.length; i++) {
        const art = fullArticles[i];
        if (art.export_status !== "completed" || !art.content_html) continue;

        const imgRegex = /<img[^>]+(?:src|data-src)=["'](https?:\/\/[^"']+)["'][^>]*>/gi;
        let match;
        let imgIdx = 1;
        while ((match = imgRegex.exec(art.content_html)) !== null) {
            const rawUrl = match[1];
            const imgExt = rawUrl.includes("wx_fmt=png") ? "png" : "jpg";
            const imgFileName = `art_${i + 1}_img_${imgIdx}.${imgExt}`;
            const imgLocalPath = path.join(imagesDir, imgFileName);

            imageDownloadQueue.push({
                rawUrl,
                imgFileName,
                imgLocalPath,
                artIndex: i
            });
            imgIdx++;
        }
    }

    if (imageDownloadQueue.length > 0) {
        sendDebugLog(`[配图下载] 正在并发下载 ${imageDownloadQueue.length} 张高清离线配图...`, "info");
        const CONCURRENT = 8;
        for (let b = 0; b < imageDownloadQueue.length; b += CONCURRENT) {
            const batch = imageDownloadQueue.slice(b, b + CONCURRENT);
            if (progressCb) {
                progressCb(`正在下载离线配图 (${b + 1}/${imageDownloadQueue.length})...`, b + 1, imageDownloadQueue.length);
            }
            await Promise.all(batch.map(item => downloadImageLocal(item.rawUrl, item.imgLocalPath)));
        }
        sendDebugLog(`[配图下载] 全部 ${imageDownloadQueue.length} 张高清离线配图下载完成！`, "success");

        // 统一替换文章正文中的图片为本地相对路径
        for (const item of imageDownloadQueue) {
            const art = fullArticles[item.artIndex];
            const relRoot = `./images/${item.imgFileName}`;
            const relSingle = `../images/${item.imgFileName}`;
            art.content_html = art.content_html.replaceAll(item.rawUrl, relRoot);
            art.content_markdown = art.content_markdown.replaceAll(item.rawUrl, relRoot);
            art.single_markdown = art.single_markdown.replaceAll(item.rawUrl, relSingle);
        }
    }

    // 1. Markdown 导出 (轻量优雅规范，本地图片引用，支持单篇+合集)
    if (formats.includes("md")) {
        try {
            const mdPath = path.join(saveDir, `${prefix}.md`);
            const singleMdDir = path.join(saveDir, "Markdown单篇知识库");
            if (!fs.existsSync(singleMdDir)) fs.mkdirSync(singleMdDir, { recursive: true });

            const docTitle = author.includes("公众号") ? `【${author}文章合集】` : `【${author}公众号合集】`;
            let mdContent = `# ${docTitle}\n\n`;
            mdContent += `> **博主/来源**：${author} (微信公众号)  \n`;
            mdContent += `> **排版整理**：微信公众号【艺杯羹】  \n`;
            mdContent += `> **导出时间**：${new Date().toLocaleString()}  \n`;
            mdContent += `> **文章总数**：共计 ${fullArticles.length} 篇  \n`;
            mdContent += `> **本地图片资源**：配图已完整保存在 \`./images/\` 目录中，支持完全断网离线阅读  \n\n---\n\n`;
            mdContent += `## 📚 目录导航 (Table of Contents)\n\n`;
            fullArticles.forEach((a, i) => {
                const mark = a.export_status === 'failed' ? ` [${a.fail_reason || '抓取失败'}]` : '';
                mdContent += `- [${i + 1}. ${a.title}](#art-${i + 1}) \`(${a.create_time})\`${mark}\n`;
            });
            mdContent += `\n---\n\n`;

            for (let i = 0; i < fullArticles.length; i++) {
                const art = fullArticles[i];
                let safeMd = (art.content_markdown || "")
                    .replace(/!\[(.*?)\]\(data:image\/[^;]+;base64,[A-Za-z0-9+/=]{100,}\)/g, `![$1](./images/art_${i+1}_img.jpg)`)
                    .replace(/\n{4,}/g, "\n\n");

                mdContent += `<a id="art-${i + 1}"></a>\n\n`;
                mdContent += `## ${i + 1}. ${art.title}\n\n`;
                mdContent += `\`\`\`yaml\n`;
                mdContent += `title: "${art.title.replace(/"/g, '\\"')}"\n`;
                mdContent += `author: "${author}"\n`;
                mdContent += `date: "${art.create_time}"\n`;
                mdContent += `url: "${art.url}"\n`;
                mdContent += `curator: "微信公众号【艺杯羹】"\n`;
                mdContent += `\`\`\`\n\n`;
                mdContent += safeMd + "\n\n";
                mdContent += `> *📖 本文档由【微信公众号：艺杯羹】整理排版 · 仅供个人离线学习与学术交流*\n\n`;
                mdContent += `\n---\n\n`;

                // 为每篇文章生成一份独立的单篇 Markdown
                try {
                    const safeTitle = (art.title || `文章_${i + 1}`).replace(/[\\/:*?"<>|]/g, "_").slice(0, 45);
                    const singlePath = path.join(singleMdDir, `${String(i + 1).padStart(2, '0')}_${safeTitle}.md`);
                    let singleDoc = `# ${art.title}\n\n`;
                    singleDoc += `> 📅 发布日期：${art.create_time} | 👤 作者：${author} | 🔗 [查看原文](${art.url})\n\n---\n\n`;
                    singleDoc += (art.single_markdown || safeMd) + "\n";
                    fs.writeFileSync(singlePath, singleDoc, "utf8");
                } catch(errSingle) {}
            }
            fs.writeFileSync(mdPath, mdContent, "utf8");
            results.push(mdPath);
            sendDebugLog(`[文件生成] Markdown 知识库合集已生成: ${path.basename(mdPath)} (已保存离线插图)`, "success");
        } catch(e) {
            console.error("MD export error:", e);
        }
    }

    // 2. HTML 离线电子书导出 (支持本地断网图片)
    if (formats.includes("html")) {
        try {
            const htmlPath = path.join(saveDir, `${prefix}.html`);
            const htmlContent = buildPremiumHtmlDocument(author, fullArticles, false);
            fs.writeFileSync(htmlPath, htmlContent, "utf8");
            results.push(htmlPath);
            sendDebugLog(`[文件生成] HTML 离线网页电子书已生成: ${path.basename(htmlPath)}`, "success");
        } catch (e) {
            console.error("HTML export error:", e);
        }
    }

    // 3. PDF 导出 (基于内置 Chromium 矢量排版引擎)
    if (formats.includes("pdf")) {
        try {
            if (progressCb) progressCb(`正在生成高清矢量 PDF 文档...`, fullArticles.length, fullArticles.length);
            const pdfPath = path.join(saveDir, `${prefix}.pdf`);
            const pdfHtmlContent = buildPremiumHtmlDocument(author, fullArticles, true);
            await generatePdfFromHtml(pdfHtmlContent, pdfPath);
            results.push(pdfPath);
            sendDebugLog(`[文件生成] PDF 高清矢量电子书已生成: ${path.basename(pdfPath)}`, "success");
        } catch(e) {
            console.error("PDF export error:", e);
            sendDebugLog(`[文件生成失败] PDF 生成异常: ${e.message}`, "error");
        }
    }

    // 4. Word (.docx) 导出 (真图内嵌、出版级字阶排版，对齐网页版 Word 模板)
    if (formats.includes("docx")) {
        try {
            const docxPath = path.join(saveDir, `${prefix}.docx`);
            const children = [
                new docx.Paragraph({
                    text: `【${author}】微信公众号文章合集`,
                    heading: docx.HeadingLevel.TITLE,
                    spacing: { before: 200, after: 120 }
                }),
                new docx.Paragraph({
                    text: `文章总数：共计 ${fullArticles.length} 篇   |   整理排版：微信公众号【艺杯羹】   |   导出时间：${new Date().toLocaleString()}`,
                    spacing: { after: 240 }
                }),
                new docx.Paragraph({
                    text: `【免责声明】本文档内容均摘取自公开网络免费内容，仅供个人学习交流与知识归档使用，严禁用于任何商业营利用途。原文知识产权归原作者所有。`,
                    spacing: { after: 360 }
                }),
                new docx.Paragraph({
                    children: [new docx.PageBreak()]
                })
            ];

            const cleanTextForDocx = (str) => {
                if (!str) return "";
                return String(str).replace(/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/g, "").trim();
            };

            const imgRegex = /!\[(.*?)\]\((.*?)\)/;

            for (let i = 0; i < fullArticles.length; i++) {
                const art = fullArticles[i];
                
                // 1. 文章大标题与元数据
                children.push(
                    new docx.Paragraph({
                        text: cleanTextForDocx(`${i + 1}. ${art.title}`),
                        heading: docx.HeadingLevel.HEADING_1,
                        spacing: { before: 280, after: 100 }
                    }),
                    new docx.Paragraph({
                        text: cleanTextForDocx(`作者：${art.author || author}   |   发布时间：${art.create_time || '未知'}   |   来源：微信公众号`),
                        spacing: { after: 80 }
                    }),
                    new docx.Paragraph({
                        text: cleanTextForDocx(`原文链接：${art.url}`),
                        spacing: { after: 200 }
                    })
                );

                // 2. 逐行解析 Markdown 正文并内嵌真实图片
                const mdContent = art.content_markdown || "";
                const lines = mdContent.replace(/\r\n/g, "\n").split("\n");
                let inCode = false;
                let codeLines = [];

                for (const line of lines) {
                    const trimmed = line.trim();

                    // 代码块处理
                    if (trimmed.startsWith("```")) {
                        if (inCode) {
                            if (codeLines.length > 0) {
                                children.push(new docx.Paragraph({
                                    text: cleanTextForDocx(codeLines.join("\n")),
                                    spacing: { before: 80, after: 80 }
                                }));
                            }
                            codeLines = [];
                            inCode = false;
                        } else {
                            inCode = true;
                        }
                        continue;
                    }

                    if (inCode) {
                        codeLines.push(line);
                        continue;
                    }

                    if (!trimmed) continue;

                    // 图片处理 (真实内嵌 ImageRun)
                    const imgMatch = imgRegex.exec(trimmed);
                    if (imgMatch) {
                        const altText = imgMatch[1] || "";
                        const imgSrc = imgMatch[2];
                        let localImgPath = "";
                        if (imgSrc.startsWith("./images/")) {
                            localImgPath = path.join(saveDir, imgSrc);
                        } else if (imgSrc.startsWith("../images/")) {
                            localImgPath = path.join(saveDir, imgSrc.replace("../", ""));
                        } else if (imgSrc.startsWith("images/")) {
                            localImgPath = path.join(saveDir, imgSrc);
                        }

                        let inserted = false;
                        if (localImgPath && fs.existsSync(localImgPath)) {
                            try {
                                const imgBuf = fs.readFileSync(localImgPath);
                                if (imgBuf.length > 200) {
                                    let width = 480;
                                    let height = 280;
                                    try {
                                        const dim = imageSize(imgBuf);
                                        if (dim && dim.width && dim.height) {
                                            if (dim.width > 500) {
                                                const ratio = 500 / dim.width;
                                                width = 500;
                                                height = Math.round(dim.height * ratio);
                                            } else {
                                                width = dim.width;
                                                height = dim.height;
                                            }
                                        }
                                    } catch(e) {}

                                    children.push(new docx.Paragraph({
                                        alignment: docx.AlignmentType.CENTER,
                                        spacing: { before: 140, after: 80 },
                                        children: [
                                            new docx.ImageRun({
                                                data: imgBuf,
                                                transformation: { width, height }
                                            })
                                        ]
                                    }));

                                    if (altText && altText !== "图片" && altText !== "img") {
                                        children.push(new docx.Paragraph({
                                            alignment: docx.AlignmentType.CENTER,
                                            text: cleanTextForDocx(`▲ ${altText}`),
                                            spacing: { after: 120 }
                                        }));
                                    }
                                    inserted = true;
                                }
                            } catch(e) {
                                console.error("Docx ImageRun error:", e);
                            }
                        }

                        if (!inserted && altText && altText !== "图片") {
                            children.push(new docx.Paragraph({
                                alignment: docx.AlignmentType.CENTER,
                                text: cleanTextForDocx(`[图片: ${altText}]`),
                                spacing: { before: 80, after: 80 }
                            }));
                        }
                        continue;
                    }

                    // 标题处理
                    if (trimmed.startsWith("### ")) {
                        children.push(new docx.Paragraph({
                            text: cleanTextForDocx(trimmed.replace(/^###\s+/, "")),
                            heading: docx.HeadingLevel.HEADING_3,
                            spacing: { before: 160, after: 80 }
                        }));
                    } else if (trimmed.startsWith("## ")) {
                        children.push(new docx.Paragraph({
                            text: cleanTextForDocx(trimmed.replace(/^##\s+/, "")),
                            heading: docx.HeadingLevel.HEADING_2,
                            spacing: { before: 180, after: 90 }
                        }));
                    } else if (trimmed.startsWith("# ")) {
                        children.push(new docx.Paragraph({
                            text: cleanTextForDocx(trimmed.replace(/^#\s+/, "")),
                            heading: docx.HeadingLevel.HEADING_1,
                            spacing: { before: 200, after: 100 }
                        }));
                    } else if (trimmed.startsWith("> ")) {
                        children.push(new docx.Paragraph({
                            text: cleanTextForDocx(trimmed.replace(/^>\s+/, "")),
                            spacing: { before: 60, after: 60 }
                        }));
                    } else {
                        children.push(new docx.Paragraph({
                            text: cleanTextForDocx(trimmed),
                            spacing: { after: 100 }
                        }));
                    }
                }

                // 每篇文章之间插入分页符
                if (i < fullArticles.length - 1) {
                    children.push(new docx.Paragraph({
                        children: [new docx.PageBreak()]
                    }));
                }
            }

            const doc = new docx.Document({
                sections: [{ properties: {}, children }]
            });

            const buffer = await docx.Packer.toBuffer(doc);
            fs.writeFileSync(docxPath, buffer);
            results.push(docxPath);
            sendDebugLog(`[文件生成] Word 文档已生成 (真图高清内嵌): ${path.basename(docxPath)}`, "success");
        } catch (e) {
            console.error("DOCX export error:", e);
        }
    }

    // 5. TXT 纯文本导出
    if (formats.includes("txt")) {
        try {
            const txtPath = path.join(saveDir, `${prefix}.txt`);
            let txtContent = `【${author}】微信公众号文章合集\n`;
            txtContent += `文章总数：共计 ${fullArticles.length} 篇\n`;
            txtContent += `导出时间：${new Date().toLocaleString()}\n`;
            txtContent += `==========================================================\n\n`;

            for (let i = 0; i < fullArticles.length; i++) {
                const art = fullArticles[i];
                txtContent += `----------------------------------------------------------\n`;
                txtContent += `第 ${i + 1} 篇：${art.title}\n`;
                txtContent += `发布日期：${art.create_time} | 原文链接：${art.url}\n`;
                txtContent += `----------------------------------------------------------\n\n`;
                const cleanBody = (art.content_markdown || "")
                    .replace(/!\[.*?\]\(.*?\)/g, "")
                    .replace(/\[(.*?)\]\(.*?\)/g, "$1")
                    .replace(/[#*`_>]/g, "")
                    .replace(/\n{3,}/g, "\n\n")
                    .trim();
                txtContent += cleanBody + "\n\n\n";
            }
            fs.writeFileSync(txtPath, txtContent, "utf8");
            results.push(txtPath);
            sendDebugLog(`[文件生成] TXT 纯文本已生成: ${path.basename(txtPath)}`, "success");
        } catch(e) {
            console.error("TXT export error:", e);
        }
    }

    // 6. Excel (.xlsx) 导出
    if (formats.includes("xlsx")) {
        try {
            const xlsxPath = path.join(saveDir, `${prefix}.xlsx`);
            const headerRow = [
                { value: "序号", fontWeight: "bold" },
                { value: "文章标题", fontWeight: "bold" },
                { value: "发布时间", fontWeight: "bold" },
                { value: "抓取状态", fontWeight: "bold" },
                { value: "是否原创", fontWeight: "bold" },
                { value: "文章摘要", fontWeight: "bold" },
                { value: "原文链接", fontWeight: "bold" }
            ];
            const dataRows = fullArticles.map((a, i) => [
                { value: i + 1, type: Number },
                { value: String(a.title || ""), type: String },
                { value: String(a.create_time || ""), type: String },
                { value: a.export_status === 'completed' ? '成功' : String(a.fail_reason || '失败'), type: String },
                { value: a.is_original ? "原创" : "非原创", type: String },
                { value: String(a.digest || ""), type: String },
                { value: String(a.url || ""), type: String }
            ]);
            const sheetData = [headerRow, ...dataRows];
            const buffer = await writeXlsxFile(sheetData).toBuffer();
            fs.writeFileSync(xlsxPath, buffer);
            results.push(xlsxPath);
            sendDebugLog(`[文件生成] Excel 数据表已生成: ${path.basename(xlsxPath)}`, "success");
        } catch (e) {
            console.error("XLSX export error:", e);
            sendDebugLog(`[文件生成失败] Excel 生成异常: ${e.message}`, "error");
        }
    }

    return { success: true, savedFiles: results, saveDir };
}

// =========================================================================
// 6. 窗口创建与主进程调度
// =========================================================================
function createMainWindow() {
    mainWindow = new BrowserWindow({
        width: 1280,
        height: 840,
        minWidth: 1020,
        minHeight: 700,
        title: "BlogDistiller (博萃) · 微信文章导出助手",
        icon: path.join(__dirname, "renderer", "assets", "logo_icon.png"),
        autoHideMenuBar: true,
        backgroundColor: "#f7f6f2",
        show: true,
        webPreferences: {
            preload: path.join(__dirname, "preload.js"),
            nodeIntegration: false,
            contextIsolation: true
        }
    });

    mainWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
    mainWindow.show();
    mainWindow.focus();

    mainWindow.on("closed", () => {
        mainWindow = null;
    });
}

app.on("second-instance", () => {
    if (mainWindow) {
        if (mainWindow.isMinimized()) mainWindow.restore();
        mainWindow.focus();
    }
});

app.whenReady().then(async () => {
    createMainWindow();

    let proxyPort = 8899;
    try {
        const certStore = new CertStore(CERTS_DIR);
        installCaToTrustStore(certStore.caCertFile);

        proxyInstance = new InterceptProxy(certStore, (data) => {
            handleCapturedAuth(data);
        });

        proxyPort = await proxyInstance.listen(8899);
        applyWindowsPac(proxyPort, true);
    } catch (err) {
        console.error("[BlogDistiller] 代理服务初始化异常:", err);
    }

    ipcMain.handle("wechat:get-status", () => wechatAuth);
    ipcMain.handle("wechat:toggle-proxy", (_, enable) => {
        applyWindowsPac(proxyPort, enable);
        return enable;
    });

    ipcMain.handle("app:open-external", (_, targetUrl) => {
        if (targetUrl && targetUrl.startsWith("http")) {
            shell.openExternal(targetUrl);
            return true;
        }
        return false;
    });

    ipcMain.handle("cache:get", (_, biz) => {
        return ArticleCacheManager.loadCache(biz);
    });

    ipcMain.handle("cache:get-all-accounts", () => {
        try {
            const cacheDir = path.join(DATA_DIR, "cache");
            if (!fs.existsSync(cacheDir)) return [];
            const files = fs.readdirSync(cacheDir).filter(f => f.startsWith("articles_") && f.endsWith(".json"));
            const accounts = [];
            for (const f of files) {
                const raw = fs.readFileSync(path.join(cacheDir, f), "utf8");
                const data = JSON.parse(raw);
                const urls = Object.keys(data);
                if (urls.length > 0) {
                    const firstArt = data[urls[0]];
                    const author = firstArt.author || "微信公众号";
                    const biz = f.replace("articles_", "").replace(".json", "");
                    const articles = urls.map((u, i) => {
                        const item = data[u];
                        return {
                            id: `art_${i + 1}`,
                            title: item.title || `文章_${i + 1}`,
                            author: item.author || author,
                            url: u,
                            create_time: item.create_time || "",
                            digest: item.digest || "",
                            is_original: item.is_original !== false,
                            biz: biz,
                            status: item.content_markdown ? "completed" : "pending"
                        };
                    });
                    accounts.push({ author, biz, articles, count: articles.length });
                }
            }
            return accounts;
        } catch(e) {
            return [];
        }
    });

    ipcMain.handle("wechat:retry-single", async (_, { articleUrl, biz }) => {
        const res = await extractArticleFullContent(articleUrl);
        if (res.status === "completed" && biz) {
            ArticleCacheManager.saveArticle(biz, articleUrl, {
                content_html: res.html,
                content_markdown: res.markdown
            });
        }
        return res;
    });

    ipcMain.handle("wechat:open-mp-login", () => {
        openMpLoginWindow();
        return true;
    });

    ipcMain.handle("app:copy-clipboard", (_, text) => {
        try {
            const { clipboard } = require("electron");
            clipboard.writeText(String(text || ""));
            return { success: true };
        } catch(e) {
            return { success: false, error: e.message };
        }
    });

    ipcMain.handle("wechat:search-articles", async (_, { target, maxArticles }) => {
        const cleanTarget = target.trim();
        if (!cleanTarget) throw new Error("请输入微信公众号推文链接或公众号名称！");

        sendDebugLog(`--------------------------------------------------------`);
        sendDebugLog(`[用户操作] 触发文章检索 -> 目标: ${cleanTarget}`, "info");

        const lines = cleanTarget.split(/\r?\n/).map(l => l.trim()).filter(l => l);
        const urls = lines.filter(l => l.startsWith("http"));

        let authorName = "微信公众号";
        let articles = [];
        let targetBiz = "";

        // 场景 1: 多篇微信文章链接批量抓取 (>= 2 篇)
        if (urls.length > 1) {
            sendDebugLog(`[多篇批量] 检测到 ${urls.length} 条微信推文链接，正在免凭证极速批量解析...`, "info");
            if (mainWindow) mainWindow.webContents.send("wechat:fetch-progress", { message: `正在极速解析 ${urls.length} 篇推文...`, current: 1, total: urls.length });

            for (let idx = 0; idx < urls.length; idx++) {
                const u = urls[idx];
                try {
                    const pageHtml = await fetchPageHtml(u);
                    const art = parseSingleArticleFromHtml(pageHtml, u);
                    if (art.author && art.author !== "微信公众号" && authorName === "微信公众号") {
                        authorName = art.author;
                    }
                    art.id = `art_${Date.now()}_${idx}`;
                    articles.push(art);
                } catch(err) {
                    articles.push({
                        id: `art_${Date.now()}_${idx}`,
                        title: `微信推文_${idx + 1}`,
                        author: authorName,
                        url: u,
                        create_time: new Date().toISOString().split("T")[0],
                        digest: "",
                        is_original: true,
                        biz: "",
                        status: "pending",
                        fail_reason: ""
                    });
                }
            }

            sendDebugLog(`[解析完成] 成功加载 ${articles.length} 篇文章，作者: 【${authorName}】`, "success");
            return { author: authorName, articles, biz: articles[0] ? articles[0].biz : "" };
        }

        // 场景 2: 单篇推文链接 (自动提取文章信息，并尝试拉取该号历史文章)
        if (urls.length === 1) {
            const cleanUrl = urls[0];
            if (mainWindow) mainWindow.webContents.send("wechat:fetch-progress", { message: "正在解析推文与公众号信息...", current: 1, total: 1 });
            const pageHtml = await fetchPageHtml(cleanUrl);
            const singleArt = parseSingleArticleFromHtml(pageHtml, cleanUrl);
            authorName = singleArt.author || authorName;
            targetBiz = (singleArt.biz && isValidBiz(singleArt.biz)) ? singleArt.biz : (wechatAuth.biz || "");

            if (targetBiz && isValidBiz(targetBiz)) {
                sendDebugLog(`[锁定公众号] 已锁定【${authorName}】(biz: ${targetBiz})，正在拉取全量历史文章...`, "info");
                try {
                    const historyRes = await fetchWechatHistoryArticles(targetBiz, authorName, maxArticles || 0, (msg, cur, tot, currentList) => {
                        if (mainWindow) mainWindow.webContents.send("wechat:fetch-progress", { message: msg, current: cur, total: tot, articles: currentList, author: authorName });
                    });
                    if (historyRes.articles && historyRes.articles.length > 0) {
                        return { author: authorName, articles: historyRes.articles, biz: targetBiz };
                    }
                    if (historyRes.lastError) {
                        throw new Error(historyRes.lastError);
                    }
                } catch (err) {
                    sendDebugLog(`[全量历史拉取受限] ${err.message}`, "warn");
                    pendingAutoFetchTarget = { target: cleanUrl, maxArticles, biz: targetBiz, author: authorName };
                    throw new Error(`未能获取公众号【${authorName}】的历史文章：${err.message}\n\n👉 解决建议：请在电脑微信中打开任意一篇公众号文章或该号主页，软件将自动刷新会话并秒级拉取全部历史！`);
                }
            }

            pendingAutoFetchTarget = { target: cleanUrl, maxArticles, biz: targetBiz, author: authorName };
            throw new Error(`未能获取公众号【${authorName}】的历史文章。\n请在电脑微信中打开任意一篇公众号文章以激活最新会话！`);
        }

        // 场景 3: 用户输入了纯文本/非 URL 内容
        if (isValidBiz(cleanTarget)) {
            sendDebugLog(`[biz检索] 正在为 biz: ${cleanTarget} 拉取全部历史推文...`, "info");
            return await fetchWechatHistoryArticles(cleanTarget, "微信公众号", maxArticles || 0, (msg, cur, total, currentList) => {
                if (mainWindow) mainWindow.webContents.send("wechat:fetch-progress", { message: msg, current: cur, total, articles: currentList, author: "微信公众号" });
            });
        }

        throw new Error(`请输入有效的微信公众号推文链接（例如：https://mp.weixin.qq.com/s/...）！`);
    });

    // 一键逆向解析推文并生成公众号专属历史主页直达链接 (对齐 Zoro/三刀原版规范)
    ipcMain.handle("wechat:generate-profile-url", async (_, { target }) => {
        if (!target || !target.trim()) {
            throw new Error("请先输入公众号任意推文链接！");
        }
        const cleanUrl = target.trim();
        sendDebugLog(`[专属主页链接生成] 正在解析推文以提取公众号 biz: ${cleanUrl}`, "info");

        // 1. 如果输入本身就包含 __biz
        const directBizMatch = cleanUrl.match(/[?&]__biz=([^&#]+)/);
        if (directBizMatch) {
            const biz = decodeURIComponent(directBizMatch[1]);
            const profileUrl = `https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=${biz}&scene=124#wechat_redirect`;
            return { profileUrl, biz, author: "微信公众号" };
        }

        // 2. 发起免凭证请求获取推文 HTML
        const html = await fetchHtmlDirect(cleanUrl);
        if (!html) {
            throw new Error("未能获取到该推文网页内容，请检查网络连接或链接是否有效。");
        }

        // 3. 提取作者名称
        let author = "微信公众号";
        const nickMatch = html.match(/var\s+nickname\s*=\s*["']([^"']+)["']/) || html.match(/id="js_name">\s*([^<]+)\s*</);
        if (nickMatch) author = nickMatch[1].trim();

        // 4. 提取 biz
        let biz = "";
        const bizMatch = html.match(/var\s+biz\s*=\s*["']([^"']+)["']/) || html.match(/__biz=([^&#"']+)/);
        if (bizMatch) biz = decodeURIComponent(bizMatch[1]);

        if (!biz) {
            throw new Error("未能从该文章解析出公众号 biz 标识，请确认链接是否为微信公众号公开文章。");
        }

        const profileUrl = `https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=${biz}&scene=124#wechat_redirect`;
        sendDebugLog(`[专属主页链接生成成功] 已锁定【${author}】(biz: ${biz})，主页链接: ${profileUrl}`, "success");
        return {
            profileUrl,
            biz,
            author
        };
    });

    // 专栏合集全自动免凭证扫描与反向挖掘 (100% 免 Session, 零风控)
    ipcMain.handle("wechat:scan-albums", async (_, { target }) => {
        if (!target || !target.trim()) {
            throw new Error("请提供有效的微信公众号推文或合集链接！");
        }
        const cleanUrl = target.trim();
        sendDebugLog(`[合集免凭证扫描] 开始解析公开网页: ${cleanUrl}`, "info");

        // 1. 发起标准 HTTP GET 获取文章公开 HTML
        const html = await fetchHtmlDirect(cleanUrl);
        if (!html) {
            throw new Error("未能获取到该推文网页内容，请检查网络连接或链接是否有效。");
        }

        // 2. 提取作者名称
        let author = "微信公众号";
        const nickMatch = html.match(/var\s+nickname\s*=\s*["']([^"']+)["']/) || html.match(/id="js_name">\s*([^<]+)\s*</);
        if (nickMatch) author = nickMatch[1].trim();

        // 3. 提取 biz
        let biz = "";
        const bizMatch = html.match(/var\s+biz\s*=\s*["']([^"']+)["']/) || cleanUrl.match(/[?&]__biz=([^&#]+)/) || html.match(/__biz=([^&#"']+)/);
        if (bizMatch) biz = decodeURIComponent(bizMatch[1]);

        // 4. 从文章中提取全部合集
        let albums = [];

        // 4.1 从正文内嵌的 appmsgalbuminfo / article_tag_list JS 对象提取
        const albumInfoBlocks = html.match(/appmsgalbuminfo\s*:\s*\{([^}]+)\}/gi) || [];
        for (const block of albumInfoBlocks) {
            const idM = block.match(/album_id\s*:\s*['"]([0-9]+)['"]/i);
            const titleM = block.match(/title\s*:\s*['"]([^'"]+)['"]/i);
            if (idM) {
                const albId = idM[1];
                let rawTitle = titleM ? titleM[1] : `专辑_${albId}`;
                let title = rawTitle.replace(/\\x26amp;/g, '&').replace(/\\x26quot;/g, '"').replace(/\\x26/g, '&').replace(/&amp;/g, '&');
                if (!albums.some(a => a.album_id === albId)) {
                    albums.push({
                        album_id: albId,
                        title: title,
                        url: `https://mp.weixin.qq.com/mp/appmsgalbum?action=getalbum&__biz=${biz}&album_id=${albId}#wechat_redirect`,
                        article_count: 0
                    });
                }
            }
        }

        // 4.2 从正文 HTML 挂载卡片提取 (a[href*='appmsgalbum'] / data-album-id)
        const albumLinkRegex = /<a[^>]+(?:href=["'][^"']*album_id=([0-9]+)[^"']*["']|data-album-id=["']([0-9]+)["'])[^>]*>([\s\S]*?)<\/a>/gi;
        let m;
        while ((m = albumLinkRegex.exec(html)) !== null) {
            const albId = m[1] || m[2];
            const rawInner = m[3] || "";
            let title = rawInner.replace(/<[^>]+>/g, "").replace(/收录于合集/g, "").trim() || `专辑_${albId}`;
            if (title.length > 50) title = title.slice(0, 50) + "...";
            if (albId && !albums.some(a => a.album_id === albId)) {
                albums.push({
                    album_id: albId,
                    title: title,
                    url: `https://mp.weixin.qq.com/mp/appmsgalbum?action=getalbum&__biz=${biz}&album_id=${albId}#wechat_redirect`,
                    article_count: 0
                });
            }
        }

        // 4.3 深度反向挖掘：如果捕获了微信通信凭证，自动拉取最近 20 篇推文并并发提取它们所属的全部合集
        if (biz && wechatAuth && wechatAuth.key) {
            try {
                sendDebugLog(`[全量合集反向挖掘] 正在并发扫描【${author}】的历史推文以挖掘全部合集...`, "info");
                const batchRes = await fetchWechatHistoryArticles(biz, author, 20);
                const artList = (batchRes && batchRes.articles) || [];
                if (artList.length > 0) {
                    sendDebugLog(`[推文列表获取成功] 正在对 ${artList.length} 篇推文进行合集反向挖掘...`, "info");
                    const tasks = artList.map(art => async () => {
                        try {
                            const subHtml = await fetchHtmlDirect(art.url);
                            if (subHtml) {
                                // 提取 appmsgalbuminfo
                                const subBlocks = subHtml.match(/appmsgalbuminfo\s*:\s*\{([^}]+)\}/gi) || [];
                                for (const b of subBlocks) {
                                    const idM = b.match(/album_id\s*:\s*['"]([0-9]+)['"]/i);
                                    const titleM = b.match(/title\s*:\s*['"]([^'"]+)['"]/i);
                                    if (idM) {
                                        const albId = idM[1];
                                        let rawTitle = titleM ? titleM[1] : `专辑_${albId}`;
                                        let t = rawTitle.replace(/\\x26amp;/g, '&').replace(/\\x26quot;/g, '"').replace(/\\x26/g, '&').replace(/&amp;/g, '&').trim();
                                        if (!albums.some(a => a.album_id === albId)) {
                                            albums.push({
                                                album_id: albId,
                                                title: t,
                                                url: `https://mp.weixin.qq.com/mp/appmsgalbum?action=getalbum&__biz=${biz}&album_id=${albId}#wechat_redirect`,
                                                article_count: 0
                                            });
                                            sendDebugLog(`[反向发现新合集] 成功挖掘到专栏: 【${t}】(ID: ${albId})`, "success");
                                        }
                                    }
                                }
                                // 提取 HTML 中的 album 链接
                                let subM;
                                const subRegex = /<a[^>]+(?:href=["'][^"']*album_id=([0-9]+)[^"']*["']|data-album-id=["']([0-9]+)["'])[^>]*>([\s\S]*?)<\/a>/gi;
                                while ((subM = subRegex.exec(subHtml)) !== null) {
                                    const albId = subM[1] || subM[2];
                                    const rawInner = subM[3] || "";
                                    let t = rawInner.replace(/<[^>]+>/g, "").replace(/收录于合集/g, "").trim() || `专辑_${albId}`;
                                    if (t.length > 50) t = t.slice(0, 50) + "...";
                                    if (albId && !albums.some(a => a.album_id === albId)) {
                                        albums.push({
                                            album_id: albId,
                                            title: t,
                                            url: `https://mp.weixin.qq.com/mp/appmsgalbum?action=getalbum&__biz=${biz}&album_id=${albId}#wechat_redirect`,
                                            article_count: 0
                                        });
                                        sendDebugLog(`[反向发现新合集] 成功挖掘到专栏: 【${t}】(ID: ${albId})`, "success");
                                    }
                                }
                            }
                        } catch(e) {}
                    });

                    // 限制并发执行
                    const concurrency = 6;
                    for (let i = 0; i < tasks.length; i += concurrency) {
                        await Promise.all(tasks.slice(i, i + concurrency).map(fn => fn()));
                    }
                }
            } catch(e) {
                sendDebugLog(`[全量合集反向挖掘受限] ${e.message}`, "warn");
            }
        }

        // 5. 如果输入本身就是合集链接
        if (cleanUrl.includes("album_id=") || cleanUrl.includes("appmsgalbum")) {
            const selfIdMatch = cleanUrl.match(/album_id=([0-9]+)/);
            if (selfIdMatch) {
                const selfAlbId = selfIdMatch[1];
                let titleMatch = html.match(/class=["']album__author-name["'][^>]*>([^<]+)</) || html.match(/<h1[^>]*>([^<]+)<\/h1>/);
                let title = titleMatch ? titleMatch[1].trim() : `专栏专辑_${selfAlbId}`;
                if (!albums.some(a => a.album_id === selfAlbId)) {
                    albums.unshift({
                        album_id: selfAlbId,
                        title: title,
                        url: cleanUrl,
                        article_count: 0
                    });
                }
            }
        }

        // 6. 持久化存储到 data/albums/albums_{biz}.json
        let savedAlbums = albums;
        if (biz) {
            const safeBiz = biz.replace(/[^a-zA-Z0-9_-]/g, "");
            const albumsDir = path.join(DATA_DIR, "albums");
            if (!fs.existsSync(albumsDir)) fs.mkdirSync(albumsDir, { recursive: true });
            const albumFile = path.join(albumsDir, `albums_${safeBiz}.json`);
            
            let existing = [];
            if (fs.existsSync(albumFile)) {
                try { existing = JSON.parse(fs.readFileSync(albumFile, "utf8")); } catch(e) {}
            }
            const existingIds = new Set(existing.map(a => a.album_id));
            for (const alb of albums) {
                if (!existingIds.has(alb.album_id)) {
                    existing.push({
                        ...alb,
                        author: author,
                        biz: biz,
                        discovered_at: new Date().toLocaleString()
                    });
                    existingIds.add(alb.album_id);
                }
            }
            fs.writeFileSync(albumFile, JSON.stringify(existing, null, 2), "utf8");
            savedAlbums = existing;
        }

        sendDebugLog(`[合集免凭证扫描完成] 成功发现 ${savedAlbums.length} 个专栏合集 (100% 零风控)`, "info");
        return {
            success: true,
            author,
            biz,
            albums: savedAlbums,
            total: savedAlbums.length
        };
    });

    ipcMain.handle("export:start", async (_, options) => {
        return await exportArticlesLocal(
            options,
            (msg, current, total) => {
                if (mainWindow) mainWindow.webContents.send("export:progress", { message: msg, current, total });
            },
            (statusData) => {
                if (mainWindow) mainWindow.webContents.send("export:article-status", statusData);
            }
        );
    });

    ipcMain.handle("fs:select-dir", async () => {
        const res = await dialog.showOpenDialog(mainWindow, {
            properties: ["openDirectory", "createDirectory"]
        });
        if (!res.canceled && res.filePaths.length > 0) {
            return res.filePaths[0];
        }
        return DEFAULT_EXPORT_DIR;
    });

    ipcMain.handle("fs:open-dir", (_, dirPath) => {
        const target = dirPath || DEFAULT_EXPORT_DIR;
        shell.openPath(target);
        return true;
    });

    ipcMain.handle("settings:get", () => {
        const settingsFile = path.join(DATA_DIR, "settings.json");
        let saved = {};
        if (fs.existsSync(settingsFile)) {
            try { saved = JSON.parse(fs.readFileSync(settingsFile, "utf8")); } catch(e) {}
        }
        return {
            exportDir: (saved && saved.exportDir) || DEFAULT_EXPORT_DIR,
            proxyActive: true
        };
    });

    ipcMain.handle("settings:save", (_, newSettings) => {
        const settingsFile = path.join(DATA_DIR, "settings.json");
        let current = {};
        if (fs.existsSync(settingsFile)) {
            try { current = JSON.parse(fs.readFileSync(settingsFile, "utf8")); } catch(e) {}
        }
        const updated = { ...current, ...newSettings };
        fs.writeFileSync(settingsFile, JSON.stringify(updated, null, 2), "utf8");
        return updated;
    });
});

function cleanup() {
    applyWindowsPac(8899, false);
    if (proxyInstance) {
        try { proxyInstance.close(); } catch (e) {}
    }
}

app.on("before-quit", cleanup);
app.on("will-quit", cleanup);
app.on("window-all-closed", () => {
    cleanup();
    if (process.platform !== "darwin") app.quit();
});
