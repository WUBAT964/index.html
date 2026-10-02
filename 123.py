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

# 本地 Edge 路径
EDGE_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

# 判断当前是不是 GitHub Actions 环境
IS_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


# ==================== 字段 ID -> 中文名 ====================
FIELD_MAP = {
    "fp1Vev": "记录编号",
    "feFvUO": "提交时间",
    "foxB4n": "提交时间年月日",
    "fz8Mdv": "二维码名称",
    "f9rQNe": "二维码地址",
    "fM0osq": "填表人",
    "fC49nw": "提交日期",
    "flZwvw": "做菜日期",
    "f8mRtK": "厨师1",
    "ff6IOQ": "厨师2",
    "fGmSN7": "菜名",
    "fbZb4u": "菜图",
}


# ==================== 工具函数 ====================
def b64_fix(s):
    s = s.strip().replace("\n", "").replace("\r", "")
    s = s.replace("-", "+").replace("_", "/")
    pad = (-len(s)) % 4
    if pad:
        s += "=" * pad
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
        ts = int(ts_ms) / 1000
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        return ""


def extract_text(cell):
    if not cell:
        return ""
    if "k1" in cell and isinstance(cell["k1"], list):
        for item in cell["k1"]:
            if isinstance(item, dict) and "k2" in item:
                return item["k2"]
    return ""


def extract_value(field_id, cell, option_map):
    if not cell:
        return ""
    text = extract_text(cell)
    if text:
        return text
    if "k4" in cell:
        return ts_to_date(cell["k4"])
    if "k8" in cell and isinstance(cell["k8"], list):
        for item in cell["k8"]:
            if isinstance(item, dict) and "k3" in item:
                return item["k3"]
    if "k17" in cell:
        opts = cell["k17"]
        if isinstance(opts, list) and opts:
            return option_map.get(field_id, {}).get(opts[0], opts[0])
    return ""


# ==================== 登录态处理 ====================
def prepare_auth():
    """在 GitHub Actions 里从环境变量还原登录态；本地检查文件是否存在"""
    if IS_GITHUB_ACTIONS:
        auth_json = os.environ.get("TENCENT_AUTH")
        if not auth_json:
            print("❌ 未找到 TENCENT_AUTH 环境变量")
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
    """本地首次登录（用 Edge）"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            executable_path=EDGE_PATH,
        )
        context = await browser.new_context(viewport={"width": 1600, "height": 900})
        page = await context.new_page()
        await page.goto(DOC_URL, wait_until="domcontentloaded")
        print("\n>>> 请在浏览器里登录，成功后回到终端按回车 <<<\n")
        input()
        await context.storage_state(path=AUTH_FILE)
        print(f"✅ 登录态已保存到 {AUTH_FILE}")
        await browser.close()


# ==================== 抓取 ====================
async def scrape_all_chunks():
    all_chunks = []
    async with async_playwright() as p:
        # GitHub Actions 用默认 Chromium，本地用 Edge
        if IS_GITHUB_ACTIONS:
            print("🌐 使用 Playwright 内置 Chromium")
            browser = await p.chromium.launch(headless=True)
        else:
            print("🌐 使用本地 Edge")
            browser = await p.chromium.launch(
                headless=True,
                executable_path=EDGE_PATH,
            )

        context = await browser.new_context(
            viewport={"width": 1600, "height": 900},
            storage_state=AUTH_FILE,
        )
        page = await context.new_page()

        async def on_response(response):
            url = response.url
            if "opendoc" in url and "dop-api" in url:
                try:
                    body = await response.text()
                    start = body.find("{")
                    end = body.rfind("}")
                    if start < 0 or end < 0:
                        return
                    data = json.loads(body[start:end + 1])
                    cv = data.get("clientVars", {})
                    ccv = cv.get("collab_client_vars", {})
                    iat = ccv.get("initialAttributedText")
                    if not iat:
                        return
                    for chunk in iat.get("text", []):
                        if chunk.get("smartsheet"):
                            all_chunks.append(chunk)
                            print(f"📥 分块: max_row={chunk.get('max_row')}, "
                                  f"row[{chunk.get('start_row_index')}~{chunk.get('end_row_index')}]")
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


# ==================== 解析 ====================
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
        if not name:
            continue
        dishes.append({
            "id": i + 1,
            "name": name,
            "chef1": row.get("厨师1", "").strip(),
            "chef2": row.get("厨师2", "").strip(),
            "date": row.get("做菜日期", "").strip(),
            "img": row.get("菜图", "").strip(),
            "_raw": row,
        })
    return dishes


# ==================== 主流程 ====================
def main():
    print("=" * 50)
    print("腾讯智能表格抓取工具")
    print(f"运行环境: {'GitHub Actions' if IS_GITHUB_ACTIONS else '本地'}")
    print(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    # 1) 准备登录态
    prepare_auth()

    # 2) 抓取
    chunks = asyncio.run(scrape_all_chunks())
    print(f"\n✅ 共抓到 {len(chunks)} 个分块")
    if not chunks:
        print("❌ 没有抓到任何分块，可能表格是空的或登录态失效")
        sys.exit(1)

    # 3) 解码
    decoded = []
    for i, chunk in enumerate(chunks):
        try:
            data = decode_chunk(chunk.get("smartsheet", ""))
            decoded.append({
                "max_row": chunk.get("max_row"),
                "max_col": chunk.get("max_col"),
                "start_row": chunk.get("start_row_index"),
                "end_row": chunk.get("end_row_index"),
                "data": data,
            })
            print(f"✅ 分块#{i} 解码成功")
        except Exception as e:
            print(f"❌ 分块#{i} 解码失败: {e}")

    # 4) 解析
    rows = parse_all_chunks(decoded)
    print(f"\n✅ 解析出 {len(rows)} 行数据")

    # 5) 生成 dishes.json
    dishes = to_dishes_json(rows)
    with open("dishes.json", "w", encoding="utf-8") as f:
        json.dump(dishes, f, ensure_ascii=False, indent=2)

    print(f"\n🎉 最终生成 {len(dishes)} 道菜")
    print("📁 dishes.json 已生成")
    print("\n===== 内容预览 =====")
    print(json.dumps(dishes, ensure_ascii=False, indent=2)[:1000])
    print("\n✅ 全部完成")


if __name__ == "__main__":
    main()