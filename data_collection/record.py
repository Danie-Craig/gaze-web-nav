import asyncio
import json
import socket
import time
import os
import threading
import shutil
import urllib.parse
from datetime import datetime
from playwright.async_api import async_playwright

# Config
GAZE_HOST = "127.0.0.1"
GAZE_PORT = 4242
TASK_URL = "http://liralabwidowx-alienware-aurora-r16.tail4d611e.ts.net:8082"
OUTPUT_DIR = "./recordings"
SCROLL_THRESHOLD = 50
HOVER_CLICK_TIMEOUT = 5.0
HOVER_MOVE_THRESHOLD = 10  # pixels
GAZE_ENABLED = False  # Set to True when GazePoint is connected

def suppress_exception(task):
    """Suppress Future exception warnings for expected browser-close errors."""
    if not task.cancelled():
        try:
            exc = task.exception()
            if exc and not ("closed" in str(exc).lower() or "target" in str(exc).lower()):
                print(f"Task error: {exc}")
        except Exception:
            pass

# JS listeners — injected on every page via add_init_script
JS_LISTENERS = """
    // Mousedown — fires BEFORE navigation
    document.addEventListener('mousedown', function(e) {
        if (e.button !== 0) return;

        // SELECT elements handled separately
        if (e.target.tagName === 'SELECT') {
            if (typeof window.__reportSelectClick === 'function') {
                window.__reportSelectClick(e.clientX, e.clientY);
            }
            return;
        }

        // Report type FIRST (before click) if field was active
        if (window.__activeField && window.__activeField.active && window.__activeField.value.length > 0) {
            if (typeof window.__reportType === 'function') {
                window.__reportType(window.__activeField.value, 'click');
            }
            window.__activeField.active = false;
        }

        // Then report click
        if (typeof window.__reportClick === 'function') {
            window.__reportClick(e.clientX, e.clientY);
        }
    });

    // Scroll listener + scroll guard — captures ALL scrollable elements
    window.__isScrolling = false;
    window.__scrollTimer = null;
    document.addEventListener('scroll', function(e) {
        window.__isScrolling = true;
        clearTimeout(window.__scrollTimer);
        window.__scrollTimer = setTimeout(function() {
            window.__isScrolling = false;
        }, 200);
        var target = e.target;
        var isPage = (target === document || target === document.documentElement || target === document.body);
        window.__lastScroll = {
            scrollX: isPage ? window.scrollX : target.scrollLeft,
            scrollY: isPage ? window.scrollY : target.scrollTop,
            t: Date.now()
        };
    }, true);

    // Type listener — whitelist of text input types only
    var TEXT_INPUT_TYPES = ['text', 'email', 'password', 'number', 'search', 'tel', 'url', ''];
    function handleFieldChange(e) {
        if (e.target.tagName === 'TEXTAREA') {
            window.__activeField = {
                value: e.target.value,
                t: Date.now(),
                active: true
            };
            return;
        }
        if (e.target.tagName === 'INPUT') {
            if (TEXT_INPUT_TYPES.indexOf(e.target.type.toLowerCase()) === -1) return;
            window.__activeField = {
                value: e.target.value,
                t: Date.now(),
                active: true
            };
        }
    }
    document.addEventListener('input', handleFieldChange);

    // Capture type on Tab — via expose_function for correct ordering
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Tab' && window.__activeField && window.__activeField.active) {
            if (typeof window.__reportType === 'function') {
                window.__reportType(window.__activeField.value, 'Tab');
            }
            window.__activeField.active = false;
        }
    });

    // SELECT — calls Python directly via expose_function
    document.addEventListener('change', function(e) {
        if (e.target.tagName === 'SELECT') {
            if (typeof window.__reportSelect === 'function') {
                window.__reportSelect(
                    e.target.value,
                    e.target.options[e.target.selectedIndex].text
                );
            }
        }
    });

    // Hover + MutationObserver with scroll guard
    window.__lastHover = null;
    document.addEventListener('mouseover', function(e) {
        if (window.__isScrolling) return;
        window.__lastHover = {
            x: e.clientX,
            y: e.clientY,
            t: Date.now(),
            domChanged: false
        };
        var observer = new MutationObserver(function(mutations) {
            if (!window.__isScrolling && window.__lastHover) {
                window.__lastHover.domChanged = true;
            }
            observer.disconnect();
        });
        observer.observe(document.body, {
            childList: true,
            subtree: true,
            attributes: true
        });
        setTimeout(function() { observer.disconnect(); }, 500);
    });
"""

# Global
gaze_data = []
gaze_lock = threading.Lock()
recording = True
browser_open = True

def read_gaze(sock):
    buffer = ""
    while recording:
        try:
            sock.settimeout(1.0)
            data = sock.recv(4096).decode("utf-8")
            buffer += data
            while "\r\n" in buffer:
                line, buffer = buffer.split("\r\n", 1)
                if "FPOGX" in line:
                    with gaze_lock:
                        gaze_data.append({"t": time.time(), "raw": line})
        except socket.timeout:
            continue
        except Exception as e:
            print(f"Gaze error: {e}")
            break

def connect_gazepoint():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((GAZE_HOST, GAZE_PORT))
    sock.sendall(b'<SET ID="ENABLE_SEND_POG_FIX" STATE="1" />\r\n')
    sock.sendall(b'<SET ID="ENABLE_SEND_CURSOR" STATE="1" />\r\n')
    sock.sendall(b'<SET ID="ENABLE_SEND_DATA" STATE="1" />\r\n')
    print("GazePoint connected!")
    return sock

async def main():
    global recording, browser_open

    # Suppress Future exception warnings from Playwright internals on browser close
    loop = asyncio.get_event_loop()
    def handle_exception(loop, context):
        exc = context.get('exception')
        if exc and ("closed" in str(exc).lower() or "target" in str(exc).lower()):
            return  # Suppress expected browser-close errors
        loop.default_exception_handler(context)
    loop.set_exception_handler(handle_exception)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = os.path.join(OUTPUT_DIR, timestamp)
    os.makedirs(session_dir, exist_ok=True)
    screenshots_dir = os.path.join(session_dir, "screenshots")
    os.makedirs(screenshots_dir, exist_ok=True)
    print(f"Session directory: {session_dir}")

    gaze_sock = None
    if GAZE_ENABLED:
        gaze_sock = connect_gazepoint()
        gaze_thread = threading.Thread(target=read_gaze, args=(gaze_sock,), daemon=True)
        gaze_thread.start()
        time.sleep(1)
    else:
        print("GazePoint disabled — running without gaze.")

    actions = []
    step = [0]
    save_lock = asyncio.Lock()
    pending_hover = [None]
    pending_select_screenshot = [None]
    page_ref = [None]
    last_click_time = [0]
    last_click_pos = [0, 0]
    last_nav_time = [0]

    async def save_action(action_type, details={}):
        async with save_lock:
            t = time.time()
            filename = f"{step[0]:04d}_{action_type}.png"
            filepath = os.path.join(screenshots_dir, filename)
            try:
                await page_ref[0].screenshot(path=filepath)
            except Exception as e:
                print(f"Screenshot error: {e}")
                return
            entry = {"step": step[0], "type": action_type, "timestamp": t, "screenshot": filename}
            entry.update(details)
            actions.append(entry)
            print(f"Step {step[0]}: {action_type} - {details}")
            step[0] += 1

    async def save_action_with_bytes(action_type, screenshot_bytes, details={}):
        async with save_lock:
            t = time.time()
            filename = f"{step[0]:04d}_{action_type}.png"
            filepath = os.path.join(screenshots_dir, filename)
            try:
                with open(filepath, "wb") as f:
                    f.write(screenshot_bytes)
            except Exception as e:
                print(f"Screenshot error: {e}")
                return
            entry = {"step": step[0], "type": action_type, "timestamp": t, "screenshot": filename}
            entry.update(details)
            actions.append(entry)
            print(f"Step {step[0]}: {action_type} - {details}")
            step[0] += 1

    async def on_type_callback(value, trigger):
        try:
            await save_action("type", {"value": value, "trigger": trigger})
        except Exception as e:
            if not ("closed" in str(e).lower() or "target" in str(e).lower()):
                print(f"Type callback error: {e}")

    async def on_click_callback(x, y):
        try:
            now = time.time()
            same_pos = (
                abs(x - last_click_pos[0]) < 10 and
                abs(y - last_click_pos[1]) < 10
            )
            if same_pos and now - last_click_time[0] < 0.3:
                return
            last_click_time[0] = now
            last_click_pos[0] = x
            last_click_pos[1] = y

            if pending_hover[0] is not None:
                elapsed = now - pending_hover[0]["timestamp"]
                if elapsed <= HOVER_CLICK_TIMEOUT:
                    hover_bytes = pending_hover[0]["screenshot_bytes"]
                    pending_hover[0] = None
                    await save_action_with_bytes("click", hover_bytes, {"x": x, "y": y})
                    return
                else:
                    pending_hover[0] = None

            await save_action("click", {"x": x, "y": y})
        except Exception as e:
            if not ("closed" in str(e).lower() or "target" in str(e).lower()):
                print(f"Click callback error: {e}")

    async def on_select_click_callback(x, y):
        try:
            screenshot_bytes = await page_ref[0].screenshot()
            pending_select_screenshot[0] = screenshot_bytes
        except Exception as e:
            if not ("closed" in str(e).lower() or "target" in str(e).lower()):
                print(f"Select click screenshot error: {e}")

    async def on_select_callback(value, label):
        try:
            async with save_lock:
                t = time.time()
                filename = f"{step[0]:04d}_select.png"
                filepath = os.path.join(screenshots_dir, filename)
                if pending_select_screenshot[0]:
                    with open(filepath, "wb") as f:
                        f.write(pending_select_screenshot[0])
                    pending_select_screenshot[0] = None
                else:
                    try:
                        await page_ref[0].screenshot(path=filepath)
                    except Exception as e:
                        if "closed" in str(e).lower() or "target" in str(e).lower():
                            return
                        print(f"Select screenshot error: {e}")
                        return
                entry = {
                    "step": step[0],
                    "type": "select",
                    "timestamp": t,
                    "screenshot": filename,
                    "value": value,
                    "label": label
                }
                actions.append(entry)
                print(f"Step {step[0]}: select - {{'value': '{value}', 'label': '{label}'}}")
                step[0] += 1
        except Exception as e:
            if not ("closed" in str(e).lower() or "target" in str(e).lower()):
                print(f"Select callback error: {e}")

    async def on_navigation(frame):
        try:
            if frame != page_ref[0].main_frame:
                return
            now = time.time()
            if now - last_nav_time[0] < 2.0:
                return
            last_nav_time[0] = now
            try:
                await page_ref[0].wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            await save_action("navigate", {"url": page_ref[0].url})
        except Exception as e:
            if not ("closed" in str(e).lower() or "target" in str(e).lower()):
                print(f"Navigation error: {e}")

    print("Starting browser...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--start-maximized"]
        )
        context = await browser.new_context(no_viewport=True)
        page = await context.new_page()
        page_ref[0] = page

        await page.expose_function("__reportType", on_type_callback)
        await page.expose_function("__reportClick", on_click_callback)
        await page.expose_function("__reportSelectClick", on_select_click_callback)
        await page.expose_function("__reportSelect", on_select_callback)
        await page.add_init_script(JS_LISTENERS)

        page.on("framenavigated", lambda frame:
            asyncio.ensure_future(on_navigation(frame)).add_done_callback(suppress_exception))

        async def on_request(request):
            try:
                if "q=" in request.url and request.is_navigation_request():
                    parsed = urllib.parse.urlparse(request.url)
                    params = urllib.parse.parse_qs(parsed.query)
                    if "q" in params:
                        query = params["q"][0].strip()
                        if query:
                            await save_action("type", {"value": query, "trigger": "search"})
            except Exception as e:
                if not ("closed" in str(e).lower() or "target" in str(e).lower()):
                    print(f"Request handler error: {e}")

        page.on("request", lambda req:
            asyncio.ensure_future(on_request(req)).add_done_callback(suppress_exception))

        last_nav_time[0] = time.time()
        await page.goto(TASK_URL)
        await save_action("start", {})
        last_nav_time[0] = time.time()

        async def poll_actions():
            last_scroll_y = 0
            last_hover_pos = None
            last_url = [page_ref[0].url]

            while browser_open:
                try:
                    hover = await page.evaluate("window.__lastHover || null")
                    if hover and hover.get("domChanged"):
                        hx, hy = hover["x"], hover["y"]
                        is_new_position = (
                            last_hover_pos is None or
                            abs(hx - last_hover_pos[0]) > HOVER_MOVE_THRESHOLD or
                            abs(hy - last_hover_pos[1]) > HOVER_MOVE_THRESHOLD
                        )
                        if is_new_position:
                            last_hover_pos = (hx, hy)
                            try:
                                screenshot_bytes = await page.screenshot()
                                pending_hover[0] = {
                                    "timestamp": time.time(),
                                    "x": hx,
                                    "y": hy,
                                    "screenshot_bytes": screenshot_bytes
                                }
                                print(f"Hover stored at ({hx}, {hy}) — waiting for click...")
                            except Exception as e:
                                if not ("closed" in str(e).lower() or "target" in str(e).lower()):
                                    print(f"Hover screenshot error: {e}")

                    scroll = await page.evaluate("window.__lastScroll || null")
                    if scroll:
                        current_url = page_ref[0].url
                        if current_url != last_url[0]:
                            last_url[0] = current_url
                            last_scroll_y = scroll["scrollY"]
                        else:
                            delta = abs(scroll["scrollY"] - last_scroll_y)
                            if delta >= SCROLL_THRESHOLD:
                                last_scroll_y = scroll["scrollY"]
                                await save_action("scroll", {
                                    "scrollX": scroll["scrollX"],
                                    "scrollY": scroll["scrollY"]
                                })

                except Exception as e:
                    if "closed" in str(e).lower() or "target" in str(e).lower():
                        break
                    await asyncio.sleep(0.3)
                await asyncio.sleep(0.1)

        asyncio.ensure_future(poll_actions()).add_done_callback(suppress_exception)

        print("Browser ready. Start browsing! Close browser when done.")
        await page.wait_for_event("close", timeout=0)
        browser_open = False

    recording = False
    with open(os.path.join(session_dir, "actions.json"), "w") as f:
        json.dump(actions, f, indent=2)
    if GAZE_ENABLED:
        with gaze_lock:
            with open(os.path.join(session_dir, "gaze.json"), "w") as f:
                json.dump(gaze_data, f, indent=2)
    else:
        with open(os.path.join(session_dir, "gaze.json"), "w") as f:
            json.dump([], f)

    print(f"\nDone! {step[0]} steps, {len(gaze_data)} gaze points saved.")

    keep = input("Keep this recording? (y/n): ").strip().lower()
    if keep != "y":
        shutil.rmtree(session_dir)
        print("Recording discarded.")
    else:
        print(f"Recording saved to {session_dir}")

    if GAZE_ENABLED and gaze_sock:
        gaze_sock.close()

asyncio.run(main())