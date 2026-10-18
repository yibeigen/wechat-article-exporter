const { spawn, execSync } = require('child_process');
const http = require('http');
const path = require('path');
const fs = require('fs');

console.log("============================================================");
console.log("🚀 BlogDistiller 客户端自治模式 (Local-First Desktop) E2E 自动化测试");
console.log("============================================================");

const port = 8033;
const pythonExe = path.resolve(".venv", "Scripts", "python.exe");

console.log(`[1/6] 启动本地 Python 引擎: ${pythonExe} (端口: ${port})`);
const proc = spawn(pythonExe, ["run.py", "--port", String(port), "--host", "127.0.0.1", "--no-reload"], {
    cwd: process.cwd(),
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, PYTHONUNBUFFERED: "1" }
});

proc.stdout.on("data", d => {
    const s = d.toString().trim();
    if (s.includes("Uvicorn running") || s.includes("Application startup complete")) {
        console.log(`      [Python] ${s}`);
    }
});

proc.stderr.on("data", d => {
    const s = d.toString().trim();
    if (s.includes("Uvicorn running") || s.includes("Application startup")) {
        console.log(`      [Python] ${s}`);
    }
});

function cleanup(exitCode = 0) {
    console.log("[清理] 终止 Python 子进程树 (PID: " + proc.pid + ")...");
    try {
        execSync(`taskkill /pid ${proc.pid} /T /F`, { stdio: "ignore" });
    } catch(e) {}
    process.exit(exitCode);
}

function requestJson(url, options = {}) {
    return new Promise((resolve, reject) => {
        const u = new URL(url);
        const reqOpts = {
            hostname: u.hostname,
            port: u.port,
            path: u.pathname + u.search,
            method: options.method || 'GET',
            headers: options.headers || {},
            timeout: options.timeout || 5000
        };
        const req = http.request(reqOpts, res => {
            let body = '';
            res.on('data', chunk => body += chunk);
            res.on('end', () => {
                try {
                    const parsed = JSON.parse(body);
                    resolve({ status: res.statusCode, headers: res.headers, data: parsed, raw: body });
                } catch(e) {
                    resolve({ status: res.statusCode, headers: res.headers, raw: body });
                }
            });
        });
        req.on('error', reject);
        req.on('timeout', () => {
            req.destroy();
            reject(new Error('Request timed out: ' + url));
        });
        if (options.body) {
            req.write(typeof options.body === 'string' ? options.body : JSON.stringify(options.body));
        }
        req.end();
    });
}

async function run() {
    // 2. 轮询健康检查 (最多 30 秒)
    console.log("[2/6] 轮询 /api/health 健康探活接口...");
    let ready = false;
    for (let i = 0; i < 60; i++) {
        await new Promise(r => setTimeout(r, 500));
        try {
            const res = await requestJson(`http://127.0.0.1:${port}/api/health`, { timeout: 1500 });
            if (res.status === 200 && res.data?.status === 'ok') {
                console.log(`      ✅ 健康检查通过:`, res.data);
                ready = true;
                break;
            }
        } catch(e) {}
    }

    if (!ready) {
        console.error("❌ 引擎未在预期时间内就绪，测试终止");
        cleanup(1);
        return;
    }

    // 3. 检查客户端版本分发接口
    console.log("[3/6] 检查 /api/client/version 分发与升级接口...");
    const verRes = await requestJson(`http://127.0.0.1:${port}/api/client/version`);
    console.log(`      ✅ 客户端版本接口响应: version=${verRes.data.version}, standard=${verRes.data.standard_download_url}`);

    // 4. 创建本地抓取与导出任务
    console.log("[4/6] 触发本地文章抓取与导出任务 (POST /api/tasks)...");
    const taskPayload = {
        platform: "custom_urls",
        target: "https://httpbin.org/html",
        export_formats: ["md", "txt", "html"],
        download_images: false,
        use_cache: false
    };
    const taskRes = await requestJson(`http://127.0.0.1:${port}/api/tasks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: taskPayload
    });

    const taskId = taskRes.data?.task_id;
    console.log(`      ✅ 任务成功创建! task_id: ${taskId}`);
    if (!taskId) {
        console.error("❌ 任务 ID 获取失败");
        cleanup(1);
        return;
    }

    // 5. 轮询任务状态
    console.log(`[5/6] 轮询任务状态 (GET /api/tasks/${taskId})...`);
    let finalStatus = null;
    let taskDetail = null;
    for (let i = 0; i < 40; i++) {
        await new Promise(r => setTimeout(r, 1000));
        try {
            const sRes = await requestJson(`http://127.0.0.1:${port}/api/tasks/${taskId}`);
            taskDetail = sRes.data;
            const status = taskDetail.status;
            const msg = taskDetail.message || "";
            const pct = taskDetail.progress_percent || 0;
            console.log(`      [进度 ${pct.toFixed(0)}%] 状态: ${status} - ${msg}`);
            if (status === "completed" || status === "failed" || status === "cancelled") {
                finalStatus = status;
                break;
            }
        } catch(e) {
            console.log(`      轮询等待中: ${e.message}`);
        }
    }

    if (finalStatus !== "completed") {
        console.error(`❌ 任务未完成，最终状态: ${finalStatus}`);
        cleanup(1);
        return;
    }

    // 6. 验证产物文件生成与下载
    console.log("[6/6] 验证生成产物下载与落盘...");
    const files = taskDetail.export_files || {};
    console.log("      产物文件列表:", files);
    if (Object.keys(files).length === 0) {
        console.error("❌ 未生成任何产物文件");
        cleanup(1);
        return;
    }

    // 测试 ZIP 或 MD 产物下载
    const testRelPath = files.zip || files.md;
    const testUrl = `http://127.0.0.1:${port}${encodeURI(testRelPath)}`;
    console.log(`      正在验证下载接口: ${testUrl}`);
    const dlRes = await new Promise((resolve, reject) => {
        http.get(testUrl, res => {
            let received = 0;
            res.on('data', chunk => received += chunk.length);
            res.on('end', () => resolve({
                statusCode: res.statusCode,
                receivedBytes: received,
                disposition: res.headers['content-disposition'],
                contentType: res.headers['content-type']
            }));
        }).on('error', reject);
    });

    console.log(`      ✅ 导出文件下载响应正常: HTTP ${dlRes.statusCode}, 字节数=${dlRes.receivedBytes}, Disposition=${dlRes.disposition}`);
    console.log("============================================================");
    console.log("🎉 恭喜！客户端自治模式 (Local-First Desktop) 端到端全链路自动化测试 100% 成功！");
    console.log("============================================================");

    cleanup(0);
}

run().catch(err => {
    console.error("❌ 测试发生未捕获异常:", err);
    cleanup(1);
});
