/* Speakr「從網址匯入」、「校正姓名」與「繁中翻譯」前端。
 * 欄位在 Vue 模板裡（開關視窗時會被重新建立），所以一律用 document 層級的事件委派。
 * 進度用右下角的浮動清單顯示（掛在 body 上，不受 Vue 重繪影響）；重新整理頁面後會接著追蹤。
 */
(function () {
  'use strict';
  const API = '/url-import/api';
  const STORE = 'speakr-url-import-jobs';
  const jobs = new Map(); // id -> {title, url, state, progress, recording_id, error}
  let box = null;
  let timer = null;

  function load() {
    try { return JSON.parse(localStorage.getItem(STORE) || '[]'); } catch (_) { return []; }
  }
  // 結束 = 失敗，或完成（校正姓名還要等逐字稿套用完）
  const finished = (j) => j.state === 'error' || (j.state === 'done' && (j.kind !== 'namecheck' || j.applied));

  function save() {
    try {
      const active = [...jobs.entries()].filter(([, j]) => !finished(j)).map(([id]) => id);
      localStorage.setItem(STORE, JSON.stringify(active));
    } catch (_) { /* 無痕模式等 */ }
  }

  function ensureBox() {
    if (box && document.body.contains(box)) return box;
    box = document.createElement('div');
    box.id = 'speakr-url-jobs';
    box.style.cssText = 'position:fixed;right:1rem;bottom:1rem;z-index:9999;display:flex;flex-direction:column;' +
      'gap:0.5rem;max-width:22rem;font-size:0.8rem;';
    document.body.appendChild(box);
    return box;
  }

  const LABEL = {
    queued: '排隊中', probing: '讀取影片資訊', downloading: '下載中', converting: '轉成 m4a',
    uploading: '上傳到 Speakr', done: '已加入轉錄佇列', error: '失敗', running: '查證中',
  };

  function render() {
    const root = ensureBox();
    root.replaceChildren();
    for (const [id, j] of jobs) {
      const card = document.createElement('div');
      card.style.cssText = 'padding:0.6rem 0.75rem;border-radius:0.5rem;box-shadow:0 4px 12px rgba(0,0,0,.15);' +
        'background:var(--bg-secondary,#fff);color:var(--text-primary,#111);border:1px solid var(--border-secondary,#ddd);';
      const title = document.createElement('div');
      title.style.cssText = 'font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
      const nc = j.kind === 'namecheck';
      title.textContent = nc ? `校正姓名 · 錄音 #${j.recording_id}` : (j.title || j.url || id);
      const line = document.createElement('div');
      line.style.cssText = 'margin-top:0.2rem;color:var(--text-secondary,#555);white-space:pre-line;';
      let text = LABEL[j.state] || j.state;
      if (j.state === 'downloading' && j.progress != null) text += ` ${j.progress.toFixed(0)}%`;
      if (nc && j.state === 'running' && j.step) text = j.step;
      if (j.state === 'error' && j.error) text += `：${j.error}`;
      if (j.state === 'done' && j.duplicate) text = '這支影片 24 小時內已匯入過';
      if (nc && j.state === 'done') text = j.applied ? ncSummary(j) : '套用到逐字稿…';
      line.textContent = (finished(j) ? (j.state === 'done' ? '✅ ' : '❌ ') : '⏳ ') + text;
      card.append(title, line);
      if (finished(j)) {
        const row = document.createElement('div');
        row.style.cssText = 'margin-top:0.4rem;display:flex;gap:0.75rem;';
        if (j.state === 'done') {
          const reload = document.createElement('button');
          reload.textContent = nc ? '重新載入看結果' : '重新整理清單';
          reload.style.cssText = 'color:var(--text-accent,#2563eb);background:none;border:none;padding:0;cursor:pointer;';
          reload.onclick = () => location.reload();
          row.append(reload);
        }
        const close = document.createElement('button');
        close.textContent = '關閉';
        close.style.cssText = 'color:var(--text-muted,#888);background:none;border:none;padding:0;cursor:pointer;';
        close.onclick = () => { jobs.delete(id); save(); render(); };
        row.append(close);
        card.append(row);
      }
      root.append(card);
    }
  }

  async function poll() {
    const active = [...jobs.entries()].filter(([, j]) => !finished(j));
    if (active.length === 0) { clearInterval(timer); timer = null; return; }
    await Promise.all(active.map(async ([id, j]) => {
      try {
        if (j.state !== 'done') {
          const r = await fetch(`${API}/jobs/${encodeURIComponent(id)}`, { credentials: 'same-origin' });
          if (r.status === 404) { jobs.set(id, { ...j, state: 'error', error: '工作已不存在（服務重啟過？）' }); return; }
          if (!r.ok) return;
          jobs.set(id, { ...j, ...(await r.json()) });
        }
        const cur = jobs.get(id);
        if (cur.kind === 'namecheck' && cur.state === 'done' && !cur.applied && !cur.applying) {
          jobs.set(id, { ...cur, applying: true });
          jobs.set(id, { ...cur, ...(await applyToTranscript(cur)), applying: false });
        }
      } catch (_) { /* 暫時連不上就下一輪再試 */ }
    }));
    save();
    render();
  }

  function track(id, initial) {
    jobs.set(id, initial);
    save();
    render();
    if (!timer) timer = setInterval(poll, 2000);
  }

  async function submit() {
    const input = document.getElementById('speakr-url-input');
    const btn = document.getElementById('speakr-url-import-btn');
    const model = document.getElementById('speakr-url-model');
    if (!input) return;
    const url = input.value.trim();
    if (!/^https?:\/\//i.test(url)) { input.focus(); input.style.borderColor = '#ef4444'; return; }
    input.style.borderColor = '';
    if (btn) { btn.disabled = true; btn.style.opacity = '0.6'; }
    try {
      const r = await fetch(`${API}/import`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Speakr-Url': '1' },
        body: JSON.stringify({ url, transcription_model: model ? model.value : '' }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        const msg = r.status === 401 ? '請先登入 Speakr' : (data.error || `HTTP ${r.status}`);
        track(`err-${Date.now()}`, { url, state: 'error', error: msg });
        return;
      }
      input.value = '';
      track(data.id, data);
    } catch (e) {
      track(`err-${Date.now()}`, { url, state: 'error', error: '連不到匯入服務（speakr-url 有在跑嗎？）' });
    } finally {
      if (btn) { btn.disabled = false; btn.style.opacity = ''; }
    }
  }

  // ------------------------------------------------------------ 校正姓名

  function ncSummary(j) {
    if (j.applyError) return `摘要已更新，但逐字稿沒存成功：${j.applyError}`;
    const parts = [j.message || '完成'];
    if (j.fixes && j.fixes.length) {
      parts.push(j.fixes.map(([a, b]) => `${a} → ${b}`).join('、'));
      parts.push(`逐字稿改了 ${j.transcriptHits || 0} 處` + (j.summary_updated ? '；摘要最後附查證結果和來源' : ''));
    } else if (j.summary_updated) {
      parts.push('沒有需要修正的名字；摘要最後附查證結果和來源');
    }
    return parts.join('\n');
  }

  // 把「錯」換成「對」，但已經是「對」的地方不動（例如 王明 → 王明德，重跑不會變成 王明德德）
  function replaceSafe(text, wrong, right) {
    let n = 0;
    const out = text.split(right).map((piece) => piece.split(wrong).map((x, i) => { if (i) n++; return x; }).join(right));
    return [out.join(right), n];
  }
  function fixString(text, fixes) {
    let total = 0;
    for (const [wrong, right] of fixes) {
      const [t, n] = replaceSafe(text, wrong, right);
      text = t; total += n;
    }
    return [text, total];
  }

  // 逐字稿用 Speakr 自己的編輯 API 存回（和「編輯逐字稿」存檔走同一條路，CSRF 由 Speakr 的 fetch 攔截器補上）
  async function applyToTranscript(j) {
    const fixes = (j.fixes || []).filter(([a, b]) => a && b && a !== b);
    if (!fixes.length) return { applied: true, transcriptHits: 0 };
    const rid = j.recording_id;
    try {
      const r = await fetch(`/api/recordings/${rid}`, { credentials: 'same-origin' });
      if (!r.ok) throw new Error(`讀取錄音 HTTP ${r.status}`);
      const data = await r.json();
      const raw = (data.recording || data).transcription;
      if (!raw) return { applied: true, transcriptHits: 0 };
      let segments = null;
      try { const v = JSON.parse(raw); if (Array.isArray(v)) segments = v; } catch (_) { /* 純文字逐字稿 */ }
      let hits = 0;
      let resp;
      if (segments) {
        for (const seg of segments) {
          if (!seg || typeof seg !== 'object') continue;
          for (const k of Object.keys(seg)) {
            if (typeof seg[k] !== 'string' || /(^|_)(time|id|label)$/.test(k)) continue;
            const [t, n] = fixString(seg[k], fixes);
            if (n) { seg[k] = t; hits += n; }
          }
        }
        if (!hits) return { applied: true, transcriptHits: 0 };
        resp = await fetch(`/recording/${rid}/update_transcript`, {
          method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ transcript_data: segments, speaker_map: {}, regenerate_summary: false }),
        });
      } else {
        const [t, n] = fixString(raw, fixes);
        hits = n;
        if (!hits) return { applied: true, transcriptHits: 0 };
        resp = await fetch(`/recording/${rid}/update_transcription`, {
          method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ transcription: t }),
        });
      }
      if (!resp.ok) {
        const e = await resp.json().catch(() => ({}));
        throw new Error(e.error || `HTTP ${resp.status}`);
      }
      return { applied: true, transcriptHits: hits };
    } catch (e) {
      return { applied: true, applyError: String(e.message || e) };
    }
  }

  async function startNamecheck(btn) {
    const rid = parseInt(btn.dataset.rid, 10);
    if (!rid) return;
    for (const j of jobs.values()) {
      if (j.kind === 'namecheck' && j.recording_id === rid && !finished(j)) return; // 已在跑
    }
    if (!confirm('上網查證這份錄音提到的人名，並修正摘要和逐字稿裡的錯字？\n（約 1–3 分鐘，會用到 Ollama）')) return;
    btn.classList.add('is-on');
    btn.disabled = true;
    try {
      const r = await fetch(`${API}/namecheck`, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Speakr-Url': '1' },
        body: JSON.stringify({ recording_id: rid }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        const msg = r.status === 401 ? '請先登入 Speakr' : (data.error || `HTTP ${r.status}`);
        track(`err-${Date.now()}`, { kind: 'namecheck', recording_id: rid, state: 'error', error: msg });
        return;
      }
      track(data.id, data);
    } catch (_) {
      track(`err-${Date.now()}`, { kind: 'namecheck', recording_id: rid, state: 'error', error: '連不到 speakr-url 服務' });
    } finally {
      btn.classList.remove('is-on');
      btn.disabled = false;
    }
  }

  // ------------------------------------------------------------ 繁中翻譯

  const TR_SHOW_ORIG = 'speakr-tr-show-original';
  const trTimers = new WeakMap();

  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'style') n.style.cssText = v;
      else if (k === 'class') n.className = v;
      else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v);
    }
    for (const c of kids) if (c != null) n.append(c);
    return n;
  }
  const fmtTime = (t) => (t == null ? '' : `${String(Math.floor(t / 60)).padStart(2, '0')}:${String(Math.floor(t % 60)).padStart(2, '0')}`);
  const showOrig = () => { try { return localStorage.getItem(TR_SHOW_ORIG) !== '0'; } catch (_) { return true; } };

  async function trFetch(rid) {
    const r = await fetch(`${API}/translation/${rid}`, { credentials: 'same-origin' });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  }

  async function trStart(host, rid) {
    const r = await fetch(`${API}/translation`, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Speakr-Url': '1' },
      body: JSON.stringify({ recording_id: rid }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok && r.status !== 409) alert(r.status === 401 ? '請先登入 Speakr' : (data.error || `HTTP ${r.status}`));
    trRender(host, rid);
  }

  function trSection(title, ...kids) {
    return el('section', { style: 'margin-bottom:1.5rem;' },
      el('div', { class: 't-meta', style: 'font-weight:600;text-transform:none;margin-bottom:0.5rem;' }, title), ...kids);
  }

  const tr_has = (d) => !!(d.transcript && d.transcript.segments && d.transcript.segments.length);

  async function trRender(host, rid) {
    clearTimeout(trTimers.get(host));
    if (!document.body.contains(host)) return;
    let data;
    try { data = await trFetch(rid); } catch (e) {
      host.replaceChildren(el('div', { class: 'banner banner--warn' }, `讀不到翻譯：${e.message}`));
      return;
    }
    if (!document.body.contains(host) || String(host.dataset.rid) !== String(rid)) return; // 已切到別的錄音
    const job = data.job || {};
    const busy = job.state === 'queued' || job.state === 'running';
    const onlyTranscript = host.classList.contains('speakr-tr-left');
    const has = data.summary || data.transcript;
    const ready = !data.status || data.status === 'COMPLETED';
    const scroll = host.scrollTop;

    const btn = el('button', { class: 'btn btn--primary btn--sm', type: 'button', onclick: () => { btn.disabled = true; trStart(host, rid); } },
      el('i', { class: 'fas fa-language', style: 'margin-right:0.35rem;' }), has ? '重新翻譯' : '翻譯成繁體中文');
    if (busy || (!ready && !has)) btn.disabled = true;
    const head = el('div', { style: 'display:flex;align-items:center;justify-content:space-between;gap:0.75rem;margin-bottom:1rem;flex-wrap:wrap;' },
      el('div', { class: 't-meta', style: 'text-transform:none;' }, onlyTranscript
        ? '逐字稿的繁中翻譯（點「Transcription」回到原文）。摘要的翻譯在右側「繁中翻譯」分頁。'
        : '原文保留在「摘要」與逐字稿，翻譯另外存放，不會覆蓋原文。'),
      btn);
    const parts = [head];
    if (busy) parts.push(el('div', { class: 'banner', style: 'margin-bottom:1rem;' }, el('span', { class: 'spinner', style: 'margin-right:0.5rem;' }), `⏳ ${job.step || '翻譯中'}（可以先離開，完成後回來看）`));
    if (job.state === 'error') parts.push(el('div', { class: 'banner banner--warn', style: 'margin-bottom:1rem;' }, `❌ ${job.error || '翻譯失敗'}`));

    if (!has && !busy && !ready) {
      parts.push(el('div', { class: 'empty' },
        el('div', { class: 'empty__icon' }, el('i', { class: 'fas fa-hourglass-half' })),
        el('div', { class: 'empty__title' }, '這份錄音還在處理中'),
        el('div', { class: 'empty__body' }, `目前狀態：${data.status}。轉錄和摘要完成後就可以翻譯，這裡會自動更新。`)));
      trTimers.set(host, setTimeout(() => trRender(host, rid), 15000));
    } else if (!has && !busy) {
      parts.push(el('div', { class: 'empty' },
        el('div', { class: 'empty__icon' }, el('i', { class: 'fas fa-language' })),
        el('div', { class: 'empty__title' }, '還沒有翻譯'),
        el('div', { class: 'empty__body' }, '按上方按鈕，會先翻摘要（約 1 分鐘），再逐段翻逐字稿（長錄音可能要十幾分鐘）。')));
    }

    if (data.summary && !onlyTranscript) {
      const md = el('div', { class: 'prose-flush' });
      md.innerHTML = window.renderMarkdownSafe ? window.renderMarkdownSafe(data.summary.text) : '';
      if (!window.renderMarkdownSafe) md.textContent = data.summary.text;
      parts.push(trSection(`摘要（${data.summary.translated_at}）`,
        data.summary.stale ? el('div', { class: 'banner banner--warn', style: 'margin-bottom:0.75rem;' }, '摘要在翻譯之後改過了，按「重新翻譯」更新。') : null,
        md));
    }

    const tr = data.transcript;
    if (onlyTranscript && has && !(tr_has(data)) && !busy) {
      parts.push(el('div', { class: 'empty' }, el('div', { class: 'empty__title' }, '逐字稿還沒翻譯，按「重新翻譯」')));
    }
    if (tr && tr.segments && tr.segments.length) {
      const done = tr.segments.filter((x) => x.translation).length;
      const toggle = el('label', { class: 't-meta', style: 'text-transform:none;display:inline-flex;align-items:center;gap:0.35rem;cursor:pointer;margin-bottom:0.75rem;' },
        el('input', { type: 'checkbox', onchange: (e) => { try { localStorage.setItem(TR_SHOW_ORIG, e.target.checked ? '1' : '0'); } catch (_) {} trRender(host, rid); } }),
        '顯示原文');
      toggle.querySelector('input').checked = showOrig();
      const list = el('div', {});
      for (const seg of tr.segments) {
        const meta = [fmtTime(seg.start), seg.speaker].filter(Boolean).join(' · ');
        list.append(el('div', { style: 'padding:0.5rem 0;border-bottom:1px solid var(--border-primary);' },
          meta ? el('div', { class: 't-meta', style: 'text-transform:none;' }, meta) : null,
          el('div', { style: 'color:var(--text-primary);line-height:1.6;' }, seg.translation || '…'),
          showOrig() ? el('div', { style: 'color:var(--text-muted);font-size:0.85em;line-height:1.5;margin-top:0.15rem;' }, seg.text) : null));
      }
      parts.push(trSection(`逐字稿（${done}/${tr.segments.length} 段${tr.complete ? `，${tr.translated_at}` : '，翻譯中'}）`, toggle, list));
    }

    host.replaceChildren(...parts);
    host.scrollTop = scroll;
    host.dataset.rendered = String(rid);
    if (busy) trTimers.set(host, setTimeout(() => trRender(host, rid), 3000));
  }

  // 桌面版分頁：Vue 建立/切換錄音時，面板元素出現或 data-rid 改變就重畫
  function trScan() {
    for (const host of document.querySelectorAll('.speakr-tr-panel')) {
      if (host.dataset.rid && host.dataset.rendered !== host.dataset.rid) {
        host.dataset.rendered = host.dataset.rid;
        trRender(host, host.dataset.rid);
      }
    }
  }
  let trScanQueued = false;
  new MutationObserver(() => {
    if (trScanQueued) return;
    trScanQueued = true;
    requestAnimationFrame(() => { trScanQueued = false; trScan(); applyLeft(); });
  }).observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ['data-rid'] });

  // 桌面版左側：「Transcription」↔「繁中翻譯」切換（翻譯區塊取代原本的逐字稿區塊）
  const LEFT_KEY = 'speakr-tr-left-on';
  let leftOn = (() => { try { return localStorage.getItem(LEFT_KEY) === '1'; } catch (_) { return false; } })();
  (function injectCss() {
    const st = document.createElement('style');
    st.textContent = '#leftMainColumn:not(.speakr-tr-left-on) > .speakr-tr-left{display:none!important}' +
      '#leftMainColumn.speakr-tr-left-on > .speakr-tr-left + div{display:none!important}';
    (document.head || document.documentElement).append(st);
  })();
  function applyLeft() {
    const col = document.getElementById('leftMainColumn');
    if (!col) return;
    const host = col.querySelector(':scope > .speakr-tr-left');
    const ours = col.querySelector('.speakr-tr-left-tab');
    const orig = col.querySelector('.tabs > .tab:not(.speakr-tr-left-tab)');
    const on = leftOn && !!host && !!ours;
    col.classList.toggle('speakr-tr-left-on', on);
    if (ours) ours.classList.toggle('is-active', on);
    if (orig) { orig.classList.toggle('is-active', !on); orig.style.cursor = on ? 'pointer' : 'default'; }
    if (on && host.dataset.rid && host.dataset.rendered !== host.dataset.rid) {
      host.dataset.rendered = host.dataset.rid;
      trRender(host, host.dataset.rid);
    }
  }
  function setLeft(on) {
    leftOn = on;
    try { localStorage.setItem(LEFT_KEY, on ? '1' : '0'); } catch (_) {}
    const host = document.querySelector('#leftMainColumn > .speakr-tr-left');
    if (on && host) host.dataset.rendered = '';   // 每次切過來都重新抓一次
    applyLeft();
  }

  // 手機版：全螢幕檢視
  function trOpenOverlay(rid) {
    const body = el('div', { class: 'speakr-tr-sheet', style: 'flex:1;overflow-y:auto;padding:1rem;' });
    body.dataset.rid = String(rid);
    const close = () => overlay.remove();
    const overlay = el('div', { style: 'position:fixed;inset:0;z-index:70;display:flex;flex-direction:column;background:var(--bg-primary,#fff);' },
      el('div', { style: 'display:flex;align-items:center;justify-content:space-between;padding:0.75rem 1rem;border-bottom:1px solid var(--border-primary);padding-top:max(0.75rem, env(safe-area-inset-top));' },
        el('div', { style: 'font-weight:600;color:var(--text-primary);' }, '繁中翻譯'),
        el('button', { class: 'btn btn--icon', type: 'button', onclick: close, title: '關閉' }, el('i', { class: 'fas fa-times' }))),
      body);
    document.body.append(overlay);
    trRender(body, rid);
  }

  // ------------------------------------------------------------ 匯入 Obsidian

  function toast(text, ok) {
    const t = el('div', { style: 'position:fixed;left:50%;bottom:1.5rem;transform:translateX(-50%);z-index:9999;max-width:90vw;' +
      'padding:0.6rem 0.9rem;border-radius:0.5rem;box-shadow:0 4px 12px rgba(0,0,0,.2);font-size:0.85rem;' +
      'background:var(--bg-secondary,#fff);color:var(--text-primary,#111);border:1px solid ' + (ok ? '#22c55e' : '#ef4444') + ';' }, text);
    document.body.append(t);
    setTimeout(() => t.remove(), 6000);
  }

  async function exportObsidian(btn, overwrite) {
    const rid = parseInt(btn.dataset.rid, 10);
    if (!rid) return;
    btn.disabled = true;
    const icon = btn.querySelector('i');
    if (icon) icon.className = 'fas fa-spinner fa-spin';
    try {
      const r = await fetch(`${API}/obsidian`, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Speakr-Url': '1' },
        body: JSON.stringify({ recording_id: rid, overwrite: !!overwrite }),
      });
      const data = await r.json().catch(() => ({}));
      if (r.status === 409 && data.exists) {
        const msg = data.edited
          ? `「${data.path}」在 Obsidian 裡被改過了。\n覆蓋會蓋掉你在 Obsidian 的修改，確定要覆蓋嗎？`
          : `「${data.path}」已經匯入過，要用最新內容更新嗎？`;
        if (confirm(msg)) return exportObsidian(btn, true);
        return;
      }
      if (!r.ok) { toast(`❌ 匯入失敗：${r.status === 401 ? '請先登入 Speakr' : (data.error || `HTTP ${r.status}`)}`, false); return; }
      toast(`✅ 已匯入 Obsidian：${data.path}（幾秒後同步到其他裝置）`, true);
    } catch (_) {
      toast('❌ 連不到 speakr-url 服務', false);
    } finally {
      btn.disabled = false;
      if (icon) icon.className = 'fas fa-gem';
    }
  }

  // ------------------------------------------------------------ 事件

  document.addEventListener('click', (e) => {
    if (e.target.closest && e.target.closest('#speakr-url-import-btn')) { e.preventDefault(); submit(); }
  });
  // 捕獲階段：手機版標題列的按鈕有 @click.stop（避免觸發標題列本身），冒泡階段收不到
  document.addEventListener('click', (e) => {
    const btn = e.target.closest && e.target.closest('.speakr-namecheck-btn');
    if (btn) { e.preventDefault(); startNamecheck(btn); return; }
    const obs = e.target.closest && e.target.closest('.speakr-obs-btn');
    if (obs) { e.preventDefault(); if (!obs.disabled) exportObsidian(obs, false); return; }
    if (e.target.closest && e.target.closest('.speakr-tr-left-tab')) { e.preventDefault(); setLeft(true); return; }
    if (e.target.closest && e.target.closest('#leftMainColumn .tabs > .tab:not(.speakr-tr-left-tab)')) { setLeft(false); return; }
    const tr = e.target.closest && e.target.closest('.speakr-tr-open');
    if (tr && tr.dataset.rid) { e.preventDefault(); trOpenOverlay(tr.dataset.rid); }
  }, true);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target && e.target.id === 'speakr-url-input') { e.preventDefault(); submit(); }
  });

  // 頁面重新整理後，繼續追蹤還沒完成的工作
  document.addEventListener('DOMContentLoaded', () => {
    for (const id of load()) jobs.set(id, { state: 'queued', title: '' });
    if (jobs.size) { render(); poll(); timer = setInterval(poll, 2000); }
  });
})();
