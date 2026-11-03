// 黄果解析站 · 共享脚本
// 本地版：静态页与 Worker 由同一个 server.js 提供，故同源、留空即可。
// 若你把前端单独放到别的端口/域名，改这里即可（Worker 已开启 CORS）。
const API_BASE = (typeof window !== 'undefined' && window.__API_BASE__) || '';
const PAGE_SIZE = 50; // 一页 50 条

function esc(s) {
  return String(s || '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
async function api(path) {
  const r = await fetch(API_BASE + '/api' + path);
  if (!r.ok) throw Error('HTTP ' + r.status);
  return r.json();
}
function cardHtml(x) {
  const pic = x.vod_pic ? `<img src="${API_BASE}${esc(x.vod_pic)}" loading="lazy" alt="${esc(x.vod_name)}">` : '<span>黄果</span>';
  const score = x.vod_score ? `<i class="score">${esc(String(x.vod_score))}</i>` : '';
  return `<article class="card" data-id="${esc(x.vod_id)}"><div class="cover">${pic}${score}</div><div class="card-body"><h3>${esc(x.vod_name)}</h3><p>${esc(x.vod_remarks || '黄果短剧')}</p></div></article>`;
}
function gridHtml(list) {
  return (list || []).map(cardHtml).join('');
}
// 视频卡片 -> 独立播放页；话题卡片 -> 专辑页
function bindCards(el) {
  el.querySelectorAll('.card').forEach((c) => {
    c.onclick = () => {
      const id = c.dataset.id;
      if (/^topic_/.test(id)) location.href = 'topics.html?slug=' + encodeURIComponent(id.replace('topic_', ''));
      else location.href = 'play.html?id=' + encodeURIComponent(id) + '&ep=1';
    };
  });
}
function fill(el, list, empty) {
  el.innerHTML = list && list.length ? gridHtml(list) : `<div class="empty">${esc(empty || '暂无内容')}</div>`;
  bindCards(el);
}
function chips(el, items, activeKey, onPick) {
  el.innerHTML = (items || []).map((i) => `<button class="${i.v === activeKey ? 'active' : ''}" data-v="${esc(i.v)}">${esc(i.n)}</button>`).join('');
  el.querySelectorAll('button').forEach((b) => {
    b.onclick = () => {
      el.querySelectorAll('button').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      onPick(b.dataset.v);
    };
  });
}
function setStatus(el, t) {
  if (el) el.textContent = t || '';
}
const CATS_FALLBACK = [
  { type_id: 'ai-duanju', type_name: 'AI成人短剧' },
  { type_id: 'ai-manju', type_name: 'AI成人漫剧' },
  { type_id: 'ai-huanlian', type_name: 'AI换脸' },
  { type_id: 'ai-mogai', type_name: 'AI魔改' }
];
const SORTS_FALLBACK = [
  { n: '最新更新', v: 'latest' },
  { n: '当前热播', v: 'hot' },
  { n: '独家原创', v: 'original' },
  { n: '随机推荐', v: 'random' }
];
const RANK_TABS = [
  { n: '热播榜', v: 'hot' },
  { n: '推荐榜', v: 'recommend' },
  { n: '潜力榜', v: 'potential' }
];
const CHIGUA_TABS = [
  { n: '全部', v: 'all' },
  { n: '热门吃瓜', v: 'remen' },
  { n: 'AI原创', v: 'yuanchuang' }
];
async function loadMeta() {
  try {
    const m = await api('/categories');
    return {
      cats: (m.categories && m.categories.length) ? m.categories.filter((c) => ['ai-duanju', 'ai-manju', 'ai-huanlian', 'ai-mogai'].indexOf(c.type_id) >= 0) : CATS_FALLBACK,
      sorts: (m.sorts && m.sorts.length) ? m.sorts : SORTS_FALLBACK
    };
  } catch (e) {
    return { cats: CATS_FALLBACK, sorts: SORTS_FALLBACK };
  }
}
