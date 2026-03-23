# Selenium + 前端 API 拦截采集示例

本项目演示了两类常见的网页数据采集思路：

1. 连接到已经打开的 Chrome 浏览器，而不是让 Selenium 新开一个“干净但未登录”的浏览器。
2. 在前端页面里注入 JavaScript，直接拦截页面本身已经发出的接口请求或 JSONP 回调。
3. 复用网页已有的登录态、参数生成逻辑和翻页行为，减少自己手搓签名、Cookie、请求头的工作量。

当前仓库包含两个示例脚本：

1. `xhs.py`
   作用：采集小红书搜索结果页数据，拦截 XHR / fetch 请求，下载封面图，并把元数据写入 CSV。
2. `tb.py`
   作用：采集天猫商品评论，拦截 JSONP 接口 `mtop.taobao.rate.detaillist.get` 的回调数据。

## 目录说明

当前仓库主要文件如下：

```text
.
├─ xhs.py
├─ tb.py
├─ .gitignore
└─ README.md
```

`xhs.py` 运行后会在本地生成如下数据目录：

```text
zaopo_cu_dataset/
├─ raw_images/        # 下载的原始封面图
├─ sam_masks/         # 预留目录，目前脚本不会写入
├─ color_features/    # 预留目录，目前脚本不会写入
├─ metadata/
│  └─ notes.csv       # 笔记元数据
└─ summary.json       # 本次运行摘要
```

## 适用场景

这个项目适合用来学习以下内容：

1. 如何让 Selenium 连接到一个已经登录的网站会话。
2. 如何在浏览器端拦截 XHR / fetch / JSONP。
3. 如何通过前端开发者工具找到“真正有用”的接口。
4. 如何把“页面滚动 + 接口抓取 + 数据解析 + 文件落盘”串成一个完整流程。

## 环境要求

建议环境：

1. Python 3.10 及以上
2. Google Chrome 最新稳定版
3. Selenium 4.6 及以上
4. `requests`

安装依赖：

```bash
pip install selenium requests
```

说明：

1. 新版 Selenium 通常会通过 Selenium Manager 自动处理浏览器驱动。
2. 如果你的环境不能自动下载驱动，需要自己安装与 Chrome 版本匹配的 ChromeDriver。

## 先理解一个关键点

这里说的“创建 Selenium 浏览器”，更准确地说是：

1. 手动启动一个开启了远程调试端口的 Chrome。
2. 用一个单独的用户数据目录 `user-data-dir` 保存登录态。
3. 再让 Selenium 通过 `debuggerAddress=localhost:9222` 连接进去。

这样做的好处是：

1. 你可以手动登录目标网站。
2. 页面里的真实 Cookie、Local Storage、Session Storage、浏览器指纹环境都保留在这个浏览器里。
3. Selenium 不需要自己处理登录逻辑，很多反爬绕过也会简单很多。

## 第一步：创建可供 Selenium 连接的 Chrome 浏览器

### Windows

先准备一个专用浏览器目录，不要使用你日常的默认 Chrome 配置目录。

示例目录：

```text
D:\selenium_profiles\xhs_profile
```

启动 Chrome：

```powershell
chrome.exe --remote-debugging-port=9222 --user-data-dir="D:\selenium_profiles\xhs_profile"
```

如果 `chrome.exe` 不在 PATH 中，可以使用完整路径，例如：

```powershell
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="D:\selenium_profiles\xhs_profile"
```

### 为什么一定要单独的 `user-data-dir`

原因很重要：

1. 避免污染你日常使用的浏览器。
2. 避免脚本、缓存、Cookie、扩展互相影响。
3. 更适合将来删除、重建、备份。
4. 便于上传 GitHub 时排除敏感目录。

### 验证浏览器是否启动成功

浏览器打开后，保留这个窗口不要关闭。

脚本会通过下面这段代码连接：

```python
chrome_options = Options()
chrome_options.add_experimental_option("debuggerAddress", "localhost:9222")
driver = Chrome(options=chrome_options)
```

如果 `localhost:9222` 连不上，脚本会在启动阶段报错。

## 第二步：在浏览器里手动完成登录

启动浏览器后，进入目标网站，手动登录。

当前两个示例分别是：

1. 小红书：登录后访问搜索结果页。
2. 天猫：进入商品详情页，打开评论区域。

建议：

1. 手动先把页面状态切到脚本预期的页面附近。
2. 确认网站已经完全加载。
3. 如果页面存在登录验证、滑块、人机校验，先手动完成，再运行脚本。

## 第三步：用前端开发者工具找到“要拦截的 API”

这是整个项目最重要的技能之一。

### 通用步骤

1. 打开目标页面。
2. 按 `F12` 打开开发者工具。
3. 切到 `Network` 面板。
4. 勾选 `Preserve log`，避免页面跳转后请求记录丢失。
5. 清空当前网络记录。
6. 在页面上执行一次你真正关心的操作。
   例如：
   - 点击“最多点赞”
   - 点击“查看全部评论”
   - 向下滚动触发加载更多
   - 切换筛选条件
7. 观察哪些请求是跟着这个操作一起出现的。
8. 点开候选请求，查看它的：
   - Request URL
   - Method
   - Response
   - Payload / Query String Parameters
   - Initiator

### 如何判断一个请求是不是你真正要的

通常有几个判断标准：

1. 响应里直接包含你想采集的结构化数据。
2. 它在你触发页面操作时稳定出现。
3. URL 或路径有稳定特征，适合脚本里做字符串匹配。
4. 响应字段清晰，便于解析。

### 对 XHR / fetch 的判断方法

如果在 `Network` 面板里看到类型是：

1. `xhr`
2. `fetch`

那通常说明这个接口适合用：

1. 重写 `XMLHttpRequest.prototype.open/send`
2. 重写 `window.fetch`

来做拦截。

这正是 `xhs.py` 的实现方式。

### 对 JSONP 的判断方法

如果你发现页面不是发 XHR/fetch，而是：

1. 动态插入一个 `<script>`
2. 请求 URL 里带有 `callback=xxx`
3. 返回内容形如 `xxx({...})`

那它通常是 JSONP。

这时适合：

1. 拦截 `document.head.appendChild`
2. 识别脚本地址里的 `callback` 参数
3. 包装页面原本的回调函数
4. 在回调执行时把数据存入 `window.__intercepted_data`

这正是 `tb.py` 的实现方式。

## 本项目里两个接口是怎么找到的

### 1. 小红书 `xhs.py`

目标动作：

1. 打开搜索结果页
2. 切换到“最多点赞”
3. 向下滚动不断加载更多笔记

在 DevTools 里观察到：

1. 页面会请求搜索结果接口。
2. 接口路径中含有稳定片段：`api/sns/web/v1/search/notes`
3. 返回 JSON 中包含 `data.items`
4. 每个 `item` 里有 `note_card`、标题、用户信息、互动信息和封面图地址

因此脚本里选择：

```python
TARGET_API = "api/sns/web/v1/search/notes"
```

然后分别拦截：

1. XHR
2. fetch

只要 URL 中包含这个片段，就把响应结果收集起来。

### 2. 天猫 `tb.py`

目标动作：

1. 打开商品详情页
2. 点击“查看全部评论”
3. 在评论弹层中继续滚动

在 DevTools 里观察到：

1. 评论数据不是通过普通 XHR 返回。
2. 页面会加载带 `callback` 参数的脚本。
3. 脚本 URL 中包含：`mtop.taobao.rate.detaillist.get`

因此脚本选择：

1. 劫持 `document.head.appendChild`
2. 找到目标 JSONP 脚本
3. 从 URL 中提取 `callback=...`
4. 包装原本的回调函数，拿到数据后再把数据传回页面原逻辑

## 第四步：理解两个脚本的执行流程

### `xhs.py` 执行流程

`xhs.py` 的主流程如下：

1. 创建本地输出目录。
2. 读取已有 `notes.csv`，实现断点续采。
3. 连接已打开的 Chrome。
4. 向页面注入 XHR / fetch 拦截器。
5. 打开预设的搜索页面 `SEARCH_URL`。
6. 通过页面交互找到筛选按钮，并点击“最多点赞”。
7. 从拦截到的接口响应中解析笔记数据。
8. 不断滚动页面，持续收集新响应。
9. 下载封面图到 `raw_images/`。
10. 把元数据追加写入 `metadata/notes.csv`。
11. 把本次运行统计写入 `summary.json`。

### `tb.py` 执行流程

`tb.py` 的主流程如下：

1. 连接已打开的 Chrome。
2. 打开商品详情页。
3. 注入 JSONP 拦截器。
4. 点击“查看全部评论”。
5. 定位评论弹层内真正可滚动的容器。
6. 不断滚动评论容器。
7. 从被包装的 JSONP 回调里拿到评论列表。
8. 把 `rateList` 交给 `save_reviews` 落盘。

## 第五步：如何运行 `xhs.py`

### 1. 修改采集参数

你可以根据需要修改 [xhs.py](./xhs.py) 中这些参数：

```python
SEARCH_URL = "..."
TARGET_API = "api/sns/web/v1/search/notes"
TARGET_NOTE_COUNT = 1000
MAX_SCROLL_ROUNDS = 300
SCROLL_PAUSE = 2.5
NO_NEW_DATA_LIMIT = 15
IMAGE_DOWNLOAD_WORKERS = 4
```

说明：

1. `SEARCH_URL`：目标搜索页面。
2. `TARGET_API`：用于判定“这是我要拦截的请求”的稳定路径片段。
3. `TARGET_NOTE_COUNT`：希望采到多少条笔记。
4. `MAX_SCROLL_ROUNDS`：最多滚动多少轮。
5. `SCROLL_PAUSE`：每轮滚动后的等待时间。
6. `NO_NEW_DATA_LIMIT`：连续多少轮没有新数据就停止。
7. `IMAGE_DOWNLOAD_WORKERS`：图片下载线程数。

### 2. 启动浏览器并登录

```powershell
chrome.exe --remote-debugging-port=9222 --user-data-dir="D:\selenium_profiles\xhs_profile"
```

然后手动登录小红书。

### 3. 运行脚本

```bash
python xhs.py
```

### 4. 输出结果

运行完成后，你会得到：

1. `zaopo_cu_dataset/raw_images/`
2. `zaopo_cu_dataset/metadata/notes.csv`
3. `zaopo_cu_dataset/summary.json`

`notes.csv` 包含字段：

1. `note_id`
2. `display_title`
3. `nickname`
4. `liked_count`
5. `cover_url`
6. `image_filename`
7. `scraped_at`

## 第六步：如何运行 `tb.py`

### 先注意一个当前仓库里的已知问题

`tb.py` 依赖：

```python
from parse_comments import save_reviews
```

但当前仓库里没有提供 `parse_comments.py`。

这意味着：

1. 脚本原理是完整的。
2. 但如果你想直接运行 `tb.py`，需要自己补一个 `parse_comments.py`。
3. 这个模块至少需要提供一个函数：

```python
def save_reviews(rate_list) -> int:
    ...
```

推荐职责：

1. 接收 `rateList`
2. 做字段清洗
3. 保存为 CSV / JSON / 数据库
4. 返回本次成功保存的评论条数

### 运行步骤

1. 启动开启远程调试的 Chrome。
2. 手动进入目标商品详情页。
3. 如有需要，先登录淘宝 / 天猫账号。
4. 修改 `tb.py` 里的目标链接：

```python
target = 'https://detail.tmall.com/item.htm?abbucket=11&id=798779785100'
```

5. 补齐 `parse_comments.py`。
6. 运行：

```bash
python tb.py
```

## 如何把这套方法迁移到别的网站

如果你要在别的站点做同类采集，建议按这个顺序思考：

1. 先不要急着写爬虫，先把页面动作定义清楚。
   例如：搜索、筛选、翻页、展开评论、切换排序。
2. 用 DevTools 看这些动作引发了什么请求。
3. 判断它属于哪一类：
   - XHR
   - fetch
   - JSONP
   - WebSocket
4. 选择对应的拦截点。
   - XHR：改 `XMLHttpRequest.prototype`
   - fetch：改 `window.fetch`
   - JSONP：包 `appendChild` 和回调函数
5. 找稳定的 URL 特征，不要匹配整条 URL。
6. 先把“拦截成功”做出来，再写数据解析。
7. 数据解析确认无误后，再写滚动、翻页、下载、落盘。

## 一个好用的调试顺序

建议按下面的顺序调试，不容易迷失：

1. 先手动在浏览器里确认目标动作能触发请求。
2. 再让注入脚本只做 `console.log`，确认拦截到了。
3. 再把结果存进 `window.__intercepted_data`。
4. 再让 Selenium 通过 `execute_script` 读出来。
5. 再写 Python 端解析逻辑。
6. 最后才做循环滚动、批量下载和文件写入。

## 常见问题

### 1. Selenium 连不上浏览器

原因通常有：

1. Chrome 不是通过 `--remote-debugging-port=9222` 启动的。
2. 你启动的是另一个浏览器窗口，不是那个开启调试端口的窗口。
3. 9222 端口被占用。
4. ChromeDriver / 浏览器版本不匹配。

### 2. 注入成功了，但没有数据

常见原因：

1. 你匹配的 URL 片段不对。
2. 页面现在改成了别的接口。
3. 操作没有真正触发目标请求。
4. 页面加载太快或太慢，时机没对上。
5. 网站改成了别的传输方式。

### 3. 页面元素找不到

原因通常是：

1. CSS 类名变了。
2. 页面结构调整了。
3. 你没有先手动登录。
4. 目标按钮在弹层里，或者被遮挡。

### 4. `tb.py` 运行时报 `ModuleNotFoundError: parse_comments`

这是当前仓库的已知现状，不是你的环境问题。你需要自己补 `parse_comments.py`。

### 5. 采集结果重复

`xhs.py` 已经通过 `note_id` 做了去重，并支持读取已有 `notes.csv` 继续采集。  
如果你自己扩展别的站点，也要尽量为每条数据选择一个稳定主键。

