'use strict';

const S = { sid: null, pages: [], cur: 0, boxes: [] }; // boxes は PDF座標(pt)で保持

const $ = (id) => document.getElementById(id);
const drop = $('drop'), app = $('app'), overlay = $('overlay'), img = $('pageimg');

/* ---------- アップロード ---------- */
$('file').addEventListener('change', (e) => e.target.files[0] && upload(e.target.files[0]));
['dragenter', 'dragover'].forEach(t => drop.addEventListener(t, e => {
  e.preventDefault(); drop.classList.add('over');
}));
['dragleave', 'drop'].forEach(t => drop.addEventListener(t, e => {
  e.preventDefault(); drop.classList.remove('over');
}));
drop.addEventListener('drop', e => {
  const f = e.dataTransfer.files[0];
  if (f) upload(f);
});

async function upload(file) {
  msg('読み込み中…');
  const res = await fetch('/upload', { method: 'POST', body: file });
  const j = await res.json();
  if (!res.ok) return msg(j.error || '読み込みに失敗しました');
  S.sid = j.sid; S.pages = j.pages; S.cur = 0; S.boxes = [];
  drop.hidden = true; app.hidden = false;
  msg(''); render();
}

/* ---------- 表示 ---------- */
function render() {
  img.src = `/page/${S.cur}?sid=${S.sid}&t=${Date.now()}`;
  $('pageinfo').textContent = `${S.cur + 1} / ${S.pages.length}`;
  $('prev').disabled = S.cur === 0;
  $('next').disabled = S.cur === S.pages.length - 1;
  drawBoxes();
}
img.addEventListener('load', drawBoxes);
window.addEventListener('resize', drawBoxes);

// 表示px ↔ PDF pt の倍率
function ratio() {
  return img.clientWidth / S.pages[S.cur].w;
}

function drawBoxes() {
  overlay.querySelectorAll('.box').forEach(n => n.remove());
  const r = ratio();
  S.boxes.forEach((b, i) => {
    if (b.page !== S.cur) return;
    const el = document.createElement('div');
    el.className = 'box';
    el.style.left = b.x0 * r + 'px';
    el.style.top = b.y0 * r + 'px';
    el.style.width = (b.x1 - b.x0) * r + 'px';
    el.style.height = (b.y1 - b.y0) * r + 'px';
    const x = document.createElement('div');
    x.className = 'x'; x.textContent = '×'; x.title = 'この黒塗りを削除';
    x.addEventListener('mousedown', ev => {
      ev.stopPropagation(); ev.preventDefault();
      S.boxes.splice(i, 1); drawBoxes(); count();
    });
    el.appendChild(x);
    overlay.appendChild(el);
  });
  count();
}

const count = () => $('count').textContent = `${S.boxes.length}個の領域`;
const msg = (t) => $('msg').textContent = t;

/* ---------- ドラッグで矩形作成 ---------- */
let draft = null, start = null;
overlay.addEventListener('mousedown', e => {
  if (e.button !== 0) return;
  const r = overlay.getBoundingClientRect();
  start = { x: e.clientX - r.left, y: e.clientY - r.top };
  draft = document.createElement('div');
  draft.className = 'box draft';
  overlay.appendChild(draft);
  e.preventDefault();
});
window.addEventListener('mousemove', e => {
  if (!draft) return;
  const r = overlay.getBoundingClientRect();
  const x = Math.min(Math.max(e.clientX - r.left, 0), r.width);
  const y = Math.min(Math.max(e.clientY - r.top, 0), r.height);
  Object.assign(draft.style, {
    left: Math.min(start.x, x) + 'px', top: Math.min(start.y, y) + 'px',
    width: Math.abs(x - start.x) + 'px', height: Math.abs(y - start.y) + 'px',
  });
});
window.addEventListener('mouseup', e => {
  if (!draft) return;
  const r = overlay.getBoundingClientRect();
  const x = Math.min(Math.max(e.clientX - r.left, 0), r.width);
  const y = Math.min(Math.max(e.clientY - r.top, 0), r.height);
  draft.remove(); draft = null;
  const k = 1 / ratio();
  const b = {
    page: S.cur,
    x0: Math.min(start.x, x) * k, y0: Math.min(start.y, y) * k,
    x1: Math.max(start.x, x) * k, y1: Math.max(start.y, y) * k,
  };
  // 誤クリック除け
  if (b.x1 - b.x0 > 2 && b.y1 - b.y0 > 2) S.boxes.push(b);
  drawBoxes();
});

/* ---------- ツールバー ---------- */
$('prev').onclick = () => { if (S.cur > 0) { S.cur--; render(); } };
$('next').onclick = () => { if (S.cur < S.pages.length - 1) { S.cur++; render(); } };
$('undo').onclick = () => { S.boxes.pop(); drawBoxes(); };
$('clearPage').onclick = () => { S.boxes = S.boxes.filter(b => b.page !== S.cur); drawBoxes(); };

document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  if (e.key === 'ArrowLeft') $('prev').click();
  if (e.key === 'ArrowRight') $('next').click();
  if ((e.metaKey || e.ctrlKey) && e.key === 'z') { e.preventDefault(); $('undo').click(); }
});

$('searchBtn').onclick = async () => {
  const term = $('term').value.trim();
  if (!term) return;
  msg('検索中…');
  const res = await fetch('/search', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sid: S.sid, term }),
  });
  const j = await res.json();
  if (!res.ok) return msg(j.error);
  // 少し余白を足して確実に覆う
  j.hits.forEach(h => S.boxes.push({
    page: h.page, x0: h.x0 - 1, y0: h.y0 - 1, x1: h.x1 + 1, y1: h.y1 + 1,
  }));
  msg(`「${term}」を ${j.hits.length}件 追加`);
  drawBoxes();
};

$('apply').onclick = async () => {
  if (!S.boxes.length && !$('raster').checked) return msg('黒塗り範囲がありません');
  msg('処理中…');
  const res = await fetch('/apply', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sid: S.sid, boxes: S.boxes, rasterize: $('raster').checked }),
  });
  if (!res.ok) { const j = await res.json().catch(() => ({})); return msg(j.error || '失敗'); }
  const blob = await res.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'redacted.pdf';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  msg('保存しました（ダウンロードフォルダ）');
};
