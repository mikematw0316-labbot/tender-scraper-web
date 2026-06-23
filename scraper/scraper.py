"""
政府採購標案自動化爬蟲 — 完整 6 階段 SOP（GitHub Actions 版）
=============================================================
Stage 1: 從採購網起始 URL 取得機關名稱
Stage 2: 台灣採購公報網搜尋機關 → 找最新年度案件清單
Stage 3: 篩選含關鍵字的案件 → 進入明細頁抓取案號/日期/名稱
Stage 4: 以案號 + 日期範圍查詢政府電子採購網
Stage 5: 進入決標公告頁抓取 7 項欄位
Stage 6: 儲存至 data/results.json（GitHub Pages 服務）

環境變數：
  START_URL       必填，政府採購網起始網址
  KEYWORDS        選填，逗號分隔，預設：設計,監造,耐震,補強,技術
  LOOKBACK_DAYS   選填，日期前後範圍天數，預設 180
"""

import asyncio
import json
import os
import random
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ─────────────────────────────────────────
# 設定（從環境變數讀取）
# ─────────────────────────────────────────
START_URL = os.environ.get("START_URL", "").strip()
KEYWORDS_ENV = os.environ.get("KEYWORDS", "設計,監造,耐震,補強,技術")
TARGET_KEYWORDS = [k.strip() for k in KEYWORDS_ENV.split(",") if k.strip()]
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "180"))
HEADLESS = True

PCC_SEARCH_URL = "https://web.pcc.gov.tw/prkms/tender/common/agent/indexTenderAgent"
TB_BASE = "https://www.taiwanbuying.com.tw"
TB_SEARCH_URL = f"{TB_BASE}/QueryCloseCase_Ori.ASP"

OUT_DIR = Path(__file__).parent.parent / "data"
OUT_FILE = OUT_DIR / "results.json"


# ─────────────────────────────────────────
# 工具函式
# ─────────────────────────────────────────
def gregorian_shift(date_str: str, days: int) -> str:
    dt = datetime.strptime(date_str, "%Y/%m/%d")
    return (dt + timedelta(days=days)).strftime("%Y/%m/%d")


def contains_keyword(text: str) -> bool:
    return any(kw in text for kw in TARGET_KEYWORDS)


async def rand_sleep(min_s=0.8, max_s=2.2):
    await asyncio.sleep(random.uniform(min_s, max_s))


def strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).replace("\xa0", " ").strip()


def parse_date_to_gregorian(date_raw: str) -> str:
    if not date_raw:
        return ""
    m = re.search(r"(\d{3})[/\-](\d{1,2})[/\-](\d{1,2})", date_raw)
    if m:
        return f"{int(m.group(1))+1911}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"
    m = re.search(r"(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})", date_raw)
    if m:
        return f"{m.group(1)}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"
    return date_raw


# ─────────────────────────────────────────
# Stage 1：從採購網頁面取得機關名稱
# ─────────────────────────────────────────
async def stage1_get_agency(page, start_url: str) -> str:
    print(f"[Stage 1] 訪問：{start_url}")
    await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=12000)
    except PlaywrightTimeoutError:
        pass

    for sel in [
        "th:has-text('機關名稱') + td",
        "td:has-text('機關名稱') + td",
        "tr:has(th:has-text('機關名稱')) td",
        "tr:has(td:has-text('機關名稱')) td:nth-child(2)",
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                text = (await loc.inner_text()).strip()
                if text and len(text) > 1:
                    print(f"[Stage 1] 機關名稱：{text}")
                    return text
        except Exception:
            pass

    content = await page.content()
    for pat in [
        r"機關名稱[：:]\s*</[^>]+>\s*<[^>]+>\s*([^<\s]{2,30})",
        r"機關名稱[：:\s]*([^\s<&\n]{2,30})",
    ]:
        m = re.search(pat, content)
        if m:
            agency = strip_html(m.group(1)).strip()
            if agency:
                print(f"[Stage 1] 機關名稱（regex）：{agency}")
                return agency

    raise RuntimeError("無法取得機關名稱，請確認 START_URL 指向含機關資訊的採購頁面")


# ─────────────────────────────────────────
# Stage 2：台灣採購公報網 → 最新年度案件列表
# ─────────────────────────────────────────
async def stage2_get_year_page(page, agency_name: str) -> str:
    print(f"\n[Stage 2] 公報網搜尋：{agency_name}")
    await page.goto(TB_SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
    await rand_sleep()

    # 填搜尋框（欄位名 keyword，公報網表單第一個文字框）
    await page.evaluate(
        """(name) => {
            const inputs = document.querySelectorAll('input[type="text"]');
            if (inputs.length > 0) inputs[0].value = name;
        }""",
        agency_name,
    )
    await rand_sleep(0.3, 0.6)

    try:
        await page.locator("input[type='submit'], button[type='submit'], input[value*='查詢']").first.click()
    except Exception:
        await page.keyboard.press("Enter")

    await page.wait_for_load_state("domcontentloaded", timeout=20000)
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except PlaywrightTimeoutError:
        pass
    await rand_sleep()

    content = await page.content()
    org_ids = re.findall(r"ShowOrgStat\.ASP\?OrgID=(\d+)", content, re.IGNORECASE)
    if not org_ids:
        for lk in await page.locator("a").all():
            href = await lk.get_attribute("href") or ""
            m = re.search(r"OrgID=(\d+)", href, re.IGNORECASE)
            if m:
                org_ids.append(m.group(1))
                break

    if not org_ids:
        raise RuntimeError(f"公報網找不到「{agency_name}」")

    org_stat_url = f"{TB_BASE}/ShowOrgStat.ASP?OrgID={org_ids[0]}"
    print(f"[Stage 2] OrgStat：{org_stat_url}")

    await page.goto(org_stat_url, wait_until="domcontentloaded", timeout=30000)
    await rand_sleep()

    content = await page.content()
    year_map: dict[int, str] = {}
    for href in re.findall(
        r"href=['\"]?([^'\">\s]*ShowOrgYearClose[^'\">\s]*)['\"]?", content, re.IGNORECASE
    ):
        m = re.search(r"Y=(\d{4})", href, re.IGNORECASE)
        if m:
            y = int(m.group(1))
            year_map[y] = href if href.startswith("http") else f"{TB_BASE}/{href.lstrip('/')}"

    if not year_map:
        for lk in await page.locator("a").all():
            txt = (await lk.inner_text()).strip()
            href = await lk.get_attribute("href") or ""
            if re.fullmatch(r"\d{4}", txt):
                y = int(txt)
                year_map[y] = href if href.startswith("http") else f"{TB_BASE}/{href.lstrip('/')}"

    if not year_map:
        raise RuntimeError("找不到年份連結")

    latest_year = max(year_map)
    year_url = year_map[latest_year]
    print(f"[Stage 2] 最新年份：{latest_year}  URL：{year_url}")
    return year_url


# ─────────────────────────────────────────
# Stage 3：篩選關鍵字案件，抓明細欄位
# ─────────────────────────────────────────
async def stage3_collect_cases(page, year_url: str) -> list[dict]:
    print(f"\n[Stage 3] 掃描年度案件：{year_url}")
    # Visit homepage first to establish session cookies
    await page.goto(TB_BASE, wait_until="domcontentloaded", timeout=20000)
    await page.wait_for_timeout(1500)
    await page.goto(year_url, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except PlaywrightTimeoutError:
        pass
    # Extra wait for JS-rendered content in headless mode
    await page.wait_for_timeout(3000)
    await rand_sleep()

    content = await page.content()
    print(f"[Stage 3] 頁面大小：{len(content)} bytes")
    if len(content) < 2000:
        print(f"[Stage 3] 頁面內容（debug）：{content[:800]}")
    rec_pattern = re.compile(r"ShowCCDetail\.ASP\?RecNo=(\d+)", re.IGNORECASE)

    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", content, re.IGNORECASE | re.DOTALL):
        rec_m = rec_pattern.search(m.group(1))
        if rec_m:
            rec = rec_m.group(1)
            if rec not in seen:
                seen.add(rec)
                candidates.append((rec, strip_html(m.group(2)).strip()))

    if not candidates:
        # Fallback: scan raw HTML for any RecNo pattern
        for rec_m in rec_pattern.finditer(content):
            rec = rec_m.group(1)
            if rec not in seen:
                seen.add(rec)
                candidates.append((rec, ""))

    # Fallback 2: use Playwright locator to find links with openWin
    if not candidates:
        print("[Stage 3] regex 掃描無結果，嘗試 Playwright locator…")
        for lk in await page.locator("a").all():
            href = await lk.get_attribute("href") or ""
            rec_m = rec_pattern.search(href)
            if rec_m:
                rec = rec_m.group(1)
                if rec not in seen:
                    seen.add(rec)
                    txt = (await lk.inner_text()).strip()
                    candidates.append((rec, txt))

    print(f"[Stage 3] 找到 {len(candidates)} 筆，篩選中…")
    matched: list[dict] = []
    for i, (rec_no, _) in enumerate(candidates):
        detail_url = f"{TB_BASE}/ShowCCDetail.ASP?RecNo={rec_no}"
        try:
            detail = await _fetch_case_detail(page, detail_url)
            if detail and contains_keyword(detail["tender_name"]):
                matched.append(detail)
                print(f"  [{i+1}] ✓ {detail['tender_name'][:50]}")
        except Exception as e:
            print(f"  [{i+1}] ⚠ RecNo={rec_no}：{e}")
        await rand_sleep(0.4, 1.0)

    print(f"[Stage 3] 符合關鍵字：{len(matched)} 筆")
    return matched


async def _fetch_case_detail(page, detail_url: str) -> dict | None:
    await page.goto(detail_url, wait_until="domcontentloaded", timeout=20000)
    content = await page.content()

    def find(labels):
        for label in labels:
            for pat in [
                rf"{re.escape(label)}\s*</t[dh]>\s*<t[dh][^>]*>\s*([^<]{{1,200}})",
                rf"{re.escape(label)}[：:\s]{{0,3}}([^\s<&\n]{{2,100}})",
            ]:
                m = re.search(pat, content, re.IGNORECASE | re.DOTALL)
                if m:
                    val = strip_html(m.group(1)).strip()
                    if val:
                        return val
        return ""

    publish_date_raw = find(["公布日期", "公佈日期", "公告日期"])
    tender_id = find(["採購案號", "案號", "標案案號"])
    tender_name = find(["採購名稱", "標案名稱", "案件名稱"])

    if not tender_id and not tender_name:
        return None

    return {
        "publish_date_raw": publish_date_raw,
        "publish_date": parse_date_to_gregorian(publish_date_raw),
        "tender_id": tender_id,
        "tender_name": tender_name,
        "source_url": detail_url,
    }


# ─────────────────────────────────────────
# Stage 4+5：查詢政府電子採購網決標資訊
# ─────────────────────────────────────────
async def stage45_query_pcc(page, case: dict) -> dict | None:
    tender_id = case["tender_id"]
    pub_date = case["publish_date"]
    if not pub_date:
        return None

    start_date = gregorian_shift(pub_date, -LOOKBACK_DAYS)
    end_date = gregorian_shift(pub_date, LOOKBACK_DAYS)
    print(f"  [4] {tender_id}  {start_date}～{end_date}")

    await page.goto(PCC_SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=12000)
    except PlaywrightTimeoutError:
        pass
    await rand_sleep()

    await page.evaluate(
        """([tenderId, startDate, endDate]) => {
            const all = [...document.querySelectorAll('input')];
            const caseEl = all.find(el => {
                const n = (el.name || el.id || '').toLowerCase();
                return n.includes('caseno') || n.includes('tenderid') || n.includes('pkatm');
            });
            if (caseEl) { caseEl.removeAttribute('readonly'); caseEl.value = tenderId; }
            const startEl = all.find(el => {
                const n = (el.name || el.id || '').toLowerCase();
                return (n.includes('start') || n.includes('begin') || n.includes('from')) && n.includes('date');
            });
            if (startEl) { startEl.removeAttribute('readonly'); startEl.value = startDate; }
            const endEl = all.find(el => {
                const n = (el.name || el.id || '').toLowerCase();
                return (n.includes('end') || n.includes('to')) && n.includes('date');
            });
            if (endEl) { endEl.removeAttribute('readonly'); endEl.value = endDate; }
        }""",
        [tender_id, start_date, end_date],
    )
    await rand_sleep(0.5, 1.0)

    try:
        await page.evaluate("agentTenderSearch()")
    except Exception:
        try:
            await page.locator("input[type='submit'][value*='查詢'], button:has-text('查詢')").first.click()
        except Exception as e2:
            print(f"  ⚠ 無法提交：{e2}")
            return None

    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except PlaywrightTimeoutError:
        pass
    await rand_sleep()

    atm_html = await page.evaluate(
        "() => { const el = document.getElementById('atm'); return el ? el.outerHTML : document.body.innerHTML; }"
    )

    if not atm_html or "無符合" in atm_html or "查無資料" in atm_html:
        return None

    # 找匹配案號的列
    chosen_link = None
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", atm_html, re.IGNORECASE | re.DOTALL):
        if tender_id in row:
            lks = re.findall(r"href=['\"]?(/prkms/[^'\">\s]+)['\"]?", row, re.IGNORECASE)
            if lks:
                chosen_link = lks[0]
                break

    if not chosen_link:
        all_lks = re.findall(r"href=['\"]?(/prkms/[^'\">\s]+)['\"]?", atm_html, re.IGNORECASE)
        chosen_link = all_lks[0] if all_lks else None

    if not chosen_link:
        return None

    detail_url = f"https://web.pcc.gov.tw{chosen_link}"
    return await stage5_extract_award(page, detail_url, case)


async def stage5_extract_award(page, detail_url: str, case: dict) -> dict | None:
    try:
        async with page.context.expect_page() as new_info:
            await page.evaluate(f"window.open('{detail_url}', '_blank')")
        detail_page = await new_info.value
        await detail_page.wait_for_load_state("domcontentloaded", timeout=20000)
    except Exception:
        detail_page = page
        await page.goto(detail_url, wait_until="domcontentloaded", timeout=20000)

    try:
        content = await detail_page.content()
        return _parse_award_fields(content, case)
    finally:
        if detail_page is not page:
            await detail_page.close()


def _parse_award_fields(content: str, case: dict) -> dict:
    def field(*labels) -> str:
        for label in labels:
            for pat in [
                rf"{re.escape(label)}\s*</t[dh]>\s*<t[dh][^>]*>\s*(.*?)\s*</t[dh]>",
                rf"{re.escape(label)}[：:\s]{{0,3}}([^\s<&\n]{{1,150}})",
            ]:
                m = re.search(pat, content, re.IGNORECASE | re.DOTALL)
                if m:
                    val = strip_html(m.group(1)).strip()
                    if val and val not in ("&nbsp;", "—", "-"):
                        return val
        return ""

    return {
        "機關名稱": field("機關名稱"),
        "決標日期": field("決標日期", "公告日期"),
        "標案名稱": field("標案名稱", "採購名稱") or case["tender_name"],
        "投標廠商家數": field("投標廠商家數", "廠商家數"),
        "新增公告傳輸次數": field("新增公告傳輸次數", "公告傳輸次數", "傳輸次數"),
        "得標廠商": field("得標廠商", "廠商名稱", "決標廠商"),
        "決標金額": field("決標金額", "決標價格", "金額"),
        "來源案號": case["tender_id"],
        "公報公布日期": case["publish_date_raw"],
        "公報URL": case["source_url"],
    }


# ─────────────────────────────────────────
# Stage 6：儲存至 data/results.json
# ─────────────────────────────────────────
def stage6_save_json(records: list[dict], agency_name: str):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "updatedAt": datetime.utcnow().isoformat() + "Z",
        "agencyName": agency_name,
        "keywords": TARGET_KEYWORDS,
        "total": len(records),
        "items": records,
    }
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[Stage 6] 已儲存 {len(records)} 筆 → {OUT_FILE}")


# ─────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────
async def main():
    if not START_URL:
        print("❌ 請設定環境變數 START_URL")
        sys.exit(1)

    print(f"START_URL = {START_URL}")
    print(f"KEYWORDS  = {TARGET_KEYWORDS}")
    print(f"LOOKBACK  = ±{LOOKBACK_DAYS} 天")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="zh-TW",
            timezone_id="Asia/Taipei",
        )
        # Hide headless browser fingerprints
        await ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['zh-TW','zh','en']});
            window.chrome = {runtime: {}};
        """)
        page = await ctx.new_page()

        try:
            agency_name = await stage1_get_agency(page, START_URL)
            year_url = await stage2_get_year_page(page, agency_name)
            matched_cases = await stage3_collect_cases(page, year_url)

            if not matched_cases:
                print("\n⚠ 沒有符合關鍵字的案件")
                stage6_save_json([], agency_name)
                return

            print(f"\n共 {len(matched_cases)} 筆，查詢採購網決標資料…")
            results: list[dict] = []
            for i, case in enumerate(matched_cases, 1):
                print(f"\n  [{i}/{len(matched_cases)}] {case['tender_id']} - {case['tender_name'][:30]}")
                try:
                    award = await stage45_query_pcc(page, case)
                    if award:
                        results.append(award)
                        print(f"  ✓ {award.get('決標金額', '—')}")
                    else:
                        results.append({
                            "機關名稱": agency_name, "決標日期": "",
                            "標案名稱": case["tender_name"], "投標廠商家數": "",
                            "新增公告傳輸次數": "", "得標廠商": "", "決標金額": "",
                            "來源案號": case["tender_id"],
                            "公報公布日期": case["publish_date_raw"],
                            "公報URL": case["source_url"],
                        })
                except Exception as e:
                    print(f"  ✗ {e}")
                    results.append({
                        "機關名稱": agency_name, "決標日期": "",
                        "標案名稱": case["tender_name"], "投標廠商家數": "",
                        "新增公告傳輸次數": "", "得標廠商": "", "決標金額": "",
                        "來源案號": case["tender_id"],
                        "公報公布日期": case["publish_date_raw"],
                        "公報URL": case["source_url"],
                    })
                await rand_sleep(1.5, 3.0)

        finally:
            await browser.close()

    stage6_save_json(results, agency_name)
    print(f"\n✅ 完成！共 {len(results)} 筆")


if __name__ == "__main__":
    asyncio.run(main())
