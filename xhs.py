# -*- coding: utf-8 -*-
"""
小红书搜索结果爬虫 —— 按"最多点赞"排序，滚动翻页拦截 API，目标 1000+ 条笔记。

使用方式:
  1. 先启动带远程调试的 Chrome:
     chrome.exe --remote-debugging-port=9222 --user-data-dir="<YOUR_LOCAL_CHROME_PROFILE_DIR>"  # TODO: 替换为你本地 Chrome 用户数据目录，勿提交真实路径
  2. 在浏览器中确保已登录小红书
  3. 运行本脚本: python scrape_xhs.py
"""

import os
import csv
import json
import time
import requests
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from selenium.webdriver import Chrome
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains

# ============================================================
# 配置
# ============================================================

SEARCH_URL = (
    "https://www.xiaohongshu.com/search_result"
    "?keyword=%25E7%25B3%259F%25E7%25B2%2595%25E9%2586%258B"
    "&source=web_search_result_notes"
)

TARGET_API = "api/sns/web/v1/search/notes"

BASE_DIR = Path(__file__).parent / "zaopo_cu_dataset"
RAW_IMAGES_DIR = BASE_DIR / "raw_images"
SAM_MASKS_DIR = BASE_DIR / "sam_masks"
COLOR_FEATURES_DIR = BASE_DIR / "color_features"
METADATA_DIR = BASE_DIR / "metadata"

# 爬取参数
TARGET_NOTE_COUNT = 1000       # 目标笔记数量
MAX_SCROLL_ROUNDS = 300        # 最大滚动轮次（安全上限）
SCROLL_PAUSE = 2.5             # 每次滚动后等待秒数
NO_NEW_DATA_LIMIT = 15         # 连续多少轮无新数据则停止
IMAGE_DOWNLOAD_WORKERS = 4     # 图片并发下载线程数

# CSV 字段定义
CSV_FIELDS = [
    "note_id", "display_title", "nickname",
    "liked_count", "cover_url", "image_filename", "scraped_at",
]

# ============================================================
# 工具函数
# ============================================================


def ensure_dirs():
    """创建目录结构（含预留目录）。"""
    for d in [RAW_IMAGES_DIR, SAM_MASKS_DIR, COLOR_FEATURES_DIR, METADATA_DIR]:
        d.mkdir(parents=True, exist_ok=True)
    print("✓ 目录结构已就绪")


def setup_driver():
    """连接已打开的 Chrome 浏览器。"""
    chrome_options = Options()
    chrome_options.add_experimental_option("debuggerAddress", "localhost:9222")
    driver = Chrome(options=chrome_options)
    wait = WebDriverWait(driver, 15)
    print("✓ 已连接到 Chrome 浏览器")
    return driver, wait


# ============================================================
# 去重管理：基于 note_id
# ============================================================


def load_existing_note_ids():
    """
    从已有的 notes.csv 中加载已采集的 note_id，
    支持断点续爬时自动跳过已有数据。
    """
    csv_path = METADATA_DIR / "notes.csv"
    seen = set()
    if csv_path.exists():
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                seen.add(row["note_id"])
        print(f"✓ 已加载 {len(seen)} 条历史记录，断点续爬模式")
    return seen


# ============================================================
# JS 拦截器
# ============================================================

INTERCEPTOR_SCRIPT = """
(function() {
    if (window.__xhs_interceptor_injected) return;
    window.__xhs_interceptor_injected = true;
    window.__xhs_intercepted_data = [];

    // --- 拦截 XMLHttpRequest ---
    var origOpen = XMLHttpRequest.prototype.open;
    var origSend = XMLHttpRequest.prototype.send;

    XMLHttpRequest.prototype.open = function(method, url) {
        this.__xhs_url = url;
        return origOpen.apply(this, arguments);
    };

    XMLHttpRequest.prototype.send = function() {
        if (this.__xhs_url && this.__xhs_url.indexOf('TARGET_API_PLACEHOLDER') !== -1) {
            this.addEventListener('load', function() {
                try {
                    var data = JSON.parse(this.responseText);
                    window.__xhs_intercepted_data.push(data);
                    console.log('[XHS拦截器] 捕获XHR');
                } catch(e) {}
            });
        }
        return origSend.apply(this, arguments);
    };

    // --- 拦截 fetch ---
    var origFetch = window.fetch;
    window.fetch = function(input, init) {
        var url = (typeof input === 'string') ? input : input.url;
        var promise = origFetch.apply(this, arguments);

        if (url && url.indexOf('TARGET_API_PLACEHOLDER') !== -1) {
            promise.then(function(response) {
                response.clone().text().then(function(text) {
                    try {
                        var data = JSON.parse(text);
                        window.__xhs_intercepted_data.push(data);
                        console.log('[XHS拦截器] 捕获fetch');
                    } catch(e) {}
                });
            });
        }
        return promise;
    };

    console.log('[XHS拦截器] 已注入');
})();
""".replace("TARGET_API_PLACEHOLDER", TARGET_API)


def inject_interceptor(driver):
    """注入 XHR/Fetch 拦截器。"""
    driver.execute_script(INTERCEPTOR_SCRIPT)
    print("✓ 网络拦截器已注入")


def collect_intercepted_data(driver):
    """取回已拦截的 API 响应并清空缓冲区。"""
    data = driver.execute_script(
        "var d = window.__xhs_intercepted_data || [];"
        "window.__xhs_intercepted_data = [];"
        "return d;"
    )
    return data if data else []


# ============================================================
# UI 交互
# ============================================================


def hover_filter_and_click_sort(driver, wait):
    """悬停筛选按钮 → 点击"最多点赞"。"""
    print("定位筛选容器...")
    filter_el = wait.until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "div.filter"))
    )
    print("✓ 找到筛选容器")

    actions = ActionChains(driver)
    actions.move_to_element(filter_el).perform()
    print("  → 已悬停，等待下拉菜单...")
    time.sleep(1.5)

    try:
        like_option = wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//*[contains(text(), '最多点赞')]")
            )
        )
        like_option.click()
        print("✓ 已点击 '最多点赞' 排序")
    except Exception:
        like_option = driver.find_element(
            By.XPATH, "//*[contains(text(), '最多点赞')]"
        )
        driver.execute_script("arguments[0].click();", like_option)
        print("✓ 已通过 JS 点击 '最多点赞' 排序")


# ============================================================
# 数据解析
# ============================================================


def extract_notes_from_response(api_response):
    """从单个 API 响应中提取笔记列表。"""
    notes = []
    items = api_response.get("data", {}).get("items", [])

    for item in items:
        note_card = item.get("note_card")
        if not note_card:
            continue

        note_id = item.get("id", "") or note_card.get("note_id", "")
        if not note_id:
            continue

        user_info = note_card.get("user", {})
        interact_info = note_card.get("interact_info", {})
        cover = note_card.get("cover", {})
        cover_url = cover.get("url_default", "") or cover.get("url", "")
        if cover_url and cover_url.startswith("//"):
            cover_url = "https:" + cover_url

        notes.append({
            "note_id": note_id,
            "display_title": note_card.get("display_title", ""),
            "nickname": user_info.get("nick_name", "") or user_info.get("nickname", ""),
            "liked_count": interact_info.get("liked_count", "0"),
            "cover_url": cover_url,
        })

    return notes


# ============================================================
# 下载与保存
# ============================================================

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.xiaohongshu.com/",
})


def download_image(url, save_path):
    """下载图片原始二进制流，不做二次压缩。返回是否成功。"""
    if not url:
        return False
    try:
        resp = _SESSION.get(url, timeout=30, stream=True)
        resp.raise_for_status()
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except Exception as e:
        print(f"  ⚠ 下载失败 {save_path.name}: {e}")
        return False


def _download_task(note, idx):
    """单条笔记的图片下载任务（供线程池调用）。"""
    ext = ".png" if ".png" in (note["cover_url"] or "").lower() else ".jpg"
    # 以 note_id 命名图片，确保唯一性
    img_filename = f"{note['note_id']}{ext}"
    img_path = RAW_IMAGES_DIR / img_filename

    if img_path.exists():
        return note, img_filename, True  # 已存在，跳过

    ok = download_image(note["cover_url"], img_path)
    return note, img_filename, ok


class CSVWriter:
    """
    增量 CSV 写入器。
    - 首次打开时写入表头
    - 后续追加行，支持断点续爬
    """

    def __init__(self, path, fields):
        self.path = Path(path)
        self.fields = fields
        self._need_header = not self.path.exists() or self.path.stat().st_size == 0
        self._file = open(self.path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=fields)
        if self._need_header:
            self._writer.writeheader()
            self._file.flush()

    def write_row(self, row):
        self._writer.writerow(row)

    def flush(self):
        self._file.flush()

    def close(self):
        self._file.close()


# ============================================================
# 滚动翻页核心逻辑
# ============================================================


def scroll_and_collect(driver, seen_ids):
    """
    反复滚动页面触发加载，收集拦截到的 API 响应中的新笔记。

    Args:
        driver: Selenium WebDriver
        seen_ids: 已采集的 note_id 集合（会被原地修改）

    Returns:
        list[dict]: 本次滚动采集到的所有新笔记
    """
    all_new_notes = []
    no_new_rounds = 0

    for scroll_round in range(1, MAX_SCROLL_ROUNDS + 1):
        # 1. 滚动页面
        driver.execute_script(
            "window.scrollTo(0, document.body.scrollHeight);"
        )
        time.sleep(SCROLL_PAUSE)

        # 2. 收集拦截数据
        intercepted = collect_intercepted_data(driver)

        round_new = 0
        for resp in intercepted:
            notes = extract_notes_from_response(resp)
            for note in notes:
                if note["note_id"] in seen_ids:
                    continue  # 去重
                seen_ids.add(note["note_id"])
                all_new_notes.append(note)
                round_new += 1

        total = len(seen_ids)
        if round_new > 0:
            no_new_rounds = 0
            print(
                f"  滚动 #{scroll_round}: "
                f"+{round_new} 新笔记 | 累计 {total} 条"
            )
        else:
            no_new_rounds += 1
            if scroll_round % 5 == 0:
                print(
                    f"  滚动 #{scroll_round}: "
                    f"无新数据 (连续 {no_new_rounds} 轮) | 累计 {total} 条"
                )

        # 3. 判断停止条件
        if total >= TARGET_NOTE_COUNT:
            print(f"✓ 已达到目标 {TARGET_NOTE_COUNT} 条，停止滚动")
            break

        if no_new_rounds >= NO_NEW_DATA_LIMIT:
            print(
                f"⚠ 连续 {NO_NEW_DATA_LIMIT} 轮无新数据，"
                f"可能已到底。当前共 {total} 条"
            )
            break

    return all_new_notes


# ============================================================
# 主流程
# ============================================================


def main():
    # 0. 创建目录
    ensure_dirs()

    # 1. 加载历史记录（断点续爬）
    seen_ids = load_existing_note_ids()

    # 2. 连接浏览器
    driver, wait = setup_driver()

    # 3. 打开 CSV 写入器
    csv_writer = CSVWriter(METADATA_DIR / "notes.csv", CSV_FIELDS)

    try:
        # 4. 导航 & 注入拦截器
        current_url = driver.current_url
        if "xiaohongshu.com" not in current_url:
            print("先导航到小红书首页以注入拦截器...")
            driver.get("https://www.xiaohongshu.com")
            time.sleep(3)

        inject_interceptor(driver)

        print(f"导航到搜索页...")
        driver.get(SEARCH_URL)
        time.sleep(4)

        # 页面导航后重新注入
        inject_interceptor(driver)

        # 5. UI 交互：排序
        hover_filter_and_click_sort(driver, wait)
        time.sleep(3)

        # 收集排序后首批数据
        first_batch = collect_intercepted_data(driver)
        for resp in first_batch:
            notes = extract_notes_from_response(resp)
            for note in notes:
                if note["note_id"] not in seen_ids:
                    seen_ids.add(note["note_id"])
        print(f"首批加载: {len(seen_ids)} 条笔记")

        # 6. 滚动翻页
        if len(seen_ids) < TARGET_NOTE_COUNT:
            print(f"\n开始滚动翻页，目标 {TARGET_NOTE_COUNT} 条...")
            new_notes = scroll_and_collect(driver, seen_ids)
        else:
            new_notes = []

        # 合并首批 + 滚动数据（去重后的全量新笔记）
        # 重新收集全部新笔记用于下载
        # 首批数据也需要加入
        all_notes_to_process = []
        # 重新从首批提取
        for resp in first_batch:
            notes = extract_notes_from_response(resp)
            for note in notes:
                all_notes_to_process.append(note)
        all_notes_to_process.extend(new_notes)

        # 再次去重（基于已加载的历史）
        existing_before = load_existing_note_ids()
        final_notes = []
        final_seen = set()
        for note in all_notes_to_process:
            nid = note["note_id"]
            if nid in existing_before or nid in final_seen:
                continue
            final_seen.add(nid)
            final_notes.append(note)

        print(f"\n本次需处理 {len(final_notes)} 条新笔记")

        if not final_notes:
            print("无新笔记需要处理。")
            return

        # 7. 并发下载图片 & 写入 CSV
        print(f"开始下载图片 (线程数={IMAGE_DOWNLOAD_WORKERS})...")
        success_count = 0
        fail_count = 0

        with ThreadPoolExecutor(max_workers=IMAGE_DOWNLOAD_WORKERS) as pool:
            futures = {
                pool.submit(_download_task, note, idx): idx
                for idx, note in enumerate(final_notes, 1)
            }

            for future in as_completed(futures):
                idx = futures[future]
                note, img_filename, ok = future.result()

                if ok:
                    success_count += 1
                else:
                    fail_count += 1
                    img_filename = None

                row = {
                    "note_id": note["note_id"],
                    "display_title": note["display_title"],
                    "nickname": note["nickname"],
                    "liked_count": note["liked_count"],
                    "cover_url": note["cover_url"],
                    "image_filename": img_filename,
                    "scraped_at": datetime.now().isoformat(),
                }
                csv_writer.write_row(row)

                total_done = success_count + fail_count
                if total_done % 20 == 0 or total_done == len(final_notes):
                    csv_writer.flush()
                    print(
                        f"  进度: {total_done}/{len(final_notes)} "
                        f"(成功 {success_count}, 失败 {fail_count})"
                    )

        csv_writer.flush()

        # 8. 保存摘要
        summary = {
            "total_notes": len(seen_ids),
            "new_this_run": len(final_notes),
            "images_downloaded": success_count,
            "images_failed": fail_count,
            "last_run": datetime.now().isoformat(),
        }
        summary_path = BASE_DIR / "summary.json"
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

        print(f"\n{'='*50}")
        print(f"爬取完成！")
        print(f"  累计笔记: {len(seen_ids)} 条")
        print(f"  本次新增: {len(final_notes)} 条")
        print(f"  图片成功: {success_count}, 失败: {fail_count}")
        print(f"  数据文件: {METADATA_DIR / 'notes.csv'}")
        print(f"  图片目录: {RAW_IMAGES_DIR}")

    except Exception as e:
        print(f"\n发生错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        csv_writer.close()


if __name__ == "__main__":
    main()
