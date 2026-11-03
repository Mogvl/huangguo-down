#!/usr/bin/env node
/**
 * 自检脚本：确认本地服务与上游数据都正常。
 * 用法：node scripts/check.js [base]
 *      默认 base = http://127.0.0.1:7732
 */
'use strict';

const BASE = (process.argv[2] || process.env.BASE || 'http://127.0.0.1:7732').replace(/\/$/, '');

const cases = [
  { name: '服务存活 /api/', path: '/api/', check: (d) => d.status === 'ok' },
  { name: '分类列表', path: '/api/categories', check: (d) => Array.isArray(d.categories) && d.categories.length > 0 },
  { name: '分类内容（官方接口）', path: '/api/category/ai-duanju?page=1&size=5', check: (d) => d.list && d.list.length > 0 },
  { name: '排行榜', path: '/api/ranks/hot?size=5', check: (d) => d.list && d.list.length > 0 },
  { name: '专辑列表', path: '/api/topics?size=5', check: (d) => d.list && d.list.length > 0 },
  { name: '专辑内容（分页）', path: '/api/topics?slug=hot-aiduanju&page=2&size=5', check: (d) => d.list && d.list.length > 0 },
  { name: '吃瓜列表', path: '/api/chigua?size=5', check: (d) => d.list && d.list.length > 0 },
  { name: '吃瓜分类分页', path: '/api/chigua?cate=remen&page=2&size=5', check: (d) => d.list && d.list.length > 0 },
  { name: '搜索（q 参数修复）', path: '/api/search/%E7%B2%BE%E5%85%B5?size=5', check: (d) => d.list && d.list.length > 0 },
  { name: '详情', path: '/api/detail/5667', check: (d) => d.list && d.list.length > 0 && (d.list[0].episodes || []).length > 0 },
  { name: '播放解析', path: '/api/play?url=https%3A%2F%2Fhuangguoai.com%2Fvideo%2F5667%2F&ep=1', check: (d) => !!d.url },
];

const extra = [
  { name: 'hls.js 本地副本', path: '/vendor/hls.min.js', type: 'raw', expect: (r) => r.status === 200 },
  { name: '首页', path: '/', type: 'raw', expect: (r) => r.status === 200 },
  { name: '播放页', path: '/play.html', type: 'raw', expect: (r) => r.status === 200 },
  { name: 'API 文档页', path: '/api.html', type: 'raw', expect: (r) => r.status === 200 },
  { name: '代理白名单（应 403）', path: '/raw?url=https%3A%2F%2Fexample.com%2F', type: 'raw', expect: (r) => r.status === 403 },
  { name: '下载探针', path: '/download?url=https%3A%2F%2Fhuangguoai.com%2Fvideo%2F5667%2F&ep=1&probe=1', type: 'json', expect: (d) => d.segments > 0 && !!d.filename },
  { name: '下载参数校验（应 400）', path: '/download', type: 'raw', expect: (r) => r.status === 400 },
  { name: '越界集数（应 404）', path: '/download?url=https%3A%2F%2Fhuangguoai.com%2Fvideo%2F5667%2F&ep=999&probe=1', type: 'raw', expect: (r) => r.status === 404 },
];

let pass = 0;
let fail = 0;

async function run() {
  console.log(`\n自检目标：${BASE}\n${'─'.repeat(56)}`);

  for (const c of cases) {
    const t0 = Date.now();
    try {
      const r = await fetch(BASE + c.path, { signal: AbortSignal.timeout(60000) });
      const d = await r.json();
      const ok = r.ok && c.check(d);
      report(ok, c.name, `${Date.now() - t0}ms`);
    } catch (e) {
      report(false, c.name, String(e.message || e).slice(0, 50));
    }
  }

  for (const c of extra) {
    const t0 = Date.now();
    try {
      const r = await fetch(BASE + c.path, { signal: AbortSignal.timeout(120000) });
      const ok =
        c.type === 'json' ? r.ok && c.expect(await r.json()) : c.expect(r);
      report(ok, c.name, `HTTP ${r.status} · ${Date.now() - t0}ms`);
    } catch (e) {
      report(false, c.name, String(e.message || e).slice(0, 50));
    }
  }

  console.log('─'.repeat(56));
  console.log(`  通过 ${pass} · 失败 ${fail}\n`);
  process.exit(fail ? 1 : 0);
}

function report(ok, name, note) {
  if (ok) pass++;
  else fail++;
  console.log(`  ${ok ? '\x1b[32m✓\x1b[0m' : '\x1b[31m✗\x1b[0m'} ${name.padEnd(24, ' ')} ${note || ''}`);
}

run();
