// BlogDistiller Content Script - 在工作台自动执行免密授权与双向通信桥梁

(function() {
    console.log("[BlogDistiller Helper] 浏览器同步助手已加载，正在自动检测各平台登录凭证...");

    // 批量抓取任务去重表：网页端会同时用 CustomEvent 和 postMessage 两条通道派发
    // 同一个批次，如果不去重，后台会把整批文章抓两遍（请求量翻倍，容易触发风控）
    const handledBatchIds = new Set();

    // 1. 自动同步知乎
    chrome.runtime.sendMessage({ action: "AUTO_SYNC_ZHIHU" }, (res) => {
        if (res && res.success) {
            // 同步请求成功 ≠ Cookie 里一定有 z_c0；浏览器 cookie 取决于当前浏览器是否已在 zhihu.com 登录
            if (res.hasZc0) {
                console.log("[BlogDistiller Helper] 知乎登录态已自动同步成功，且包含 key=z_c0。", res);
            } else {
                console.warn("[BlogDistiller Helper] 知乎 Cookie 已同步，但当前浏览器未找到 key=z_c0。请检查：1) 是否已在当前浏览器登录 www.zhihu.com；2) 知乎账号是否处于登录态。", res);
            }
            window.dispatchEvent(new CustomEvent("BlogDistillerAuthSynced", {
                detail: { platform: "zhihu", status: res }
            }));
            if (typeof window.checkZhihuAuth === "function") {
                window.checkZhihuAuth();
            }
        } else {
            console.warn("[BlogDistiller Helper] 知乎自动同步失败：", res);
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

    // 3. 标记扩展就绪状态并通知网页端（支持属性标记、CustomEvent 与 postMessage）
    function notifyReady() {
        try {
            document.documentElement.setAttribute("data-blogdistiller-extension", "1.4.0");
            window.dispatchEvent(new CustomEvent("BlogDistillerExtensionReady", {
                detail: { version: "1.4.0", status: "active" }
            }));
            window.postMessage({ type: "BlogDistillerExtensionReady", version: "1.4.0" }, "*");
        } catch (e) {}
    }
    notifyReady();
    // 延迟再次通知一次，确保页面脚本加载完毕后能收到
    setTimeout(notifyReady, 500);
    setTimeout(notifyReady, 1500);

    // 4. 响应网页端心跳探针 (CustomEvent 与 postMessage 双通道)
    window.addEventListener("BlogDistillerPing", () => {
        window.dispatchEvent(new CustomEvent("BlogDistillerPong", {
            detail: { version: "1.4.0", status: "active", localRelayReady: true }
        }));
    });
    window.addEventListener("message", (e) => {
        if (e.data && e.data.type === "BlogDistillerPing") {
            window.postMessage({ type: "BlogDistillerPong", version: "1.4.0", localRelayReady: true }, "*");
        }
        if (e.data && e.data.type === "BlogDistillerBatchFetchRelay") {
            const detail = e.data.detail || {};
            const batchId = detail.batchId || String(Date.now());
            // 同一批次只处理一次（另一条通道会收到重复派发，直接忽略）
            if (handledBatchIds.has(batchId)) return;
            handledBatchIds.add(batchId);
            chrome.runtime.sendMessage({
                action: "BATCH_FETCH_URLS_RELAY",
                batchId: batchId,
                items: detail.items || [],
                concurrency: detail.concurrency || 3
            }, (res) => {
                // 检测消息通道错误（如后台服务休眠、消息超限），把失败暴露出来而不是静默
                if (chrome.runtime.lastError) {
                    window.postMessage({
                        type: "BlogDistillerBatchFetchRelayResponse_" + batchId,
                        detail: { success: false, error: chrome.runtime.lastError.message }
                    }, "*");
                    return;
                }
                window.postMessage({
                    type: "BlogDistillerBatchFetchRelayResponse_" + batchId,
                    detail: res || { success: false, error: "批量中继无响应" }
                }, "*");
            });
        }
    });

    // 5. 监听来自网页端的单篇本地住宅IP中继抓取请求
    window.addEventListener("BlogDistillerFetchArticleRelay", (e) => {
        const detail = e.detail || {};
        const requestId = detail.requestId || String(Date.now());
        chrome.runtime.sendMessage({
            action: "FETCH_URL_RELAY",
            url: detail.url,
            headers: detail.headers || {}
        }, (res) => {
            window.dispatchEvent(new CustomEvent("BlogDistillerFetchArticleRelayResponse_" + requestId, {
                detail: res || { success: false, error: "扩展无响应" }
            }));
        });
    });

    // 6. 监听来自网页端的批量本地住宅IP中继抓取请求
    window.addEventListener("BlogDistillerBatchFetchRelay", (e) => {
        const detail = e.detail || {};
        const batchId = detail.batchId || String(Date.now());
        // 同一批次只处理一次（另一条通道会收到重复派发，直接忽略）
        if (handledBatchIds.has(batchId)) return;
        handledBatchIds.add(batchId);
        chrome.runtime.sendMessage({
            action: "BATCH_FETCH_URLS_RELAY",
            batchId: batchId,
            items: detail.items || [],
            concurrency: detail.concurrency || 3
        }, (res) => {
            // 检测消息通道错误（如后台服务休眠、消息超限），把失败暴露出来而不是静默
            if (chrome.runtime.lastError) {
                window.postMessage({
                    type: "BlogDistillerBatchFetchRelayResponse_" + batchId,
                    detail: { success: false, error: chrome.runtime.lastError.message }
                }, "*");
                return;
            }
            window.postMessage({
                type: "BlogDistillerBatchFetchRelayResponse_" + batchId,
                detail: res || { success: false, error: "批量中继无响应" }
            }, "*");
        });
    });

    // 监听来自后台脚本的批量抓取实时进度并分发给前端页面
    // 注意：必须同时用 CustomEvent 和 postMessage 两种通道分发。
    // 工作台页面(app.html)只监听 window 的 message 事件（postMessage 通道），
    // 之前只发 CustomEvent 导致页面永远收不到进度，计数一直卡在 0。
    chrome.runtime.onMessage.addListener((msg) => {
        if (msg && msg.action === "RELAY_BATCH_PROGRESS" && msg.batchId) {
            // 只提取页面需要的字段，避免把 action/batchId 等冗余信息反复拷贝
            const payload = { current: msg.current, total: msg.total, item: msg.item };
            window.dispatchEvent(new CustomEvent("BlogDistillerBatchFetchRelayProgress_" + msg.batchId, {
                detail: payload
            }));
            window.postMessage({
                type: "BlogDistillerBatchFetchRelayProgress_" + msg.batchId,
                detail: payload
            }, "*");
        }
    });

    // 7. 监听来自网页端的强制重新同步指令
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

