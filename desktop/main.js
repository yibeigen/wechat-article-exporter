process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";

const { app, BrowserWindow, ipcMain, dialog, shell, nativeImage, session } = require("electron");
const path = require("path");
const fs = require("fs");
const http = require("http");
const https = require("https");
const net = require("net");
const tls = require("tls");
const url = require("url");
const os = require("os");
const { exec, execFile, spawn, execSync, spawnSync } = require("child_process");
const forge = require("node-forge");
const docx = require("docx");
const writeXlsxFile = require("write-excel-file/node");
const { imageSize } = require("image-size");
const { unzipSync } = require("fflate");
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
const HISTORY_FILE = path.join(DATA_DIR, "accounts_history.json");

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
let localPythonManager = null;

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
    mpConnected: false,
    lastTrafficAt: 0  // 最近一次嗅探到微信 HTTPS 流量的时间戳，用于判断代理链路是否畅通
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

// =========================================================================
// 1.1 本地公众号历史档案库磁盘持久化管理 (AccountHistoryManager)
// 彻底解决浏览器 localStorage 5MB 限制与重启丢失问题
// =========================================================================
class AccountHistoryManager {
    static getAll() {
        try {
            if (fs.existsSync(HISTORY_FILE)) {
                const raw = fs.readFileSync(HISTORY_FILE, "utf8");
                const list = JSON.parse(raw);
                if (Array.isArray(list) && list.length > 0) return list;
            }
            // 若历史文件尚无数据，尝试自动从 cache 目录导入旧缓存数据
            const cacheDir = path.join(DATA_DIR, "cache");
            if (fs.existsSync(cacheDir)) {
                const files = fs.readdirSync(cacheDir).filter(f => f.startsWith("articles_") && f.endsWith(".json"));
                const migrated = [];
                for (const f of files) {
                    try {
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
                            migrated.push({
                                author,
                                biz,
                                count: articles.length,
                                updatedAt: new Date().toLocaleString(),
                                articles
                            });
                        }
                    } catch(e) {}
                }
                if (migrated.length > 0) {
                    fs.writeFileSync(HISTORY_FILE, JSON.stringify(migrated, null, 2), "utf8");
                    return migrated;
                }
            }
        } catch(e) {
            console.error("[AccountHistory] 读取历史档案异常:", e);
        }
        return [];
    }

    static saveAccount(accountData) {
        if (!accountData || !accountData.articles || accountData.articles.length === 0) return false;
        try {
            const list = this.getAll();
            const key = accountData.biz || accountData.author;
            const filtered = list.filter(item => {
                const itemKey = item.biz || item.author;
                return itemKey !== key;
            });

            const newItem = {
                author: accountData.author || "微信公众号",
                biz: accountData.biz || "",
                targetUrl: accountData.targetUrl || "",
                count: accountData.articles.length,
                updatedAt: accountData.updatedAt || new Date().toLocaleString(),
                articles: accountData.articles
            };

            filtered.unshift(newItem);
            const finalList = filtered.slice(0, 100);
            fs.writeFileSync(HISTORY_FILE, JSON.stringify(finalList, null, 2), "utf8");
            return true;
        } catch(e) {
            console.error("[AccountHistory] 保存历史档案异常:", e);
            return false;
        }
    }

    static deleteAccount(key) {
        try {
            const list = this.getAll();
            const filtered = list.filter(item => {
                const itemKey = item.biz || item.author;
                return itemKey !== key && item.biz !== key && item.author !== key;
            });
            fs.writeFileSync(HISTORY_FILE, JSON.stringify(filtered, null, 2), "utf8");
            return true;
        } catch(e) {
            console.error("[AccountHistory] 删除历史档案异常:", e);
            return false;
        }
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
                // 记录最近一次微信流量时间，并打限频心跳日志（5 秒最多 1 条）
                wechatAuth.lastTrafficAt = Date.now();
                const now = Date.now();
                if (!this._lastConnectLog || now - this._lastConnectLog > 5000) {
                    this._lastConnectLog = now;
                    sendDebugLog(`[代理链路] 已截获微信 HTTPS 连接: ${host} (微信流量正经过工具代理 ✓)`, "info");
                }
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

        // 诊断增强：记录所有"首次出现"的请求路径（按路径去重，不再 5 秒限频）。
        // 目的：当用户在电脑微信里滚动公众号主页时，看清微信客户端到底用哪个接口拉文章列表，
        // 从而区分"服务器返回空列表"还是"客户端改用了工具未解析的新接口"这两种截然不同的根因。
        const reqPath = reqUrl.split("?")[0];
        if (!this._seenReqPaths) this._seenReqPaths = new Set();
        if (!this._seenReqPaths.has(reqPath)) {
            this._seenReqPaths.add(reqPath);
            sendDebugLog(`[代理链路] 新请求路径: ${reqPath}`, "info");
        }

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

                // 微信主页/文章经常通过 302/307 跳转附带全新 key/pass_ticket，
                // 但跳转目标可能不会再被客户端重新发起，因此在这里主动嗅探 Location。
                const location = forwardRes.headers["location"];
                if (location && typeof location === "string" && location.includes("mp.weixin.qq.com")) {
                    this.parseAndFire(location, "", setCookies, "", referer);
                }
                
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
                    if (bodyStr.startsWith("{") && bodyStr.endsWith("}")) {
                        const jsonBody = JSON.parse(bodyStr);
                        if (!uin && jsonBody.uin) uin = String(jsonBody.uin);
                        if (!key && jsonBody.key) key = String(jsonBody.key);
                        if (!pass_ticket && jsonBody.pass_ticket) pass_ticket = String(jsonBody.pass_ticket);
                        if (!appmsg_token && jsonBody.appmsg_token) appmsg_token = String(jsonBody.appmsg_token);
                        if (!biz && jsonBody.__biz) biz = String(jsonBody.__biz);
                    } else {
                        const bodyParams = new URLSearchParams(bodyStr);
                        if (!uin) uin = bodyParams.get("uin") || "";
                        if (!key) key = bodyParams.get("key") || "";
                        if (!pass_ticket) pass_ticket = bodyParams.get("pass_ticket") || "";
                        if (!appmsg_token) appmsg_token = bodyParams.get("appmsg_token") || "";
                        if (!biz) biz = bodyParams.get("__biz") || "";
                    }
                } catch(e) {}
            }

            if (refererHeader) {
                try {
                    const refUrl = new URL(refererHeader.startsWith("http") ? refererHeader : `https://${this.target}${refererHeader}`);
                    if (!key) key = refUrl.searchParams.get("key") || "";
                    if (!uin) uin = refUrl.searchParams.get("uin") || "";
                    if (!pass_ticket) pass_ticket = refUrl.searchParams.get("pass_ticket") || "";
                    if (!appmsg_token) appmsg_token = refUrl.searchParams.get("appmsg_token") || "";
                    if (!biz) {
                        const refBiz = refUrl.searchParams.get("__biz");
                        if (refBiz && isValidBiz(refBiz)) biz = refBiz;
                    }
                } catch(e) {
                    const refMatch = refererHeader.match(/__biz=([^&#]+)/);
                    if (refMatch && isValidBiz(decodeURIComponent(refMatch[1]))) {
                        biz = decodeURIComponent(refMatch[1]);
                    }
                }
            }

            let wap_sid2 = "";
            const cookieStr = [cookieHeader, ...setCookies].join("; ");
            const sidMatch = cookieStr.match(/(?:^|;\s*)wap_sid2=([^;]+)/);
            if (sidMatch) wap_sid2 = sidMatch[1];
            const ptMatch = cookieStr.match(/(?:^|;\s*)pass_ticket=([^;]+)/);
            if (ptMatch && !pass_ticket) pass_ticket = ptMatch[1];
            const uinMatch = cookieStr.match(/(?:^|;\s*)(?:wxuin|uin)=([^;]+)/);
            if (uinMatch && !uin) uin = uinMatch[1];
            const keyMatch = cookieStr.match(/(?:^|;\s*)key=([^;]+)/);
            if (keyMatch && !key) key = keyMatch[1];
            const tokenMatch = cookieStr.match(/(?:^|;\s*)appmsg_token=([^;]+)/);
            if (tokenMatch && !appmsg_token) appmsg_token = tokenMatch[1];

            const isProfileRequest = reqUrl.includes("profile_ext") || (refererHeader && refererHeader.includes("profile_ext"));

            if (key || pass_ticket || wap_sid2 || appmsg_token || biz) {
                this.onCaptured({ uin, key, pass_ticket, appmsg_token, wap_sid2, biz, isProfileRequest });
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
        // 同时把 ProxyOverride（绕过列表）重置为仅绕过本地地址，防止残留的 *.qq.com 等条目把微信流量绕过代理
        const cmd = `reg delete "${INET_KEY}" /v AutoConfigURL /f 2>nul & reg add "${INET_KEY}" /v ProxyEnable /t REG_DWORD /d 1 /f & reg add "${INET_KEY}" /v ProxyServer /t REG_SZ /d "127.0.0.1:${port}" /f & reg add "${INET_KEY}" /v ProxyOverride /t REG_SZ /d "<local>" /f`;
        exec(cmd, () => {
            exec(refreshCmd, () => {
                console.log(`[BlogDistiller] Windows 系统代理已强制激活: 127.0.0.1:${port}`);
                // 回读注册表实际状态并输出到诊断日志，方便确认代理是否真的生效
                exec(`reg query "${INET_KEY}" /v ProxyServer & reg query "${INET_KEY}" /v ProxyOverride 2>nul`, (qErr, qStdout) => {
                    const state = qStdout ? qStdout.replace(/\s+/g, " ").trim() : "注册表回读失败";
                    sendDebugLog(`[系统代理] 已激活并广播刷新 (127.0.0.1:${port})。回读状态: ${state}`, "info");
                });
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

// 从任意响应体（HTML/JS/JSON）中抓取微信凭证，解决 key 只出现在页面脚本或 JSON 中的情况。
function extractAuthFromBody(resBodyStr, reqUrl = "") {
    if (!resBodyStr) return;
    const payload = resBodyStr;
    const keyM = payload.match(/["']?key["']?\s*[:=]\s*["']([a-f0-9]{32,})["']/i) ||
                 payload.match(/var\s+key\s*=\s*["']([a-f0-9]{32,})["']/i);
    const passM = payload.match(/["']?pass_ticket["']?\s*[:=]\s*["']([^"']+)["']/i) ||
                  payload.match(/var\s+pass_ticket\s*=\s*["']([^"']+)["']/i);
    const tokenM = payload.match(/["']?appmsg_token["']?\s*[:=]\s*["']([^"']+)["']/i) ||
                   payload.match(/var\s+appmsg_token\s*=\s*["']([^"']+)["']/i);
    const bizM = payload.match(/["']?__?biz["']?\s*[:=]\s*["']([A-Za-z0-9+/=]{10,})["']/i) ||
                 payload.match(/var\s+biz\s*=\s*["']([A-Za-z0-9+/=]{10,})["']/i);
    if (keyM || passM || tokenM) {
        const tokenMatch = reqUrl.match(/profile_ext\?([^\s]*)/);
        const isProfileRequest = tokenMatch ? tokenMatch[0].includes("action=home") || tokenMatch[0].includes("action=getmsg") : false;
        handleCapturedAuth({
            key: keyM ? keyM[1] : undefined,
            pass_ticket: passM ? passM[1] : undefined,
            appmsg_token: tokenM ? tokenM[1] : undefined,
            biz: bizM ? bizM[1] : undefined,
            isProfileRequest
        });
    }
}

function parseResponseArticles(reqUrl, resBodyStr) {
    if (!resBodyStr) return;

    // 恢复 v1.0 的嗅探活动日志（限频 5 秒防刷屏）：让用户能在诊断日志里直观看到嗅探通道是否在工作
    const sniffNow = Date.now();
    if (!parseResponseArticles._lastLog || sniffNow - parseResponseArticles._lastLog > 5000) {
        parseResponseArticles._lastLog = sniffNow;
        sendDebugLog(`[微信嗅探] 收到响应: ${reqUrl.slice(0, 80)} (${resBodyStr.length} 字符)`, "info");
    }

    // 任何响应都可能携带新 key/pass_ticket/appmsg_token，先在响应体里抓一次凭证。
    extractAuthFromBody(resBodyStr, reqUrl);

    // 调试采样：把嗅探到的 profile_ext 响应全文保存到本地（带时间戳，永不覆盖），
    // 供离线分析微信真实数据结构。这是定位"嗅探到主页响应却提取不到文章"的决定性手段。
    if (reqUrl.includes("profile_ext")) {
        try {
            const now = new Date();
            const ts = `${now.getHours().toString().padStart(2,"0")}${now.getMinutes().toString().padStart(2,"0")}${now.getSeconds().toString().padStart(2,"0")}_${now.getMilliseconds().toString().padStart(3,"0")}`;
            const isGetmsg = reqUrl.includes("action=getmsg");
            const actionTag = isGetmsg ? "getmsg" : "home";
            const sampleFile = path.join(DATA_DIR, `debug_${actionTag}_${ts}.${isGetmsg ? "json" : "html"}`);
            fs.writeFileSync(sampleFile, resBodyStr, "utf8");
            // 同步保存一份当前最新副本，方便快速查看
            const latestFile = path.join(DATA_DIR, isGetmsg ? "debug_last_getmsg.json" : "debug_last_home.html");
            fs.writeFileSync(latestFile, resBodyStr, "utf8");
            if (!isGetmsg) {
                const rawMsg = extractMsgListFromHtml(resBodyStr);
                const parsedPreview = parseMsgListRaw(rawMsg || "");
                // 打印主页 HTML 的关键特征，并保存提取到的 msgList 原始字符串，便于定位解析失败点
                const feat = {
                    长度: resBodyStr.length,
                    "var_msgList": /var\s+msgList/.test(resBodyStr),
                    "msgList赋值": /msgList\s*=/.test(resBodyStr),
                    "window_msgList": /window\s*\.\s*msgList/.test(resBodyStr),
                    "含getmsg接口串": resBodyStr.includes("action=getmsg"),
                    "文章链接数": (resBodyStr.match(/mp\.weixin\.qq\.com\/s\?__biz/g) || []).length,
                    "含home_page_list": resBodyStr.includes("home_page_list"),
                    "含general_msg_list": resBodyStr.includes("general_msg_list"),
                    "是否验证页": resBodyStr.includes("请在微信客户端打开链接"),
                    "提取rawMsg长度": rawMsg ? rawMsg.length : 0,
                    "解析后列表长度": parsedPreview.length
                };
                sendDebugLog(`[主页采样] 已保存 ${sampleFile}。特征: ${JSON.stringify(feat)}`, "info");
                if (rawMsg) {
                    fs.writeFileSync(path.join(DATA_DIR, `debug_home_${ts}_raw.txt`), rawMsg, "utf8");
                } else if (/msgList/i.test(resBodyStr)) {
                    // msgList 存在但提取失败：保存其前后 600 字符片段，直接看清微信当前的赋值格式
                    const idx = resBodyStr.search(/msgList/i);
                    const snippet = resBodyStr.slice(Math.max(0, idx - 100), idx + 500);
                    fs.writeFileSync(path.join(DATA_DIR, `debug_home_${ts}_snippet.txt`), snippet, "utf8");
                    sendDebugLog(`[主页采样] msgList 提取失败，已保存上下文片段至 debug_home_${ts}_snippet.txt`, "warn");
                }
            } else {
                sendDebugLog(`[分页采样] 已保存 ${sampleFile} (${resBodyStr.length} 字符)`, "info");
            }
        } catch(e) {}
    }

    // 尝试从页面或响应中提取公众号名称与 biz
    let detectedAuthor = wechatAuth.author || "微信公众号";
    
    // 多维度智能提取公众号名称 (支持微信最新桌面版/H5/LiteApp等各种模板)
    const nickPatterns = [
        /var\s+nickname\s*=\s*['"]([^'"]+)['"]/i,
        /<strong[^>]*class="[^"]*profile_nickname[^"]*"[^>]*>([\s\S]*?)<\/strong>/i,
        /<a[^>]*id="js_name"[^>]*>([\s\S]*?)<\/a>/i,
        /<div[^>]*class="[^"]*profile_nickname[^"]*"[^>]*>([\s\S]*?)<\/div>/i,
        /<p[^>]*class="[^"]*profile_account_name[^"]*"[^>]*>([\s\S]*?)<\/p>/i,
        /"nickname"\s*:\s*["']([^"']+)["']/i,
        /"author"\s*:\s*["']([^"']+)["']/i,
        /<meta\s+property="og:title"\s+content="([^"]+)"/i
    ];

    for (const p of nickPatterns) {
        const m = resBodyStr.match(p);
        if (m && m[1] && m[1].trim()) {
            const clean = unescapeWechatText(m[1].replace(/<[^>]+>/g, "").trim());
            if (clean && !clean.includes("微信") && !clean.includes("JavaScript") && !clean.includes("页面不存在") && clean.length <= 40) {
                detectedAuthor = clean;
                wechatAuth.author = clean;
                break;
            } else if (clean && clean !== "微信公众号" && clean.length <= 40) {
                detectedAuthor = clean;
                wechatAuth.author = clean;
            }
        }
    }
    if (!detectedAuthor || detectedAuthor === "微信公众号") {
        if (wechatAuth.author && wechatAuth.author !== "微信公众号") {
            detectedAuthor = wechatAuth.author;
        }
    }

    const detectedBiz = extractWechatBiz(reqUrl, resBodyStr, wechatAuth.biz);
    if (detectedBiz && isValidBiz(detectedBiz)) {
        wechatAuth.biz = detectedBiz;
        sendDebugLog(`[微信嗅探] 成功锁定公众号【${detectedAuthor}】(biz: ${detectedBiz})`, "success");
    }

    // 1. profile_ext?action=getmsg (分页历史文章列表)
    if (reqUrl.includes("profile_ext") && reqUrl.includes("action=getmsg")) {
        try {
            const data = JSON.parse(resBodyStr);
            const rawList = data.general_msg_list || data.msg_list || data.app_msg_list || data.list || data.home_page_list;
            if (rawList) {
                const listObj = typeof rawList === "string" ? JSON.parse(rawList) : rawList;
                const msgList = Array.isArray(listObj) ? listObj : (listObj.list || listObj.app_msg_list || []);
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
                    if (appInfo.multi_app_msg_item_list && Array.isArray(appInfo.multi_app_msg_item_list)) {
                        for (let subIdx = 0; subIdx < appInfo.multi_app_msg_item_list.length; subIdx++) {
                            const sub = appInfo.multi_app_msg_item_list[subIdx];
                            if (sub.title && sub.content_url) {
                                const cleanSubUrl = sub.content_url.replace(/&amp;/g, "&");
                                extracted.push({
                                    id: `art_${comm.id || Date.now()}_${subIdx + 1}`,
                                    title: sub.title.replace(/<[^>]+>/g, "").trim(),
                                    author: detectedAuthor,
                                    url: cleanSubUrl.startsWith("http") ? cleanSubUrl : `https://mp.weixin.qq.com${cleanSubUrl}`,
                                    create_time,
                                    digest: sub.digest || "",
                                    cover: sub.cover || "",
                                    is_original: sub.copyright_stat === 11 || sub.copyright_stat === 1,
                                    biz: wechatAuth.biz,
                                    status: "pending",
                                    fail_reason: ""
                                });
                            }
                        }
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
            // 使用可靠的引号配对扫描提取 msgList（旧正则会把 {\"list\" 截断成 "{" 导致解析必败）
            const rawMsg = extractMsgListFromHtml(resBodyStr);
            if (rawMsg) {
                const msgList = parseMsgListRaw(rawMsg);
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
                    if (appInfo.multi_app_msg_item_list && Array.isArray(appInfo.multi_app_msg_item_list)) {
                        for (let subIdx = 0; subIdx < appInfo.multi_app_msg_item_list.length; subIdx++) {
                            const sub = appInfo.multi_app_msg_item_list[subIdx];
                            if (sub.title && sub.content_url) {
                                const cleanSubUrl = sub.content_url.replace(/&amp;/g, "&");
                                extracted.push({
                                    id: `art_${comm.id || Date.now()}_${subIdx + 1}`,
                                    title: sub.title.replace(/<[^>]+>/g, "").trim(),
                                    author: detectedAuthor,
                                    url: cleanSubUrl.startsWith("http") ? cleanSubUrl : `https://mp.weixin.qq.com${cleanSubUrl}`,
                                    create_time,
                                    digest: sub.digest || "",
                                    cover: sub.cover || "",
                                    is_original: sub.copyright_stat === 11 || sub.copyright_stat === 1,
                                    biz: wechatAuth.biz,
                                    status: "pending",
                                    fail_reason: ""
                                });
                            }
                        }
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
        const normalizedStr = resBodyStr.replace(/\\\//g, "/");
        const urlPattern = /(?:https?:)?\/\/mp\.weixin\.qq\.com\/s(?:\/|(?:\?[^"'\s<>]*))/g;
        let match;
        const autoExtracted = [];
        const seenUrls = new Set();
        while ((match = urlPattern.exec(normalizedStr)) !== null) {
            let rawUrl = match[0];
            if (rawUrl.startsWith("//")) rawUrl = "https:" + rawUrl;
            const cleanUrl = rawUrl.replace(/&amp;/g, "&").replace(/\\x26/g, "&");
            if (!seenUrls.has(cleanUrl)) {
                seenUrls.add(cleanUrl);
                const startPos = Math.max(0, match.index - 600);
                const endPos = Math.min(normalizedStr.length, match.index + 600);
                const snippet = normalizedStr.slice(startPos, endPos);
                
                const titleMatch = snippet.match(/"title"\s*:\s*"([^"]+)"/) ||
                                   snippet.match(/"msg_title"\s*:\s*"([^"]+)"/) ||
                                   snippet.match(/title="([^"]+)"/) ||
                                   snippet.match(/data-title="([^"]+)"/) ||
                                   snippet.match(/<h[1-6][^>]*>([\s\S]*?)<\/h[1-6]>/) ||
                                   snippet.match(/<a[^>]+>([\s\S]*?)<\/a>/);
                
                let title = titleMatch ? unescapeWechatText(titleMatch[1].replace(/<[^>]+>/g, "")) : "";
                if (!title || title.length <= 2 || title.includes("weixin.qq.com") || title.includes("JavaScript")) {
                    title = `推文_${autoExtracted.length + 1}`;
                }

                const timeMatch = snippet.match(/"datetime"\s*:\s*([0-9]{9,11})/) || snippet.match(/"create_time"\s*:\s*([0-9]{9,11})/);
                const create_time = timeMatch ? new Date(parseInt(timeMatch[1], 10) * 1000).toISOString().split("T")[0] : "";

                autoExtracted.push({
                    id: `art_${Date.now()}_${autoExtracted.length}`,
                    title,
                    author: detectedAuthor,
                    url: cleanUrl,
                    create_time,
                    digest: "",
                    cover: "",
                    is_original: true,
                    biz: wechatAuth.biz || "",
                    status: "pending",
                    fail_reason: ""
                });
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
    // 保存旧凭证，用于判断本次嗅探是否带来了新的会话要素
    const prevKey = wechatAuth.key;
    const prevPassTicket = wechatAuth.pass_ticket;
    const prevWapSid2 = wechatAuth.wap_sid2;

    const isNewKey = Boolean(data.key && data.key !== prevKey);
    const isNewPassTicket = Boolean(data.pass_ticket && data.pass_ticket !== prevPassTicket);
    const isNewWapSid2 = Boolean(data.wap_sid2 && data.wap_sid2 !== prevWapSid2);

    // 关键修复：用展开运算符保留 mpToken/mpCookie/mpConnected/lastTrafficAt 等字段，
    // 否则每次嗅探到新凭证都会把"官方扫码通道已连接"的状态整体抹掉，
    // 导致界面上官方通道显示退回"未连接"、链路自检永远误报"流量未经过代理"。
    wechatAuth = {
        ...wechatAuth,
        captured: true,
        uin: data.uin || wechatAuth.uin,
        key: data.key || wechatAuth.key,
        pass_ticket: data.pass_ticket || wechatAuth.pass_ticket,
        appmsg_token: data.appmsg_token || wechatAuth.appmsg_token,
        wap_sid2: data.wap_sid2 || wechatAuth.wap_sid2,
        biz: (data.biz && isValidBiz(data.biz)) ? data.biz : wechatAuth.biz,
        author: data.author || wechatAuth.author || "",
        captured_at: new Date().toLocaleTimeString(),
        // 新增：毫秒级时间戳。此前只存 toLocaleTimeString()（如 "10:30:45"），
        // 渲染层 new Date() 解析不出日期，导致 30 分钟凭证倒计时永远显示满格。
        captured_ts: Date.now()
    };
    try {
        fs.writeFileSync(AUTH_FILE, JSON.stringify(wechatAuth, null, 2), "utf8");
    } catch(e) {}

    if ((isNewKey || isNewPassTicket || isNewWapSid2) && wechatAuth.key !== lastLoggedKey) {
        lastLoggedKey = wechatAuth.key;
        sendDebugLog(`[通信状态] 成功截获并更新微信最新会话凭证 (uin: ${wechatAuth.uin || "已具备"}, key: ${wechatAuth.key ? wechatAuth.key.slice(0, 8) + "..." : "已具备"}, pass_ticket: ${wechatAuth.pass_ticket ? "已具备" : "缺省"})`, "success");
    }
    if (mainWindow) {
        mainWindow.webContents.send("wechat:status-change", wechatAuth);
    }

    // 自动触发全量文章拉取（恢复 v1.0 可用版行为：只要嗅探到任何微信会话活动且有凭证，立即重试待办任务。
    // 反重力版本收紧为"必须凭证更新才触发"，导致用户打开文章/主页后软件经常不自动重试，表现为"获取不到"）
    if (pendingAutoFetchTarget && (wechatAuth.key || wechatAuth.pass_ticket || wechatAuth.wap_sid2)) {
        const targetInfo = pendingAutoFetchTarget;
        pendingAutoFetchTarget = null;
        sendDebugLog(`[自动就绪] 嗅探到微信会话活动，立即自动触发对【${targetInfo.author || "公众号"}】的全量历史文章拉取！`, "info");
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
        const safeUin = formatWechatUin(wechatAuth.uin);
        if (safeUin) {
            cookieArr.push(`wxuin=${safeUin}`);
            cookieArr.push(`uin=${safeUin}`);
        }
        if (wechatAuth.key) cookieArr.push(`key=${wechatAuth.key}`);
        if (cookieArr.length > 0) options.headers["cookie"] = cookieArr.join("; ");
        options.headers["referer"] = "https://mp.weixin.qq.com/";

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
    // 新版微信桌面端在某些请求里会把 uin 以纯数字形式暴露出来，
    // 而 profile_ext 接口约定参数为 base64，因此这里做兼容转换。
    if (/^\d+$/.test(str)) {
        return Buffer.from(str).toString("base64");
    }
    return str;
}

// 通用转换 msgList/app_msg_list 中为文章对象
function parseWechatMsgList(msgList, author, biz) {
    const result = [];
    for (const item of msgList) {
        const comm = item.comm_msg_info || {};
        const appInfo = item.app_msg_ext_info;
        if (!appInfo) continue;

        const create_time = comm.datetime ? new Date(comm.datetime * 1000).toISOString().split("T")[0] : "";

        // 头条文章
        if (appInfo.title && appInfo.content_url) {
            const cleanUrl = appInfo.content_url.replace(/&amp;/g, "&");
            result.push({
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
                    result.push({
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
    return result;
}

// 从主页 HTML 中提取 msgList 原始字符串（兼容单引号转义、双引号、裸对象三种格式）。
// 关键修复：旧正则 ['"]([^'"]+)['"] 遇到 {\"list\" 里的双引号会把内容截断成单个 "{"，
// 导致 JSON.parse 在 position 1 报错，主页首屏文章永远解析不出来（该 bug 在 v1.0 就存在，只是被静默吞掉）。
function extractMsgListFromHtml(html) {
    if (!html) return null;
    // 方式1：var msgList = '...'; —— 引号包裹，内部双引号带反斜杠转义（微信主页标准格式）
    const startMatch = html.match(/var\s+msgList\s*=\s*(['"])/i);
    if (startMatch) {
        const quote = startMatch[1];
        const startPos = startMatch.index + startMatch[0].length;
        // 手动扫描配对的结束引号，正确跳过反斜杠转义的字符（\" 不会误判为结束），
        // 并要求结束引号后必须是语句终结符（; , } 或换行），避免 JSON 内容里的裸引号（如标题 It's）误判
        let end = -1;
        for (let i = startPos; i < html.length; i++) {
            const ch = html[i];
            if (ch === "\\") { i++; continue; }
            if (ch === quote) {
                const rest = html.slice(i + 1, i + 8);
                if (/^\s*[;,}\r\n]/.test(rest)) { end = i; break; }
            }
        }
        if (end > startPos) {
            let raw = html.slice(startPos, end);
            // 内容是 JSON 字符串字面量（含 \" 等转义序列），按 JSON 字面量整体解码还原
            try { raw = JSON.parse('"' + raw + '"'); } catch(e) {
                // 解码失败则手动还原常见转义后继续
                raw = raw.replace(/\\"/g, '"');
            }
            return raw;
        }
    }
    // 方式2：裸 JSON 对象 var msgList = {...};
    const objMatch = html.match(/var\s+msgList\s*=\s*([{\[][\s\S]*?[}\]])\s*;/i);
    if (objMatch) return objMatch[1];
    return null;
}

// 把 msgList 原始字符串解析为微信消息列表数组（清理 HTML 实体后 JSON.parse）
function parseMsgListRaw(raw) {
    if (!raw) return [];
    try {
        const cleaned = String(raw)
            .replace(/\\x26quot;/g, '"')
            .replace(/&quot;/g, '"')
            .replace(/&amp;/g, "&");
        const listObj = JSON.parse(cleaned);
        return Array.isArray(listObj) ? listObj : (listObj.list || listObj.app_msg_list || []);
    } catch(e) {
        return [];
    }
}

// 当 profile_ext?action=getmsg 返回空列表时，尝试直接请求公众号主页 HTML 并解析首屏 msgList。
// 这是为了兼容某些场景：桌面微信的 getmsg 接口会要求"主页专属 key"，但主页 HTML 内嵌了首屏历史。
async function fetchProfileHomeFallback(biz, author, safeUin, baseHeaders) {
    sendDebugLog(`[Home页兜底] getmsg 接口首屏为空，尝试请求公众号主页 HTML 解析首屏历史文章...`, "info");

    const homeParams = new URLSearchParams({
        action: "home",
        __biz: biz,
        scene: "124",
        uin: safeUin,
        key: wechatAuth.key || "",
        pass_ticket: wechatAuth.pass_ticket || "",
        appmsg_token: wechatAuth.appmsg_token || ""
    });
    const homeUrl = `https://mp.weixin.qq.com/mp/profile_ext?${homeParams.toString()}#wechat_redirect`;

    const headers = {
        ...baseHeaders,
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "referer": `https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=${biz}&scene=124#wechat_redirect`
    };

    try {
        const html = await new Promise((resolve, reject) => {
            const req = https.get(homeUrl, { headers }, (res) => {
                let text = "";
                res.on("data", chunk => text += chunk);
                res.on("end", () => resolve(text));
            });
            req.on("error", reject);
            req.setTimeout(15000, () => req.destroy(new Error("主页请求超时")));
        });

        // 使用可靠的引号配对扫描提取 msgList（旧正则会把 {\"list\" 截断成 "{" 导致解析必败）
        const rawMsg = extractMsgListFromHtml(html);
        if (!rawMsg) {
            // 打印主页 HTML 的关键特征，便于判断微信返回的是验证页还是正常主页
            const isVerifyPage = html.includes("请在微信客户端打开链接") || html.includes("环境异常");
            sendDebugLog(`[Home页兜底] 主页 HTML 中未找到 msgList 字段，兜底失败。(HTML长度: ${html.length}, 是否验证页: ${isVerifyPage ? "是——微信拒绝了本次请求" : "否"})`, "warn");
            return { articles: [] };
        }

        const msgList = parseMsgListRaw(rawMsg);
        const parsed = parseWechatMsgList(msgList, author, biz);
        if (parsed.length > 0) {
            sendDebugLog(`[Home页兜底] 成功从主页 HTML 解析到 ${parsed.length} 篇首屏文章`, "success");
        } else {
            sendDebugLog(`[Home页兜底] 主页 msgList 解析成功但未提取到文章 (list长度: ${msgList.length})。`, "warn");
        }
        return { articles: parsed };
    } catch(err) {
        sendDebugLog(`[Home页兜底] 主页兜底异常: ${err.message}`, "warn");
        return { articles: [] };
    }
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
        const articlesBeforePage = articles.length;

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

        // 更详细的诊断输出（包含接口调用 URL 与部分响应内容），便于排查"有凭证但拿不到列表"
        // 日志中脱敏 key/pass_ticket/appmsg_token，避免敏感凭证留在本地诊断记录中
        if (resData.ret !== 0 || !resData.general_msg_list) {
            let preview = String(resData.errmsg || "");
            if (resData.ret === 0) {
                // ret=0 却没有文章列表：打印完整响应结构（脱敏），定位微信到底返回了什么
                try {
                    const safeJson = JSON.stringify(resData)
                        .replace(/"key":"[^"]+"/g, '"key":"***"')
                        .replace(/"pass_ticket":"[^"]+"/g, '"pass_ticket":"***"')
                        .replace(/"appmsg_token":"[^"]+"/g, '"appmsg_token":"***"');
                    preview = `keys=[${Object.keys(resData).join(",")}] body=${safeJson.slice(0, 300)}`;
                } catch(e) {}
            }
            const safeUrl = apiUrl
                .replace(/key=[^&]+/g, "key=***")
                .replace(/pass_ticket=[^&]+/g, "pass_ticket=***")
                .replace(/appmsg_token=[^&]+/g, "appmsg_token=***");
            sendDebugLog(`[接口调试] offset=${offset}, URL=${safeUrl}, 响应预览: ${preview.slice(0, 320) || "(empty)"}`, "info");
        }

        sendDebugLog(`[接口返回] 偏移量 offset=${offset}, 微信响应: ret=${resData.ret}, errmsg="${resData.errmsg || "ok"}"`, resData.ret === 0 ? "success" : "warn");

        if (resData.ret === 0) {
            rateLimitRetries = 0;
            let msgList = [];
            const rawList = resData.general_msg_list || resData.msg_list || resData.app_msg_list || resData.list || resData.home_page_list;
            if (rawList) {
                try {
                    const listObj = typeof rawList === "string" ? JSON.parse(rawList) : rawList;
                    msgList = Array.isArray(listObj) ? listObj : (listObj.list || listObj.app_msg_list || []);
                } catch(e) {}
            }

            // 首屏就为空时，先尝试主页 HTML 兜底解析，避免直接报错
            if (msgList.length === 0 && offset === 0) {
                const fallback = await fetchProfileHomeFallback(biz, author, safeUin, headers);
                if (fallback.articles.length > 0) {
                    articles.push(...fallback.articles);
                    if (progressCb) progressCb(`已快速索引 ${articles.length} 篇文章目录...`, articles.length, maxArticles || articles.length, [...articles]);
                    if (maxArticles > 0 && articles.length >= maxArticles) {
                        articles = articles.slice(0, maxArticles);
                    }
                    // 主页兜底通常只有首屏，结束循环
                    hasMore = false;
                    break;
                }

                // 关键自愈：嗅探通道列表为空但已连通官方通道时，直接自动切换官方接口拉取。
                // 官方 search_biz + appmsg 接口能拉到"发布+群发"全部记录，不受新旧号/客户端版本影响。
                if (wechatAuth.mpConnected && wechatAuth.mpToken && author && author !== "微信公众号") {
                    sendDebugLog(`[自动切换] 嗅探通道历史列表为空，自动切换微信公众平台官方通道拉取【${author}】...`, "info");
                    try {
                        const official = await fetchArticlesViaMpOfficial(author, maxArticles || 0, progressCb);
                        if (official.articles && official.articles.length > 0) {
                            sendDebugLog(`[自动切换成功] 官方通道已拉取【${author}】${official.articles.length} 篇文章！`, "success");
                            return { author: official.author, articles: official.articles, biz: official.biz, lastError: "" };
                        }
                    } catch (mpErr) {
                        sendDebugLog(`[官方通道切换失败] ${mpErr.message}，继续按嗅探通道结果处理...`, "warn");
                    }
                }

                // 修正后的诊断（原"未关注"判断已被实测证伪——用户关注后依旧为空）：
                // 实测证据：截获的主页响应 nickname/is_subscribed/msgList 全为空 = 服务器返回无账号上下文的空壳页。
                // 最常见原因是 2023 年后注册的新公众号使用"发布"模式（无群发权限），
                // 而 profile_ext 旧主页接口的 msgList 只包含"群发"记录，发布模式的文章永远不在此列表中。
                lastErrorText = `嗅探通道确认该公众号历史列表为空（微信返回空壳主页，与是否关注无关）。👉 唯一可行路径：点击顶部【微信官方扫码连接】扫码。注意：扫码账号可以是【任意公众号账号】——不需要是目标号的号主！没有公众号可免费注册一个个人订阅号（身份证+手机号，约5分钟）。扫码成功后工具将自动继续拉取全部历史！`;
                // 链路自检：如果超过 60 秒没有嗅探到任何微信流量，说明微信流量根本没经过工具代理，
                // 最常见原因是"微信比工具先启动"（微信内置浏览器缓存了旧的代理配置）。
                const trafficStale = !wechatAuth.lastTrafficAt || (Date.now() - wechatAuth.lastTrafficAt > 60000);
                if (trafficStale) {
                    lastErrorText += " ⚠️ 检测到微信流量未经过工具代理：请先完全退出电脑微信（任务栏右键→退出），确保本工具保持运行，再重新打开微信！";
                    sendDebugLog(`[链路自检] 超过 60 秒未嗅探到任何微信流量，微信疑似未走工具代理（微信先于工具启动或被安全软件干扰）。`, "warn");
                }
                sendDebugLog(`[全量历史拉取受限] 微信响应 ret=0 且列表为空；主页 HTML 兜底也未解析到文章。`, "warn");
                break;
            }

            articles.push(...parseWechatMsgList(msgList, author, biz));

            if (progressCb) progressCb(`已快速索引 ${articles.length} 篇文章目录...`, articles.length, maxArticles || articles.length, [...articles]);

            if (maxArticles > 0 && articles.length >= maxArticles) {
                articles = articles.slice(0, maxArticles);
                break;
            }

            // 防死循环：只有本页确实拿到新文章，且服务端明确还有更多数据时才翻页
            const addedThisPage = articles.length - articlesBeforePage;
            const hasNextFlag = resData.can_msg_continue == 1 || resData.can_msg_continue === true;
            const hasNextOffset = resData.next_offset !== undefined && resData.next_offset !== null && Number(resData.next_offset) > offset;
            const likelyMore = (msgList && msgList.length >= count) && !(resData.can_msg_continue === 0 || resData.can_msg_continue === false);
            const serverMore = hasNextFlag || hasNextOffset || likelyMore;

            if (addedThisPage > 0 && serverMore) {
                if (hasNextOffset) {
                    offset = Number(resData.next_offset);
                } else {
                    offset += count;
                }
                sendDebugLog(`[多页拉取] 正在自动翻页 (offset=${offset})，已索引 ${articles.length} 篇...`, "info");

                // 1. 主动防风控批次冷却：每连续拉取 100 篇，主动沉睡 3.5 秒，彻底清空服务端滑动突发计数器
                if (articles.length > 0 && articles.length % 100 === 0) {
                    sendDebugLog(`[主动防风控] 已连续拉取 ${articles.length} 篇，工具自动进入 3.5 秒静默安全降温期...`, "info");
                    if (progressCb) progressCb(`已索引 ${articles.length} 篇，工具自动进行防风控安全降温 (3秒)...`, articles.length, maxArticles || 0, [...articles]);
                    await new Promise(r => setTimeout(r, 3500));
                } else {
                    // 拟人化动态安全延迟 (800ms~1300ms)
                    const sleepTime = 800 + Math.floor(Math.random() * 500);
                    await new Promise(r => setTimeout(r, sleepTime));
                }
                rateLimitRetries = 0; // 只要拉取成功，重置限流重试计数器
            } else {
                if (addedThisPage === 0 && serverMore) {
                    sendDebugLog(`[翻页终止] 本页未拿到新文章，防止空转死循环，结束拉取。`, "warn");
                }
                hasMore = false;
            }
        } else if (resData.ret === -3) {
            lastErrorText = "微信会话已过期 (ret=-3, no session)。请在电脑微信中打开任意一篇公众号推文以激活最新会话凭证。";
            break;
        } else if (resData.ret === -6) {
            // 被动自愈机制：绝不中断甩给用户，工具自动逐秒倒计时降温后全自动重新请求
            if (offset > 0 && rateLimitRetries < 5) {
                rateLimitRetries++;
                const backoffSeconds = rateLimitRetries === 1 ? 8 : (rateLimitRetries === 2 ? 15 : (rateLimitRetries === 3 ? 25 : 35));
                sendDebugLog(`[自动自愈冷却] 触发微信频控保护(ret=-6)，工具启动第 ${rateLimitRetries}/5 级静默安全降温，自动倒计时 ${backoffSeconds} 秒后继续...`, "warn");

                for (let s = backoffSeconds; s > 0; s--) {
                    if (progressCb) {
                        progressCb(`触发微信频控保护，工具正在自动静默降温(${s}秒)，倒计时结束后全自动继续，无需手动操作...`, articles.length, maxArticles || 0, [...articles]);
                    }
                    await new Promise(r => setTimeout(r, 1000));
                }
                sendDebugLog(`[自动自愈冷却完成] 静默降温结束，工具全自动重新发起请求...`, "info");
                continue;
            } else {
                lastErrorText = "微信安全频控限制 (ret=-6)。该公众号单次拉取篇数已达微信服务器安全阈值，已为您妥善保留并展示当前所有已拉取的文章。";
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
async function openMpLoginWindow() {
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
            contextIsolation: true,
            // 关键修复：扫码登录窗使用独立会话分区（下方强制直连）。
            // 此前登录窗走系统代理，会穿过工具自己的 MITM 代理(127.0.0.1:8899)，
            // 登录流量被自家代理截获/转发存在干扰风险——这可能是"扫码连不上"的隐患之一。
            partition: "mp-login-direct"
        }
    });

    // 强制扫码窗直连网络，彻底绕开本地代理链路，保证登录环境干净稳定
    await session.fromPartition("mp-login-direct").setProxy({ mode: "direct" });

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

            // 官方通道就绪联动：若此前有检索任务因嗅探通道受限而挂起（pendingAutoFetchTarget），
            // 立即通知渲染层自动重试——用户扫码后无需任何手动操作，工具自动继续拉取全部历史。
            if (pendingAutoFetchTarget && mainWindow && !mainWindow.isDestroyed()) {
                const targetInfo = pendingAutoFetchTarget;
                pendingAutoFetchTarget = null;
                sendDebugLog(`[官方通道就绪] 自动重试对【${targetInfo.author || "公众号"}】的全量历史拉取...`, "info");
                mainWindow.webContents.send("wechat:auto-trigger-search", targetInfo);
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

// =========================================================================
// 知乎官方扫码登录窗口 (Zhihu Official Login Window)
// =========================================================================
let zhihuLoginWindow = null;
function openZhihuLoginWindow() {
    if (zhihuLoginWindow && !zhihuLoginWindow.isDestroyed()) {
        zhihuLoginWindow.focus();
        return;
    }
    zhihuLoginWindow = new BrowserWindow({
        width: 1020,
        height: 760,
        title: "知乎官方快速扫码登录 (zhihu.com)",
        autoHideMenuBar: true,
        webPreferences: {
            nodeIntegration: false,
            contextIsolation: true,
            partition: "persist:zhihu-direct"
        }
    });

    zhihuLoginWindow.loadURL("https://www.zhihu.com/signin");

    let checkInterval = null;
    const pollZhihuCookies = async () => {
        if (!zhihuLoginWindow || zhihuLoginWindow.isDestroyed()) {
            if (checkInterval) clearInterval(checkInterval);
            return;
        }
        try {
            const cookies = await zhihuLoginWindow.webContents.session.cookies.get({ domain: ".zhihu.com" });
            const zc0 = cookies.find(c => c.name === "z_c0");
            if (zc0 && zc0.value) {
                if (checkInterval) clearInterval(checkInterval);
                const cookieDict = {};
                cookies.forEach(c => { cookieDict[c.name] = c.value; });
                const cookieStr = cookies.map(c => `${c.name}=${c.value}`).join("; ");

                // 写入数据持久化文件
                const projectRoot = localPythonManager ? localPythonManager.getProjectRoot() : process.cwd();
                const sessionFileProject = path.join(projectRoot, "data", "zhihu_session.json");
                const sessionFileDataDir = path.join(DATA_DIR, "zhihu_session.json");
                try {
                    fs.mkdirSync(path.dirname(sessionFileProject), { recursive: true });
                    fs.writeFileSync(sessionFileProject, JSON.stringify(cookieDict, null, 2), "utf8");
                } catch(e) {}
                try {
                    fs.writeFileSync(sessionFileDataDir, JSON.stringify(cookieDict, null, 2), "utf8");
                } catch(e) {}

                // 同步至本地 Python 后端
                const port = (localPythonManager && localPythonManager.port) || 8000;
                try {
                    const postData = JSON.stringify({ cookie: cookieStr });
                    const req = http.request({
                        hostname: "127.0.0.1",
                        port: port,
                        path: "/api/zhihu/set-cookie",
                        method: "POST",
                        headers: {
                            "Content-Type": "application/json",
                            "Content-Length": Buffer.byteLength(postData)
                        }
                    });
                    req.on("error", () => {});
                    req.write(postData);
                    req.end();
                } catch(e) {}

                sendDebugLog("[知乎登录] 知乎官方登录成功，登录凭证 (z_c0) 已同步至本地引擎！", "success");
                if (mainWindow && !mainWindow.isDestroyed()) {
                    mainWindow.webContents.send("zhihu:login-success", { success: true });
                }

                setTimeout(() => {
                    if (zhihuLoginWindow && !zhihuLoginWindow.isDestroyed()) {
                        zhihuLoginWindow.close();
                    }
                }, 1200);
            }
        } catch(e) {}
    };

    checkInterval = setInterval(pollZhihuCookies, 1200);
    zhihuLoginWindow.webContents.on("did-navigate", () => pollZhihuCookies());
    zhihuLoginWindow.webContents.on("did-navigate-in-page", () => pollZhihuCookies());
    zhihuLoginWindow.on("closed", () => {
        if (checkInterval) clearInterval(checkInterval);
        zhihuLoginWindow = null;
    });
}

// =========================================================================
// 新浪微博官方扫码登录窗口 (Weibo Official Login Window)
// =========================================================================
let weiboLoginWindow = null;
function openWeiboLoginWindow() {
    if (weiboLoginWindow && !weiboLoginWindow.isDestroyed()) {
        weiboLoginWindow.focus();
        return;
    }
    weiboLoginWindow = new BrowserWindow({
        width: 1020,
        height: 760,
        title: "新浪微博官方快速扫码登录 (weibo.com)",
        autoHideMenuBar: true,
        webPreferences: {
            nodeIntegration: false,
            contextIsolation: true,
            partition: "persist:weibo-direct"
        }
    });

    weiboLoginWindow.loadURL("https://weibo.com/login.php");

    let checkInterval = null;
    const pollWeiboCookies = async () => {
        if (!weiboLoginWindow || weiboLoginWindow.isDestroyed()) {
            if (checkInterval) clearInterval(checkInterval);
            return;
        }
        try {
            const cookies = await weiboLoginWindow.webContents.session.cookies.get({ domain: ".weibo.com" });
            const sub = cookies.find(c => c.name === "SUB");
            if (sub && sub.value) {
                if (checkInterval) clearInterval(checkInterval);
                const cookieDict = {};
                cookies.forEach(c => { cookieDict[c.name] = c.value; });
                const cookieStr = cookies.map(c => `${c.name}=${c.value}`).join("; ");

                // 写入数据持久化文件
                const projectRoot = localPythonManager ? localPythonManager.getProjectRoot() : process.cwd();
                const sessionFileProject = path.join(projectRoot, "data", "weibo_session.json");
                const sessionFileDataDir = path.join(DATA_DIR, "weibo_session.json");
                try {
                    fs.mkdirSync(path.dirname(sessionFileProject), { recursive: true });
                    fs.writeFileSync(sessionFileProject, JSON.stringify(cookieDict, null, 2), "utf8");
                } catch(e) {}
                try {
                    fs.writeFileSync(sessionFileDataDir, JSON.stringify(cookieDict, null, 2), "utf8");
                } catch(e) {}

                // 同步至本地 Python 后端
                const port = (localPythonManager && localPythonManager.port) || 8000;
                try {
                    const postData = JSON.stringify({ cookies: cookieDict, cookie: cookieStr });
                    const req = http.request({
                        hostname: "127.0.0.1",
                        port: port,
                        path: "/api/weibo/set-cookie",
                        method: "POST",
                        headers: {
                            "Content-Type": "application/json",
                            "Content-Length": Buffer.byteLength(postData)
                        }
                    });
                    req.on("error", () => {});
                    req.write(postData);
                    req.end();
                } catch(e) {}

                sendDebugLog("[微博登录] 微博官方登录成功，登录凭证 (SUB) 已同步至本地引擎！", "success");
                if (mainWindow && !mainWindow.isDestroyed()) {
                    mainWindow.webContents.send("weibo:login-success", { success: true });
                }

                setTimeout(() => {
                    if (weiboLoginWindow && !weiboLoginWindow.isDestroyed()) {
                        weiboLoginWindow.close();
                    }
                }, 1200);
            }
        } catch(e) {}
    };

    checkInterval = setInterval(pollWeiboCookies, 1200);
    weiboLoginWindow.webContents.on("did-navigate", () => pollWeiboCookies());
    weiboLoginWindow.webContents.on("did-navigate-in-page", () => pollWeiboCookies());
    weiboLoginWindow.on("closed", () => {
        if (checkInterval) clearInterval(checkInterval);
        weiboLoginWindow = null;
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

    // 官方接口统一 GET + JSON 解析小工具（search_biz 与 appmsg 共用）
    const mpGetJson = (url) => new Promise((resolve, reject) => {
        https.get(url, { headers }, (res) => {
            let data = "";
            res.on("data", c => data += c);
            res.on("end", () => {
                try { resolve(JSON.parse(data)); } catch(e) { resolve({ base_resp: { ret: -1 } }); }
            });
        }).on("error", reject);
    });

    // 频控统一处理：返回 true 表示已冷却应重试，抛错表示不可恢复
    const COOLDOWN_RETS = [200013, 200011, 200012]; // 微信频控相关返回码
    const handleRetOrThrow = async (ret, label, attempt) => {
        if (COOLDOWN_RETS.includes(ret)) {
            if (attempt >= 4) {
                throw new Error(`官方接口连续 4 次触发频控（ret=${ret}）。\n\n当前扫码账号权限较低（新注册/未认证个人号额度最小）。建议：① 等待 10 分钟后重试；② 换用已认证的公众号账号扫码，额度更大。`);
            }
            const waitSec = 20 * attempt; // 指数退避：20s → 40s → 60s → 80s
            sendDebugLog(`[官方直连频控] ${label} 触发频控(ret=${ret})，第 ${attempt}/4 次自动冷却 ${waitSec} 秒后重试...`, "warn");
            for (let s = waitSec; s > 0; s--) {
                if (progressCb) progressCb(`官方接口限流，自动冷却降温中(${s}秒)，第 ${attempt}/4 次重试...`, 0, 0);
                await new Promise(r => setTimeout(r, 1000));
            }
            return true; // 应重试
        }
        if (ret === -6 || ret === 200002) {
            // 登录态失效：清空官方通道状态并广播，让界面回到"未连接"
            wechatAuth.mpConnected = false;
            wechatAuth.mpToken = "";
            if (mainWindow && !mainWindow.isDestroyed()) {
                mainWindow.webContents.send("wechat:mp-status-change", { connected: false });
            }
            throw new Error("公众平台登录状态已过期（ret=-6），请点击顶部【微信官方扫码连接】重新扫码！");
        }
        return false;
    };

    // 1. 搜索目标公众号获取 fakeid 与全称
    //    关键修复：search_biz 此前完全没做频控处理——一旦被限流就误报"搜索不到公众号"，
    //    这正是当初官方通道被判"不能用"而弃用的根本原因。现在加入指数退避重试。
    const searchUrl = `https://mp.weixin.qq.com/cgi-bin/searchbiz?action=search_biz&begin=0&count=5&query=${encodeURIComponent(targetName)}&token=${token}&lang=zh_CN&f=json&ajax=1`;
    const headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Cookie": cookie,
        "Referer": `https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit_v2&action=edit&isNew=1&type=77&createType=0&token=${token}&lang=zh_CN`
    };

    let searchRes = null;
    for (let attempt = 1; attempt <= 5; attempt++) {
        searchRes = await mpGetJson(searchUrl);
        const ret = (searchRes.base_resp && searchRes.base_resp.ret) || 0;
        if (await handleRetOrThrow(ret, "搜索接口", attempt)) continue; // 频控冷却后重试
        break; // 正常返回（含业务层空结果），跳出
    }

    const bizList = searchRes.list || [];
    if (bizList.length === 0) {
        throw new Error(`未在微信平台搜索到公众号【${targetName}】，请核对公众号名称（若刚扫码成功，请等 1 分钟后再试，新号搜索接口有延迟）！`);
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

        // 单页请求 + 统一频控退避（替代旧版"15秒无限冷却"——旧版频控持续时会死循环）
        let listRes = null;
        let pageAttempt = 1;
        while (true) {
            listRes = await mpGetJson(listUrl);
            const ret = (listRes.base_resp && listRes.base_resp.ret) || 0;
            if (ret === 0) break; // 本页正常
            if (await handleRetOrThrow(ret, "文章列表接口", pageAttempt)) { pageAttempt++; continue; }
            // 其他未知错误码：不再硬性中断整个任务，保留已拉到的文章结算
            sendDebugLog(`[官方直连] 文章列表接口返回异常(ret=${ret})，停止翻页，以已获取的 ${articles.length} 篇结算。`, "warn");
            break;
        }
        if (listRes.base_resp && listRes.base_resp.ret !== 0) break; // 异常页：跳出翻页循环，走结算

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

        // 拟人化动态随机延迟与批次降温
        if (articles.length > 0 && articles.length % 80 === 0) {
            await new Promise(r => setTimeout(r, 2500));
        } else {
            await new Promise(r => setTimeout(r, 650 + Math.floor(Math.random() * 350)));
        }
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

        pdfWin.webContents.on("render-process-gone", (_event, details) => {
            if (!isDone) {
                isDone = true;
                clearTimeout(timer);
                cleanup();
                reject(new Error(`PDF 渲染进程异常退出 (${details && details.reason ? details.reason : '内存超限'})`));
            }
        });

        pdfWin.webContents.on("crashed", () => {
            if (!isDone) {
                isDone = true;
                clearTimeout(timer);
                cleanup();
                reject(new Error("PDF 渲染内核崩溃 (可能超出系统内存限制)"));
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
function buildPremiumHtmlDocument(author, fullArticles, isPdf = false, volumeSubTitle = "") {
    const nowStr = new Date().toLocaleString();
    const volSuffix = volumeSubTitle ? ` - ${escapeHtml(volumeSubTitle)}` : "";
    const coverTitle = (author.includes("公众号") ? `【${escapeHtml(author)}文章合集】` : `【${escapeHtml(author)}公众号合集】`) + volSuffix;
    const coverSub = `共 ${fullArticles.length} 篇文章 ${volumeSubTitle ? '· ' + escapeHtml(volumeSubTitle) : ''} · 微信公众号`;

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

// 本地下载微信图片助手 (支持高清原图 / 智能缩减图，带防盗链 Referer、自动重定向与 3 次重试支持)
async function downloadImageLocal(imgUrl, saveFilePath, quality = "reduced") {
    if (!imgUrl || !imgUrl.startsWith("http")) return false;
    if (fs.existsSync(saveFilePath) && fs.statSync(saveFilePath).size > 200) return true;

    // 针对微信 CDN 调整 URL 请求
    let targetUrl = imgUrl;
    if (quality === "original") {
        // 高清原图：替换 /640? 或 /300? 为 /0? 获取无损原始母图
        targetUrl = targetUrl.replace(/\/640\?/, "/0?").replace(/\/300\?/, "/0?").replace(/\/200\?/, "/0?");
    } else {
        // 缩减优化图：确保请求轻量压缩版本 /640?，避免拉取数MB大图
        targetUrl = targetUrl.replace(/\/0\?/, "/640?");
    }

    for (let retry = 0; retry < 3; retry++) {
        try {
            const success = await new Promise((resolve) => {
                const parsed = new URL(targetUrl);
                const client = parsed.protocol === "https:" ? https : http;
                const req = client.get(targetUrl, {
                    headers: {
                        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 MicroMessenger/7.0.20",
                        "referer": "https://mp.weixin.qq.com/",
                        "accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
                    },
                    timeout: 12000
                }, (res) => {
                    // 处理 301/302 重定向
                    if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
                        return downloadImageLocal(res.headers.location, saveFilePath, quality).then(resolve);
                    }
                    if (res.statusCode >= 200 && res.statusCode < 300) {
                        const chunks = [];
                        res.on("data", chunk => chunks.push(chunk));
                        res.on("end", () => {
                            try {
                                const buf = Buffer.concat(chunks);
                                if (buf.length > 100) {
                                    // 缩减图模式下，通过 nativeImage 智能降维与轻量压缩 (节省70%+空间，极大防止内存溢出)
                                    if (quality === "reduced") {
                                        try {
                                            const img = nativeImage.createFromBuffer(buf);
                                            if (!img.isEmpty()) {
                                                const size = img.getSize();
                                                let processed = img;
                                                if (size.width > 1080) {
                                                    processed = img.resize({ width: 1080, quality: "better" });
                                                }
                                                const compBuf = saveFilePath.endsWith(".png") ? processed.toPNG() : processed.toJPEG(75);
                                                if (compBuf && compBuf.length > 0 && compBuf.length < buf.length) {
                                                    fs.writeFileSync(saveFilePath, compBuf);
                                                    return resolve(true);
                                                }
                                            }
                                        } catch(e) {}
                                    }
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

// 生成单份排版级 Word (.docx) 文档助手 (支持单本或按卷导出)
async function generateSingleDocxFile(docxPath, author, articlesList, saveDir, volumeTag = "") {
    const volSuffix = volumeTag ? ` - ${volumeTag}` : "";
    const children = [
        new docx.Paragraph({
            text: `【${author}】微信公众号文章合集${volSuffix}`,
            heading: docx.HeadingLevel.TITLE,
            spacing: { before: 200, after: 120 }
        }),
        new docx.Paragraph({
            text: `文章总数：共计 ${articlesList.length} 篇   |   整理排版：微信公众号【艺杯羹】   |   导出时间：${new Date().toLocaleString()}`,
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

    for (let i = 0; i < articlesList.length; i++) {
        const art = articlesList[i];
        
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
                    } catch(e) {}
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
        if (i < articlesList.length - 1) {
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
    return docxPath;
}

async function exportArticlesLocal(exportOptions, progressCb, statusCb) {
    const { articles, author, formats, outputDir, biz, imageQuality = "reduced" } = exportOptions;
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

    // ------------------------------------------
    // 快速路径：仅导出链接清单
    // ------------------------------------------
    // 当用户只选择 links 格式时，没有必要逐篇抓取正文、下载配图，
    // 直接根据已经拿到的文章元数据生成链接文件即可，上千篇也能秒出。
    if (formats.length === 1 && formats[0] === "links") {
        const linksPrefix = `【${author}】文章链接清单`;

        // 文件 1：带标题、发布时间的完整清单，方便人工核对
        const detailLines = [
            "序号\t标题\t发布时间\t原文链接",
            ...articles.map((a, i) => `${i + 1}\t${a.title || ""}\t${a.create_time || ""}\t${a.url || ""}`)
        ];
        const detailPath = path.join(saveDir, `${linksPrefix}.txt`);
        fs.writeFileSync(detailPath, detailLines.join("\n"), "utf8");

        // 文件 2：仅 URL，一行一个，方便交给下载器/脚本批量处理
        const urlOnlyPath = path.join(saveDir, `${linksPrefix}_仅链接.txt`);
        fs.writeFileSync(urlOnlyPath, articles.map(a => a.url || "").filter(Boolean).join("\n"), "utf8");

        sendDebugLog(`[链接清单] 已为 ${articles.length} 篇文章生成链接文件：${path.basename(detailPath)}`, "success");
        return { success: true, savedFiles: [detailPath, urlOnlyPath], saveDir };
    }

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
            
            // 拟人化动态随机延迟 (180ms~380ms) + 40 篇批次安全冷却缓冲，防止触发微信单 IP 频率风控
            let crawlDelay = 180 + Math.floor(Math.random() * 200);
            if (i > 0 && i % 40 === 0) {
                sendDebugLog(`[防频控保护] 已连续抓取 40 篇正文，进入 2 秒拟人化安全冷却缓冲...`, "info");
                crawlDelay = 2000;
            }
            await new Promise(r => setTimeout(r, crawlDelay));
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
        const CONCURRENT = 5;
        for (let b = 0; b < imageDownloadQueue.length; b += CONCURRENT) {
            const batch = imageDownloadQueue.slice(b, b + CONCURRENT);
            if (progressCb) {
                progressCb(`正在下载离线配图 (${b + 1}/${imageDownloadQueue.length})...`, b + 1, imageDownloadQueue.length);
            }
            await Promise.all(batch.map(item => downloadImageLocal(item.rawUrl, item.imgLocalPath, imageQuality)));
            await new Promise(r => setTimeout(r, 40));
        }
        sendDebugLog(`[配图下载] 全部 ${imageDownloadQueue.length} 张离线配图下载处理完成！`, "success");

        // 统一替换文章正文中的图片为本地相对路径 (兼顾 HTML & Markdown，处理特殊转义)
        for (const item of imageDownloadQueue) {
            const art = fullArticles[item.artIndex];
            const relRoot = `./images/${item.imgFileName}`;
            const relSingle = `../images/${item.imgFileName}`;
            const rawUrl = item.rawUrl;
            const decodedUrl = rawUrl.replaceAll("&amp;", "&");
            const encodedUrl = rawUrl.replaceAll("&", "&amp;");

            const urlVariants = Array.from(new Set([rawUrl, decodedUrl, encodedUrl]));
            for (const u of urlVariants) {
                art.content_html = art.content_html.replaceAll(u, relRoot);
                art.content_markdown = art.content_markdown.replaceAll(u, relRoot);
                art.single_markdown = art.single_markdown.replaceAll(u, relSingle);
            }
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

    // 3. PDF 导出 (基于内置 Chromium 矢量排版引擎，支持大批量智能分卷防卡死)
    if (formats.includes("pdf")) {
        try {
            const CHUNK_SIZE = 100;
            if (fullArticles.length > 80) {
                const totalVolumes = Math.ceil(fullArticles.length / CHUNK_SIZE);
                sendDebugLog(`[PDF导出] 当前篇数较多(${fullArticles.length}篇)，已启动智能分卷生成（共 ${totalVolumes} 卷，每卷约 ${CHUNK_SIZE} 篇），彻底防止 Chromium 渲染内存溢出与整机卡死`, "info");
                for (let v = 0; v < totalVolumes; v++) {
                    const startIdx = v * CHUNK_SIZE;
                    const endIdx = Math.min(startIdx + CHUNK_SIZE, fullArticles.length);
                    const chunkArticles = fullArticles.slice(startIdx, endIdx);
                    const volNum = String(v + 1).padStart(2, '0');
                    const volTag = `第${volNum}卷 (${startIdx + 1}-${endIdx}篇)`;
                    const volName = `【${author}】文章合集_${volTag}.pdf`;
                    const pdfPath = path.join(saveDir, volName);
                    
                    if (progressCb) progressCb(`正在生成 PDF ${volTag} (${v + 1}/${totalVolumes} 卷)...`, v + 1, totalVolumes);
                    try {
                        const pdfHtmlContent = buildPremiumHtmlDocument(author, chunkArticles, true, volTag);
                        await generatePdfFromHtml(pdfHtmlContent, pdfPath);
                        results.push(pdfPath);
                        sendDebugLog(`[文件生成] PDF ${volTag} 已生成: ${volName}`, "success");
                    } catch(volErr) {
                        console.error(`PDF ${volTag} 生成异常:`, volErr);
                        sendDebugLog(`[PDF分卷异常] ${volTag} 生成异常: ${volErr.message}，已自动跳过并继续`, "error");
                    }
                }
            } else {
                if (progressCb) progressCb(`正在生成高清矢量 PDF 文档...`, fullArticles.length, fullArticles.length);
                const pdfPath = path.join(saveDir, `${prefix}.pdf`);
                const pdfHtmlContent = buildPremiumHtmlDocument(author, fullArticles, true);
                await generatePdfFromHtml(pdfHtmlContent, pdfPath);
                results.push(pdfPath);
                sendDebugLog(`[文件生成] PDF 高清矢量电子书已生成: ${path.basename(pdfPath)}`, "success");
            }
        } catch(e) {
            console.error("PDF export error:", e);
            sendDebugLog(`[文件生成失败] PDF 生成异常: ${e.message}`, "error");
        }
    }

    // 4. Word (.docx) 导出 (真图内嵌、出版级字阶排版，支持大批量智能分卷防内存溢出)
    if (formats.includes("docx")) {
        try {
            const CHUNK_SIZE = 100;
            if (fullArticles.length > 80) {
                const totalVolumes = Math.ceil(fullArticles.length / CHUNK_SIZE);
                sendDebugLog(`[Word导出] 当前篇数较多(${fullArticles.length}篇)，已启动智能分卷生成（共 ${totalVolumes} 卷，每卷约 ${CHUNK_SIZE} 篇），防止内存溢出`, "info");
                for (let v = 0; v < totalVolumes; v++) {
                    const startIdx = v * CHUNK_SIZE;
                    const endIdx = Math.min(startIdx + CHUNK_SIZE, fullArticles.length);
                    const chunkArticles = fullArticles.slice(startIdx, endIdx);
                    const volNum = String(v + 1).padStart(2, '0');
                    const volTag = `第${volNum}卷 (${startIdx + 1}-${endIdx}篇)`;
                    const volName = `【${author}】文章合集_${volTag}.docx`;
                    const docxPath = path.join(saveDir, volName);

                    if (progressCb) progressCb(`正在生成 Word ${volTag} (${v + 1}/${totalVolumes} 卷)...`, v + 1, totalVolumes);
                    try {
                        await generateSingleDocxFile(docxPath, author, chunkArticles, saveDir, volTag);
                        results.push(docxPath);
                        sendDebugLog(`[文件生成] Word ${volTag} 已生成: ${volName}`, "success");
                    } catch(volErr) {
                        console.error(`Word ${volTag} 生成异常:`, volErr);
                        sendDebugLog(`[Word分卷异常] ${volTag} 生成异常: ${volErr.message}，已自动跳过并继续`, "error");
                    }
                }
            } else {
                if (progressCb) progressCb(`正在生成排版级 Word 文档...`, fullArticles.length, fullArticles.length);
                const docxPath = path.join(saveDir, `${prefix}.docx`);
                await generateSingleDocxFile(docxPath, author, fullArticles, saveDir);
                results.push(docxPath);
                sendDebugLog(`[文件生成] Word 文档已生成 (真图高清内嵌): ${path.basename(docxPath)}`, "success");
            }
        } catch (e) {
            console.error("DOCX export error:", e);
            sendDebugLog(`[文件生成失败] Word 生成异常: ${e.message}`, "error");
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
// 6. 本地 Python 引擎生命周期管理 (LocalPythonManager)
// =========================================================================
class LocalPythonManager {
    constructor(electronApp) {
        this.app = electronApp;
        this.child = null;
        this.port = 8000;
        this.running = false;
        this.lastError = null;
        this.stdoutLogs = [];
    }

    async findFreePort(startPort = 8000, maxPort = 8050) {
        return new Promise((resolve) => {
            let currentPort = startPort;
            const tryPort = () => {
                if (currentPort > maxPort) {
                    const server = net.createServer();
                    server.listen(0, "127.0.0.1", () => {
                        const assigned = server.address().port;
                        server.close(() => resolve(assigned));
                    });
                    return;
                }
                const server = net.createServer();
                server.once("error", (err) => {
                    currentPort++;
                    tryPort();
                });
                server.once("listening", () => {
                    server.close(() => resolve(currentPort));
                });
                server.listen(currentPort, "127.0.0.1");
            };
            tryPort();
        });
    }

    getProjectRoot() {
        if (this.app && this.app.isPackaged) {
            if (fs.existsSync(path.join(process.resourcesPath, "run.py"))) {
                return process.resourcesPath;
            }
            if (fs.existsSync(path.join(process.resourcesPath, "app", "run.py"))) {
                return path.join(process.resourcesPath, "app");
            }
        }
        const devRoot = path.resolve(__dirname, "..");
        if (fs.existsSync(path.join(devRoot, "run.py"))) {
            return devRoot;
        }
        return process.cwd();
    }

    // 解析可用的 Python 解释器，返回 { cmd, prefix }：
    // - prefix 用于 py 启动器（spawn 时需带上 ["-3"] 参数）
    // - 全部候选都不可用时返回 null，由上层决定是否自动部署内核
    resolvePython() {
        const root = this.getProjectRoot();
        const candidates = [
            path.join(process.resourcesPath, "python", "python.exe"),
            path.join(process.resourcesPath, "python", "bin", "python"),
            path.join(root, ".venv", "Scripts", "python.exe"),
            path.join(root, "venv", "Scripts", "python.exe"),
            path.join(root, ".venv", "bin", "python"),
            path.join(__dirname, "..", ".venv", "Scripts", "python.exe"),
            path.join(process.cwd(), ".venv", "Scripts", "python.exe"),
            path.join(__dirname, ".venv", "Scripts", "python.exe")
        ];
        for (const c of candidates) {
            if (path.isAbsolute(c) && fs.existsSync(c)) {
                return { cmd: c, prefix: [] };
            }
        }
        // 兜底一：Windows 官方 py 启动器（装过官方 Python 的电脑基本都有，不依赖 PATH 里的 python.exe）
        if (process.platform === "win32") {
            try {
                const r = spawnSync("py", ["-3", "--version"], { encoding: "utf8", timeout: 8000, windowsHide: true });
                if (!r.error) return { cmd: "py", prefix: ["-3"] };
            } catch (_) {}
        }
        // 兜底二：PATH 里的 python/python3
        try {
            const name = process.platform === "win32" ? "python.exe" : "python3";
            const r = spawnSync(name, ["--version"], { encoding: "utf8", timeout: 8000, windowsHide: true });
            if (!r.error) return { cmd: name, prefix: [] };
        } catch (_) {}
        return null;
    }

    // 统一的命令执行封装：超时自动杀进程，永不抛异常（失败以 ok:false 表达）
    runCmd(cmd, args, timeoutMs = 120000) {
        return new Promise((resolve) => {
            execFile(cmd, args, { timeout: timeoutMs, windowsHide: true, encoding: "utf8" }, (err, stdout, stderr) => {
                resolve({ ok: !err, stdout: String(stdout || ""), stderr: String(stderr || "") });
            });
        });
    }

    // 依赖体检：快速探测该解释器是否能 import 后端全部核心依赖（30 秒内完成）
    async checkDeps(cmd, prefix) {
        const probe = "import fastapi,uvicorn,httpx,bs4,markdownify,lxml,docx,jinja2,pydantic,PIL";
        const r = await this.runCmd(cmd, [...prefix, "-c", probe], 30000);
        return r.ok;
    }

    // 首次运行内核自动部署：在用户目录创建独立 venv 并安装后端依赖（与 start.bat 本地包逻辑一致）
    // 成功返回可用的解释器 { cmd, prefix }，失败返回 null
    async ensureKernel(cmd, prefix, projectRoot) {
        const baseDir = path.join(process.env.LOCALAPPDATA || app.getPath("userData"), "BlogDistillerKernel");
        const venvDir = path.join(baseDir, "venv");
        const isWin = process.platform === "win32";
        const venvPy = isWin ? path.join(venvDir, "Scripts", "python.exe") : path.join(venvDir, "bin", "python");
        const reqFile = path.join(projectRoot, "requirements.txt");

        // 已有内核缓存则直接校验复用（二次启动秒过）
        if (fs.existsSync(venvPy)) {
            setBootStatus("检测到本地内核缓存，正在校验依赖…");
            if (await this.checkDeps(venvPy, [])) {
                console.log("[LocalPython] 复用已缓存的本地内核:", venvPy);
                return { cmd: venvPy, prefix: [] };
            }
            // 缓存损坏（装了一半断电/断网）则推倒重建
            try { fs.rmSync(venvDir, { recursive: true, force: true }); } catch (_) {}
        }

        setBootStatus("正在创建独立运行环境 (venv)…");
        const mk = await this.runCmd(cmd, [...prefix, "-m", "venv", venvDir], 180000);
        if (!mk.ok || !fs.existsSync(venvPy)) {
            console.error("[LocalPython] 创建 venv 失败:", mk.stderr || mk.err);
            return null;
        }

        if (!fs.existsSync(reqFile)) {
            console.error("[LocalPython] 未找到依赖清单 requirements.txt:", reqFile);
            return null;
        }
        setBootStatus("正在安装核心依赖（约 2~5 分钟，视网速而定）…");
        let pip = await this.runCmd(venvPy, ["-m", "pip", "install", "-r", reqFile, "--disable-pip-version-check", "-q"], 600000);
        if (!pip.ok) {
            setBootStatus("默认源安装较慢或失败，切换国内镜像源重试…");
            pip = await this.runCmd(venvPy, ["-m", "pip", "install", "-r", reqFile, "--disable-pip-version-check", "-q", "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"], 600000);
        }
        if (!pip.ok) {
            console.error("[LocalPython] 依赖安装失败:", pip.stderr);
            return null;
        }

        // Playwright 渲染内核（知乎专栏等动态页面需要）放到服务就绪后后台安装，不阻塞进工作台
        this.needPlaywrightSetup = true;
        return { cmd: venvPy, prefix: [] };
    }

    // 判断是否为我们自下载的便携内嵌内核（目录特征：BlogDistillerKernel/python）
    isEmbeddedKernel(cmd) {
        return cmd.includes(path.join("BlogDistillerKernel", "python"));
    }

    // 带进度地下载文件，返回 Buffer；失败抛异常由调用方兜底
    async downloadFile(url) {
        const resp = await fetch(url, { redirect: "follow" });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const total = Number(resp.headers.get("content-length") || 0);
        const reader = resp.body.getReader();
        const chunks = [];
        let got = 0;
        let lastPct = -1;
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            chunks.push(value);
            got += value.length;
            if (total) {
                const pct = Math.floor((got / total) * 100);
                if (pct >= lastPct + 10) {
                    lastPct = pct;
                    setBootStatus(`下载进度 ${pct}%（${(got / 1024 / 1024).toFixed(1)} MB）…`);
                }
            }
        }
        return Buffer.concat(chunks);
    }

    // 裸机兜底：电脑上完全没有 Python 时，自动下载官方便携内嵌版 Python 并启用 pip
    // - Windows 7/8（内核版本 6.x）只能用 Python 3.8.10；Windows 10/11 用 3.12.6
    // - 优先国内镜像（华为云/npmmirror），python.org 官方兜底
    async ensureEmbeddedPython() {
        if (process.platform !== "win32") return null;
        const baseDir = path.join(process.env.LOCALAPPDATA || app.getPath("userData"), "BlogDistillerKernel");
        const pyDir = path.join(baseDir, "python");
        const pyExe = path.join(pyDir, "python.exe");
        const legacy = parseInt(os.release().split(".")[0], 10) < 10; // 6.x = Win7/8/8.1
        const pyVer = legacy ? "3.8.10" : "3.12.6";

        // 已有内核缓存则校验复用（二次启动秒过）；校验不过则清掉重下
        if (fs.existsSync(pyExe)) {
            setBootStatus("检测到已下载的 Python 内核，正在校验…");
            if (await this.checkDeps(pyExe, [])) {
                console.log("[LocalPython] 复用已下载的内嵌 Python 内核:", pyExe);
                return { cmd: pyExe, prefix: [] };
            }
            try { fs.rmSync(pyDir, { recursive: true, force: true }); } catch (_) {}
        }

        const zipName = `python-${pyVer}-embed-amd64.zip`;
        const mirrors = [
            `https://mirrors.huaweicloud.com/python/${pyVer}/${zipName}`,
            `https://registry.npmmirror.com/-/binary/python/${pyVer}/${zipName}`,
            `https://www.python.org/ftp/python/${pyVer}/${zipName}`
        ];
        let zipBuf = null;
        for (let i = 0; i < mirrors.length; i++) {
            try {
                setBootStatus(`正在下载便携 Python 内核 ${pyVer}（约 12MB）— 线路 ${i + 1}/${mirrors.length}…`);
                zipBuf = await this.downloadFile(mirrors[i]);
                if (zipBuf && zipBuf.length > 1000000) break; // 简单体积校验，防止拿到错误页
                zipBuf = null;
            } catch (e) {
                console.warn(`[LocalPython] 内核下载线路 ${i + 1} 失败:`, e.message);
                zipBuf = null;
            }
        }
        if (!zipBuf) return null;

        setBootStatus("正在解压 Python 内核…");
        try {
            const files = unzipSync(zipBuf);
            fs.mkdirSync(pyDir, { recursive: true });
            for (const [name, content] of Object.entries(files)) {
                const target = path.join(pyDir, name);
                if (name.endsWith("/") || name.endsWith("\\")) {
                    fs.mkdirSync(target, { recursive: true });
                    continue;
                }
                fs.mkdirSync(path.dirname(target), { recursive: true });
                fs.writeFileSync(target, Buffer.from(content));
            }
        } catch (e) {
            console.error("[LocalPython] 解压 Python 内核失败:", e);
            return null;
        }

        // 关键：内嵌版默认不加载 site-packages，必须重写 ._pth 才能让 pip 装的依赖被 import
        try {
            const pthName = legacy ? "python38._pth" : "python312._pth";
            fs.writeFileSync(path.join(pyDir, pthName), `python${legacy ? "38" : "312"}.zip\n.\nLib/site-packages\nimport site\n`);
        } catch (e) {
            console.error("[LocalPython] 写入 ._pth 失败:", e);
            return null;
        }

        // 安装 pip（3.8 需用官方保留的 legacy 引导脚本）
        setBootStatus("正在初始化包管理器 (pip)…");
        const getpipUrl = legacy ? "https://bootstrap.pypa.io/pip/3.8/get-pip.py" : "https://bootstrap.pypa.io/get-pip.py";
        try {
            const getpip = await this.downloadFile(getpipUrl);
            const getpipPath = path.join(baseDir, "get-pip.py");
            fs.writeFileSync(getpipPath, getpip);
            const gp = await this.runCmd(pyExe, [getpipPath, "--no-warn-script-location", "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"], 300000);
            if (!gp.ok) {
                console.error("[LocalPython] pip 初始化失败:", gp.stderr);
                return null;
            }
        } catch (e) {
            console.error("[LocalPython] 下载 get-pip 失败:", e.message);
            return null;
        }

        return { cmd: pyExe, prefix: [] };
    }

    // 给内嵌 Python 安装后端依赖：内嵌版不支持 venv，直接 pip --target 装进它自己的 site-packages
    async installDepsIntoEmbedded(pyExe, projectRoot) {
        const reqFile = path.join(projectRoot, "requirements.txt");
        if (!fs.existsSync(reqFile)) {
            console.error("[LocalPython] 未找到依赖清单 requirements.txt:", reqFile);
            return null;
        }
        const siteDir = path.join(path.dirname(pyExe), "Lib", "site-packages");
        setBootStatus("正在安装核心依赖（约 3~8 分钟，视网速而定）…");
        let r = await this.runCmd(pyExe, ["-m", "pip", "install", "--target", siteDir, "-r", reqFile, "--disable-pip-version-check", "-q"], 900000);
        if (!r.ok) {
            setBootStatus("默认源安装较慢或失败，切换国内镜像源重试…");
            r = await this.runCmd(pyExe, ["-m", "pip", "install", "--target", siteDir, "-r", reqFile, "--disable-pip-version-check", "-q", "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"], 900000);
        }
        if (!r.ok) {
            console.error("[LocalPython] 依赖安装失败:", r.stderr);
            return null;
        }
        // Playwright 渲染内核放到服务就绪后后台安装
        this.needPlaywrightSetup = true;
        return { cmd: pyExe, prefix: [] };
    }

    getRunScriptPath() {
        const root = this.getProjectRoot();
        return path.join(root, "run.py");
    }

    checkHealth(port, timeoutMs = 1500) {
        return new Promise((resolve) => {
            const req = http.get(`http://127.0.0.1:${port}/api/health`, { timeout: timeoutMs }, (res) => {
                if (res.statusCode === 200) {
                    resolve(true);
                } else {
                    resolve(false);
                }
            });
            req.on("error", () => resolve(false));
            req.on("timeout", () => {
                req.destroy();
                resolve(false);
            });
        });
    }

    async waitForReady(port, maxAttempts = 30, intervalMs = 300) {
        for (let i = 0; i < maxAttempts; i++) {
            const ok = await this.checkHealth(port);
            if (ok) return true;
            await new Promise(r => setTimeout(r, intervalMs));
        }
        return false;
    }

    async start() {
        if (this.running && this.child) {
            const ok = await this.checkHealth(this.port);
            if (ok) {
                return this.getStatus();
            }
        }

        const port = await this.findFreePort(8000, 8050);
        this.port = port;
        const projectRoot = this.getProjectRoot();
        const runScript = this.getRunScriptPath();

        console.log(`[LocalPython] 正在启动本地 Python 服务...`);
        console.log(`[LocalPython] 项目根目录: ${projectRoot}`);
        console.log(`[LocalPython] 启动脚本: ${runScript}, 监听端口: ${port}`);

        if (!fs.existsSync(runScript)) {
            const err = `未找到 run.py 启动脚本: ${runScript}`;
            console.error(`[LocalPython] ${err}`);
            this.lastError = err;
            return this.getStatus();
        }

        try {
            // ===== Python 解释器解析 + 首次运行内核自动部署 =====
            setBootStatus("正在检测本地 Python 运行环境…");
            const resolved = this.resolvePython();
            let pythonCmd = null;
            let pyPrefix = [];

            if (resolved) {
                console.log(`[LocalPython] Python 解释器: ${resolved.cmd} ${resolved.prefix.join(" ")}`);
                pythonCmd = resolved.cmd;
                pyPrefix = resolved.prefix;
            } else {
                // 裸机兜底：完全没有 Python → 自动下载官方便携内嵌版（Win7 用 3.8.10，Win10/11 用 3.12.6）
                setBootStatus("未检测到 Python，正在自动下载便携 Python 内核（约 12MB）…");
                const emb = await this.ensureEmbeddedPython();
                if (!emb) {
                    const err = "Python 内核自动下载失败，请检查网络后重启应用，或到 python.org 手动安装 Python";
                    console.error(`[LocalPython] ${err}`);
                    this.lastError = err;
                    setBootStatus("❌ " + err);
                    return this.getStatus();
                }
                pythonCmd = emb.cmd;
                pyPrefix = emb.prefix;
            }

            // 依赖体检不通过 → 自动安装依赖：
            // - 内嵌内核：pip --target 直接装进其 site-packages（内嵌版不支持 venv）
            // - 系统 Python：创建独立 venv 内核安装（与源码包 start.bat 行为对齐）
            if (!(await this.checkDeps(pythonCmd, pyPrefix))) {
                setBootStatus("首次运行：正在自动部署本地 Python 内核，请保持网络畅通…");
                let kernel = null;
                if (this.isEmbeddedKernel(pythonCmd)) {
                    kernel = await this.installDepsIntoEmbedded(pythonCmd, projectRoot);
                } else {
                    kernel = await this.ensureKernel(pythonCmd, pyPrefix, projectRoot);
                }
                if (!kernel) {
                    const err = "本地 Python 内核自动部署失败，请检查网络后重启应用重试";
                    console.error(`[LocalPython] ${err}`);
                    this.lastError = err;
                    setBootStatus("❌ " + err);
                    return this.getStatus();
                }
                pythonCmd = kernel.cmd;
                pyPrefix = kernel.prefix;
            }
            // ===== 内核部署逻辑结束 =====

            const args = [...pyPrefix, runScript, "--port", String(port), "--host", "127.0.0.1", "--no-reload"];
            const childEnv = {
                ...process.env,
                PYTHONUNBUFFERED: "1"
            };

            this.child = spawn(pythonCmd, args, {
                cwd: projectRoot,
                stdio: ["ignore", "pipe", "pipe"],
                windowsHide: true,
                env: childEnv
            });

            this.child.stdout.on("data", (data) => {
                const text = data.toString();
                this.stdoutLogs.push(text);
                if (this.stdoutLogs.length > 200) this.stdoutLogs.shift();
                process.stdout.write(`[LocalPython STDOUT] ${text}`);
            });

            this.child.stderr.on("data", (data) => {
                const text = data.toString();
                process.stderr.write(`[LocalPython STDERR] ${text}`);
            });

            this.child.on("error", (err) => {
                console.error(`[LocalPython] 进程发生错误:`, err);
                this.lastError = err.message;
                this.running = false;
            });

            this.child.on("exit", (code, signal) => {
                console.log(`[LocalPython] 进程退出 code=${code}, signal=${signal}`);
                this.running = false;
                this.child = null;
            });

            // 等待健康检查响应
            console.log(`[LocalPython] 正在等待端口 ${port} 服务就绪...`);
            const ready = await this.waitForReady(port);
            if (ready) {
                console.log(`[LocalPython] 本地 Python 服务就绪! PID: ${this.child.pid}, Port: ${port}`);
                this.running = true;
                this.lastError = null;
                // 后台静默安装 Playwright Chromium 渲染内核（知乎专栏等动态页面通道用）
                // 不阻塞工作台加载；未装完前相关通道会提示重试，其余功能不受影响
                if (this.needPlaywrightSetup) {
                    this.needPlaywrightSetup = false;
                    try {
                        const pwChild = spawn(pythonCmd, [...pyPrefix, "-m", "playwright", "install", "chromium"], {
                            cwd: projectRoot, stdio: "ignore", windowsHide: true, detached: true
                        });
                        pwChild.on("error", (e) => console.warn("[LocalPython] Playwright 内核后台安装失败:", e.message));
                        pwChild.unref();
                        console.log("[LocalPython] 已在后台开始安装 Playwright Chromium 内核…");
                    } catch (e) {
                        console.warn("[LocalPython] Playwright 内核后台安装异常:", e.message);
                    }
                }
            } else {
                console.warn(`[LocalPython] 服务未在预期时间内响应健康检查，但进程已创建 (PID: ${this.child ? this.child.pid : '未知'})`);
                this.running = true;
            }
        } catch (err) {
            console.error(`[LocalPython] 启动失败:`, err);
            this.lastError = err.message;
            this.running = false;
        }

        return this.getStatus();
    }

    stop() {
        if (!this.child) {
            this.running = false;
            return;
        }
        const pid = this.child.pid;
        console.log(`[LocalPython] 正在关闭本地 Python 进程树 (PID: ${pid})...`);
        try {
            if (process.platform === "win32") {
                execSync(`taskkill /pid ${pid} /T /F`, { stdio: "ignore" });
            } else {
                this.child.kill("SIGTERM");
            }
        } catch (e) {
            try { this.child.kill("SIGKILL"); } catch (_) {}
        }
        this.child = null;
        this.running = false;
    }

    async restart() {
        this.stop();
        await new Promise(r => setTimeout(r, 600));
        return await this.start();
    }

    getStatus() {
        return {
            running: this.running,
            port: this.port,
            pid: this.child ? this.child.pid : null,
            error: this.lastError,
            url: `http://127.0.0.1:${this.port}`
        };
    }
}

// =========================================================================
// 7. 窗口创建与主进程调度
// =========================================================================
// ===== 初始化进度页与工作台导航 =====
// bootPageActive 标记当前窗口是否停在初始化进度页（此时才允许 setBootStatus 推送文案）
let bootPageActive = false;

// 向初始化页推送进度文案（内核部署可能耗时数分钟，让用户知道没卡死）
// 注意：初始化页状态元素 id 是 "st"（见 createMainWindow 的 bootHtml）；
// 必须在 IIFE 内更新——直接注入顶层 `const el` 会在第二次调用时
// 因同一全局作用域重复声明而抛 SyntaxError，之后所有进度推送全部静默失效
function setBootStatus(text) {
    if (!mainWindow || !bootPageActive) return;
    mainWindow.webContents.executeJavaScript(
        `(() => { const el = document.getElementById('st'); if (el) el.textContent = ${JSON.stringify(text)}; })()`,
        true
    ).catch(() => {});
}

// 内核就绪后切入新版多平台工作台。
// 旧逻辑：8 秒内连不上就静默回退内置旧版渲染模版——本地服务可能稍后才就绪，
// 用户却被永久留在 v1.2.0 旧界面上，且毫无提示。
// 新逻辑：持续轮询本地服务健康检查（最长 3 分钟，覆盖首次启动部署依赖的场景），
// 就绪后加载新版工作台；超时则在初始化页明确报错并载入诚实的错误兜底页，
// 绝不再静默回退旧版假工作台。
function navigateMainWindowToWorkbench(port) {
    if (!mainWindow) return;
    const localUrl = `http://127.0.0.1:${port}/app?client_mode=1&port=${port}`;
    const healthUrl = `http://127.0.0.1:${port}/api/health`;
    console.log(`[BlogDistiller] 准备加载本地桌面界面: ${localUrl}`);

    const POLL_INTERVAL_MS = 2000;
    const MAX_POLL_MS = 3 * 60 * 1000; // 首次启动要部署 Python 内核依赖，最长等 3 分钟
    const pollStart = Date.now();

    const pollHealthOnce = () => new Promise((resolve) => {
        const req = http.get(healthUrl, { timeout: 1500 }, (res) => {
            res.resume();
            resolve(res.statusCode === 200);
        });
        req.on("error", () => resolve(false));
        req.on("timeout", () => { req.destroy(); resolve(false); });
    });

    const enterWorkbench = async () => {
        let serviceReady = false;
        while (Date.now() - pollStart < MAX_POLL_MS) {
            if (await pollHealthOnce()) { serviceReady = true; break; }
            const elapsedSec = Math.round((Date.now() - pollStart) / 1000);
            setBootStatus(`正在等待本地服务就绪…（已等待 ${elapsedSec} 秒，首次启动部署依赖可能需要数分钟）`);
            await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
        }

        if (!serviceReady) {
            console.error("[BlogDistiller] 本地服务 3 分钟内未就绪，展示明确错误，不再回退旧界面");
            setBootStatus("❌ 本地服务启动超时。请关闭本应用后重新打开重试；若反复失败，请检查网络代理或杀毒软件是否拦截。");
            mainWindow.loadFile(path.join(__dirname, "renderer", "index.html")).catch(() => {});
            return;
        }

        bootPageActive = false;
        try {
            await mainWindow.loadURL(localUrl);
            console.log("[BlogDistiller] 新版工作台加载成功");
        } catch (err) {
            console.warn(`[BlogDistiller] 工作台首次加载失败，1 秒后重试: ${err.message}`);
            setTimeout(() => {
                mainWindow.loadURL(localUrl).catch((e2) => {
                    console.error("[BlogDistiller] 工作台加载失败:", e2.message);
                });
            }, 1000);
        }
    };
    enterWorkbench();
}

function createMainWindow(port = null) {
    mainWindow = new BrowserWindow({
        width: 1280,
        height: 840,
        minWidth: 1020,
        minHeight: 700,
        title: "BlogDistiller (博萃) · 文章导出助手",
        icon: path.join(__dirname, "renderer", "assets", "logo_icon.png"),
        autoHideMenuBar: true,
        backgroundColor: "#f7f6f2",
        show: true,
        webPreferences: {
            preload: path.join(__dirname, "preload.js"),
            nodeIntegration: false,
            contextIsolation: true,
            webSecurity: false,
            allowRunningInsecureContent: true
        }
    });

    // 未传 port：先展示初始化进度页，等内核部署完成后由 navigateMainWindowToWorkbench 切入工作台
    if (!port) {
        bootPageActive = true;
        const bootHtml = `<!doctype html><html><head><meta charset="utf-8"><title>BlogDistiller 初始化中</title><style>body{font-family:'Segoe UI','Microsoft YaHei',system-ui,sans-serif;background:#f7f6f2;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}.card{text-align:center;max-width:560px;padding:40px}.spin{width:44px;height:44px;border:4px solid #d1d5db;border-top-color:#059669;border-radius:50%;margin:0 auto 22px;animation:s 1s linear infinite}@keyframes s{to{transform:rotate(360deg)}}h1{font-size:1.25rem;color:#111827;margin:0 0 10px}p{color:#6b7280;font-size:.92rem;line-height:1.7;margin:0}#st{margin-top:18px;color:#059669;font-weight:600;min-height:1.4em}</style></head><body><div class="card"><div class="spin"></div><h1>BlogDistiller · 博萃 正在初始化</h1><p>首次启动需要部署本地 Python 运行内核，请保持网络畅通并耐心等待，完成后将自动进入工作台。</p><div id="st">正在启动…</div></div></body></html>`;
        mainWindow.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(bootHtml)).catch(() => {});
        return;
    }

    navigateMainWindowToWorkbench(port);

    // 快捷键支持：F5 / Ctrl+R 刷新界面，F12 / Ctrl+Shift+I 开启开发者调试工具
    mainWindow.webContents.on('before-input-event', (event, input) => {
        if (input.type === 'keyDown') {
            if (input.key === 'F5' || ((input.control || input.meta) && input.key.toLowerCase() === 'r')) {
                mainWindow.webContents.reload();
                event.preventDefault();
            }
            if (input.key === 'F12' || ((input.control || input.meta) && input.shift && input.key.toLowerCase() === 'i')) {
                mainWindow.webContents.toggleDevTools();
                event.preventDefault();
            }
        }
    });

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
    localPythonManager = new LocalPythonManager(app);
    let pythonStatus = { port: 8000 };

    // 先把窗口立起来显示初始化进度页，再启动本地内核（首次启动会自动部署 Python 环境，耗时数分钟）
    createMainWindow(null);
    try {
        pythonStatus = await localPythonManager.start();
    } catch (err) {
        console.error("[BlogDistiller] 本地 Python 引擎启动异常:", err);
    }
    navigateMainWindowToWorkbench(pythonStatus.port || 8000);

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

        // 场景 2: 单篇推文链接 (自动提取文章信息，并尝试拉取该号全量历史文章)
        if (urls.length === 1) {
            const cleanUrl = urls[0];
            if (mainWindow) mainWindow.webContents.send("wechat:fetch-progress", { message: "正在解析推文与公众号信息...", current: 1, total: 1 });
            let pageHtml = "";
            try {
                pageHtml = await fetchPageHtml(cleanUrl);
            } catch(fetchErr) {
                sendDebugLog(`[推文拉取异常] ${fetchErr.message}`, "warn");
            }
            const singleArt = pageHtml ? parseSingleArticleFromHtml(pageHtml, cleanUrl) : {
                id: `art_${Date.now()}_0`,
                title: "微信推文",
                author: authorName,
                url: cleanUrl,
                create_time: new Date().toISOString().split("T")[0],
                digest: "",
                is_original: true,
                biz: wechatAuth.biz || "",
                status: "pending",
                fail_reason: ""
            };
            authorName = (singleArt.author && singleArt.author !== "微信公众号") ? singleArt.author : (wechatAuth.author || authorName);
            targetBiz = (singleArt.biz && isValidBiz(singleArt.biz)) ? singleArt.biz : (wechatAuth.biz || "");
            if (authorName && authorName !== "微信公众号") wechatAuth.author = authorName;
            if (targetBiz && isValidBiz(targetBiz)) wechatAuth.biz = targetBiz;

            if (targetBiz && isValidBiz(targetBiz)) {
                // 关键增强：若已扫码连接公众平台官方通道，优先走官方 search_biz + appmsg 接口。
                // 原因：微信服务端对"电脑微信未关注"的公众号，profile_ext 历史接口会直接返回
                // 空列表（ret=0 但 home_page_list:[]，连微信客户端自己主页都刷不出历史），
                // 嗅探通道对此无解；只有官方通道不受"是否关注"限制，可拉全网任意号全量历史。
                if (wechatAuth.mpConnected && wechatAuth.mpToken && authorName && authorName !== "微信公众号") {
                    sendDebugLog(`[官方直连优先] 已连通公众平台，跳过受限的嗅探通道，直接按名称【${authorName}】官方全量拉取...`, "info");
                    try {
                        const officialRes = await fetchArticlesViaMpOfficial(authorName, maxArticles || 0, (msg, cur, total, currentList) => {
                            if (mainWindow) mainWindow.webContents.send("wechat:fetch-progress", { message: msg, current: cur, total, articles: currentList || [], author: authorName });
                        });
                        if (officialRes.articles && officialRes.articles.length > 0) {
                            return { author: officialRes.author, articles: officialRes.articles, biz: officialRes.biz };
                        }
                        sendDebugLog(`[官方直连] 按名称未拉到结果，回退到嗅探通道继续尝试...`, "warn");
                    } catch (mpErr) {
                        sendDebugLog(`[官方直连异常] ${mpErr.message}，回退到嗅探通道...`, "warn");
                    }
                }

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
                    // 记录待自动补全目标：一旦用户在电脑微信打开主页或更新凭证，立即自动补拉全量历史
                    pendingAutoFetchTarget = { target: cleanUrl, maxArticles: maxArticles || 0, biz: targetBiz, author: authorName };

                    // 零操作引导：嗅探通道拿不到时，若官方通道未连接，自动打开扫码窗口。
                    // 用户扫码成功后由 checkNavigation 触发 pendingAutoFetchTarget 自动重试，全程无需手动再点。
                    if (!wechatAuth.mpConnected) {
                        sendDebugLog(`[自动引导] 嗅探通道受限，已自动为您打开微信公众平台扫码窗口，扫码成功后工具将自动继续拉取全部历史...`, "info");
                        try { openMpLoginWindow(); } catch(e) {}
                    }

                    // 降级兜底：如果拉取全部历史受限，但当前单篇解析成功，优雅返回该单篇，绝不中断甩错
                    if (singleArt.title && singleArt.title !== "未知标题") {
                        sendDebugLog(`[单篇就绪] 已成功解析单篇推文《${singleArt.title}》，可直接勾选导出！(如需全部历史，请在电脑微信点开该号主页)`, "info");
                        return { author: authorName, articles: [singleArt], biz: targetBiz, fallbackSingle: true };
                    }
                    throw new Error(`未能获取公众号【${authorName}】的历史文章：${err.message}\n\n👉 已为您自动打开微信官方扫码窗口，扫码成功后工具将自动继续拉取（官方通道可无视新旧号/客户端版本限制）。`);
                }
            }

            // 如果暂未捕获到历史翻页 biz，但单篇解析成功：优雅呈现该推文
            if (singleArt.title && singleArt.title !== "未知标题") {
                sendDebugLog(`[单篇解析] 成功获取单篇推文: 《${singleArt.title}》 (作者: 【${authorName}】)`, "success");
                return { author: authorName, articles: [singleArt], biz: "", fallbackSingle: true };
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

        // 场景 4: 用户输入了公众号名称（纯文本）。
        // 关键修复：官方扫码通道函数 fetchArticlesViaMpOfficial 此前定义了却从未被调用（死代码），
        // 导致用户扫码连接后按名称检索仍报"请输入有效链接"。这里正式接入官方通道：
        // 已扫码连接 → 走 search_biz + appmsg 官方接口秒级全量拉取；未连接 → 给出明确引导。
        if (wechatAuth.mpConnected && wechatAuth.mpToken) {
            sendDebugLog(`[官方直连] 检测到已扫码连接公众平台，按名称【${cleanTarget}】走官方通道全量拉取...`, "info");
            return await fetchArticlesViaMpOfficial(cleanTarget, maxArticles || 0, (msg, cur, total, currentList) => {
                if (mainWindow) mainWindow.webContents.send("wechat:fetch-progress", { message: msg, current: cur, total, articles: currentList || [], author: cleanTarget });
            });
        }

        throw new Error(`请输入有效的微信公众号推文链接（例如：https://mp.weixin.qq.com/s/...）或公众号名称！\n\n💡 提示：点击上方【微信官方扫码连接】后，可直接输入公众号名称秒级拉取全部历史文章。`);
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

    // 本地磁盘持久化公众号历史档案库 IPC
    ipcMain.handle("history:get-all", () => {
        return AccountHistoryManager.getAll();
    });

    ipcMain.handle("history:save-account", (_, accountData) => {
        return AccountHistoryManager.saveAccount(accountData);
    });

    ipcMain.handle("history:delete-account", (_, key) => {
        return AccountHistoryManager.deleteAccount(key);
    });

    ipcMain.handle("history:get-info", () => {
        return {
            dataDir: DATA_DIR,
            historyFile: HISTORY_FILE,
            exportDir: DEFAULT_EXPORT_DIR
        };
    });

    ipcMain.handle("history:open-dir", () => {
        shell.openPath(DATA_DIR);
        return true;
    });

    // 链接提取与文本文件保存 IPC
    ipcMain.handle("fs:save-text", async (_, { filename, content, defaultPath }) => {
        try {
            const res = await dialog.showSaveDialog(mainWindow, {
                title: "保存文章链接文件",
                defaultPath: defaultPath || path.join(DEFAULT_EXPORT_DIR, filename || `文章链接提取_${Date.now()}.txt`),
                filters: [
                    { name: "文本/表格文件", extensions: ["txt", "csv", "md"] }
                ]
            });
            if (!res.canceled && res.filePath) {
                fs.writeFileSync(res.filePath, content, "utf8");
                return { success: true, filePath: res.filePath };
            }
            return { success: false, canceled: true };
        } catch(e) {
            return { success: false, error: e.message };
        }
    });

    // =========================================================================
    // 本地优先桌面客户端自治 IPC (Local-First Autonomy)
    // =========================================================================
    ipcMain.handle("local-service:start", async () => {
        return localPythonManager ? await localPythonManager.start() : { running: false };
    });

    ipcMain.handle("local-service:stop", () => {
        if (localPythonManager) localPythonManager.stop();
        return { running: false };
    });

    ipcMain.handle("local-service:status", () => {
        return localPythonManager ? localPythonManager.getStatus() : { running: false, port: 8000 };
    });

    ipcMain.handle("local-service:restart", async () => {
        if (!localPythonManager) return { running: false };
        return await localPythonManager.restart();
    });

    ipcMain.handle("fs:reveal-file", (_, filePath) => {
        try {
            if (filePath && fs.existsSync(filePath)) {
                shell.showItemInFolder(filePath);
                return true;
            } else if (filePath) {
                shell.openPath(filePath);
                return true;
            }
        } catch(e) {}
        return false;
    });

    ipcMain.handle("fs:get-downloads-path", () => {
        try {
            const root = localPythonManager ? localPythonManager.getProjectRoot() : process.cwd();
            const dl = path.join(root, "downloads");
            if (fs.existsSync(dl)) return dl;
        } catch(e) {}
        return DEFAULT_EXPORT_DIR;
    });

    ipcMain.handle("fs:show-open-dialog", async (_, options) => {
        return await dialog.showOpenDialog(mainWindow, options || {});
    });

    ipcMain.handle("auth:open-zhihu-login", () => {
        openZhihuLoginWindow();
        return { success: true };
    });

    ipcMain.handle("auth:open-weibo-login", () => {
        openWeiboLoginWindow();
        return { success: true };
    });

    ipcMain.on("app:restart", () => {
        cleanup();
        app.relaunch();
        app.exit(0);
    });
});

function cleanup() {
    applyWindowsPac(8899, false);
    if (proxyInstance) {
        try { proxyInstance.close(); } catch (e) {}
    }
    if (localPythonManager) {
        try { localPythonManager.stop(); } catch (e) {}
    }
}

app.on("before-quit", cleanup);
app.on("will-quit", cleanup);
app.on("window-all-closed", () => {
    cleanup();
    if (process.platform !== "darwin") app.quit();
});
