'use strict';
/**
 * HLS 服务端下载
 *
 * 流程：解析播放地址 → 取 m3u8 → 下载全部分片 → AES-128 解密 → 按顺序拼接
 *      → 以 Content-Disposition: attachment 流式吐给浏览器。
 *
 * 为什么放在 Node 侧而不是 Cloudflare Worker：
 *   Worker 免费档有 CPU 时间与请求时长限制，一部剧动辄 50+ 分片、
 *   上百 MB，在边缘跑必然超限。本地 Node 没有这些限制，且能边下边吐，
 *   内存占用只跟并发窗口有关（约 4 个分片）。
 */

const crypto = require('crypto');

const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36';
const REFERER = 'https://huangguoai.com/';

// 并发下载窗口。太大容易触发 CDN 限流，太小则慢。
const CONCURRENCY = 4;
const RETRY = 3;

// ---------------------------------------------------------------- 工具

function fetchWithTimeout(url, opts = {}, ms = 30000) {
  return fetch(url, {
    ...opts,
    headers: {
      'User-Agent': UA,
      Referer: REFERER,
      ...(opts.headers || {}),
    },
    signal: AbortSignal.timeout(ms),
  });
}

async function fetchBuffer(url, ms) {
  let lastErr;
  for (let i = 0; i < RETRY; i++) {
    try {
      const r = await fetchWithTimeout(url, {}, ms);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return Buffer.from(await r.arrayBuffer());
    } catch (e) {
      lastErr = e;
      // 指数退避：300ms / 900ms / 2700ms
      await new Promise((r) => setTimeout(r, 300 * Math.pow(3, i)));
    }
  }
  throw lastErr || new Error('fetch failed: ' + url);
}

/**
 * 还原被 /raw 代理包了一层的地址。
 *
 * m3u8 是经 Worker 的 /raw 取回来的，Worker 会把分片/密钥 URI 重写成
 * `/raw?url=<原始地址>`。下载端若照单全收，每个分片都要再绕一次本地代理，
 * 实测把 65 段（149MB）的耗时从 5 秒拉到 65 秒。
 * 这里直接把原始地址解出来，由 Node 直连 CDN。
 */
function unwrapProxy(u) {
  const m = /[?&]url=([^&]+)/.exec(String(u || ''));
  if (!m) return u;
  try {
    const inner = decodeURIComponent(m[1]);
    if (/^https?:\/\//i.test(inner)) return inner;
  } catch (e) {
    /* 解码失败就原样返回 */
  }
  return u;
}

// ---------------------------------------------------------------- m3u8 解析

/**
 * 解析 m3u8。返回 { segments:[{url,seq}], key:{uri,iv}|null, init:url|null, duration }
 * 仅支持 VOD（点播），不支持直播滑动窗口。
 */
function parseM3U8(text, baseUrl) {
  const lines = text.split(/\r?\n/);
  const segments = [];
  let key = null;
  let init = null;
  let duration = 0;
  let seq = 0;
  let pendingInf = null;

  const abs = (u) => {
    let href = u;
    try {
      href = new URL(u, baseUrl).href;
    } catch (e) {
      href = u;
    }
    return unwrapProxy(href);
  };

  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;

    if (line.startsWith('#EXT-X-MEDIA-SEQUENCE:')) {
      seq = Number(line.split(':')[1]) || 0;
      continue;
    }
    if (line.startsWith('#EXT-X-KEY:')) {
      const attrs = line.slice(11);
      const method = (/METHOD=([A-Z0-9-]+)/i.exec(attrs) || [])[1] || 'NONE';
      if (method === 'NONE') {
        key = null;
      } else {
        const uri = (/URI="([^"]+)"/.exec(attrs) || [])[1] || '';
        const ivHex = (/IV=0x([0-9a-fA-F]+)/.exec(attrs) || [])[1] || '';
        key = {
          method,
          uri: uri ? abs(uri) : '',
          iv: ivHex ? Buffer.from(ivHex.padStart(32, '0').slice(0, 32), 'hex') : null,
        };
      }
      continue;
    }
    if (line.startsWith('#EXT-X-MAP:')) {
      const uri = (/URI="([^"]+)"/.exec(line) || [])[1] || '';
      if (uri) init = abs(uri);
      continue;
    }
    if (line.startsWith('#EXTINF:')) {
      pendingInf = parseFloat(line.slice(8)) || 0;
      duration += pendingInf;
      continue;
    }
    if (line.startsWith('#')) continue;

    // 普通行 = 分片地址
    segments.push({ url: abs(line), seq: seq + segments.length });
    pendingInf = null;
  }

  return { segments, key, init, duration };
}

/** master playlist 里挑码率最高的一路 */
function pickVariant(text, baseUrl) {
  const lines = text.split(/\r?\n/);
  let best = null;
  let bestBw = -1;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line.startsWith('#EXT-X-STREAM-INF:')) continue;
    const bw = Number((/BANDWIDTH=(\d+)/.exec(line) || [])[1] || 0);
    for (let j = i + 1; j < lines.length; j++) {
      const nxt = lines[j].trim();
      if (!nxt || nxt.startsWith('#')) continue;
      if (bw > bestBw) {
        bestBw = bw;
        best = new URL(nxt, baseUrl).href;
      }
      break;
    }
  }
  return best;
}

// ---------------------------------------------------------------- 解密

function decryptSegment(buf, keyBuf, iv) {
  if (!keyBuf) return buf;
  if (buf.length === 0) return buf;
  if (buf.length % 16 !== 0) {
    // 不整除说明不是标准 CBC 密文，原样返回避免产出坏数据
    return buf;
  }
  const d = crypto.createDecipheriv('aes-128-cbc', keyBuf, iv);
  // 关键：HLS 的 AES-128 按 RFC 8216 使用 **PKCS#7 填充**。
  // 这里先关掉自动去填充，自己按最后一个字节裁剪——
  // 因为分片长度可能正好是 16 的倍数，自动去填充遇到非法填充会抛异常，
  // 而手动判断可以安全地容忍不规范的分片。
  d.setAutoPadding(false);
  const out = Buffer.concat([d.update(buf), d.final()]);
  return stripPKCS7(out);
}

/**
 * 去掉 PKCS#7 填充。
 * 若尾部不符合填充规则则原样返回（有些源站的分片本身就不填充）。
 */
function stripPKCS7(buf) {
  const len = buf.length;
  if (!len) return buf;
  const pad = buf[len - 1];
  if (pad < 1 || pad > 16 || pad > len) return buf;
  for (let i = len - pad; i < len; i++) {
    if (buf[i] !== pad) return buf;
  }
  return buf.subarray(0, len - pad);
}

/** 没有显式 IV 时，按 RFC 8216 用媒体序列号做大端 128 位 IV */
function ivFromSeq(seq) {
  const b = Buffer.alloc(16);
  b.writeUInt32BE(seq >>> 0, 12);
  return b;
}

// ---------------------------------------------------------------- 主流程

/**
 * 解析出可下载的流信息。resolveStream 由调用方注入（复用 Worker 的 /api/play 逻辑）。
 */
async function resolve(streamUrl) {
  let url = unwrapProxy(streamUrl);
  let text = '';

  for (let hop = 0; hop < 3; hop++) {
    const r = await fetchWithTimeout(url, {}, 20000);
    if (!r.ok) throw new Error('拉取播放列表失败：HTTP ' + r.status);
    text = await r.text();
    if (!text.trimStart().startsWith('#EXTM3U')) {
      throw new Error('返回内容不是 m3u8（可能是防盗链或已过期）');
    }
    if (/#EXT-X-STREAM-INF/i.test(text)) {
      const v = pickVariant(text, url);
      if (!v) break;
      url = v;
      continue;
    }
    break;
  }

  const parsed = parseM3U8(text, url);
  if (!parsed.segments.length) throw new Error('播放列表里没有分片');

  let keyBuf = null;
  if (parsed.key && parsed.key.method === 'AES-128' && parsed.key.uri) {
    keyBuf = await fetchBuffer(parsed.key.uri, 20000);
    if (keyBuf.length !== 16) throw new Error('密钥长度异常：' + keyBuf.length);
  }

  return { ...parsed, keyBuf, playlistUrl: url };
}

/** 按顺序产出解密后的分片（带并发预取） */
async function* segmentStream(info) {
  const { segments, keyBuf, key } = info;

  if (info.init) {
    // fMP4 初始化段（本站在用不到，兼容性保留）
    yield await fetchBuffer(info.init, 30000);
  }

  let next = 0;
  const inflight = new Map();

  const start = (i) => {
    const seg = segments[i];
    const iv = (key && key.iv) || ivFromSeq(seg.seq);
    inflight.set(
      i,
      fetchBuffer(seg.url, 60000).then((raw) => decryptSegment(raw, keyBuf, iv))
    );
  };

  // 预填窗口
  while (next < segments.length && inflight.size < CONCURRENCY) {
    start(next);
    next++;
  }

  for (let i = 0; i < segments.length; i++) {
    const p = inflight.get(i);
    if (!p) start(i);
    const buf = await inflight.get(i);
    inflight.delete(i);
    while (next < segments.length && inflight.size < CONCURRENCY) {
      start(next);
      next++;
    }
    yield buf;
  }
}

/** 写入并处理背压 */
function writeAsync(res, buf) {
  return new Promise((resolve, reject) => {
    if (res.write(buf)) return resolve();
    const onDrain = () => {
      res.off('error', onErr);
      resolve();
    };
    const onErr = (e) => {
      res.off('drain', onDrain);
      reject(e);
    };
    res.once('drain', onDrain);
    res.once('error', onErr);
  });
}

function sanitizeFilename(name) {
  return String(name || 'video')
    .replace(/[\\/:*?"<>|\r\n\t]/g, '_')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 80) || 'video';
}

/**
 * 处理一次下载请求。
 * @param {object} ctx
 * @param {http.IncomingMessage} ctx.req
 * @param {http.ServerResponse} ctx.res
 * @param {URL} ctx.url
 * @param {(pageUrl:string, ep:string)=>Promise<object>} ctx.resolveStream 复用 Worker 的 play()
 * @param {(id:string)=>Promise<object>} ctx.getDetail 复用 Worker 的 detail()（取剧名做文件名）
 */
async function handleDownload({ req, res, url, resolveStream, getDetail }) {
  const pageUrl = url.searchParams.get('url') || '';
  const ep = url.searchParams.get('ep') || '1';
  const probeOnly = url.searchParams.get('probe') === '1';

  if (!pageUrl) {
    return sendJSON(res, 400, { error: '缺少 url 参数' });
  }

  let info;
  let meta;
  try {
    // 复用 Worker 的 play()，它返回的是 { stream, declared, title, ... }
    meta = await resolveStream(pageUrl, ep);
    const stream = meta && (meta.stream || meta.url);
    if (!stream) {
      return sendJSON(res, 404, { error: '未解析到播放地址' });
    }
    meta.stream = stream;
    if (!/m3u8/i.test(stream)) {
      // 直接就是 mp4：交给浏览器即可，不重复中转
      return sendJSON(res, 200, { direct: stream, note: '该集为 mp4，可直接保存' });
    }
    info = await resolve(stream);
  } catch (e) {
    return sendJSON(res, 502, { error: String((e && e.message) || e) });
  }

  // 上游 /api/videos/{id}/play 的 title 只有「第1集」，需要另外补剧名；
  // 同时借详情接口校验集数是否真实存在——play() 在集数越界时会退回抓页面，
  // 抓到的其实是第 1 集的流（上游 /play?ep=999 返回「该集不存在或已下架」），
  // 不校验的话用户会以为下到了第 999 集，实际拿到的是第 1 集。
  const detailId = meta.id || detailIdFromUrl(pageUrl);
  const detail = await lookupDetail(detailId, getDetail);
  const eps = (detail && detail.episodes) || [];
  if (eps.length) {
    const hit = eps.some((e) => String(e.ep) === String(ep));
    if (!hit) {
      return sendJSON(res, 404, {
        error: `第 ${ep} 集不存在，该剧共 ${eps.length} 集`,
        episodes: eps.length,
      });
    }
  }
  const seriesName = (detail && detail.vod_name) || meta.title || meta.id || 'video';
  const filename = `${sanitizeFilename(seriesName)}_第${ep}集.ts`;

  if (probeOnly) {
    return sendJSON(res, 200, {
      id: meta.id,
      ep,
      title: meta.title,
      duration: Math.round(info.duration * 10) / 10,
      segments: info.segments.length,
      encrypted: !!info.keyBuf,
      keyMethod: info.key ? info.key.method : 'NONE',
      filename,
    });
  }

  // 先取第一个分片：成功后再发响应头，失败还能返回 JSON 错误
  let firstBuf;
  try {
    const seg = info.segments[0];
    const iv = (info.key && info.key.iv) || ivFromSeq(seg.seq);
    firstBuf = decryptSegment(await fetchBuffer(seg.url, 60000), info.keyBuf, iv);
  } catch (e) {
    return sendJSON(res, 502, { error: '首个分片下载失败：' + String((e && e.message) || e) });
  }

  const asciiName = filename.replace(/[^\x20-\x7e]/g, '_');
  res.writeHead(200, {
    'content-type': 'video/mp2t',
    'content-disposition': `attachment; filename="${asciiName}"; filename*=UTF-8''${encodeURIComponent(filename)}`,
    'cache-control': 'no-store',
    'x-segments': String(info.segments.length),
    'x-duration': String(Math.round(info.duration * 10) / 10),
  });

  try {
    await writeAsync(res, firstBuf);
    for await (const buf of segmentStream({ ...info, segments: info.segments.slice(1) })) {
      if (res.destroyed) return;
      await writeAsync(res, buf);
    }
    res.end();
  } catch (e) {
    // 头已发出，无法改状态码；直接断开，让浏览器把这次下载标记为失败
    console.error('[download] 中断：', (e && e.message) || e);
    res.destroy();
  }
}

/**
 * 取剧名用于文件名。优先问 Worker 的 /api/detail，失败则退回页面 URL 里的 id。
 * 结果做 10 分钟缓存，避免同一次批量下载反复请求。
 */
const detailCache = new Map();
async function lookupDetail(id, getDetail) {
  if (!id || typeof getDetail !== 'function') return null;
  const hit = detailCache.get(id);
  if (hit && Date.now() - hit.at < 10 * 60 * 1000) return hit.value;
  try {
    const d = await getDetail(id);
    const item = d && d.list && d.list[0];
    if (item) {
      detailCache.set(id, { value: item, at: Date.now() });
      return item;
    }
  } catch (e) {
    /* 忽略，退回 id */
  }
  return null;
}

/** 从页面 URL 反推详情 id：吃瓜内容走 /detail/{n}/，对应 archives_{n} */
function detailIdFromUrl(pageUrl) {
  const m = /\/detail\/(\d+)/.exec(String(pageUrl || ''));
  return m ? 'archives_' + m[1] : '';
}

function sendJSON(res, status, data) {
  const body = JSON.stringify(data);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'content-length': Buffer.byteLength(body),
  });
  res.end(body);
}

module.exports = { handleDownload, parseM3U8, decryptSegment, stripPKCS7, ivFromSeq, sanitizeFilename, detailIdFromUrl, unwrapProxy };
