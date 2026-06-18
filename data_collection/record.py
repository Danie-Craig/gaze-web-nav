#!/usr/bin/env python3
"""
record.py  —  web-navigation demonstration recorder (full UI-TARS action space)

Records (screenshot, action, gaze) triples while a human performs a WebArena task,
in a format that maps 1:1 onto UI-TARS-1.5's native COMPUTER_USE action vocabulary.

UI-TARS-1.5 COMPUTER_USE action space (verbatim from the official system prompt):
    click(start_box='(x,y)')
    left_double(start_box='(x,y)')
    right_single(start_box='(x,y)')
    drag(start_box='(x,y)', end_box='(x,y)')
    hotkey(key='...')
    type(content='...')                              # \n at end submits
    scroll(start_box='(x,y)', direction='down/up/right/left')
    wait()                                           # model-time pause + re-screenshot
    finished(content='...')                          # terminal: task done / answer

How each is captured here
-------------------------
    click          -> left mousedown, no movement, no second click             (x, y)
    left_double    -> two left clicks at ~same spot within DBLCLICK_WINDOW      (x, y)
    right_single   -> right mousedown (button 2), no movement                  (x, y)
    drag           -> mousedown -> move > DRAG_THRESHOLD -> mouseup    (start x,y + end x,y)
    hotkey         -> keydown with a modifier, or Escape, or lone Enter        (key combo + raw)
    type           -> input value, flushed on click-away / Tab / Enter         (value, trigger)
    scroll         -> scroll event + live cursor position           (x, y, direction, deltas)
    wait           -> NOT captured from humans (see note below)
    finished       -> end of session; you are prompted for the answer          (content)

select (native <select>): UI-TARS has NO select primitive, and Playwright cannot
    screenshot the OS dropdown. We therefore record the *click point on the select*
    plus the element box, value, and label, and leave the exact UI-TARS mapping
    (almost certainly click-on-box) to preprocess.py. The key point vs last time:
    coordinates ARE captured.

start / navigate: recorded as non-trainable boundary markers (preprocess skips them).
    UI-TARS has no navigate action; navigation is a consequence of click/type.

NOTE on wait(): there is no honest human signal for "wait" (it is a model decision to
    pause and re-look while a page loads). Capturing it would be fabrication, so it is
    deliberately omitted here. If we ever want it, preprocess can synthesize a wait when
    it sees a long load gap between actions. This omission is intentional, not a miss.

NOTE on coordinates: spatial actions are stored in SCREENSHOT pixels. Browser events give
    CSS pixels (clientX/clientY); emit() multiplies them by devicePixelRatio so the stored
    x,y index the screenshot PNG directly (screenshots are native/device-scale). So no DPR
    scaling is needed downstream — preprocess only does the Qwen2.5-VL smart_resize to the
    model's input size (UI-TARS-1.5 uses absolute pixel coords, not 0-1000). devicePixelRatio
    and the viewport/PNG sizes are recorded in session_info.json (divide x,y by dpr to recover
    CSS px). The eval-time agent must screenshot at the SAME (device) scale to stay consistent.

NOTE on timing (matters for gaze alignment): each action row carries two times.
    `timestamp`   = the DECISION moment — when the human committed to the action while
                    looking at the screen the screenshot shows. For pointer actions this
                    is the mousedown; for type/hotkey it's the keypress; for scroll it's
                    when the scroll is detected. The screenshot is the frame from that
                    same moment, so screenshot and timestamp are aligned.
    `recorded_at` = when the row was actually written. For a single click this is
                    ~DBLCLICK_WINDOW later (we wait to rule out a double-click); for
                    everything else it ~equals `timestamp`. Preprocess windows gaze
                    against `timestamp`, NOT `recorded_at`.
"""

import asyncio
import json
import os
import shutil
import socket
import struct
import threading
import time
from datetime import datetime

from playwright.async_api import async_playwright

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────
GAZE_HOST = "127.0.0.1"
GAZE_PORT = 4242
TASK_URL = "http://liralabwidowx-alienware-aurora-r16.tail4d611e.ts.net:8082"
OUTPUT_DIR = "./recordings"

GAZE_ENABLED = False          # set True once GazePoint is calibrated and streaming

# Start already-logged-in by loading a Playwright storage-state JSON (cookies), so the
# login steps don't pollute the trajectory and the human starts in the same state the
# eval does. Point this at a local copy of WebArena's shopping_state.json. If the file
# doesn't exist, the recorder falls back to a fresh (logged-out) context.
STORAGE_STATE = "./shopping_state.json"

# Pointer-gesture tuning (tunable). Thresholds/distances are in CSS px (event space),
# applied before coordinates are scaled to screenshot px.
DRAG_THRESHOLD = 20           # px: mousedown->mouseup distance above which it's a drag
DBLCLICK_WINDOW = 0.35        # s: max gap between the two mousedowns of a double-click
DBLCLICK_DIST = 12            # px: max distance between the two clicks of a double-click
SCROLL_THRESHOLD = 50         # px: minimum scroll delta before a scroll step is recorded


# ──────────────────────────────────────────────────────────────────────────────
# Small helpers
# ──────────────────────────────────────────────────────────────────────────────
def closed_err(e):
    s = str(e).lower()
    return "closed" in s or "target" in s


def suppress_exception(task):
    """Swallow expected 'browser closed' future errors; surface real ones."""
    if not task.cancelled():
        try:
            exc = task.exception()
            if exc and not closed_err(exc):
                print(f"Task error: {exc}")
        except Exception:
            pass


def png_size(b):
    """Return [width, height] from PNG bytes (reads the IHDR header), or None."""
    try:
        if not b or len(b) < 24 or b[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        w, h = struct.unpack(">II", b[16:24])
        return [w, h]
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# GazePoint (OpenGaze API over TCP, XML stream) — runs in a background thread
# ──────────────────────────────────────────────────────────────────────────────
gaze_data = []
gaze_lock = threading.Lock()
recording = True


def connect_gazepoint():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((GAZE_HOST, GAZE_PORT))
    sock.sendall(b'<SET ID="ENABLE_SEND_POG_FIX" STATE="1" />\r\n')
    sock.sendall(b'<SET ID="ENABLE_SEND_CURSOR" STATE="1" />\r\n')
    sock.sendall(b'<SET ID="ENABLE_SEND_DATA" STATE="1" />\r\n')
    print("GazePoint connected!")
    return sock


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


# ──────────────────────────────────────────────────────────────────────────────
# Injected page listeners.
#   - mousedown is async so the screenshot call is dispatched at the earliest
#     possible moment (before click->navigation), capturing the page the human saw.
#   - All gesture *classification* happens in Python (one source of truth, and the
#     double-click timer survives navigation there).
# ──────────────────────────────────────────────────────────────────────────────
JS_LISTENERS = """
(function () {
  if (window.__recInstalled) return;
  window.__recInstalled = true;

  // Default the cursor to viewport center so a scroll performed before any mouse
  // movement on a fresh page records a sensible start_box (center) instead of (0,0).
  window.__mousePos   = { x: Math.round(window.innerWidth / 2), y: Math.round(window.innerHeight / 2) };
  window.__lastScroll = null;
  window.__isScrolling = false;
  window.__scrollTimer = null;
  window.__activeField = null;

  document.addEventListener('mousemove', function (e) {
    window.__mousePos = { x: e.clientX, y: e.clientY };
  }, true);

  // POINTER DOWN
  document.addEventListener('mousedown', async function (e) {
    try {
      // Native <select> has its own open/change path (OS dropdown not screenshottable)
      if (e.target && e.target.tagName === 'SELECT') {
        if (typeof window.__onSelectDown === 'function') {
          var r = e.target.getBoundingClientRect();
          await window.__onSelectDown(e.clientX, e.clientY, r.left, r.top, r.width, r.height);
        }
        return;
      }
      // Flush pending typed text BEFORE this pointer interaction (keeps type -> click order)
      if (window.__activeField && window.__activeField.active && window.__activeField.value.length > 0) {
        if (typeof window.__onType === 'function') {
          await window.__onType(window.__activeField.value, 'pointer');
        }
        window.__activeField.active = false;
      }
      if (typeof window.__onPointerDown === 'function') {
        await window.__onPointerDown(e.clientX, e.clientY, e.button);
      }
    } catch (err) { /* page may be navigating */ }
  }, true);

  // POINTER UP — Python classifies click / left_double / right_single / drag
  document.addEventListener('mouseup', function (e) {
    if (e.target && e.target.tagName === 'SELECT') return;
    if (typeof window.__onPointerUp === 'function') {
      window.__onPointerUp(e.clientX, e.clientY, e.button);
    }
  }, true);

  // TYPE tracking — real text inputs only.
  // We grab the screenshot the moment a text field gains FOCUS (still empty), and the
  // type action pairs with that empty-focused-field frame — the state UI-TARS sees when
  // it decides to type, matching inference. `input` just tracks the value.
  var TEXT_INPUT_TYPES = ['text', 'email', 'password', 'number', 'search', 'tel', 'url', ''];
  function isTextField(el) {
    if (!el) return false;
    var tag = el.tagName;
    return (tag === 'TEXTAREA') ||
           (tag === 'INPUT' && TEXT_INPUT_TYPES.indexOf((el.type || '').toLowerCase()) !== -1);
  }
  document.addEventListener('focusin', async function (e) {
    if (isTextField(e.target) && typeof window.__onTypeStart === 'function') {
      await window.__onTypeStart();   // buffer the empty-field screenshot
    }
  }, true);
  document.addEventListener('input', function (e) {
    if (!isTextField(e.target)) return;
    window.__activeField = { value: e.target.value, active: true };
  }, true);

  // KEYDOWN — type submission (Tab / Enter) and hotkeys (modifier combos, Escape, lone Enter)
  document.addEventListener('keydown', async function (e) {
    try {
      var k = e.key;
      if (k === 'Control' || k === 'Meta' || k === 'Alt' || k === 'Shift') {
        // Buffer the pre-press frame the instant a hotkey modifier goes down — before any
        // combo key, so before any effect. Skip lone Shift: it's held during ordinary text
        // selection and would fire constantly; real Shift hotkeys (e.g. Ctrl+Shift+A) still
        // begin with a Ctrl/Alt/Meta down, which is captured here.
        if (k !== 'Shift' && typeof window.__onModifierDown === 'function') {
          await window.__onModifierDown();
        }
        return;
      }
      var hasMod = e.ctrlKey || e.metaKey || e.altKey;

      // Submit / flush typed text (Tab, or Enter without Shift)
      var flushKey = (k === 'Tab') || (k === 'Enter' && !e.shiftKey);
      if (flushKey && !hasMod && window.__activeField && window.__activeField.active) {
        if (typeof window.__onType === 'function') {
          await window.__onType(window.__activeField.value, k);
        }
        window.__activeField.active = false;
        return; // Enter may also submit (navigation); type already captured
      }

      // Hotkeys
      var isHotkey = hasMod || (k === 'Escape') || (k === 'Enter');
      if (isHotkey) {
        var parts = [];
        if (e.ctrlKey)  parts.push('ctrl');
        if (e.metaKey)  parts.push('meta');
        if (e.altKey)   parts.push('alt');
        if (e.shiftKey) parts.push('shift');
        parts.push(k.length === 1 ? k.toLowerCase() : k.toLowerCase());
        if (typeof window.__onHotkey === 'function') {
          await window.__onHotkey(
            parts.join(' '),
            JSON.stringify({ ctrl: e.ctrlKey, meta: e.metaKey, alt: e.altKey, shift: e.shiftKey, key: k, code: e.code })
          );
        }
      }
    } catch (err) { /* ignore */ }
  }, true);

  // SELECT change — native dropdown value chosen
  document.addEventListener('change', function (e) {
    if (e.target && e.target.tagName === 'SELECT') {
      if (typeof window.__onSelect === 'function') {
        var idx = e.target.selectedIndex;
        var label = (idx >= 0 && e.target.options[idx]) ? e.target.options[idx].text : '';
        window.__onSelect(e.target.value, label);
      }
    }
  }, true);

  // SCROLL — track source (page vs element) so baselines reset correctly
  document.addEventListener('scroll', function (e) {
    window.__isScrolling = true;
    clearTimeout(window.__scrollTimer);
    window.__scrollTimer = setTimeout(function () { window.__isScrolling = false; }, 200);
    var t = e.target;
    var isPage = (t === document || t === document.documentElement || t === document.body);
    window.__lastScroll = {
      scrollX: isPage ? window.scrollX : t.scrollLeft,
      scrollY: isPage ? window.scrollY : t.scrollTop,
      isPage: isPage,
      t: Date.now()
    };
  }, true);
})();
"""


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
async def main():
    global recording

    loop = asyncio.get_event_loop()

    def handle_exception(loop, context):
        exc = context.get("exception")
        if exc and closed_err(exc):
            return
        loop.default_exception_handler(context)

    loop.set_exception_handler(handle_exception)

    # ── Session metadata ──────────────────────────────────────────────────────
    task_id = input("Task ID (e.g. 574): ").strip()

    # Resolve the session directory. Refuse to silently reuse an existing one: a prior
    # attempt at the same task/traj would leave orphan screenshots mixed in with this
    # run (the screenshots folder is the one place stale files can accumulate, since
    # actions.json / gaze.json / session_info.json are simply overwritten). Offer to
    # clear it, or let the user choose a different trajectory.
    while True:
        trajectory = input("Trajectory (e.g. 1): ").strip()
        session_dir = os.path.join(OUTPUT_DIR, f"task{task_id}_traj{trajectory}")
        if os.path.isdir(session_dir) and os.listdir(session_dir):
            ans = input(f"  '{session_dir}' already exists — overwrite it? (y/n): ").strip().lower()
            if ans == "y":
                shutil.rmtree(session_dir, ignore_errors=True)
                break
            print("  OK — pick a different trajectory number (or press Ctrl+C to quit).")
            continue
        break

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(session_dir, exist_ok=True)            # fresh or cleared — no orphan screenshots
    screenshots_dir = os.path.join(session_dir, "screenshots")
    os.makedirs(screenshots_dir, exist_ok=True)

    session_info = {
        "task_id": task_id,
        "trajectory": trajectory,
        "timestamp": timestamp,
        "gaze_enabled": GAZE_ENABLED,
        "task_url": TASK_URL,
    }
    session_info_path = os.path.join(session_dir, "session_info.json")
    with open(session_info_path, "w") as f:           # written immediately (survives discard)
        json.dump(session_info, f, indent=2)
    print(f"Session directory: {session_dir}")

    # ── GazePoint ─────────────────────────────────────────────────────────────
    gaze_sock = None
    if GAZE_ENABLED:
        try:
            gaze_sock = connect_gazepoint()
        except Exception as e:
            print(f"\n  ERROR: could not connect to GazePoint at {GAZE_HOST}:{GAZE_PORT} ({e}).")
            print("  Is GazePoint Control running and streaming (calibrated, OpenGaze enabled)?")
            print("  Aborting so you don't record a gaze-less trajectory by mistake.\n")
            return
        threading.Thread(target=read_gaze, args=(gaze_sock,), daemon=True).start()
        time.sleep(1)
    else:
        print("GazePoint disabled — running without gaze.")

    # ── Recording state ───────────────────────────────────────────────────────
    actions = []
    step = 0
    save_lock = asyncio.Lock()

    last_down = None            # {x, y, button, time, shot_task}
    pending_left = None         # {x, y, shot_task, down_time, token}
    pending_left_task = None    # asyncio task that resolves a single click after the dbl window
    pending_select = None       # {x, y, box, shot_task}
    type_shot_task = None       # pre-typing screenshot for the next type action
    hotkey_pre_task = None      # pre-press screenshot for the next hotkey (buffered on modifier-down)
    hotkey_pre_step = None      # step counter when that pre-press shot was taken (staleness guard)

    latest_screenshot = None    # most recent action screenshot (used by finished())
    dpr = 1.0                   # device pixel ratio; set after page load, used to scale coords

    last_scroll_x = 0
    last_scroll_y = 0
    last_scroll_is_page = True
    last_url = TASK_URL
    last_nav_time = 0.0

    page = None
    browser_open = True

    # ── Screenshot + emit helpers ─────────────────────────────────────────────
    async def take_shot():
        # Native (device-scale) screenshot — no live repaint, so no flicker. The PNG is
        # in device pixels (css px * dpr); emit() scales recorded coordinates by the same
        # dpr so they index this PNG directly.
        try:
            return await page.screenshot()
        except Exception:
            return None

    async def get_shot(task):
        try:
            return await task
        except Exception:
            return None

    async def emit(action_type, screenshot_bytes, details=None, event_time=None):
        # `timestamp` = the DECISION moment (when the human committed to the action and
        # was looking at the screen the screenshot shows). This is the anchor preprocess
        # uses to window gaze. `recorded_at` = when this row was actually written (which,
        # for a single click, is ~DBLCLICK_WINDOW later because we wait to rule out a
        # double). For type/hotkey/scroll/navigate/start/finished the two coincide.
        nonlocal step, latest_screenshot
        details = details or {}
        async with save_lock:
            now = time.time()
            t = event_time if event_time is not None else now
            filename = None
            if screenshot_bytes:
                latest_screenshot = screenshot_bytes        # most recent frame, for finished()
                filename = f"{step:04d}_{action_type}.png"
                try:
                    with open(os.path.join(screenshots_dir, filename), "wb") as f:
                        f.write(screenshot_bytes)
                except Exception as e:
                    print(f"  ! screenshot write failed (step {step}, {action_type}): {e}")
                    filename = None
            entry = {"step": step, "type": action_type,
                     "timestamp": t, "recorded_at": now, "screenshot": filename}
            entry.update(details)
            # Scale CSS-pixel coordinates to device pixels so they index the PNG directly.
            if dpr and dpr != 1:
                for k in ("x", "y", "start_x", "start_y", "end_x", "end_y"):
                    if isinstance(entry.get(k), (int, float)):
                        entry[k] = round(entry[k] * dpr)
                if isinstance(entry.get("box"), dict):
                    entry["box"] = {bk: round(bv * dpr) for bk, bv in entry["box"].items()}
            actions.append(entry)
            shown = {k: v for k, v in entry.items()
                     if k not in ("step", "type", "timestamp", "recorded_at", "screenshot", "raw")}
            print(f"Step {step}: {action_type} {shown}")
            step += 1

    # ── Pointer state machine ─────────────────────────────────────────────────
    async def on_pointer_down(x, y, button):
        nonlocal last_down
        # Capture coords NOW (cheap, never lost); fire the screenshot as a background
        # task so a very fast mouseup can't beat it — we await the task at emit time.
        shot_task = asyncio.ensure_future(take_shot())
        shot_task.add_done_callback(suppress_exception)
        last_down = {"x": x, "y": y, "button": button, "time": time.time(), "shot_task": shot_task}

    async def emit_pending_as_click():
        nonlocal pending_left
        if pending_left is None:
            return
        pl = pending_left
        pending_left = None
        await emit("click", await get_shot(pl["shot_task"]), {"x": pl["x"], "y": pl["y"]}, event_time=pl["down_time"])

    def schedule_single_resolve(token):
        nonlocal pending_left_task

        async def _resolve():
            try:
                await asyncio.sleep(DBLCLICK_WINDOW)
            except asyncio.CancelledError:
                return
            # Still the same un-upgraded pending click? -> it was a single click.
            if pending_left is not None and pending_left.get("token") is token:
                await emit_pending_as_click()

        pending_left_task = asyncio.ensure_future(_resolve())
        pending_left_task.add_done_callback(suppress_exception)

    async def on_pointer_up(x, y, button):
        nonlocal last_down, pending_left, pending_left_task
        if last_down is None:
            return
        ld = last_down
        last_down = None

        dx, dy = x - ld["x"], y - ld["y"]
        dist = (dx * dx + dy * dy) ** 0.5

        # DRAG — moved beyond threshold between down and up
        if dist > DRAG_THRESHOLD:
            if pending_left is not None:
                if pending_left_task:
                    pending_left_task.cancel()
                await emit_pending_as_click()
            await emit("drag", await get_shot(ld["shot_task"]),
                       {"start_x": ld["x"], "start_y": ld["y"], "end_x": x, "end_y": y},
                       event_time=ld["time"])
            return

        # RIGHT CLICK
        if button == 2:
            if pending_left is not None:
                if pending_left_task:
                    pending_left_task.cancel()
                await emit_pending_as_click()
            await emit("right_single", await get_shot(ld["shot_task"]), {"x": ld["x"], "y": ld["y"]}, event_time=ld["time"])
            return

        # LEFT CLICK — decide single vs double
        if button == 0:
            is_double = (
                pending_left is not None
                and (ld["time"] - pending_left["down_time"]) <= DBLCLICK_WINDOW
                and abs(ld["x"] - pending_left["x"]) <= DBLCLICK_DIST
                and abs(ld["y"] - pending_left["y"]) <= DBLCLICK_DIST
            )
            if is_double:
                if pending_left_task:
                    pending_left_task.cancel()
                pl = pending_left
                pending_left = None
                await emit("left_double", await get_shot(pl["shot_task"]), {"x": pl["x"], "y": pl["y"]}, event_time=pl["down_time"])
                return

            # Not a double: flush any prior pending click (e.g. rapid radio-button changes
            # at different positions), then start a fresh pending click for this one.
            if pending_left is not None:
                if pending_left_task:
                    pending_left_task.cancel()
                await emit_pending_as_click()

            token = object()
            pending_left = {
                "x": ld["x"], "y": ld["y"],
                "shot_task": ld["shot_task"],
                "down_time": ld["time"],
                "token": token,
            }
            schedule_single_resolve(token)
            return
        # middle / other buttons: ignored (no UI-TARS equivalent)

    # ── Type / hotkey ─────────────────────────────────────────────────────────
    async def on_type_start():
        # Buffer a screenshot the instant typing begins (field focused, ~empty).
        nonlocal type_shot_task
        type_shot_task = asyncio.ensure_future(take_shot())
        type_shot_task.add_done_callback(suppress_exception)

    async def on_type(value, trigger):
        nonlocal type_shot_task
        try:
            shot = await get_shot(type_shot_task) if type_shot_task is not None else await take_shot()
            type_shot_task = None
            await emit("type", shot, {"value": value, "trigger": trigger})
        except Exception as e:
            if not closed_err(e):
                print(f"Type callback error: {e}")

    async def on_modifier_down():
        # A modifier (Ctrl/Alt/Meta) just went down with no combo key yet. Ctrl alone has
        # no effect, so this is the frame BEFORE a hotkey's effect — buffer it now so a
        # combo like Ctrl+A pairs with the pre-press state instead of the post-effect one
        # (the selection that Ctrl+A produces). Each modifier-down replaces the last buffer.
        nonlocal hotkey_pre_task, hotkey_pre_step
        hotkey_pre_task = asyncio.ensure_future(take_shot())
        hotkey_pre_task.add_done_callback(suppress_exception)
        hotkey_pre_step = step

    async def on_hotkey(combo, raw_json):
        nonlocal hotkey_pre_task, hotkey_pre_step
        try:
            try:
                raw = json.loads(raw_json)
            except Exception:
                raw = {}
            # Prefer the pre-press frame buffered on modifier-down — the true "before" state
            # for Ctrl/Alt/Meta combos. Use it only if nothing has been emitted since it was
            # taken (step unchanged), so it can't be stale. Lone Escape / lone Enter have no
            # modifier-down to buffer on, so they fall back to a fresh (post-effect) shot;
            # that's acceptable since their most common target (a browser context menu) isn't
            # captured in a page screenshot anyway.
            if hotkey_pre_task is not None and hotkey_pre_step == step:
                shot = await get_shot(hotkey_pre_task)
            else:
                shot = await take_shot()
            hotkey_pre_task = None
            hotkey_pre_step = None
            await emit("hotkey", shot, {"key": combo, "raw": raw})
        except Exception as e:
            if not closed_err(e):
                print(f"Hotkey callback error: {e}")

    # ── Native <select> ───────────────────────────────────────────────────────
    async def on_select_down(x, y, left, top, w, h):
        nonlocal pending_select
        shot_task = asyncio.ensure_future(take_shot())
        shot_task.add_done_callback(suppress_exception)
        pending_select = {
            "x": x, "y": y,
            "box": {"left": left, "top": top, "width": w, "height": h},
            "shot_task": shot_task,
            "time": time.time(),
        }

    async def on_select(value, label):
        nonlocal pending_select
        try:
            if pending_select is not None:
                ps = pending_select
                pending_select = None
                await emit("select", await get_shot(ps["shot_task"]),
                           {"x": ps["x"], "y": ps["y"], "box": ps["box"],
                            "value": value, "label": label},
                           event_time=ps["time"])
            else:
                # change without a tracked mousedown (e.g. keyboard) — no coords available
                await emit("select", await take_shot(),
                           {"x": None, "y": None, "box": None, "value": value, "label": label})
        except Exception as e:
            if not closed_err(e):
                print(f"Select callback error: {e}")

    # ── Navigation (non-trainable boundary; also finalizes a pending click) ────
    async def on_navigation(frame):
        nonlocal last_nav_time, last_down, pending_left, pending_left_task
        try:
            if page is None or frame != page.main_frame:
                return
            # A navigation means any pending single click is final (no double coming).
            if pending_left is not None:
                if pending_left_task:
                    pending_left_task.cancel()
                await emit_pending_as_click()
            last_down = None

            now = time.time()
            if now - last_nav_time < 2.0:
                return
            last_nav_time = now
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            await emit("navigate", await take_shot(), {"url": page.url})
        except Exception as e:
            if not closed_err(e):
                print(f"Navigation error: {e}")

    # ── Poll loop: scroll detection + throttled "final state" screenshot ───────
    async def poll_loop():
        nonlocal last_scroll_x, last_scroll_y, last_scroll_is_page, last_url
        while browser_open:
            try:
                # Reset the scroll baseline the moment the URL changes, reading the new page's
                # ACTUAL scroll position (normally the top) instead of the last scroll EVENT's
                # stale value. Doing it here — before any scroll on the new page — means the
                # first scroll step measures its delta from the correct origin. (Previously the
                # reset was deferred until a scroll was first observed, so a fast scroll right
                # after a page change made that first step's delta_y under-count the real jump.)
                # __lastScroll is freshly null on a real navigation (the init script re-runs),
                # so this reset can't trigger a phantom scroll step.
                cur_url = page.url
                if cur_url != last_url:
                    last_url = cur_url
                    try:
                        pos = await page.evaluate("({x: window.scrollX, y: window.scrollY})")
                        last_scroll_x = pos.get("x", 0) or 0
                        last_scroll_y = pos.get("y", 0) or 0
                    except Exception:
                        last_scroll_x, last_scroll_y = 0, 0
                    last_scroll_is_page = True

                scroll = await page.evaluate("window.__lastScroll || null")
                if scroll:
                    is_page = scroll.get("isPage", True)
                    sx = scroll.get("scrollX", 0) or 0
                    sy = scroll.get("scrollY", 0) or 0

                    if is_page != last_scroll_is_page:            # switched page<->element scroll
                        last_scroll_x, last_scroll_y, last_scroll_is_page = sx, sy, is_page
                    else:
                        d_x, d_y = sx - last_scroll_x, sy - last_scroll_y
                        if max(abs(d_x), abs(d_y)) >= SCROLL_THRESHOLD:
                            if abs(d_y) >= abs(d_x):
                                direction = "down" if d_y > 0 else "up"
                            else:
                                direction = "right" if d_x > 0 else "left"
                            cur = await page.evaluate("window.__mousePos || {x:0,y:0}")
                            await emit("scroll", await take_shot(), {
                                "x": cur.get("x"), "y": cur.get("y"),
                                "direction": direction,
                                "delta_x": d_x, "delta_y": d_y,
                                "scrollX": sx, "scrollY": sy,
                                "is_page": is_page,
                            })
                            last_scroll_x, last_scroll_y = sx, sy
            except Exception as e:
                if closed_err(e):
                    break
                await asyncio.sleep(0.3)
            await asyncio.sleep(0.1)

    # ── Launch ────────────────────────────────────────────────────────────────
    print("Starting browser...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, args=["--start-maximized"])

        # Start logged-in if a storage-state file is available (keeps login out of the
        # trajectory and matches the eval's starting state). Otherwise, fresh context.
        ctx_kwargs = {"no_viewport": True}
        if STORAGE_STATE and os.path.exists(STORAGE_STATE):
            ctx_kwargs["storage_state"] = STORAGE_STATE
            print(f"Loading logged-in state from {STORAGE_STATE}")
        else:
            print("No storage_state found — starting logged-out (you'll need to sign in).")
        context = await browser.new_context(**ctx_kwargs)
        page = await context.new_page()

        await page.expose_function("__onPointerDown", on_pointer_down)
        await page.expose_function("__onPointerUp", on_pointer_up)
        await page.expose_function("__onTypeStart", on_type_start)
        await page.expose_function("__onType", on_type)
        await page.expose_function("__onHotkey", on_hotkey)
        await page.expose_function("__onModifierDown", on_modifier_down)
        await page.expose_function("__onSelectDown", on_select_down)
        await page.expose_function("__onSelect", on_select)
        await page.add_init_script(JS_LISTENERS)

        page.on("framenavigated",
                lambda frame: asyncio.ensure_future(on_navigation(frame)).add_done_callback(suppress_exception))

        last_nav_time = time.time()
        await page.goto(TASK_URL, wait_until="load")

        # Coordinate context. Screenshots are device-scale; emit() scales coordinates by
        # dpr so x,y index the PNG directly. screenshot_size = inner_size * dpr.
        try:
            ctx = await page.evaluate(
                "({dpr: window.devicePixelRatio, iw: window.innerWidth, ih: window.innerHeight,"
                " ow: window.outerWidth, oh: window.outerHeight,"
                " sw: window.screen.width, sh: window.screen.height,"
                " sx: window.screenX, sy: window.screenY})"
            )
        except Exception:
            ctx = {}
        dpr = ctx.get("dpr") or 1.0
        start_shot = await take_shot()
        ss = png_size(start_shot)
        session_info.update({
            "device_pixel_ratio": ctx.get("dpr"),
            "inner_width": ctx.get("iw"),
            "inner_height": ctx.get("ih"),
            "outer_width": ctx.get("ow"),
            "outer_height": ctx.get("oh"),
            # Screen geometry (CSS px, like inner/outer; multiply by dpr for physical px).
            # Needed to map GazePoint gaze — reported as a fraction of the physical screen —
            # onto the content area and then the screenshot. Captured once at start, so do NOT
            # move or resize the browser window mid-recording or the gaze mapping will drift.
            "screen_width": ctx.get("sw"),
            "screen_height": ctx.get("sh"),
            "window_screen_x": ctx.get("sx"),
            "window_screen_y": ctx.get("sy"),
            "screenshot_size": ss,                     # [w, h] of the actual PNG (device px)
            "coordinate_space": "screenshot_pixels (device scale); x,y index the PNG directly",
        })
        with open(session_info_path, "w") as f:
            json.dump(session_info, f, indent=2)
        print(f"  viewport(css)={ctx.get('iw')}x{ctx.get('ih')}  dpr={ctx.get('dpr')}  "
              f"screenshot={ss}  (coords are scaled by dpr to match the screenshot)")

        last_nav_time = time.time()
        await emit("start", start_shot, {"url": TASK_URL})
        last_nav_time = time.time()

        asyncio.ensure_future(poll_loop()).add_done_callback(suppress_exception)

        print("\nBrowser ready — perform the task, then CLOSE the browser window when done.\n")
        await page.wait_for_event("close", timeout=0)
        browser_open = False

    # ── Wind down (browser closed) ────────────────────────────────────────────
    recording = False
    if pending_left_task:
        pending_left_task.cancel()
    if pending_left is not None:            # don't lose a final click
        await emit_pending_as_click()

    # FINISHED — terminal action, always recorded. Captures the human's answer for
    # string-match tasks (empty otherwise). Uses the last buffered full-page screenshot.
    try:
        answer = input("\nFinal ANSWER for this task (for string-match tasks); press Enter if none: ").strip()
    except EOFError:
        answer = ""
    await emit("finished", latest_screenshot, {"content": answer})

    # Persist
    with open(os.path.join(session_dir, "actions.json"), "w") as f:
        json.dump(actions, f, indent=2)
    if GAZE_ENABLED:
        with gaze_lock:
            with open(os.path.join(session_dir, "gaze.json"), "w") as f:
                json.dump(gaze_data, f, indent=2)
    else:
        with open(os.path.join(session_dir, "gaze.json"), "w") as f:
            json.dump([], f)

    print(f"\nDone! {step} steps recorded, {len(gaze_data)} gaze points.")
    keep = input("Keep this recording? (y/n): ").strip().lower()
    if keep != "y":
        shutil.rmtree(session_dir, ignore_errors=True)
        print("Recording discarded.")
    else:
        print(f"Recording saved to {session_dir}")

    if GAZE_ENABLED and gaze_sock:
        gaze_sock.close()


if __name__ == "__main__":
    asyncio.run(main())
