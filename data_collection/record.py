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

    # Connect GazePoint
    gaze_sock = connect_gazepoint()
    gaze_thread = threading.Thread(target=read_gaze, args=(gaze_sock,), daemon=True)
    gaze_thread.start()
    time.sleep(1)

    actions = []
    step = [0]

    async def save_action(action_type, details={}):
        t = time.time()
        filename = f"{step[0]:04d}_{action_type}.png"
        filepath = os.path.join(screenshots_dir, filename)
        try:
            await page.screenshot(path=filepath)
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

    print("Starting browser...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        # Intercept only navigation search requests (Enter pressed)
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

        # Re-inject listeners on every page load
        async def inject_listeners():
            try:
                await page.evaluate("""
                    // Click listener
                    document.addEventListener('click', function(e) {
                        window.__lastClick = {x: e.clientX, y: e.clientY, t: Date.now()};
                    });

                    // Scroll listener
                    window.addEventListener('scroll', function(e) {
                        window.__lastScroll = {
                            scrollX: window.scrollX,
                            scrollY: window.scrollY,
                            t: Date.now()
                        };
                    });

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

                    // Capture type on Tab only (Enter handled by request interceptor)
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

                    // Capture type on click while field was active
                    document.addEventListener('mousedown', function(e) {
                        if (window.__activeField && window.__activeField.active && window.__activeField.value.length > 0) {
                            window.__lastType = {
                                value: window.__activeField.value,
                                trigger: 'click',
                                t: Date.now()
                            };
                            window.__activeField.active = false;
                        }
                    });
                """)
                print("Listeners injected!")
            except Exception as e:
                print(f"Inject error: {e}")

        page.on("load", lambda: asyncio.ensure_future(inject_listeners()))
        await page.goto(TASK_URL)
        await inject_listeners()

        # Poll for actions
        async def poll_actions():
            last_click = None
            last_scroll_y = 0
            last_type = None

            while browser_open:
                try:
                    # Check type (Tab and click triggers only)
                    typed = await page.evaluate("window.__lastType || null")
                    if typed and typed != last_type:
                        last_type = typed
                        await save_action("type", {
                            "value": typed["value"],
                            "trigger": typed["trigger"]
                        })

                    # Check click
                    click = await page.evaluate("window.__lastClick || null")
                    if click and click != last_click:
                        last_click = click
                        await save_action("click", {"x": click["x"], "y": click["y"]})

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
                    last_click = None
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
    with gaze_lock:
        with open(os.path.join(session_dir, "gaze.json"), "w") as f:
            json.dump(gaze_data, f, indent=2)

    print(f"\nDone! {step[0]} steps, {len(gaze_data)} gaze points saved.")

    # Keep or discard prompt
    keep = input("Keep this recording? (y/n): ").strip().lower()
    if keep != "y":
        shutil.rmtree(session_dir)
        print("Recording discarded.")
    else:
        print(f"Recording saved to {session_dir}")

    gaze_sock.close()

asyncio.run(main())