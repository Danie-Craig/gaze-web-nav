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

# JS listeners — injected on every page via add_init_script
JS_LISTENERS = """
    // Mousedown listener — fires BEFORE navigation starts
    document.addEventListener('mousedown', function(e) {
        if (e.button !== 0) return;  // left click only

        // Capture type if field was active
        if (window.__activeField && window.__activeField.active && window.__activeField.value.length > 0) {
            window.__lastType = {
                value: window.__activeField.value,
                trigger: 'click',
                t: Date.now()
            };
            window.__activeField.active = false;
        }

        // Report click for screenshot
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
    }, true);  // capture=true catches scroll on ALL elements including side cart

    // Type listener — tracks active field value
    function handleFieldChange(e) {
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') {
            window.__activeField = {
                value: e.target.value,
                t: Date.now(),
                active: true
            };
        }
    }
    document.addEventListener('input', handleFieldChange);
    document.addEventListener('change', handleFieldChange);
    document.addEventListener('keyup', function(e) {
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') {
            window.__activeField = {
                value: e.target.value,
                t: Date.now(),
                active: true
            };
        }
    });

    // Capture type on Tab
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Tab' && window.__activeField && window.__activeField.active) {
            window.__lastType = {
                value: window.__activeField.value,
                trigger: 'Tab',
                t: Date.now()
            };
            window.__activeField.active = false;
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

        setTimeout(function() {
            observer.disconnect();
        }, 500);
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
                        gaze_data.append({
                            "t": time.time(),
                            "raw": line
                        })
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

    # Create output directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = os.path.join(OUTPUT_DIR, timestamp)
    os.makedirs(session_dir, exist_ok=True)
    screenshots_dir = os.path.join(session_dir, "screenshots")
    os.makedirs(screenshots_dir, exist_ok=True)
    print(f"Session directory: {session_dir}")

    # Connect GazePoint if enabled
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
    pending_hover = [None]
    page_ref = [None]
    last_click_time = [0]
    last_nav_time = [0]

    async def save_action(action_type, details={}):
        t = time.time()
        filename = f"{step[0]:04d}_{action_type}.png"
        filepath = os.path.join(screenshots_dir, filename)
        try:
            await page_ref[0].screenshot(path=filepath)
        except Exception as e:
            print(f"Screenshot error: {e}")
            return
        entry = {
            "step": step[0],
            "type": action_type,
            "timestamp": t,
            "screenshot": filename
        }
        entry.update(details)
        actions.append(entry)
        print(f"Step {step[0]}: {action_type} - {details}")
        step[0] += 1

    async def save_pending_hover():
        if pending_hover[0] is None:
            return
        hover = pending_hover[0]
        pending_hover[0] = None
        filename = f"{step[0]:04d}_hover.png"
        filepath = os.path.join(screenshots_dir, filename)
        with open(filepath, "wb") as f:
            f.write(hover["screenshot_bytes"])
        entry = {
            "step": step[0],
            "type": "hover",
            "timestamp": hover["timestamp"],
            "screenshot": filename,
            "x": hover["x"],
            "y": hover["y"]
        }
        actions.append(entry)
        print(f"Step {step[0]}: hover at ({hover['x']}, {hover['y']}) — saved")
        step[0] += 1

    # Mousedown callback — fires BEFORE navigation, captures current page state
    async def on_click_callback(x, y):
        now = time.time()
        if now - last_click_time[0] < 0.5:
            return
        last_click_time[0] = now

        if pending_hover[0] is not None:
            elapsed = now - pending_hover[0]["timestamp"]
            if elapsed <= HOVER_CLICK_TIMEOUT:
                await save_pending_hover()
            else:
                pending_hover[0] = None

        # Take screenshot IMMEDIATELY on mousedown — before navigation starts
        await save_action("click", {"x": x, "y": y})

    # Capture screenshot after every page navigation
    async def on_navigation(frame):
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

    print("Starting browser...")
    async with async_playwright() as p:
        # Launch maximized for full screen real estate
        browser = await p.chromium.launch(
            headless=False,
            args=["--start-maximized"]
        )
        context = await browser.new_context(no_viewport=True)
        page = await context.new_page()
        page_ref[0] = page

        # Expose click callback via mousedown
        await page.expose_function("__reportClick", on_click_callback)

        # Add init script — runs automatically on every navigation
        await page.add_init_script(JS_LISTENERS)

        # Capture screenshot after every page navigation
        page.on("framenavigated", lambda frame: asyncio.ensure_future(on_navigation(frame)))

        # Intercept only navigation search requests
        async def on_request(request):
            if "q=" in request.url and request.is_navigation_request():
                parsed = urllib.parse.urlparse(request.url)
                params = urllib.parse.parse_qs(parsed.query)
                if "q" in params:
                    query = params["q"][0].strip()
                    if query:
                        await save_action("type", {
                            "value": query,
                            "trigger": "search"
                        })

        page.on("request", lambda req: asyncio.ensure_future(on_request(req)))

        await page.goto(TASK_URL)

        # Capture initial page state as step 0
        await save_action("start", {})

        # Poll for scroll, hover, and type only
        async def poll_actions():
            last_scroll_y = 0
            last_type = None
            last_hover_pos = None

            while browser_open:
                try:
                    # Check type
                    typed = await page.evaluate("window.__lastType || null")
                    if typed and typed != last_type:
                        last_type = typed
                        await save_action("type", {
                            "value": typed["value"],
                            "trigger": typed["trigger"]
                        })

                    # Check hover — only trigger if position moved > threshold
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
                                print(f"Hover detected at ({hx}, {hy}) — screenshot taken, waiting for click...")
                            except Exception as e:
                                print(f"Hover screenshot error: {e}")

                    # Check scroll
                    scroll = await page.evaluate("window.__lastScroll || null")
                    if scroll:
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
                    last_type = None
                await asyncio.sleep(0.1)

        asyncio.ensure_future(poll_actions())

        print("Browser ready. Start browsing! Close browser when done.")
        await page.wait_for_event("close", timeout=0)
        browser_open = False

    # Save logs
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

    # Keep or discard prompt
    keep = input("Keep this recording? (y/n): ").strip().lower()
    if keep != "y":
        shutil.rmtree(session_dir)
        print("Recording discarded.")
    else:
        print(f"Recording saved to {session_dir}")

    if GAZE_ENABLED and gaze_sock:
        gaze_sock.close()

asyncio.run(main())