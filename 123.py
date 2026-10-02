import asyncio
import json
import base64
import zlib
import os
import sys
from datetime import datetime
from playwright.async_api import async_playwright

DOC_URL = "https://docs.qq.com/smartsheet/DYUVLcnVzemtsREVw?tab=WMypN4&viewId=vabcde"
AUTH_FILE = "tencent_auth.json"
EDGE_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

IS_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"

FIELD_MAP = {
    "fp1Vev": "记录编号", "feFvUO": "提交时间", "foxB4n": "提交时间年月日",
    "fz8Mdv": "二维码名称", "f9rQNe": "二维码地址", "fM0osq": "填表人",
    "fC49nw": "提交日期", "flZwvw": "做菜日期", "f8mRtK": "厨师1",
    "ff6IOQ": "厨师2", "fGmSN7": "菜名", "fbZb4u": "菜图",
}


def b64_fix(s):
    s = s.strip().replace("\n", "").replace("\r", "").replace("-", "+").replace("_", "/")
    pad = (-len(s)) % 4
    if pad: s += "=" * pad
    return s


def decode_chunk(b64_str):
    raw = base64.b64decode(b64_fix(b64_str))
    try:
        text = zlib.decompress(raw).decode("utf-8", errors="replace")
    except zlib.error:
        text = zlib.decompress(raw, -zlib.MAX_WBITS).decode("utf-8", errors="replace")
    return json.loads(text)


def ts_to_date(ts_ms):
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000).strftime("%Y-%m-%d")
    except Exception:
        return ""


def extract_text(cell):
    if not cell: return ""
    if "k1" in cell and isinstance(cell["k1"], list):
        for item in cell["k1"]:
            if isinstance(item, dict) and "k2" in item: return item["k2"]
    return ""


def extract_value(field_id, cell, option_map):
    if not cell: return ""
    text = extract_text(cell)
    if text: return text
    if "k4" in cell: return ts_to_date(cell["k4"])
    if "k8" in cell and isinstance(cell["k8"], list):
        for item in cell["k8"]:
            if isinstance(item, dict) and "k3" in item: return item["k3"]
    if "k17" in cell:
        opts = cell["k17"]
        if isinstance(opts, list) and opts:
            return option_map.get(field_id, {}).get(opts[0], opts[0])
    return ""


def prepare_auth():
    if IS_GITHUB_ACTIONS:
        auth_json = os.environ.get("TENCENT_AUTH")
        if not auth_json:
            print("❌ 未找到 TENCENT_AUTH 环境变量");
            sys.exit(1)
        with open(AUTH_FILE, "w", encoding="utf-8") as f:
            f.write(auth_json)
        print("✅ 已从 Secrets 还原登录态")
    else:
        if not os.path.exists(AUTH_FILE):
            print("⚠️ 本地未发现登录态，需要手动登录一次")
            asyncio.run(local_login())
        else:
            print(f"✅ 发现本地登录态 {AUTH_FILE}")


async def local_login():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, executable_path=EDGE_PATH)
        context = await browser.new_context(viewport={"width": 1600, "height": 900})
        page = await context.new_page()
        await page.goto(DOC_URL, wait_until="domcontentloaded")
        print("\n>>> 请在浏览器里登录，成功后回到终端按回车 <<<\n")
        input()
        await context.storage_state(path=AUTH_FILE)
        print(f"✅ 登录态已保存到 {AUTH_FILE}")
        await browser.close()


async def scrape_all_chunks():
    all_chunks = []
    async with async_playwright() as p:
        if IS_GITHUB_ACTIONS:
            browser = await p.chromium.launch(headless=True)
        else:
            browser = await p.chromium.launch(headless=True, executable_path=EDGE_PATH)

        context = await browser.new_context(viewport={"width": 1600, "height": 900}, storage_state=AUTH_FILE)
        page = await context.new_page()

        async def on_response(response):
            url = response.url
            if "opendoc" in url and "dop-api" in url:
                try:
                    body = await response.text()
                    start = body.find("{")
                    end = body.rfind("}")
                    if start < 0 or end < 0: return
                    data = json.loads(body[start:end + 1])
                    cv = data.get("clientVars", {})
                    ccv = cv.get("collab_client_vars", {})
                    iat = ccv.get("initialAttributedText")
                    if not iat: return
                    for chunk in iat.get("text", []):
                        if chunk.get("smartsheet"):
                            all_chunks.append(chunk)
                            print(
                                f"📥 分块: max_row={chunk.get('max_row')}, row[{chunk.get('start_row_index')}~{chunk.get('end_row_index')}]")
                except Exception as e:
                    print(f"处理响应失败: {e}")

        page.on("response", on_response)
        print("正在打开文档...")
        await page.goto(DOC_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(10000)

        print("滚动页面触发更多数据...")
        for i in range(10):
            await page.mouse.wheel(0, 4000)
            await page.wait_for_timeout(1500)
        await browser.close()
    return all_chunks


def parse_all_chunks(decoded_chunks):
    option_map = {}
    for chunk in decoded_chunks:
        for row in chunk.get("data", []):
            for item in row:
                if item.get("t") == 3005:
                    fields = (item.get("c", {}).get("k3", {}).get("k3", {}))
                    for fid, fdef in fields.items():
                        if "k17" in fdef:
                            opts = fdef["k17"].get("k3", [])
                            option_map[fid] = {o["k1"]: o["k2"] for o in opts}
    print(f"📋 选项映射: {option_map}")

    all_rows = []
    for chunk in decoded_chunks:
        for row in chunk.get("data", []):
            for item in row:
                if item.get("t") == 3028:
                    rows_obj = (item.get("c", {}).get("k2", {}).get("k1", {}))
                    for row_id, row_data in rows_obj.items():
                        cells = row_data.get("k1", {})
                        parsed = {}
                        for fid, cn_name in FIELD_MAP.items():
                            parsed[cn_name] = extract_value(fid, cells.get(fid, {}), option_map)
                        all_rows.append(parsed)
    return all_rows


def to_dishes_json(rows):
    dishes = []
    for i, row in enumerate(rows):
        name = row.get("菜名", "").strip()
        if not name: continue
        dishes.append({
            "id": i + 1, "name": name,
            "chef1": row.get("厨师1", "").strip(), "chef2": row.get("厨师2", "").strip(),
            "date": row.get("做菜日期", "").strip(), "img": row.get("菜图", "").strip(),
            "_raw": row,
        })
    return dishes


# ==================== 核心修改：处理图片下载（截图法） ====================
async def process_images(dishes):
    if not dishes: return
    has_web_link = any("cli.im" in d.get("img", "") for d in dishes)
    if not has_web_link: return

    os.makedirs("images", exist_ok=True)
    print(f"📸 开始处理图片下载，共 {len(dishes)} 道菜...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True) if IS_GITHUB_ACTIONS else await p.chromium.launch(
            headless=True, executable_path=EDGE_PATH)

        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        for dish in dishes:
            img_url = dish.get("img", "")
            if not img_url or "cli.im" not in img_url: continue

            print(f"  -> 正在解析图片: {dish['name']}")
            try:
                try:
                    await page.goto(img_url, wait_until="commit", timeout=15000)
                except Exception:
                    pass

                await page.wait_for_timeout(5000)

                real_img_element = await page.evaluate_handle('''() => {
                    const imgs = Array.from(document.querySelectorAll('img'));
                    const visibleImgs = imgs.filter(img => img.offsetWidth > 100 && img.offsetHeight > 100);
                    if (visibleImgs.length === 0) return null;
                    let largestImg = visibleImgs[0];
                    let maxArea = visibleImgs[0].offsetWidth * visibleImgs[0].offsetHeight;
                    for (let img of visibleImgs) {
                        let area = img.offsetWidth * img.offsetHeight;
                        if (area > maxArea) { maxArea = area; largestImg = img; }
                    }
                    return largestImg;
                }''')

                if real_img_element:
                    filename = f"{dish['id']}_{dish['name']}.jpg"
                    filepath = os.path.join("images", filename)
                    await real_img_element.scroll_into_view_if_needed()
                    await real_img_element.screenshot(path=filepath)
                    dish["img"] = f"images/{filename}"
                    print(f"     ✅ 已截图保存到 {filepath}")
                else:
                    print(f"     ⚠️ 未提取到真实图片元素")
            except Exception as e:
                print(f"     ❌ 处理失败: {e}")
        await browser.close()


# ==================== 主流程 ====================
def main():
    print("=" * 50)
    print(f"运行环境: {'GitHub Actions' if IS_GITHUB_ACTIONS else '本地'}")
    print(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    prepare_auth()
    chunks = asyncio.run(scrape_all_chunks())
    print(f"\n✅ 共抓到 {len(chunks)} 个分块")
    if not chunks: sys.exit(1)

    decoded = []
    for i, chunk in enumerate(chunks):
        try:
            data = decode_chunk(chunk.get("smartsheet", ""))
            decoded.append({"max_row": chunk.get("max_row"), "max_col": chunk.get("max_col"),
                            "start_row": chunk.get("start_row_index"), "end_row": chunk.get("end_row_index"),
                            "data": data})
            print(f"✅ 分块#{i} 解码成功")
        except Exception as e:
            print(f"❌ 分块#{i} 解码失败: {e}")

    rows = parse_all_chunks(decoded)
    dishes = to_dishes_json(rows)
    print(f"✅ 原始生成 {len(dishes)} 道菜")

    asyncio.run(process_images(dishes))

    with open("dishes.json", "w", encoding="utf-8") as f:
        json.dump(dishes, f, ensure_ascii=False, indent=2)

    print(f"\n🎉 最终生成 {len(dishes)} 道菜")


if __name__ == "__main__":
    main()
