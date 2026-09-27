// BlogDistiller Extension - Background Service Worker (知乎 & 微博 平台凭证自动无感同步)

const SYNC_HOSTS = ["https://doc.305758.xyz", "http://127.0.0.1:8000", "http://localhost:8000"];

async function postToHosts(endpoint, payload) {
    let lastData = null;
    let anySuccess = false;
    for (const host of SYNC_HOSTS) {
        try {
            const res = await fetch(`${host}${endpoint}`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            if (res.ok) {
                lastData = await res.json();
                anySuccess = true;
            }
        } catch (e) {}
    }
    return { anySuccess, lastData };
}

// 1. 同步知乎登录态
async function syncZhihuCookies() {
    try {
        const cookies = await chrome.cookies.getAll({ domain: "zhihu.com" });
        if (!cookies || cookies.length === 0) {
            return { success: false, message: "未检测到知乎凭证，请在当前浏览器登录 zhihu.com" };
        }

        const cookiePairs = [];
        let hasZc0 = false;
        for (const c of cookies) {
            cookiePairs.push(`${c.name}=${c.value}`);
            if (c.name === 'z_c0') hasZc0 = true;
        }

        const cookieStr = cookiePairs.join('; ');
        const { anySuccess, lastData } = await postToHosts("/api/zhihu/set-cookie", { cookie: cookieStr });

        return {
            success: anySuccess,
            hasZc0: hasZc0,
            count: cookies.length,
            data: lastData
        };
    } catch (err) {
        return { success: false, message: "知乎同步异常: " + err.message };
    }
}

// 2. 同步微博登录态
async function syncWeiboCookies() {
    try {
        const cookieMap = {};
        let hasSub = false;

        const weiboCom = await chrome.cookies.getAll({ domain: "weibo.com" });
        for (const c of weiboCom) {
            cookieMap[c.name] = c.value;
            if (c.name === 'SUB') hasSub = true;
        }

        const weiboCn = await chrome.cookies.getAll({ domain: "weibo.cn" });
        for (const c of weiboCn) {
            cookieMap[c.name] = c.value;
            if (c.name === 'SUB') hasSub = true;
        }

        if (Object.keys(cookieMap).length === 0) {
            return { success: false, message: "未检测到微博凭证，请在当前浏览器登录 weibo.com" };
        }

        const cookiePairs = Object.entries(cookieMap).map(([k, v]) => `${k}=${v}`);
        const cookieStr = cookiePairs.join('; ');

        const { anySuccess, lastData } = await postToHosts("/api/weibo/set-cookie", { cookie: cookieStr });

        return {
            success: anySuccess,
            hasSub: hasSub,
            count: Object.keys(cookieMap).length,
            data: lastData
        };
    } catch (err) {
        return { success: false, message: "微博同步异常: " + err.message };
    }
}

// 3. 打开官方工作台网站
function openOfficialWebsite() {
    chrome.tabs.create({ url: "https://doc.305758.xyz/app" });
}

// 4. 监听消息分发
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action === "AUTO_SYNC_ZHIHU" || request.action === "SYNC_NOW") {
        syncZhihuCookies().then(res => sendResponse(res));
        return true;
    }

    if (request.action === "SYNC_WEIBO_NOW" || request.action === "AUTO_SYNC_WEIBO") {
        syncWeiboCookies().then(res => sendResponse(res));
        return true;
    }

    if (request.action === "SYNC_ALL") {
        (async () => {
            const zhihuRes = await syncZhihuCookies();
            const weiboRes = await syncWeiboCookies();
            return {
                zhihu: zhihuRes,
                weibo: weiboRes,
                success: (zhihuRes && zhihuRes.success) || (weiboRes && weiboRes.success)
            };
        })().then(res => sendResponse(res));
        return true;
    }

    if (request.action === "OPEN_WEBSITE") {
        openOfficialWebsite();
        sendResponse({ success: true });
        return true;
    }

    // 本地直连健康检测：用本机网络对知乎首页发一次真实请求
    if (request.action === "PING_LOCAL_RELAY") {
        (async () => {
            const testRes = await relayFetchUrl("https://www.zhihu.com/", {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            });
            // 只要收到响应（2xx/3xx/4xx）就算本地网络可用，只有请求抛错才算异常
            const networkReachable = testRes.success || (!!testRes.status && testRes.status > 0 && testRes.status < 500);
            sendResponse({
                success: networkReachable,
                status: testRes.status,
                error: testRes.error || null
            });
        })();
        return true;
    }

    // 5. 本地住宅IP中继抓取单篇 (100% 消耗用户本机家庭宽带，规避云端机房IP风控)
    if (request.action === "FETCH_URL_RELAY") {
        relayFetchUrl(request.url, request.headers || {}).then(res => sendResponse(res));
        return true;
    }

    // 6. 本地住宅IP批量并发中继抓取
    if (request.action === "BATCH_FETCH_URLS_RELAY") {
        const tabId = sender.tab ? sender.tab.id : null;
        relayBatchFetchUrls(request.items || [], request.concurrency || 3, (prog) => {
            if (tabId) {
                chrome.tabs.sendMessage(tabId, {
                    action: "RELAY_BATCH_PROGRESS",
                    batchId: request.batchId,
                    ...prog
                }).catch(() => {});
            }
        })
            .then(res => {
                // 每篇结果已经随进度消息逐篇送回页面了。
                // 大批量(>20篇)时最终回包只带状态和总数，不带原始 HTML——
                // 否则几百 MB 的数据包会超出 Chrome 消息通道上限导致回传失败。
                const includeResults = res.length <= 20;
                sendResponse({
                    success: true,
                    total: res.length,
                    results: includeResults ? res : []
                });
            })
            .catch(err => sendResponse({ success: false, error: err.message }));
        return true;
    }
});

// 6. 本地网络中继抓取核心引擎：使用用户本机真实环境发起请求
async function relayFetchUrl(url, customHeaders = {}) {
    try {
        const defaultHeaders = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
        };
        const headers = { ...defaultHeaders, ...customHeaders };
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 20000);

        const resp = await fetch(url, {
            method: "GET",
            headers: headers,
            credentials: "include",
            signal: controller.signal
        });
        clearTimeout(timeoutId);

        const text = await resp.text();
        return {
            success: resp.ok,
            status: resp.status,
            url: resp.url || url,
            raw_html: text
        };
    } catch (e) {
        return {
            success: false,
            error: e.name === "AbortError" ? "请求超时(20s)" : (e.message || "本地网络请求失败"),
            url: url
        };
    }
}

async function relayBatchFetchUrls(items, concurrency = 3, onProgress = null) {
    const results = [];
    const queue = [...items];
    const total = items.length;
    let completed = 0;

    async function worker() {
        while (queue.length > 0) {
            const item = queue.shift();
            try {
                const res = await relayFetchUrl(item.url, item.headers || {});
                const resultItem = {
                    ...item,
                    raw_html: res.raw_html || "",
                    is_failed: !res.success,
                    error_reason: res.error || null,
                    status_code: res.status
                };
                results.push(resultItem);
                completed++;
                if (onProgress) {
                    onProgress({ current: completed, total: total, item: resultItem });
                }
            } catch (err) {
                const failItem = {
                    ...item,
                    is_failed: true,
                    error_reason: err.message
                };
                results.push(failItem);
                completed++;
                if (onProgress) {
                    onProgress({ current: completed, total: total, item: failItem });
                }
            }
        }
    }

    const workers = [];
    const workerCount = Math.min(concurrency, Math.max(1, items.length));
    for (let i = 0; i < workerCount; i++) {
        workers.push(worker());
    }
    await Promise.all(workers);
    return results;
}

// 7. 监听 Cookie 动态变更自动同步
chrome.cookies.onChanged.addListener((changeInfo) => {
    if (changeInfo.cookie.domain.includes("zhihu.com")) syncZhihuCookies().catch(() => {});
    if (changeInfo.cookie.domain.includes("weibo.com") || changeInfo.cookie.domain.includes("weibo.cn")) syncWeiboCookies().catch(() => {});
});

