"""
政府採購標案爬蟲
- 政府電子採購網 (PCC) 決標查詢
- 台灣採購公報網 招標公告關鍵字查詢

輸出: data/results.json
"""
import json
import re
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

KEYWORDS = ['設計', '監造', '耐震', '補強', '技術']
LOOKBACK_DAYS = 90

PCC_BASE = 'https://web.pcc.gov.tw'
TB_BASE  = 'https://www.taiwanbuying.com.tw'

HEADERS_PCC = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml',
    'Accept-Language': 'zh-TW,zh;q=0.9',
    'Referer': 'https://web.pcc.gov.tw/',
}

HEADERS_TB = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Content-Type': 'application/x-www-form-urlencoded',
    'Referer': 'https://www.taiwanbuying.com.tw/',
    'Accept': 'text/html,application/xhtml+xml',
}


def fetch_get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    resp = urllib.request.urlopen(req, timeout=20)
    return resp.read().decode('utf-8', errors='replace')


def fetch_post(url, body, headers, cookies=''):
    if cookies:
        headers = {**headers, 'Cookie': cookies}
    data = body.encode('utf-8') if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    resp = urllib.request.urlopen(req, timeout=20)
    cookie = resp.headers.get('Set-Cookie', '')
    raw = resp.read()
    try:
        content = raw.decode('big5')
    except Exception:
        content = raw.decode('utf-8', errors='replace')
    return content, cookie


def strip_tags(html):
    return re.sub(r'<[^>]+>', '', html)


def clean(text):
    return re.sub(r'\s+', ' ', text or '').strip()


def roc_to_gregorian(roc_str):
    """Convert ROC date like '115/06/23' to '2026/06/23'"""
    m = re.match(r'(\d{2,3})/(\d{1,2})/(\d{1,2})', (roc_str or '').strip())
    if m:
        y = int(m.group(1)) + 1911
        return f"{y}/{m.group(2).zfill(2)}/{m.group(3).zfill(2)}"
    return roc_str or ''


# ── PCC ──────────────────────────────────────────────────────────
def fetch_pcc(keyword, start_date, end_date):
    params = urllib.parse.urlencode({
        'pageSize': '', 'firstSearch': 'false', 'isQuery': '',
        'isBinding': 'N', 'isLogIn': 'N',
        'orgName': '', 'orgId': '',
        'tenderName': keyword, 'tenderId': '',
        'tenderStatus': 'TENDER_STATUS_1',
        'tenderWay': 'TENDER_WAY_ALL_DECLARATION',
        'awardAnnounceStartDate': start_date,
        'awardAnnounceEndDate': end_date,
        'radProctrgCate': '', 'tenderRange': 'TENDER_RANGE_ALL',
        'minBudget': '', 'maxBudget': '', 'item': '',
        'gottenVendorName': '', 'gottenVendorId': '',
        'submitVendorName': '', 'submitVendorId': '',
        'execLocation': '', 'priorityCate': '',
        'radReConstruct': '', 'policyAdvocacy': '', 'isCpp': '',
    })
    url = f"{PCC_BASE}/prkms/tender/common/agent/readTenderAgent?{params}"
    print(f"  [PCC] GET {url[:80]}…")
    html = fetch_get(url, HEADERS_PCC)
    return parse_pcc(html, keyword)


def parse_pcc(html, keyword):
    atm_idx = html.find('id="atm"')
    if atm_idx < 0:
        return []
    tbl_start = html.rfind('<table', 0, atm_idx)
    tbl_end   = html.find('</table>', atm_idx) + len('</table>')
    table_html = html[tbl_start:tbl_end]

    items = []
    for row in re.findall(r'<tr[^>]*>([\s\S]*?)</tr>', table_html):
        cells = re.findall(r'<td[^>]*>([\s\S]*?)</td>', row)
        if len(cells) < 7:
            continue
        seq = clean(strip_tags(cells[0]))
        if not seq.isdigit():
            continue

        agency = clean(strip_tags(cells[1]))
        title_m = re.search(r'title="([^"]+)"', cells[2])
        tender_name = title_m.group(1) if title_m else clean(strip_tags(cells[2])).split()[0]
        tender_id = clean(strip_tags(cells[2])).split()[0]
        pub_date = roc_to_gregorian(clean(strip_tags(cells[5])))
        amount   = clean(strip_tags(cells[6]))
        link_m   = re.search(r'href="(/prkms/urlSelector/common/atm\?pk=[^"]+)"', cells[7] if len(cells) > 7 else '')
        detail   = PCC_BASE + link_m.group(1) if link_m else ''

        items.append({
            'src': 'PCC決標', 'keyword': keyword,
            'agency': agency, 'tenderName': tender_name,
            'tenderId': tender_id, 'pubDate': pub_date,
            'amount': amount, 'detailLink': detail,
        })
    return items


# ── taiwanbuying ─────────────────────────────────────────────────
def fetch_taiwanbuying(keyword):
    body = urllib.parse.urlencode({'keyword': keyword})
    print(f"  [公報網] POST keyword={keyword}…")
    html, _ = fetch_post(f"{TB_BASE}/Query_KeywordAction.ASP", body, HEADERS_TB)
    return parse_tb(html, keyword)


def parse_tb(html, keyword):
    items = []
    link_regex = re.compile(
        r'(<a\b[^>]+ShowDetail\.ASP\?RecNo=(\d+)[^>]*>)([^<]+)</a>',
        re.IGNORECASE
    )
    for m in link_regex.finditer(html):
        full_tag = m.group(1)
        rec_no   = m.group(2)
        raw_text = m.group(3).replace('\xa0', ' ').strip()
        clean_text = re.sub(r'\s*\(\d{4}/\d+/\d+\s*更新\)\s*$', '', raw_text).strip()

        colon = clean_text.find(':')
        if colon > 0:
            agency = clean_text[:colon].strip()
            tender_name = clean_text[colon+1:].strip()
        else:
            agency, tender_name = '', clean_text

        title_m = re.search(r"title='([^']+)'", full_tag)
        title_attr = title_m.group(1) if title_m else ''
        date_m = re.search(r'(\d{2,3})/(\d{1,2})/(\d{1,2})', title_attr)
        pub_date = ''
        if date_m:
            y = int(date_m.group(1)) + 1911
            pub_date = f"{y}/{date_m.group(2).zfill(2)}/{date_m.group(3).zfill(2)}"

        items.append({
            'src': '公報網招標', 'keyword': keyword,
            'agency': agency, 'tenderName': tender_name,
            'tenderId': '', 'pubDate': pub_date,
            'amount': '', 'detailLink': f"{TB_BASE}/ShowDetail.ASP?RecNo={rec_no}",
        })
    return items


# ── Main ──────────────────────────────────────────────────────────
def main():
    now = datetime.now()
    end_date   = now.strftime('%Y/%m/%d')
    start_date = (now - timedelta(days=LOOKBACK_DAYS)).strftime('%Y/%m/%d')

    print(f"查詢日期範圍: {start_date} ~ {end_date}")
    print(f"關鍵字: {KEYWORDS}\n")

    all_items = []
    for kw in KEYWORDS:
        print(f"\n=== 關鍵字: {kw} ===")
        try:
            pcc_items = fetch_pcc(kw, start_date, end_date)
            print(f"  PCC: {len(pcc_items)} 筆")
            all_items.extend(pcc_items)
        except Exception as e:
            print(f"  PCC 錯誤: {e}", file=sys.stderr)

        try:
            tb_items = fetch_taiwanbuying(kw)
            print(f"  公報網: {len(tb_items)} 筆")
            all_items.extend(tb_items)
        except Exception as e:
            print(f"  公報網 錯誤: {e}", file=sys.stderr)

    # Dedup
    seen = set()
    unique = []
    for item in all_items:
        key = f"{item['src']}|{item.get('tenderId') or item['detailLink']}|{item['tenderName'][:8]}"
        if key not in seen:
            seen.add(key)
            unique.append(item)

    print(f"\n合計: {len(all_items)} 筆 → 去重後 {len(unique)} 筆")

    output = {
        'updatedAt': now.isoformat(),
        'dateRange': {'start': start_date, 'end': end_date},
        'keywords': KEYWORDS,
        'total': len(unique),
        'items': unique,
    }

    out_path = Path(__file__).parent.parent / 'data' / 'results.json'
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"輸出: {out_path}")


if __name__ == '__main__':
    main()
