#!/usr/bin/env node
/**
 * 黄果解析 · 后端服务（API）
 *
 * 职责：把 worker/index.js（原本是 Cloudflare Workers 的 fetch handler）
 *      挂到 Node 内置 http 服务上，对外提供 /api/*、/media、/raw、/download。
 *
 * 部署形态：
 *   Docker   前端 nginx 占 7731，本进程占 7732；nginx 把 API 请求反代过来。
 *   裸机     `node server.js` 同时提供静态页与 API（SERVE_STATIC 默认开启）。
 *
 * 依赖：Node 18+（需要全局 fetch / Response / Request / crypto.subtle）。
 *
 * 用法：
 *      node server.js                  # 默认 0.0.0.0:7732
 *      PORT=9000 node server.js
 *      SERVE_STATIC=0 node server.js   # 只跑 API，静态页交给 nginx
 */
'use strict';

const http = require('http');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const PORT = Number(process.env.PORT || process.argv[2] || 7732);
const HOST = process.env.HOST || '0.0.0.0';
// 默认开启静态托管：裸机 `node server.js` 就能直接用。
// Docker 里由 nginx 负责静态文件，设 SERVE_STATIC=0 关闭，避免两份职责重叠。
const SERVE_STATIC = process.env.SERVE_STATIC !== '0';

const ROOT = __dirname;
const PUBLIC_DIR = path.join(ROOT, 'public');
const WORKER_ENTRY = path.join(ROOT, 'worker', 'index.js');

// ---------------------------------------------------------------- 加载 Worker
// Worker 用 addEventListener('fetch', ...) 注册处理器，这里捕获它。
let fetchHandler = null;
const sandbox = {
  console,
  crypto: globalThis.crypto,
  Response,
  Request,
  URL,
  URLSearchParams,
  Headers,
  TextDecoder,
  TextEncoder,
  Uint8Array,
  ArrayBuffer,
  fetch,
  setTimeout,
  clearTimeout,
  atob,
  btoa,
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;
sandbox.addEventListener = (type, fn) => {
  if (type === 'fetch') fetchHandler = fn;
};

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(WORKER_ENTRY, 'utf8'), sandbox, { filename: WORKER_ENTRY });

if (typeof fetchHandler !== 'function') {
  console.error('[fatal] worker/index.js 未注册 fetch 处理器');
  process.exit(1);
}

// 复用 Worker 里已有的 play() / detail()，避免重复实现一遍解析逻辑。
// 下载接口拿到的 stream 与播放页完全一致。
const resolveStream = (pageUrl, ep) => sandbox.play(pageUrl, ep);
const getDetail = (id) => sandbox.detail(String(id));

const { handleDownload } = require('./lib/download');

// ---------------------------------------------------------------- 静态文件
const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.gif': 'image/gif',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
  '.map': 'application/json; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
};

function serveStatic(req, res, pathname) {
  let rel = decodeURIComponent(pathname);
  if (rel === '/' || rel === '') rel = '/index.html';

  // 防目录穿越：解析后必须仍在 PUBLIC_DIR 内
  const target = path.resolve(PUBLIC_DIR, '.' + rel);
  if (target !== PUBLIC_DIR && !target.startsWith(PUBLIC_DIR + path.sep)) {
    res.writeHead(403, { 'content-type': 'text/plain; charset=utf-8' });
    res.end('403 Forbidden');
    return;
  }

  fs.stat(target, (err, st) => {
    if (err || !st.isFile()) {
      // 无扩展名时回退到同名 .html（例如 /play -> play.html）
      if (!path.extname(target)) {
        const alt = target + '.html';
        if (fs.existsSync(alt)) return sendFile(res, alt);
      }
      res.writeHead(404, { 'content-type': 'text/html; charset=utf-8' });
      res.end('<h1>404</h1><p>页面不存在：' + escapeHtml(pathname) + '</p><p><a href="/">返回首页</a></p>');
      return;
    }
    sendFile(res, target, st);
  });
}

function sendFile(res, file, st) {
  const ext = path.extname(file).toLowerCase();
  const type = MIME[ext] || 'application/octet-stream';
  const headers = { 'content-type': type, 'cache-control': 'no-cache' };
  if (st) headers['content-length'] = st.size;
  res.writeHead(200, headers);
  fs.createReadStream(file).pipe(res);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ---------------------------------------------------------------- HTTP 服务
const server = http.createServer(async (req, res) => {
  const host = req.headers.host || `${HOST}:${PORT}`;
  const url = new URL(req.url, `http://${host}`);
  const pathname = url.pathname;

  // 健康检查：给 docker healthcheck / 反向代理探活用
  if (pathname === '/healthz') {
    const body = JSON.stringify({ status: 'ok', uptime: Math.round(process.uptime()), port: PORT });
    res.writeHead(200, { 'content-type': 'application/json; charset=utf-8', 'content-length': Buffer.byteLength(body) });
    return res.end(body);
  }

  // 下载接口：走 Node 侧流式处理，不进 Worker（Worker 免费档撑不住长任务）。
  // 必须放在静态文件分支之前，否则会被当成 public/download 去找。
  if (pathname === '/download') {
    try {
      return await handleDownload({ req, res, url, resolveStream, getDetail });
    } catch (e) {
      console.error('[download error]', (e && e.stack) || e);
      if (!res.headersSent) {
        res.writeHead(500, { 'content-type': 'application/json; charset=utf-8' });
      }
      return res.end(JSON.stringify({ error: String((e && e.message) || e) }));
    }
  }

  // 这三类交给 Worker（含 /api/*、封面解密、m3u8 重写代理）
  // 注意用 '/api/' 而不是 '/api'，否则 /api.html 这个文档页会被 Worker 抢走
  const isWorkerRoute =
    pathname === '/api' ||
    pathname.startsWith('/api/') ||
    pathname === '/media' ||
    pathname === '/raw' ||
    pathname === '/hls.js'; // 兼容旧前端；本地已有 vendor/hls.min.js，一般不会走到

  if (!isWorkerRoute) {
    if (!SERVE_STATIC) {
      res.writeHead(404, { 'content-type': 'application/json; charset=utf-8' });
      return res.end(JSON.stringify({ error: '静态页由前端服务提供，本端口只处理 API' }));
    }
    return serveStatic(req, res, pathname);
  }

  let pending;
  try {
    await fetchHandler({
      request: new Request(`http://${host}${req.url}`, {
        method: req.method,
        headers: req.headers,
      }),
      respondWith: (p) => {
        pending = p;
      },
    });
    const response = await pending;
    const buf = Buffer.from(await response.arrayBuffer());
    const headers = {};
    response.headers.forEach((v, k) => {
      headers[k] = v;
    });
    // /raw 的 m3u8 会重写成绝对地址，必须回显真实端口而不是写死 origin
    res.writeHead(response.status, headers);
    res.end(buf);
  } catch (e) {
    const msg = (e && e.stack) || String(e);
    console.error('[worker error]', req.method, req.url, '\n', msg);
    if (!res.headersSent) {
      res.writeHead(500, { 'content-type': 'application/json; charset=utf-8' });
    }
    res.end(JSON.stringify({ error: String((e && e.message) || e) }));
  }
});

server.listen(PORT, HOST, () => {
  console.log('');
  console.log('  黄果解析 · 后端 API');
  console.log('  ────────────────────────────────────────');
  console.log(`  监听      http://${HOST}:${PORT}`);
  console.log(`  健康检查  http://127.0.0.1:${PORT}/healthz`);
  console.log(`  接口自检  http://127.0.0.1:${PORT}/api/`);
  console.log(`  静态页    ${SERVE_STATIC ? '由本进程提供（/）' : '已关闭，交给前端服务'}`);
  console.log('  ────────────────────────────────────────');
  console.log('');
});

server.on('error', (e) => {
  if (e.code === 'EADDRINUSE') {
    console.error(`[fatal] 端口 ${PORT} 已被占用。换一个：PORT=${PORT + 1} node server.js`);
    process.exit(1);
  }
  throw e;
});

// 容器停止时优雅退出，避免正在进行的下载被硬切断
for (const sig of ['SIGTERM', 'SIGINT']) {
  process.on(sig, () => {
    console.log(`\n[${sig}] 正在关闭…`);
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 5000).unref();
  });
}
