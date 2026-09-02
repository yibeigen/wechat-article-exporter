// BlogDistiller Content Script - 在工作台自动执行免密授权与双向通信桥梁

(function() {
    console.log("[BlogDistiller Helper] 浏览器同步助手已加载，正在自动检测各平台登录凭证...");

    // 1. 自动同步知乎
    chrome.runtime.sendMessage({ action: "AUTO_SYNC_ZHIHU" }, (res) => {
        if (res && res.success) {
            console.log("[BlogDistiller Helper] 知乎登录态已自动同步成功！", res);
            window.dispatchEvent(new CustomEvent("BlogDistillerAuthSynced", {
                detail: { platform: "zhihu", status: res }
            }));
            if (typeof window.checkZhihuAuth === "function") {
                window.checkZhihuAuth();
            }
        }
    });

    // 2. 自动同步微博
    chrome.runtime.sendMessage({ action: "AUTO_SYNC_WEIBO" }, (res) => {
        if (res && res.success) {
            console.log("[BlogDistiller Helper] 微博凭证已自动同步成功！", res);
            window.dispatchEvent(new CustomEvent("BlogDistillerAuthSynced", {
                detail: { platform: "weibo", status: res }
            }));
            if (typeof window.checkWeiboAuth === "function") {
                window.checkWeiboAuth();
            }
        }
    });

    // 3. 监听来自网页端的强制重新同步指令
    window.addEventListener("TriggerBlogDistillerExtensionSync", (e) => {
        const targetPlatform = (e && e.detail && e.detail.platform) || "all";
        if (targetPlatform === "zhihu" || targetPlatform === "all") {
            chrome.runtime.sendMessage({ action: "SYNC_NOW" }, (res) => {
                if (typeof window.checkZhihuAuth === "function") window.checkZhihuAuth();
            });
        }
        if (targetPlatform === "weibo" || targetPlatform === "all") {
            chrome.runtime.sendMessage({ action: "SYNC_WEIBO_NOW" }, (res) => {
                if (typeof window.checkWeiboAuth === "function") window.checkWeiboAuth();
            });
        }
    });
})();
