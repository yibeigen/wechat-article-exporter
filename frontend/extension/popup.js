document.addEventListener("DOMContentLoaded", () => {
    const zhihuBadge = document.getElementById("zhihuBadge");
    const zhihuDesc = document.getElementById("zhihuDesc");
    const btnSyncZhihu = document.getElementById("btnSyncZhihu");

    const weiboBadge = document.getElementById("weiboBadge");
    const weiboDesc = document.getElementById("weiboDesc");
    const btnSyncWeibo = document.getElementById("btnSyncWeibo");

    const btnSyncAll = document.getElementById("btnSyncAll");
    const btnOpenWebsite = document.getElementById("btnOpenWebsite");
    const linkOfficial = document.getElementById("linkOfficial");
    const linkGithub = document.getElementById("linkGithub");

    function syncZhihu() {
        if (zhihuBadge) {
            zhihuBadge.innerText = "同步中...";
            zhihuBadge.className = "status-badge badge-warn";
        }
        chrome.runtime.sendMessage({ action: "SYNC_NOW" }, (res) => {
            if (chrome.runtime.lastError) {
                if (zhihuBadge) {
                    zhihuBadge.innerText = "异常";
                    zhihuBadge.className = "status-badge badge-fail";
                }
                if (zhihuDesc) zhihuDesc.innerText = "扩展通信异常，请在 edge://extensions 重新加载扩展";
                return;
            }
            if (res && res.success) {
                if (zhihuBadge) {
                    zhihuBadge.innerText = "已连接";
                    zhihuBadge.className = "status-badge badge-success";
                }
                if (zhihuDesc) zhihuDesc.innerText = `已同步 ${res.count || 0} 个 Cookie 凭证 (含 z_c0)`;
            } else {
                if (zhihuBadge) {
                    zhihuBadge.innerText = "未登录";
                    zhihuBadge.className = "status-badge badge-fail";
                }
                if (zhihuDesc) zhihuDesc.innerText = res ? (res.message || "未检测到凭证") : "请先在当前浏览器打开 zhihu.com 登录";
            }
        });
    }

    function syncWeibo() {
        if (weiboBadge) {
            weiboBadge.innerText = "同步中...";
            weiboBadge.className = "status-badge badge-warn";
        }
        chrome.runtime.sendMessage({ action: "SYNC_WEIBO_NOW" }, (res) => {
            if (chrome.runtime.lastError) {
                if (weiboBadge) {
                    weiboBadge.innerText = "异常";
                    weiboBadge.className = "status-badge badge-fail";
                }
                if (weiboDesc) weiboDesc.innerText = "扩展通信异常，请在 edge://extensions 重新加载扩展";
                return;
            }
            if (res && res.success) {
                if (weiboBadge) {
                    weiboBadge.innerText = "已连接";
                    weiboBadge.className = "status-badge badge-success";
                }
                if (weiboDesc) weiboDesc.innerText = `已同步 ${res.count || 0} 个微博 Cookie (含 SUB)`;
            } else {
                if (weiboBadge) {
                    weiboBadge.innerText = "未登录";
                    weiboBadge.className = "status-badge badge-fail";
                }
                if (weiboDesc) weiboDesc.innerText = res ? (res.message || "未检测到凭证") : "请先在当前浏览器打开 weibo.com 登录";
            }
        });
    }

    if (btnSyncZhihu) btnSyncZhihu.addEventListener("click", syncZhihu);
    if (btnSyncWeibo) btnSyncWeibo.addEventListener("click", syncWeibo);
    if (btnSyncAll) {
        btnSyncAll.addEventListener("click", () => {
            syncZhihu();
            syncWeibo();
        });
    }

    if (btnOpenWebsite) {
        btnOpenWebsite.addEventListener("click", () => {
            chrome.tabs.create({ url: "https://doc.305758.xyz/app" });
        });
    }

    if (linkOfficial) {
        linkOfficial.addEventListener("click", (e) => {
            e.preventDefault();
            chrome.tabs.create({ url: "https://doc.305758.xyz" });
        });
    }

    if (linkGithub) {
        linkGithub.addEventListener("click", (e) => {
            e.preventDefault();
            chrome.tabs.create({ url: "https://github.com/yibeigen/wechat-article-exporter" });
        });
    }

    // 初始自动检测
    syncZhihu();
    syncWeibo();
});
