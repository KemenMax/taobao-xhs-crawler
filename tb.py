from selenium.webdriver import Chrome
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
import time
import json
from parse_comments import save_reviews

# 启动配置
chrome_options = Options()
chrome_options.add_experimental_option("debuggerAddress", "localhost:9222") # 连接已打开的浏览器
# chrome_options.add_argument("--headless") # 可选: 无头模式
driver = Chrome(options=chrome_options)
wait = WebDriverWait(driver, 10)

def inject_interceptor(driver):
    """
    注入JS脚本以拦截 mtop.taobao.rate.detaillist.get 的 JSONP 回调
    """
    script = """
    (function() {
        if (window.__interceptor_injected) return;
        window.__interceptor_injected = true;
        window.__intercepted_data = [];

        var originalAppendChild = document.head.appendChild;
        document.head.appendChild = function(element) {
            if (element.tagName === 'SCRIPT' && element.src && element.src.includes('mtop.taobao.rate.detaillist.get')) {
                console.log('拦截到目标脚本:', element.src);
                var match = element.src.match(/callback=([^&]+)/);
                if (match) {
                    var callbackName = match[1];
                    
                    var checkAndWrap = function() {
                        if (window[callbackName] && !window[callbackName].__wrapped) {
                            var originalCallback = window[callbackName];
                            window[callbackName] = function(data) {
                                console.log('捕获数据:', callbackName);
                                window.__intercepted_data.push(data);
                                return originalCallback(data);
                            };
                            window[callbackName].__wrapped = true;
                        } else {
                             if (!window[callbackName]) {
                                window[callbackName] = function(data) {
                                    console.log('捕获数据 (占位符):', callbackName);
                                    window.__intercepted_data.push(data);
                                };
                             }
                        }
                    };
                    checkAndWrap();
                }
            }
            return originalAppendChild.call(document.head, element);
        };
        console.log('JSONP 拦截器已注入');
    })();
    """
    driver.execute_script(script)

def find_scrollable_element(driver):
    """
    寻找真正可以滚动的容器 (scrollHeight > clientHeight)
    """
    # 常见的评论容器选择器
    selectors = [
        ".comments--ChxC7GEN", # 通常是这个
        ".beautify-scroll-bar",
        "div[class*='comments--']",
        "div[style*='overflow']"
    ]
    
    print("正在寻找可滚动的评论容器...")
    for css in selectors:
        elements = driver.find_elements(By.CSS_SELECTOR, css)
        for elem in elements:
            try:
                # 检查是否溢出 (即内容高度大于可视高度)
                is_scrollable = driver.execute_script(
                    "return arguments[0].scrollHeight > arguments[0].clientHeight && arguments[0].clientHeight > 0;", 
                    elem
                )
                if is_scrollable:
                    print(f"找到可滚动容器: {css}")
                    return elem
            except:
                continue
    return None

def run_spider(url):
    print("开始运行爬虫...")
    driver.get(url)
    inject_interceptor(driver)

    # 1. 滚动主页面以确保按钮可见
    print("滚动主页面...")
    driver.execute_script("window.scrollTo(0, 1000);")
    time.sleep(2)

    # 2. 点击“查看全部评价”
    try:
        print("尝试点击 '查看全部' 按钮...")
        btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "div[class*='ShowButton--fMu7HZNs']")))
        driver.execute_script("arguments[0].click();", btn)
        print("点击成功，等待弹窗加载...")
    except Exception as e:
        print("未找到或无法点击 '查看全部' 按钮 (可能已打开或选择器变更)")
    
    time.sleep(3)
    inject_interceptor(driver) # 再次注入以防万一

    # 3. 定位滚动容器
    container = find_scrollable_element(driver)
    
    if not container:
        print("错误: 未找到可滚动的评论容器。请检查页面结构。")
        # 尝试使用 body 滚动作为备选，虽然不太可能是 body
        # container = driver.find_element(By.TAG_NAME, "body")
        return

    # 4. 循环滚动并收集数据
    print("开始循环滚动获取数据...")
    total_reviews = 0
    max_scrolls = 100 # 最大滚动次数
    
    last_scroll_top = -1
    retry_count = 0

    for i in range(max_scrolls):
        # 检查拦截的数据
        data_batch = driver.execute_script("return window.__intercepted_data;")
        if data_batch:
            driver.execute_script("window.__intercepted_data = [];") # 清空JS缓冲区
            
            for data in data_batch:
                rate_list = data.get('data', {}).get('rateList', [])
                count = save_reviews(rate_list)
                total_reviews += count
                print(f"已保存 {count} 条评论. 总计: {total_reviews}")

        # 执行滚动
        try:
            # 方法: 滚动到底部
            driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight;", container)
            time.sleep(2) # 等待加载
            
            # 检查是否真的动了
            current_scroll_top = driver.execute_script("return arguments[0].scrollTop;", container)
            scroll_height = driver.execute_script("return arguments[0].scrollHeight;", container)
            client_height = driver.execute_script("return arguments[0].clientHeight;", container)
            
            # print(f"当前位置: {current_scroll_top}, 总高度: {scroll_height}")
            
            if current_scroll_top == last_scroll_top:
                retry_count += 1
                if retry_count > 3:
                     print("没有更多内容加载 (滚动位置未变化)，停止。")
                     break
            else:
                retry_count = 0
                
            last_scroll_top = current_scroll_top
            
        except Exception as e:
            print(f"滚动时发生错误: {e}")
            break
        
    print(f"抓取结束。总计保存 {total_reviews} 条评论。")


if __name__ == '__main__':
    target = 'https://detail.tmall.com/item.htm?abbucket=11&id=798779785100'
    try:
        run_spider(target)
    except KeyboardInterrupt:
        print("用户手动停止。")
    except Exception as e:
        print(f"发生未捕获错误: {e}")
    finally:
        # driver.quit() 
        pass
