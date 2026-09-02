const { contextBridge, ipcRenderer, shell } = require("electron");

contextBridge.exposeInMainWorld("blogDistiller", {
    // 微信中继与连接状态
    getWeChatStatus: () => ipcRenderer.invoke("wechat:get-status"),
    onWeChatStatusChange: (callback) => {
        const handler = (_event, data) => callback(data);
        ipcRenderer.on("wechat:status-change", handler);
        return () => ipcRenderer.removeListener("wechat:status-change", handler);
    },
    onAutoTriggerSearch: (callback) => {
        const handler = (_event, data) => callback(data);
        ipcRenderer.on("wechat:auto-trigger-search", handler);
        return () => ipcRenderer.removeListener("wechat:auto-trigger-search", handler);
    },
    openMpLogin: () => ipcRenderer.invoke("wechat:open-mp-login"),
    onMpStatusChange: (callback) => {
        const handler = (_event, data) => callback(data);
        ipcRenderer.on("wechat:mp-status-change", handler);
        return () => ipcRenderer.removeListener("wechat:mp-status-change", handler);
    },
    triggerSystemProxy: (enable) => ipcRenderer.invoke("wechat:toggle-proxy", enable),

    searchArticles: (params) => ipcRenderer.invoke("wechat:search-articles", params),
    generateProfileUrl: (params) => ipcRenderer.invoke("wechat:generate-profile-url", params),
    scanAlbums: (params) => ipcRenderer.invoke("wechat:scan-albums", params),
    onStreamArticles: (callback) => {
        const handler = (_event, data) => callback(data);
        ipcRenderer.on("wechat:stream-articles", handler);
        return () => ipcRenderer.removeListener("wechat:stream-articles", handler);
    },
    onFetchProgress: (callback) => {
        const handler = (_event, data) => callback(data);
        ipcRenderer.on("wechat:fetch-progress", handler);
        return () => ipcRenderer.removeListener("wechat:fetch-progress", handler);
    },
    onDebugLog: (callback) => {
        const handler = (_event, data) => callback(data);
        ipcRenderer.on("wechat:debug-log", handler);
        return () => ipcRenderer.removeListener("wechat:debug-log", handler);
    },
    retrySingleArticle: (params) => ipcRenderer.invoke("wechat:retry-single", params),

    // 导出与逐篇状态追踪
    exportArticles: (params) => ipcRenderer.invoke("export:start", params),
    onExportProgress: (callback) => {
        const handler = (_event, data) => callback(data);
        ipcRenderer.on("export:progress", handler);
        return () => ipcRenderer.removeListener("export:progress", handler);
    },
    onArticleStatus: (callback) => {
        const handler = (_event, data) => callback(data);
        ipcRenderer.on("export:article-status", handler);
        return () => ipcRenderer.removeListener("export:article-status", handler);
    },

    // 文件与外部系统调用
    selectOutputDir: () => ipcRenderer.invoke("fs:select-dir"),
    openOutputDir: (dirPath) => ipcRenderer.invoke("fs:open-dir", dirPath),
    openExternal: (targetUrl) => ipcRenderer.invoke("app:open-external", targetUrl),
    copyToClipboard: (text) => ipcRenderer.invoke("app:copy-clipboard", text),

    // 本地缓存与设置
    getArticleCache: (biz) => ipcRenderer.invoke("cache:get", biz),
    getHistoryAccounts: () => ipcRenderer.invoke("cache:get-all-accounts"),
    getSettings: () => ipcRenderer.invoke("settings:get"),
    saveSettings: (settings) => ipcRenderer.invoke("settings:save", settings)
});
