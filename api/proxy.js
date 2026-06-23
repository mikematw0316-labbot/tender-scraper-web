/**
 * Vercel Serverless Function — 反向代理政府採購網站
 * 繞過瀏覽器 CORS 限制，由伺服器端直接抓取 HTML
 */
const fetch = require('node-fetch');
const iconv = require('iconv-lite');

const ALLOWED = ['web.pcc.gov.tw', 'www.taiwanbuying.com.tw', 'taiwanbuying.com.tw'];

module.exports = async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  if (req.method === 'OPTIONS') return res.status(200).end();
  if (req.method !== 'POST') return res.status(405).json({ error: 'POST only' });

  const { url, method = 'GET', body, headers: extraHeaders = {}, cookies = '' } = req.body || {};
  if (!url) return res.status(400).json({ error: 'url required' });

  // 只允許政府採購相關網域
  try {
    const parsed = new URL(url);
    if (!ALLOWED.some(d => parsed.hostname === d || parsed.hostname.endsWith('.' + d))) {
      return res.status(403).json({ error: `網域不允許：${parsed.hostname}` });
    }
  } catch {
    return res.status(400).json({ error: '無效 URL' });
  }

  const reqHeaders = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept-Encoding': 'gzip, deflate',
    ...(cookies ? { Cookie: cookies } : {}),
    ...extraHeaders,
  };
  if (method === 'POST') {
    reqHeaders['Content-Type'] = 'application/x-www-form-urlencoded';
  }

  try {
    const upstream = await fetch(url, {
      method,
      headers: reqHeaders,
      body: method !== 'GET' ? body : undefined,
      redirect: 'follow',
    });

    const ct = upstream.headers.get('content-type') || '';
    const buf = await upstream.buffer();

    // 自動偵測 Big5 編碼（台灣政府網站常見）
    let html;
    if (/big5/i.test(ct)) {
      html = iconv.decode(buf, 'big5');
    } else {
      const utf8peek = buf.slice(0, 2000).toString('utf-8');
      html = /charset=["\']?big5/i.test(utf8peek)
        ? iconv.decode(buf, 'big5')
        : buf.toString('utf-8');
    }

    // 回傳 Set-Cookie 讓前端維持 session
    const setCookie = upstream.headers.get('set-cookie') || '';
    res.json({ ok: upstream.ok, status: upstream.status, html, setCookie, ct });

  } catch (e) {
    console.error('[proxy]', e.message);
    res.status(500).json({ error: e.message });
  }
};
