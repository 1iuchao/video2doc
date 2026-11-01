// 抖音视频详情抓取器（headless Edge + CDP）
//
// 走 headless Edge 打开视频页，拦截官方 aweme/detail 接口的响应，拿
// play_addr 等真实播放地址。比 yt-dlp 稳，因为签名（a_bogus / msToken）
// 由浏览器自己生成，我们不需要实现。
//
// 用法: node tools/douyin_detail.js <aweme_id|视频页URL> [--profile <浏览器数据目录>]
// 输出: 一行 JSON（成功 {"ok":true,"aweme":{...}}，失败 {"ok":false,"error":"..."}）

const { spawn } = require('node:child_process');
const http = require('node:http');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36';
const TIMEOUT_MS = 45000;

function findEdge() {
  const cands = [
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  ];
  for (const c of cands) if (fs.existsSync(c)) return c;
  return null;
}

const argv = process.argv.slice(2);
let target = '';
let profile = path.join(os.tmpdir(), 'douyin-skill-profile');
for (let i = 0; i < argv.length; i++) {
  if (argv[i] === '--profile') { profile = argv[++i]; }
  else if (!target) { target = argv[i]; }
}

if (!target) {
  console.log(JSON.stringify({ ok: false, error: '缺少参数：作品 id 或视频页 URL' }));
  process.exit(2);
}
if (!/^\d+$/.test(target) && !/^https?:\/\//.test(target)) {
  console.log(JSON.stringify({ ok: false, error: `参数不是 id 也不是链接：${target}` }));
  process.exit(2);
}

const EDGE = findEdge();
if (!EDGE) {
  console.log(JSON.stringify({ ok: false, error: '找不到 Microsoft Edge，这套方案依赖 Edge 的 CDP 接口' }));
  process.exit(3);
}

const url = /^\d+$/.test(target) ? `https://www.douyin.com/video/${target}` : target;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const PORT = 9333 + (process.pid % 500);

function getJSON(p) {
  return new Promise((resolve, reject) => {
    http.get({ host: '127.0.0.1', port: PORT, path: p }, (res) => {
      let b = '';
      res.on('data', (c) => (b += c));
      res.on('end', () => { try { resolve(JSON.parse(b)); } catch (e) { reject(e); } });
    }).on('error', reject);
  });
}

async function waitDevtools(timeoutMs = 20000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try { return await getJSON('/json/version'); } catch { await sleep(250); }
  }
  throw new Error('浏览器调试端口没起来（可能被安全软件拦截）');
}

class CDP {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map(); this.listeners = [];
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
      } else if (msg.method) {
        this.listeners.forEach((f) => f(msg));
      }
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) { this.pending.delete(id); reject(new Error(`CDP 超时: ${method}`)); }
      }, 30000);
    });
  }
  on(fn) { this.listeners.push(fn); }
}

function summarize(aweme) {
  const v = aweme.video || {};
  const st = aweme.statistics || {};
  const play = ((v.play_addr || {}).url_list) || [];
  const h264 = ((v.play_addr_h264 || {}).url_list) || [];
  const dl = ((v.download_addr || {}).url_list) || [];
  return {
    id: aweme.aweme_id || '',
    desc: aweme.desc || '',
    author: (aweme.author || {}).nickname || '',
    author_id: (aweme.author || {}).sec_uid || '',
    create_time: aweme.create_time || 0,
    duration_ms: v.duration || 0,
    statistics: {
      digg: st.digg_count || 0,
      comment: st.comment_count || 0,
      share: st.share_count || 0,
      collect: st.collect_count || 0,
      play: st.play_count || 0,
    },
    music: (aweme.music || {}).title || '',
    play_urls: [...play, ...h264, ...dl].filter(Boolean),
    cover: (((v.cover || {}).url_list) || [])[0] || '',
  };
}

(async () => {
  fs.mkdirSync(profile, { recursive: true });

  const proc = spawn(EDGE, [
    '--headless=new',
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--mute-audio',
    '--window-size=1280,900',
    `--user-agent=${UA}`,
    'about:blank',
  ], { stdio: 'ignore' });

  let ws;
  try {
    await waitDevtools();
    const list = await getJSON('/json/list');
    const page = list.find((t) => t.type === 'page');
    if (!page) throw new Error('没有可用的标签页');

    ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
      ws.addEventListener('open', res);
      ws.addEventListener('error', rej);
    });

    const cdp = new CDP(ws);
    const hits = [];

    cdp.on((msg) => {
      if (msg.method !== 'Network.responseReceived') return;
      const r = msg.params.response;
      if (/aweme\/v1\/web\/aweme\/detail/.test(r.url)) {
        hits.push({ url: r.url, status: r.status, requestId: msg.params.requestId });
      }
    });

    await cdp.send('Network.enable', { maxTotalBufferSize: 209715200, maxResourceBufferSize: 52428800 });
    await cdp.send('Page.enable');
    await cdp.send('Page.navigate', { url });

    let detail = null;
    const deadline = Date.now() + TIMEOUT_MS;

    while (Date.now() < deadline) {
      await sleep(1200);
      if (!hits.length) continue;
      for (const hit of hits) {
        try {
          const body = await cdp.send('Network.getResponseBody', { requestId: hit.requestId });
          if (!body || !body.body) continue;
          const parsed = JSON.parse(body.body);
          if (parsed && parsed.aweme_detail) { detail = parsed.aweme_detail; break; }
        } catch { /* 下一轮再试 */ }
      }
      if (detail) break;
    }

    if (!detail) {
      console.log(JSON.stringify({ ok: false, error: '没抓到 aweme_detail。可能是风控拦截，或页面要求登录。用有头 Edge 打开 profile 目录手动登录一次再重试。' }));
      process.exitCode = 1;
    } else {
      console.log(JSON.stringify({ ok: true, aweme: summarize(detail) }));
    }

    try { await cdp.send('Browser.close'); } catch { /* ignore */ }
  } catch (e) {
    console.log(JSON.stringify({ ok: false, error: String((e && e.message) || e) }));
    process.exitCode = 1;
  } finally {
    try { if (ws) ws.close(); } catch { /* ignore */ }
    try { proc.kill(); } catch { /* ignore */ }
    setTimeout(() => process.exit(process.exitCode || 0), 300);
  }
})();
