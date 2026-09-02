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
});

// 5. 监听 Cookie 动态变更自动同步
chrome.cookies.onChanged.addListener((changeInfo) => {
    if (changeInfo.cookie.domain.includes("zhihu.com")) syncZhihuCookies().catch(() => {});
    if (changeInfo.cookie.domain.includes("weibo.com") || changeInfo.cookie.domain.includes("weibo.cn")) syncWeiboCookies().catch(() => {});
});
