// BlogDistiller WeChat MP Content Script
// 当用户访问微信公众平台后台 (mp.weixin.qq.com) 时，自动提取当前登录 Token 并支持同源直连拉取

(function() {
    console.log("[BlogDistiller] 微信公众平台同源助手已就绪...");

    function extractAndSyncWeChatToken() {
        const urlParams = new URLSearchParams(window.location.search);
        const token = urlParams.get('token');

        if (token) {
            let fakeid = "";
            let accountName = "";

            try {
                const html = document.documentElement.innerHTML;
                const fakeidMatch = html.match(/fakeid\s*[:=]\s*["']([A-Za-z0-9+/=]+)["']/);
                if (fakeidMatch) fakeid = fakeidMatch[1];
                const dataFakeid = document.querySelector('[data-fakeid]');
                if (dataFakeid && !fakeid) fakeid = dataFakeid.getAttribute('data-fakeid') || '';

                const nameEl = document.querySelector('.user_name');
                if (nameEl) accountName = nameEl.textContent.trim();
            } catch (e) {
                console.log("[BlogDistiller] 提取公众号 fakeid/昵称 失败:", e);
            }

            console.log("[BlogDistiller] 成功捕获微信公众平台活跃 Token:", token, "fakeid:", fakeid, "accountName:", accountName);
            chrome.runtime.sendMessage({
                action: "SYNC_WECHAT_NOW",
                token: token,
                fakeid: fakeid,
                account_name: accountName
            }, (response) => {
                if (response && response.success) {
                    console.log("[BlogDistiller] 微信公众平台凭证已自动无感同步至本地导出器！", response);
                }
            });
        }
    }

    // 页面加载完成后提取
    extractAndSyncWeChatToken();

    // 监听 URL 变化
    let lastUrl = location.href;
    new MutationObserver(() => {
        const url = location.href;
        if (url !== lastUrl) {
            lastUrl = url;
            extractAndSyncWeChatToken();
        }
    }).observe(document, { subtree: true, childList: true });

    // 监听来自 Background 的同源文章检索请求 (同源 0 风控执行)
    chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
        if (request.action === "EXECUTE_WECHAT_FETCH_IN_MP_TAB") {
            const target = request.target;
            const maxArticles = request.maxArticles || 0;
            const urlParams = new URLSearchParams(window.location.search);
            const token = urlParams.get('token') || request.token;

            if (!token) {
                sendResponse({ success: false, message: "未能从公众平台页面获取活跃 Token" });
                return true;
            }

            (async () => {
                try {
                    let fakeid = request.fakeid || "";
                    let nickname = target;

                    // 1. 如果未提供 fakeid，则搜索 fakeid (带频控重试)
                    if (!fakeid) {
                        let searchAttempts = 0;
                        while (searchAttempts < 3) {
                            const searchUrl = `/cgi-bin/searchbiz?action=search_biz&begin=0&count=5&query=${encodeURIComponent(target)}&token=${token}&lang=zh_CN&f=json&ajax=1`;
                            const sRes = await fetch(searchUrl);
                            const sData = await sRes.json();
                            if (sData.base_resp) {
                                if (sData.base_resp.ret === 200003) {
                                    return { success: false, message: "微信公众平台登录态已过期，请在 mp.weixin.qq.com 后台刷新并重新扫码登录！" };
                                }
                                if (sData.base_resp.ret === 200013) {
                                    searchAttempts++;
                                    await new Promise(r => setTimeout(r, 1500));
                                    continue;
                                }
                            }
                            const bizList = sData.list || [];
                            if (bizList.length > 0) {
                                fakeid = bizList[0].fakeid;
                                nickname = bizList[0].nickname || target;
                                break;
                            } else {
                                break;
                            }
                        }
                    }

                    if (!fakeid) {
                        return { success: false, message: `在微信公众平台后台未检索到公众号【${target}】，请确认名称是否准确，或稍候重试` };
                    }

                    // 2. 分页遍历
                    let begin = 0;
                    const count = 5;
                    let articles = [];
                    let totalCount = 0;
                    let pageIndex = 1;

                    let freqRetry = 0;
                    while (true) {
                        const listUrl = `/cgi-bin/appmsg?action=list_ex&begin=${begin}&count=${count}&fakeid=${fakeid}&type=9&query=&token=${token}&lang=zh_CN&f=json&ajax=1`;
                        const lRes = await fetch(listUrl, {
                            credentials: "include",
                            headers: {
                                "X-Requested-With": "XMLHttpRequest",
                                "Accept": "application/json, text/javascript, */*; q=0.01"
                            }
                        });
                        const lData = await lRes.json();

                        if (lData.base_resp && lData.base_resp.ret === 200013) {
                            freqRetry++;
                            if (freqRetry >= 3) {
                                if (articles.length > 0) {
                                    return {
                                        success: true,
                                        articles: articles,
                                        author: nickname,
                                        total: totalCount || articles.length,
                                        partial: true,
                                        message: `因触发微信公众平台官方每小时频率保护，已为您自动保留结算当前已拉取的 ${articles.length} 篇历史文章。`
                                    };
                                }
                                return {
                                    success: false,
                                    code: "FREQ_CONTROL",
                                    err_code: 200013,
                                    target: nickname,
                                    fakeid: fakeid,
                                    token: token,
                                    title: "微信公众平台触发官方频控保护 (200013)",
                                    message: "由于当前登录会话在短时间内调用接口频次达到腾讯服务器限制，腾讯官方暂时锁定了当前 Token 的反查他人文章配额。",
                                    steps: [
                                        "切换到已打开的 mp.weixin.qq.com 微信公众平台标签页，点击右上角【退出登录】",
                                        "用手机微信重新扫码登录微信公众号后台（立即获取全新 Token，频控配额瞬间重置为零）",
                                        "回到本页面，刷新后重新点击【🚀 检索文章】"
                                    ],
                                    devInfo: `In-Tab Same-Origin: /cgi-bin/appmsg?action=list_ex | ret: 200013 | Target fakeid: ${fakeid} | Token: ${token}`
                                };
                            }
                            chrome.runtime.sendMessage({
                                action: "WECHAT_FETCH_PROGRESS_FORWARD",
                                current: articles.length,
                                total: totalCount || 0,
                                statusText: `微信触发频控安全等待，第 ${freqRetry}/3 次重试中...`,
                                latestTitles: []
                            });
                            await new Promise(r => setTimeout(r, 2000));
                            continue;
                        }
                        freqRetry = 0;

                        const appMsgList = lData.app_msg_list || [];
                        if (appMsgList.length === 0) break;

                        totalCount = lData.app_msg_cnt || 0;
                        const newTitles = [];

                        for (const msg of appMsgList) {
                            const ts = msg.create_time;
                            let dateStr = "";
                            if (ts) {
                                const d = new Date(ts * 1000);
                                dateStr = d.toISOString().split('T')[0];
                            }
                            articles.push({
                                id: String(msg.aid || msg.link || ''),
                                url: msg.link,
                                title: msg.title,
                                author: nickname,
                                create_time: dateStr
                            });
                            newTitles.push(msg.title);
                            if (maxArticles > 0 && articles.length >= maxArticles) break;
                        }

                        // 发送实时进度心跳
                        chrome.runtime.sendMessage({
                            action: "WECHAT_FETCH_PROGRESS_FORWARD",
                            current: articles.length,
                            total: totalCount,
                            latestTitles: newTitles.slice(0, 3),
                            page: pageIndex
                        });

                        if (maxArticles > 0 && articles.length >= maxArticles) break;
                        begin += count;
                        pageIndex++;
                        // 采用 1.5s ~ 2.3s 的拟人化安全平滑间隔，彻底杜绝触发腾讯高频风控
                        await new Promise(r => setTimeout(r, 1500 + Math.random() * 800));
                    }

                    articles.sort((a, b) => (b.create_time || '').localeCompare(a.create_time || ''));

                    return {
                        success: true,
                        articles: articles,
                        author: nickname,
                        total: totalCount || articles.length
                    };
                } catch (e) {
                    return { success: false, message: "公众平台同源拉取异常: " + e.message };
                }
            })().then(res => sendResponse(res));

            return true;
        }
    });
})();
