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

    // 本地住宅 IP 直连状态相关 DOM
    const relayBadge = document.getElementById("relayBadge");
    const relayDesc = document.getElementById("relayDesc");
    const btnTestRelay = document.getElementById("btnTestRelay");
    const relayLog = document.getElementById("relayLog");

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
                if (res.hasZc0) {
                    if (zhihuBadge) {
                        zhihuBadge.innerText = "已连接";
                        zhihuBadge.className = "status-badge badge-success";
                    }
                    if (zhihuDesc) zhihuDesc.innerText = `已同步 ${res.count || 0} 个 Cookie 凭证（含 z_c0）`;
                } else {
                    if (zhihuBadge) {
                        zhihuBadge.innerText = "缺 z_c0";
                        zhihuBadge.className = "status-badge badge-warn";
                    }
                    if (zhihuDesc) {
                        zhihuDesc.innerText = `已同步 ${res.count || 0} 个 Cookie，但未找到 z_c0。请先在当前浏览器打开 www.zhihu.com 登录后再点同步。`;
                    }
                }
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

    // 显示本地直连测试结果日志
    function showRelayLog(text, isSuccess) {
        if (!relayLog) return;
        relayLog.textContent = text;
        relayLog.classList.add("show");
        relayLog.classList.toggle("success", isSuccess);
        relayLog.classList.toggle("fail", !isSuccess);
    }

    // 检测本地直连中继是否正常
    function checkRelay(manual = false) {
        if (relayBadge) {
            relayBadge.innerText = manual ? "测试中..." : "检测中...";
            relayBadge.className = "status-badge badge-warn";
        }
        if (relayLog && !manual) relayLog.classList.remove("show");

        chrome.runtime.sendMessage({ action: "PING_LOCAL_RELAY" }, (res) => {
            if (chrome.runtime.lastError) {
                if (relayBadge) {
                    relayBadge.innerText = "未就绪";
                    relayBadge.className = "status-badge badge-fail";
                }
                if (relayDesc) relayDesc.innerText = "扩展后台服务工作异常，请重新加载扩展。";
                showRelayLog("错误：无法与扩展后台通信。" + chrome.runtime.lastError.message, false);
                return;
            }

            if (res && res.success) {
                if (relayBadge) {
                    relayBadge.innerText = "已就绪";
                    relayBadge.className = "status-badge badge-success";
                }
                if (relayDesc) relayDesc.innerText = "本地住宅 IP 直连中继正常，抓取时会走本机网络。";
                if (manual) {
                    showRelayLog(`本地直连测试成功（HTTP ${res.status || "OK"}）。抓取请求将由当前电脑和家庭宽带直接发出。`, true);
                }
            } else {
                if (relayBadge) {
                    relayBadge.innerText = "未就绪";
                    relayBadge.className = "status-badge badge-fail";
                }
                if (relayDesc) relayDesc.innerText = "本地直连中继异常，请检查本机网络或扩展权限。";
                showRelayLog(`本地直连测试失败：${res && res.error ? res.error : "未知错误"}`, false);
            }
        });
    }

    if (btnSyncZhihu) btnSyncZhihu.addEventListener("click", syncZhihu);
    if (btnSyncWeibo) btnSyncWeibo.addEventListener("click", syncWeibo);
    if (btnTestRelay) btnTestRelay.addEventListener("click", () => checkRelay(true));
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

    // 初始自动检测：凭证 + 本地直连状态
    syncZhihu();
    syncWeibo();
    checkRelay();
});
