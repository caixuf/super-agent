#!/usr/bin/env python3
"""Zero-dependency browser portal for Hermes Bot Desktop (RFB over WebSocket).

Provides a pure-stdlib HTTP + RFC 6455 WebSocket server so operators can watch,
start/stop, take over (2FA/CAPTCHA/login), and hand back Bot Desktop screens
directly in a web browser without installing a separate desktop client.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import struct
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
import urllib.parse

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agentcore.screen_lease import (
    acquire_bot_desktop_lease,
    read_bot_desktop_lease,
    release_bot_desktop_lease,
    viewer_holds_lease,
)
from install.setup_screen import resolve_hermes_bin, resolve_hermes_home

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_CUT_TEXT = 256 * 1024
_INPUT_TYPES = frozenset({4, 5, 6, 251, 255})
_FIXED_CLIENT_MSG_LEN = {
    0: 20,    # SetPixelFormat
    3: 10,    # FramebufferUpdateRequest
    4: 8,     # KeyEvent
    5: 6,     # PointerEvent
    150: 10,  # EnableContinuousUpdates
    255: 12,  # QEMU Extended KeyEvent
}


class RfbInputGate:
    """RFC 6143 client->server byte stream parser enforcing human takeover lease."""

    def __init__(self, allow_input: Callable[[], bool]) -> None:
        self._allow_input = allow_input
        self._buf = bytearray()
        self._handshake_left = 12 + 1 + 1  # ProtocolVersion(12) + SecurityType(1) + ClientInit(1)

    def feed(self, chunk: bytes) -> bytes:
        self._buf.extend(chunk)
        out = bytearray()
        if self._handshake_left > 0:
            take = min(self._handshake_left, len(self._buf))
            if take:
                head = bytes(self._buf[:take])
                if self._handshake_left - take == 0:
                    head = head[:-1] + b"\x01"  # Force shared=1
                out.extend(head)
                del self._buf[:take]
                self._handshake_left -= take
            if self._handshake_left > 0:
                return bytes(out)

        while self._buf:
            msg_len = self._next_len()
            if msg_len is None or len(self._buf) < msg_len:
                break
            msg = bytes(self._buf[:msg_len])
            del self._buf[:msg_len]
            if msg[0] in _INPUT_TYPES and not self._allow_input():
                continue
            out.extend(msg)
        return bytes(out)

    def _next_len(self) -> Optional[int]:
        msg_type = self._buf[0]
        fixed = _FIXED_CLIENT_MSG_LEN.get(msg_type)
        if fixed is not None:
            return fixed
        if msg_type == 2:  # SetEncodings
            if len(self._buf) < 4:
                return None
            count = int.from_bytes(self._buf[2:4], "big")
            return 4 + 4 * count
        if msg_type == 6:  # ClientCutText
            if len(self._buf) < 8:
                return None
            length = int.from_bytes(self._buf[4:8], "big", signed=True)
            if abs(length) > _MAX_CUT_TEXT:
                raise ValueError("clipboard payload exceeds limit")
            return 8 + abs(length)
        if msg_type == 248:  # Fence
            if len(self._buf) < 9:
                return None
            return 9 + self._buf[8]
        if msg_type == 251:  # SetDesktopSize
            if len(self._buf) < 8:
                return None
            return 8 + 16 * self._buf[6]
        raise ValueError(f"unsupported RFB client message type: {msg_type}")


def discover_profiles(default_home: Path) -> Dict[str, Path]:
    """Return mapping of profile_name -> hermes_home directory."""
    profiles: Dict[str, Path] = {}
    if default_home.parent.name == "profiles":
        profiles_dir = default_home.parent
    else:
        profiles_dir = default_home / "profiles"

    if profiles_dir.is_dir():
        for entry in sorted(profiles_dir.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                profiles[entry.name] = entry.resolve()

    default_name = default_home.name if default_home.parent.name == "profiles" else "default"
    profiles.setdefault(default_name, default_home.resolve())
    return profiles


def inspect_profile_screen(profile_name: str, profile_home: Path, viewer_id: str = "") -> Dict[str, Any]:
    """Build live status dictionary for a single profile's Bot Desktop."""
    bd_dir = profile_home / "bot-desktop"
    sock_path = bd_dir / "rfb.sock"
    pid_path = bd_dir / "launcher.pid"
    disp_path = bd_dir / "display"

    pid: Optional[int] = None
    pid_alive = False
    if pid_path.is_file():
        try:
            tokens = pid_path.read_text(encoding="utf-8").split()
            if tokens:
                pid = int(tokens[0])
                os.kill(pid, 0)
                pid_alive = True
        except (OSError, ValueError):
            pid_alive = False

    display_str: Optional[str] = None
    if disp_path.is_file():
        try:
            raw_disp = disp_path.read_text(encoding="utf-8").strip()
            if raw_disp:
                display_str = raw_disp if raw_disp.startswith(":") else f":{raw_disp}"
        except OSError:
            display_str = None

    running = sock_path.exists() and pid_alive
    lease = read_bot_desktop_lease(profile_home).to_public_dict()
    i_hold = viewer_holds_lease(profile_home, viewer_id) if viewer_id else False

    return {
        "profile": profile_name,
        "hermes_home": str(profile_home),
        "running": running,
        "pid": pid if running else None,
        "display": display_str if running else None,
        "lease": lease,
        "viewer_holds_control": i_hold,
    }


def _ws_accept_key(sec_key: str) -> str:
    digest = hashlib.sha1((sec_key.strip() + _WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


async def _ws_read_frame(reader: asyncio.StreamReader) -> Optional[Tuple[int, bytes]]:
    try:
        head = await reader.readexactly(2)
    except (asyncio.IncompleteReadError, ConnectionError):
        return None
    b0, b1 = head[0], head[1]
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    if length > 4 * 1024 * 1024:
        raise ValueError("WebSocket frame too large")
    mask_key = await reader.readexactly(4) if masked else b""
    payload = bytearray(await reader.readexactly(length))
    if masked:
        for i in range(length):
            payload[i] ^= mask_key[i & 3]
    return opcode, bytes(payload)


def _ws_encode_frame(payload: bytes, opcode: int = 0x02) -> bytes:
    n = len(payload)
    header = bytearray([0x80 | (opcode & 0x0F)])
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", n))
    return bytes(header) + payload


_PORTAL_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Super Agent 实时屏幕控制台</title>
<style>
  :root {
    --bg: #0f1116;
    --panel: #171b24;
    --border: #283040;
    --text: #e5e9f0;
    --muted: #939db0;
    --accent: #3b82f6;
    --danger: #dc2626;
    --ok: #16a34a;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
  }
  header {
    background: var(--panel);
    border-bottom: 1px solid var(--border);
    padding: 10px 16px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 12px;
    font-weight: 600;
    font-size: 15px;
  }
  select, button {
    background: #1f2532;
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 12px;
    font-size: 13px;
    cursor: pointer;
  }
  button:hover:not(:disabled) { border-color: #4b5874; background: #262e3f; }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.danger { background: var(--danger); border-color: var(--danger); color: #fff; }
  .status-row {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 13px;
    color: var(--muted);
  }
  .badge {
    padding: 3px 8px;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 600;
    border: 1px solid var(--border);
    background: #1d2330;
    color: var(--text);
  }
  .badge.ok { border-color: rgba(22,163,74,0.5); color: #4ade80; }
  .badge.takeover { border-color: rgba(220,38,38,0.8); background: rgba(220,38,38,0.18); color: #fca5a5; }
  .controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  main {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 12px;
    overflow: hidden;
    position: relative;
  }
  .screen-frame {
    border: 2px solid var(--border);
    border-radius: 8px;
    background: #090b0f;
    max-width: 100%;
    max-height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
    position: relative;
    overflow: hidden;
    box-shadow: 0 12px 32px rgba(0,0,0,0.45);
  }
  .screen-frame.takeover-active {
    border-color: var(--danger);
    box-shadow: 0 0 0 2px rgba(220,38,38,0.35), 0 12px 32px rgba(0,0,0,0.55);
  }
  canvas {
    display: block;
    max-width: calc(100vw - 32px);
    max-height: calc(100vh - 86px);
    width: auto;
    height: auto;
    outline: none;
  }
  .overlay-msg {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 12px;
    background: rgba(12, 15, 20, 0.86);
    color: var(--muted);
    font-size: 14px;
    pointer-events: auto;
  }
  .hidden { display: none !important; }
</style>
</head>
<body>
<header>
  <div class="brand">
    <span>Super Agent 实时屏幕</span>
    <label for="profileSelect" style="font-weight:400;color:var(--muted);font-size:13px;">智能体：</label>
    <select id="profileSelect"></select>
    <span id="runBadge" class="badge">检测中</span>
    <span id="leaseBadge" class="badge">Bot 控制中</span>
    <span id="dispInfo" style="color:var(--muted);font-size:12px;"></span>
  </div>
  <div class="controls">
    <button id="btnStart">启动屏幕</button>
    <button id="btnTakeover" class="primary">接管屏幕 (登录/验证码)</button>
    <button id="btnRelease" class="danger hidden">交还给 Bot</button>
    <button id="btnForceRelease" class="hidden">强制交还</button>
    <button id="btnStop">停止屏幕</button>
  </div>
</header>
<main>
  <div id="screenFrame" class="screen-frame">
    <canvas id="vncCanvas" width="1440" height="900" tabindex="0"></canvas>
    <div id="overlay" class="overlay-msg">
      <div id="overlayText">正在连接桌面流...</div>
    </div>
  </div>
</main>
<script>
(() => {
  const VIEWER_KEY = "superagent_screen_viewer_id";
  let viewerId = sessionStorage.getItem(VIEWER_KEY);
  if (!viewerId) {
    const arr = new Uint8Array(12);
    crypto.getRandomValues(arr);
    viewerId = Array.from(arr, b => b.toString(16).padStart(2, "0")).join("");
    sessionStorage.setItem(VIEWER_KEY, viewerId);
  }

  const sel = document.getElementById("profileSelect");
  const runBadge = document.getElementById("runBadge");
  const leaseBadge = document.getElementById("leaseBadge");
  const dispInfo = document.getElementById("dispInfo");
  const btnStart = document.getElementById("btnStart");
  const btnStop = document.getElementById("btnStop");
  const btnTakeover = document.getElementById("btnTakeover");
  const btnRelease = document.getElementById("btnRelease");
  const btnForceRelease = document.getElementById("btnForceRelease");
  const frameEl = document.getElementById("screenFrame");
  const canvas = document.getElementById("vncCanvas");
  const ctx = canvas.getContext("2d", { alpha: false });
  const overlay = document.getElementById("overlay");
  const overlayText = document.getElementById("overlayText");

  let currentProfile = "__DEFAULT_PROFILE__";
  let profileMap = {};
  let ws = null;
  let reconnectTimer = null;
  let iHoldControl = false;

  async function api(path, method = "GET", body = null) {
    const opts = { method, headers: {} };
    if (body !== null) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const r = await fetch(path, opts);
    return await r.json();
  }

  function renderState(st) {
    if (!st) return;
    iHoldControl = !!st.viewer_holds_control;
    if (st.running) {
      runBadge.textContent = "运行中";
      runBadge.className = "badge ok";
      dispInfo.textContent = (st.display || "") + " (" + canvas.width + "x" + canvas.height + ")";
      btnStart.disabled = true;
      btnStop.disabled = false;
    } else {
      runBadge.textContent = "未启动";
      runBadge.className = "badge";
      dispInfo.textContent = "";
      btnStart.disabled = false;
      btnStop.disabled = true;
      overlayText.textContent = "当前智能体实时屏幕未启动，点击右上角「启动屏幕」即可开启。";
      overlay.classList.remove("hidden");
    }

    const human = st.lease && st.lease.is_human_takeover;
    if (human) {
      leaseBadge.textContent = iHoldControl ? "人工接管中 (当前窗口)" : "人工接管中 (其他窗口)";
      leaseBadge.className = "badge takeover";
      frameEl.classList.add("takeover-active");
      if (iHoldControl) {
        btnTakeover.classList.add("hidden");
        btnRelease.classList.remove("hidden");
        btnForceRelease.classList.add("hidden");
      } else {
        btnTakeover.classList.remove("hidden");
        btnRelease.classList.add("hidden");
        btnForceRelease.classList.remove("hidden");
      }
    } else {
      leaseBadge.textContent = "Bot 控制中 (只读观察)";
      leaseBadge.className = "badge";
      frameEl.classList.remove("takeover-active");
      btnTakeover.classList.remove("hidden");
      btnRelease.classList.add("hidden");
      btnForceRelease.classList.add("hidden");
    }
    btnTakeover.disabled = !st.running;
  }

  async function refreshStatus() {
    try {
      const data = await api("/api/profiles?viewer_id=" + encodeURIComponent(viewerId));
      profileMap = {};
      const prev = sel.value || currentProfile;
      sel.innerHTML = "";
      for (const item of (data.profiles || [])) {
        profileMap[item.profile] = item;
        const opt = document.createElement("option");
        opt.value = item.profile;
        opt.textContent = item.profile + (item.running ? " [运行中]" : "");
        sel.appendChild(opt);
      }
      if (profileMap[prev]) {
        currentProfile = prev;
      } else if (data.default_profile && profileMap[data.default_profile]) {
        currentProfile = data.default_profile;
      } else if (sel.options.length > 0) {
        currentProfile = sel.options[0].value;
      }
      sel.value = currentProfile;
      const st = profileMap[currentProfile];
      renderState(st);
      if (st && st.running && (!ws || ws.readyState > 1)) {
        connectRfb();
      } else if (st && !st.running && ws) {
        ws.close();
        ws = null;
      }
    } catch (e) {
      runBadge.textContent = "连接断开";
      runBadge.className = "badge";
    }
  }

  // Minimal RFC 6143 RFB 3.8 client over binary WebSocket (Raw + CopyRect, 32bpp BGRX/RGBX)
  class ByteQueue {
    constructor() { this.chunks = []; this.len = 0; }
    push(u8) { if (u8.byteLength) { this.chunks.push(u8); this.len += u8.byteLength; } }
    peek(n) {
      if (this.len < n) return null;
      if (this.chunks[0].byteLength >= n) return this.chunks[0].subarray(0, n);
      const out = new Uint8Array(n);
      let off = 0;
      for (const c of this.chunks) {
        const take = Math.min(c.byteLength, n - off);
        out.set(c.subarray(0, take), off);
        off += take;
        if (off === n) break;
      }
      return out;
    }
    read(n) {
      const out = this.peek(n);
      if (!out) return null;
      let rem = n;
      while (rem > 0) {
        const c = this.chunks[0];
        if (c.byteLength <= rem) {
          rem -= c.byteLength;
          this.chunks.shift();
        } else {
          this.chunks[0] = c.subarray(rem);
          rem = 0;
        }
      }
      this.len -= n;
      return out;
    }
  }

  function connectRfb() {
    if (ws) { try { ws.close(); } catch (_) {} ws = null; }
    clearTimeout(reconnectTimer);
    overlayText.textContent = "正在建立桌面画面连接...";
    overlay.classList.remove("hidden");

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = proto + "//" + location.host + "/ws/rfb?profile=" +
                encodeURIComponent(currentProfile) + "&viewer_id=" + encodeURIComponent(viewerId);
    const sock = new WebSocket(url);
    sock.binaryType = "arraybuffer";
    ws = sock;

    const q = new ByteQueue();
    let stage = "version";
    let fbW = 1440, fbH = 900;
    let rectLeft = 0;
    let curRect = null;
    let reqPending = false;

    function sendBytes(arr) {
      if (sock.readyState === 1) sock.send(arr);
    }

    function requestUpdate(incremental) {
      if (sock.readyState !== 1) return;
      const b = new Uint8Array(10);
      const dv = new DataView(b.buffer);
      b[0] = 3;
      b[1] = incremental ? 1 : 0;
      dv.setUint16(2, 0, false);
      dv.setUint16(4, 0, false);
      dv.setUint16(6, fbW, false);
      dv.setUint16(8, fbH, false);
      sendBytes(b);
      reqPending = true;
    }

    function sendSetup() {
      // SetPixelFormat: 32bpp, depth 24, little-endian, true-color, R@0 G@8 B@16 -> matches ImageData RGBA!
      const pf = new Uint8Array(20);
      const dv = new DataView(pf.buffer);
      pf[0] = 0;
      pf[4] = 32; pf[5] = 24; pf[6] = 0; pf[7] = 1;
      dv.setUint16(8, 255, false);
      dv.setUint16(10, 255, false);
      dv.setUint16(12, 255, false);
      pf[14] = 0; pf[15] = 8; pf[16] = 16;
      sendBytes(pf);

      // SetEncodings: CopyRect(1), Raw(0), DesktopSize(-223)
      const enc = new Uint8Array(4 + 3 * 4);
      const edv = new DataView(enc.buffer);
      enc[0] = 2;
      edv.setUint16(2, 3, false);
      edv.setInt32(4, 1, false);
      edv.setInt32(8, 0, false);
      edv.setInt32(12, -223, false);
      sendBytes(enc);

      requestUpdate(false);
    }

    function pump() {
      while (true) {
        if (stage === "version") {
          const v = q.read(12);
          if (!v) return;
          sendBytes(new TextEncoder().encode("RFB 003.008\n"));
          stage = "sectypes";
        } else if (stage === "sectypes") {
          const h = q.peek(1);
          if (!h) return;
          const n = h[0];
          if (n === 0) return;
          const full = q.read(1 + n);
          if (!full) return;
          sendBytes(new Uint8Array([1])); // SecurityType None
          stage = "secresult";
        } else if (stage === "secresult") {
          const r = q.read(4);
          if (!r) return;
          const status = new DataView(r.buffer, r.byteOffset, 4).getUint32(0, false);
          if (status !== 0) { overlayText.textContent = "RFB 握手失败"; return; }
          sendBytes(new Uint8Array([1])); // ClientInit shared=1
          stage = "serverinit";
        } else if (stage === "serverinit") {
          const head = q.peek(24);
          if (!head) return;
          const dv = new DataView(head.buffer, head.byteOffset, 24);
          const nameLen = dv.getUint32(20, false);
          const full = q.read(24 + nameLen);
          if (!full) return;
          const fdv = new DataView(full.buffer, full.byteOffset, 24);
          fbW = fdv.getUint16(0, false);
          fbH = fdv.getUint16(2, false);
          canvas.width = fbW;
          canvas.height = fbH;
          overlay.classList.add("hidden");
          sendSetup();
          stage = "msg";
        } else if (stage === "msg") {
          if (rectLeft > 0) {
            if (!curRect) {
              const rh = q.read(12);
              if (!rh) return;
              const dv = new DataView(rh.buffer, rh.byteOffset, 12);
              curRect = {
                x: dv.getUint16(0, false),
                y: dv.getUint16(2, false),
                w: dv.getUint16(4, false),
                h: dv.getUint16(6, false),
                enc: dv.getInt32(8, false),
              };
            }
            const r = curRect;
            if (r.enc === -223) {
              const changed = (canvas.width !== r.w || canvas.height !== r.h);
              fbW = r.w; fbH = r.h;
              if (changed) {
                canvas.width = fbW;
                canvas.height = fbH;
                setTimeout(() => requestUpdate(false), 10);
              }
              curRect = null; rectLeft--;
            } else if (r.w === 0 || r.h === 0) {
              curRect = null; rectLeft--;
            } else if (r.enc === 0) {
              const byteLen = r.w * r.h * 4;
              const px = q.read(byteLen);
              if (!px) return;
              const copy = new Uint8ClampedArray(byteLen);
              copy.set(px);
              for (let i = 3; i < byteLen; i += 4) copy[i] = 255;
              const img = new ImageData(copy, r.w, r.h);
              ctx.putImageData(img, r.x, r.y);
              curRect = null; rectLeft--;
            } else if (r.enc === 1) {
              const cp = q.read(4);
              if (!cp) return;
              const dv = new DataView(cp.buffer, cp.byteOffset, 4);
              const sx = dv.getUint16(0, false), sy = dv.getUint16(2, false);
              ctx.drawImage(canvas, sx, sy, r.w, r.h, r.x, r.y, r.w, r.h);
              curRect = null; rectLeft--;
            } else {
              sock.close();
              return;
            }
            if (rectLeft === 0) {
              reqPending = false;
              setTimeout(() => requestUpdate(true), 30);
            }
            continue;
          }
          const t = q.peek(1);
          if (!t) return;
          if (t[0] === 0) {
            const h = q.read(4);
            if (!h) return;
            rectLeft = new DataView(h.buffer, h.byteOffset, 4).getUint16(2, false);
            if (rectLeft === 0) {
              reqPending = false;
              setTimeout(() => requestUpdate(true), 40);
            }
          } else if (t[0] === 2) {
            q.read(1); // Bell
          } else if (t[0] === 3) {
            const h = q.peek(8);
            if (!h) return;
            const n = new DataView(h.buffer, h.byteOffset, 8).getUint32(4, false);
            if (!q.read(8 + n)) return;
          } else {
            sock.close();
            return;
          }
        }
      }
    }

    sock.onmessage = (ev) => {
      q.push(new Uint8Array(ev.data));
      pump();
    };
    sock.onclose = () => {
      if (ws === sock) {
        ws = null;
        reconnectTimer = setTimeout(() => {
          const st = profileMap[currentProfile];
          if (st && st.running) connectRfb();
        }, 1200);
      }
    };
  }

  // Input forwarding (active only when iHoldControl is true)
  let mouseMask = 0;
  function canvasCoords(e) {
    const rect = canvas.getBoundingClientRect();
    const x = Math.max(0, Math.min(canvas.width - 1, Math.round((e.clientX - rect.left) * (canvas.width / rect.width))));
    const y = Math.max(0, Math.min(canvas.height - 1, Math.round((e.clientY - rect.top) * (canvas.height / rect.height))));
    return [x, y];
  }
  function sendPointer(mask, x, y) {
    if (!iHoldControl || !ws || ws.readyState !== 1) return;
    const b = new Uint8Array(6);
    const dv = new DataView(b.buffer);
    b[0] = 5; b[1] = mask;
    dv.setUint16(2, x, false);
    dv.setUint16(4, y, false);
    ws.send(b);
  }
  canvas.addEventListener("contextmenu", e => { if (iHoldControl) e.preventDefault(); });
  canvas.addEventListener("mousemove", e => {
    if (!iHoldControl) return;
    const [x, y] = canvasCoords(e);
    sendPointer(mouseMask, x, y);
  });
  canvas.addEventListener("mousedown", e => {
    if (!iHoldControl) return;
    canvas.focus();
    e.preventDefault();
    const bit = e.button === 0 ? 1 : (e.button === 1 ? 2 : (e.button === 2 ? 4 : 0));
    mouseMask |= bit;
    const [x, y] = canvasCoords(e);
    sendPointer(mouseMask, x, y);
  });
  window.addEventListener("mouseup", e => {
    if (!iHoldControl || mouseMask === 0) return;
    const bit = e.button === 0 ? 1 : (e.button === 1 ? 2 : (e.button === 2 ? 4 : 0));
    mouseMask &= ~bit;
    const [x, y] = canvasCoords(e);
    sendPointer(mouseMask, x, y);
  });
  canvas.addEventListener("wheel", e => {
    if (!iHoldControl) return;
    e.preventDefault();
    const [x, y] = canvasCoords(e);
    const bit = e.deltaY < 0 ? 8 : 16;
    sendPointer(mouseMask | bit, x, y);
    sendPointer(mouseMask, x, y);
  }, { passive: false });

  const KEYSYM_MAP = {
    Backspace: 0xff08, Tab: 0xff09, Enter: 0xff0d, Escape: 0xff1b,
    Delete: 0xffff, Home: 0xff50, Left: 0xff51, Up: 0xff52, Right: 0xff53, Down: 0xff54,
    PageUp: 0xff55, PageDown: 0xff56, End: 0xff57, Insert: 0xff63,
    ShiftLeft: 0xffe1, ShiftRight: 0xffe2, ControlLeft: 0xffe3, ControlRight: 0xffe4,
    AltLeft: 0xffe9, AltRight: 0xffea, MetaLeft: 0xffeb, MetaRight: 0xffec,
    F1: 0xffbe, F2: 0xffbf, F3: 0xffc0, F4: 0xffc1, F5: 0xffc2, F6: 0xffc3,
    F7: 0xffc4, F8: 0xffc5, F9: 0xffc6, F10: 0xffc7, F11: 0xffc8, F12: 0xffc9,
  };
  function toKeysym(e) {
    if (KEYSYM_MAP[e.code]) return KEYSYM_MAP[e.code];
    if (KEYSYM_MAP[e.key]) return KEYSYM_MAP[e.key];
    if (e.key && e.key.length === 1) {
      const cp = e.key.codePointAt(0);
      if (cp >= 0x20 && cp <= 0xff) return cp;
      return 0x01000000 | cp;
    }
    return null;
  }
  function sendKey(down, keysym) {
    if (!iHoldControl || !ws || ws.readyState !== 1 || keysym === null) return;
    const b = new Uint8Array(8);
    const dv = new DataView(b.buffer);
    b[0] = 4; b[1] = down ? 1 : 0;
    dv.setUint32(4, keysym >>> 0, false);
    ws.send(b);
  }
  canvas.addEventListener("keydown", e => {
    if (!iHoldControl) return;
    const ks = toKeysym(e);
    if (ks !== null) { e.preventDefault(); sendKey(true, ks); }
  });
  canvas.addEventListener("keyup", e => {
    if (!iHoldControl) return;
    const ks = toKeysym(e);
    if (ks !== null) { e.preventDefault(); sendKey(false, ks); }
  });

  sel.addEventListener("change", () => {
    currentProfile = sel.value;
    if (ws) { ws.close(); ws = null; }
    refreshStatus();
  });
  btnStart.addEventListener("click", async () => {
    btnStart.disabled = true;
    overlayText.textContent = "正在启动桌面进程...";
    overlay.classList.remove("hidden");
    await api("/api/screen/start", "POST", { profile: currentProfile });
    await refreshStatus();
  });
  btnStop.addEventListener("click", async () => {
    btnStop.disabled = true;
    await api("/api/screen/stop", "POST", { profile: currentProfile, viewer_id: viewerId });
    await refreshStatus();
  });
  btnTakeover.addEventListener("click", async () => {
    await api("/api/lease/acquire", "POST", { profile: currentProfile, viewer_id: viewerId, reason: "web_portal_takeover" });
    await refreshStatus();
    canvas.focus();
  });
  btnRelease.addEventListener("click", async () => {
    await api("/api/lease/release", "POST", { profile: currentProfile, viewer_id: viewerId, force: false });
    await refreshStatus();
  });
  btnForceRelease.addEventListener("click", async () => {
    await api("/api/lease/release", "POST", { profile: currentProfile, viewer_id: viewerId, force: true });
    await refreshStatus();
  });

  refreshStatus();
  setInterval(refreshStatus, 2000);
})();
</script>
</body>
</html>
"""


class ScreenPortalServer:
    """Asyncio HTTP + WebSocket bridge serving the Bot Desktop browser console."""

    def __init__(self, default_home: Path, hermes_bin: Optional[Path] = None) -> None:
        self.default_home = default_home.resolve()
        self.explicit_hermes_bin = hermes_bin

    @property
    def default_profile_name(self) -> str:
        return self.default_home.name if self.default_home.parent.name == "profiles" else "default"

    def resolve_profile_home(self, profile: Optional[str]) -> Tuple[str, Path]:
        profiles = discover_profiles(self.default_home)
        target = (profile or "").strip() or self.default_profile_name
        if target not in profiles:
            raise KeyError(f"Unknown profile '{target}'")
        return target, profiles[target]

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req_line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not req_line:
                return
            parts = req_line.decode("latin-1").strip().split(" ")
            if len(parts) < 2:
                return
            method, raw_target = parts[0].upper(), parts[1]

            headers: Dict[str, str] = {}
            while True:
                hline = await asyncio.wait_for(reader.readline(), timeout=10.0)
                if hline in (b"\r\n", b"\n", b""):
                    break
                decoded = hline.decode("latin-1").strip()
                if ":" in decoded:
                    k, v = decoded.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            parsed = urllib.parse.urlsplit(raw_target)
            query = urllib.parse.parse_qs(parsed.query)

            if headers.get("upgrade", "").lower() == "websocket" and parsed.path == "/ws/rfb":
                await self._handle_ws_rfb(reader, writer, headers, query)
                return

            content_len = int(headers.get("content-length", "0") or "0")
            body_bytes = await reader.readexactly(content_len) if content_len > 0 else b""

            status_code, content_type, payload = await self._dispatch_http(
                method, parsed.path, query, body_bytes
            )
            resp_head = (
                f"HTTP/1.1 {status_code}\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(payload)}\r\n"
                f"Cache-Control: no-store\r\n"
                f"Connection: close\r\n\r\n"
            ).encode("ascii")
            writer.write(resp_head + payload)
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch_http(
        self,
        method: str,
        path: str,
        query: Dict[str, List[str]],
        body_bytes: bytes,
    ) -> Tuple[str, str, bytes]:
        if method == "GET" and path == "/":
            html = _PORTAL_HTML.replace("__DEFAULT_PROFILE__", self.default_profile_name)
            return "200 OK", "text/html; charset=utf-8", html.encode("utf-8")

        if method == "GET" and path == "/api/profiles":
            viewer_id = (query.get("viewer_id") or [""])[0]
            profiles = discover_profiles(self.default_home)
            items = [
                inspect_profile_screen(name, home, viewer_id=viewer_id)
                for name, home in profiles.items()
            ]
            return self._json_resp("200 OK", {
                "ok": True,
                "default_profile": self.default_profile_name,
                "profiles": items,
            })

        if method == "POST":
            try:
                data = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            except ValueError:
                return self._json_resp("400 Bad Request", {"ok": False, "error": "invalid_json"})

            try:
                prof_name, prof_home = self.resolve_profile_home(data.get("profile"))
            except KeyError as exc:
                return self._json_resp("404 Not Found", {"ok": False, "error": str(exc)})

            viewer_id = str(data.get("viewer_id") or "").strip()

            if path == "/api/lease/acquire":
                if not viewer_id:
                    viewer_id = secrets.token_hex(12)
                reason = str(data.get("reason") or "web_portal")
                acquire_bot_desktop_lease(prof_home, viewer_id, reason=reason)
                return self._json_resp("200 OK", {
                    "ok": True,
                    "status": inspect_profile_screen(prof_name, prof_home, viewer_id=viewer_id),
                })

            if path == "/api/lease/release":
                force = bool(data.get("force", False))
                release_bot_desktop_lease(prof_home, viewer_id or None, force=force)
                return self._json_resp("200 OK", {
                    "ok": True,
                    "status": inspect_profile_screen(prof_name, prof_home, viewer_id=viewer_id),
                })

            if path in ("/api/screen/start", "/api/screen/stop"):
                action = "start" if path.endswith("/start") else "stop"
                if action == "stop":
                    release_bot_desktop_lease(prof_home, viewer_id or None, force=False)
                hermes_bin = resolve_hermes_bin(prof_home, explicit=self.explicit_hermes_bin)
                if hermes_bin is None:
                    return self._json_resp("500 Internal Server Error", {
                        "ok": False,
                        "error": "hermes_binary_not_found",
                    })
                env = dict(os.environ)
                env["HERMES_HOME"] = str(prof_home)
                proc = await asyncio.create_subprocess_exec(
                    str(hermes_bin), "computer-use", "screen", action,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                out, _ = await proc.communicate()
                return self._json_resp("200 OK", {
                    "ok": proc.returncode == 0,
                    "output": out.decode("utf-8", errors="replace").strip(),
                    "status": inspect_profile_screen(prof_name, prof_home, viewer_id=viewer_id),
                })

        return self._json_resp("404 Not Found", {"ok": False, "error": "not_found"})

    @staticmethod
    def _json_resp(status: str, obj: Dict[str, Any]) -> Tuple[str, str, bytes]:
        return status, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False).encode("utf-8")

    async def _handle_ws_rfb(
        self,
        ws_reader: asyncio.StreamReader,
        ws_writer: asyncio.StreamWriter,
        headers: Dict[str, str],
        query: Dict[str, List[str]],
    ) -> None:
        sec_key = headers.get("sec-websocket-key", "")
        if not sec_key:
            return
        try:
            _, prof_home = self.resolve_profile_home((query.get("profile") or [None])[0])
        except KeyError:
            return

        viewer_id = (query.get("viewer_id") or [""])[0].strip()
        sock_path = prof_home / "bot-desktop" / "rfb.sock"
        if not sock_path.exists():
            return

        rfb_reader, rfb_writer = await asyncio.open_unix_connection(str(sock_path))

        accept = _ws_accept_key(sec_key)
        handshake = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("ascii")
        ws_writer.write(handshake)
        await ws_writer.drain()

        cached_ok = False
        cached_at = 0.0

        def _may_input() -> bool:
            nonlocal cached_ok, cached_at
            now = time.monotonic()
            if now - cached_at >= 0.2:
                cached_ok = viewer_holds_lease(prof_home, viewer_id)
                cached_at = now
            return cached_ok

        gate = RfbInputGate(_may_input)

        async def _ws_to_rfb() -> None:
            try:
                while True:
                    frame = await _ws_read_frame(ws_reader)
                    if frame is None:
                        break
                    opcode, payload = frame
                    if opcode == 0x08:  # Close
                        break
                    if opcode == 0x09:  # Ping
                        ws_writer.write(_ws_encode_frame(payload, opcode=0x0A))
                        await ws_writer.drain()
                        continue
                    if opcode in (0x01, 0x02):
                        filtered = gate.feed(payload)
                        if filtered:
                            rfb_writer.write(filtered)
                            await rfb_writer.drain()
            finally:
                with os.popen("true") if False else _nullctx():
                    rfb_writer.close()

        async def _rfb_to_ws() -> None:
            while True:
                chunk = await rfb_reader.read(65536)
                if not chunk:
                    break
                ws_writer.write(_ws_encode_frame(chunk, opcode=0x02))
                await ws_writer.drain()

        t1 = asyncio.create_task(_ws_to_rfb())
        t2 = asyncio.create_task(_rfb_to_ws())
        try:
            await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (t1, t2):
                t.cancel()
            try:
                rfb_writer.close()
                await rfb_writer.wait_closed()
            except Exception:
                pass


class _nullctx:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: Any) -> None:
        return None


def render_portal_service(
    hermes_home: Path,
    host: str = "127.0.0.1",
    port: int = 9120,
    python_bin: str = sys.executable,
) -> str:
    script = Path(__file__).resolve()
    return (
        "[Unit]\n"
        "Description=Super Agent Bot Desktop Web Portal\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f'Environment="HERMES_HOME={hermes_home}"\n'
        f'ExecStart="{python_bin}" "{script}" --hermes-home "{hermes_home}" --host "{host}" --port "{port}"\n'
        "KillMode=process\n"
        "Restart=always\n"
        "RestartSec=3\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


async def serve_portal(
    hermes_home: Path,
    host: str = "127.0.0.1",
    port: int = 9120,
    hermes_bin: Optional[Path] = None,
) -> None:
    portal = ScreenPortalServer(default_home=hermes_home, hermes_bin=hermes_bin)
    server = await asyncio.start_server(portal.handle_client, host=host, port=port)
    print(f"[OK] Super Agent Screen Web Portal listening on http://{host}:{port}")
    async with server:
        await server.serve_forever()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Super Agent Bot Desktop Web Portal")
    parser.add_argument("--hermes-home", type=Path, default=None, help="Default HERMES_HOME directory")
    parser.add_argument("--hermes-bin", type=Path, default=None, help="Explicit hermes binary path")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9120, help="Bind port (default: 9120)")
    args = parser.parse_args(argv)

    hermes_home = resolve_hermes_home(args.hermes_home)
    try:
        asyncio.run(
            serve_portal(
                hermes_home=hermes_home,
                host=args.host,
                port=args.port,
                hermes_bin=args.hermes_bin,
            )
        )
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
