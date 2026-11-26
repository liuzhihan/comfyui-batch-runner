# -*- coding: utf-8 -*-
"""
ComfyUI 批量工作流运行器
========================
带图形界面的批量执行工具，通过 HTTP API 调用本机 ComfyUI 批量跑工作流。

  * 文生图页：载入 API 格式工作流 JSON，把提示词框里每一行提示词依次写入指定节点，逐条执行。
  * 图生图页：载入 API 格式工作流 JSON，遍历指定目录下的全部图片，逐张上传到 ComfyUI
    输入目录并写入指定节点，逐条执行。

依赖：仅使用 Python 标准库（tkinter + urllib），不需要安装任何第三方包。
运行：python comfy_batch_runner.py
"""

from __future__ import annotations

import copy
import json
import mimetypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_NAME = "ComfyUI 批量工作流运行器"
APP_VERSION = "1.0"
DEFAULT_SERVER = "http://127.0.0.1:8188"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".jpe", ".jfif", ".webp", ".bmp", ".gif",
              ".tif", ".tiff", ".avif"}

# 文本类输入的候选键名（按优先级排列）
TEXT_KEYS = ("text", "prompt", "text_g", "text_l", "positive", "positive_prompt",
             "string", "value", "文本", "提示词", "正向提示词")
# 图片类输入的候选键名（按优先级排列）
IMAGE_KEYS = ("image", "image_path", "img", "input_image", "image_name",
              "filename", "图片", "图像", "底图", "参考图")
# 已知以文件名为输入的节点，即使当前没有该输入项也按此键名写入
IMAGE_NODE_TYPES = ("LoadImage", "LoadImageMask", "ImageLoad", "LoadImageOutput",
                    "LoadImageBatch")


# ---------------------------------------------------------------- 基础工具 ---

class UserError(Exception):
    """可以直接展示给用户的错误。"""


class ComfyError(Exception):
    """与 ComfyUI 通讯或执行过程中产生的错误。"""


class Stopped(Exception):
    """用户中途停止。"""


def app_dir() -> Path:
    """程序所在目录（打包成 exe 时取 exe 所在目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_PATH = app_dir() / "批量运行器配置.json"
LOG_PATH = app_dir() / "批量运行日志.log"


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(data: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


def write_log_line(text: str) -> None:
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        pass


def guess_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


# ------------------------------------------------------------ ComfyUI 客户端 ---

class ComfyClient:
    """ComfyUI HTTP API 的最小封装。"""

    def __init__(self, base_url: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # -- 底层 -------------------------------------------------------------
    def _url(self, path: str) -> str:
        return self.base_url + path

    def get_json(self, path: str, timeout: int | None = None):
        req = urllib.request.Request(self._url(path), headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise ComfyError(_http_error_text(exc)) from exc
        except urllib.error.URLError as exc:
            raise ComfyError(f"无法连接 ComfyUI（{self.base_url}）：{exc.reason}") from exc
        except Exception as exc:
            raise ComfyError(f"请求失败：{exc}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ComfyError(f"服务端返回了无法解析的内容：{body[:200]}") from exc

    def post_json(self, path: str, payload: dict, timeout: int | None = None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._url(path), data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise ComfyError(_http_error_text(exc)) from exc
        except urllib.error.URLError as exc:
            raise ComfyError(f"无法连接 ComfyUI（{self.base_url}）：{exc.reason}") from exc
        except Exception as exc:
            raise ComfyError(f"提交失败：{exc}") from exc
        try:
            return json.loads(body) if body.strip() else {}
        except json.JSONDecodeError as exc:
            raise ComfyError(f"服务端返回了无法解析的内容：{body[:200]}") from exc

    def post_multipart(self, path: str, fields: dict, files: list) -> dict:
        """fields: {name: value}; files: [(name, filename, payload, content_type)]"""
        boundary = "----ComfyBatch" + uuid.uuid4().hex
        chunks = []
        for name, value in fields.items():
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f'{value}\r\n'.encode("utf-8"))
        for name, filename, payload, ctype in files:
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'.encode("utf-8"))
            chunks.append(payload)
            chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(chunks)
        req = urllib.request.Request(
            self._url(path), data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise ComfyError(_http_error_text(exc)) from exc
        except urllib.error.URLError as exc:
            raise ComfyError(f"上传失败：{exc.reason}") from exc
        except Exception as exc:
            raise ComfyError(f"上传失败：{exc}") from exc
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ComfyError(f"上传接口返回异常：{text[:200]}") from exc

    # -- 业务 -------------------------------------------------------------
    def ping(self) -> bool:
        """检查 ComfyUI 是否已启动。"""
        try:
            self.get_json("/system_stats", timeout=5)
            return True
        except Exception:
            return False

    def upload_image(self, path: str, subfolder: str = "batch") -> str:
        """上传图片到 ComfyUI 输入目录，返回可供 LoadImage 使用的相对文件名。"""
        with open(path, "rb") as fh:
            payload = fh.read()
        result = self.post_multipart(
            "/upload/image",
            {"type": "input", "overwrite": "false", "subfolder": subfolder},
            [("image", os.path.basename(path), payload, guess_mime(path))])
        name = result.get("name")
        if not name:
            raise ComfyError(f"上传接口未返回文件名：{result}")
        # ComfyUI 返回的 name 不含子目录前缀，节点里需要写完整相对路径
        sub = str(result.get("subfolder") or "").replace("\\", "/").strip("/")
        return f"{sub}/{name}" if sub else name

    def queue_prompt(self, prompt: dict, client_id: str) -> str:
        result = self.post_json("/prompt", {"prompt": prompt, "client_id": client_id})
        pid = result.get("prompt_id")
        if not pid:
            raise ComfyError(f"提交工作流后未返回 prompt_id：{result}")
        return pid

    def queue_size(self) -> int:
        """当前排队（含运行中）的任务数。"""
        try:
            data = self.get_json("/queue", timeout=5)
            return len(data.get("queue_running") or []) + len(data.get("queue_pending") or [])
        except Exception:
            return -1

    def interrupt(self) -> None:
        try:
            self.post_json("/interrupt", {}, timeout=5)
        except Exception:
            pass

    def wait_prompt(self, prompt_id: str, timeout: int = 1800,
                    should_stop=None, tick=None) -> dict:
        """轮询历史记录直到工作流结束，返回 history 条目。"""
        start = time.time()
        last_note = 0.0
        net_errors = 0
        while True:
            if should_stop is not None and should_stop():
                self.interrupt()
                raise Stopped("已停止")
            try:
                history = self.get_json(f"/history/{prompt_id}", timeout=15)
                net_errors = 0
            except ComfyError:
                net_errors += 1
                if net_errors >= 5:
                    raise
                history = {}
            if prompt_id in history:
                entry = history[prompt_id] or {}
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    raise ComfyError(_history_error_text(entry))
                return entry
            if time.time() - start > timeout:
                self.interrupt()
                raise ComfyError(f"等待超时（{timeout} 秒），已中断该任务")
            if tick is not None and time.time() - last_note > 2.0:
                last_note = time.time()
                pending = self.queue_size()
                tick(pending)
            time.sleep(0.35)

    def history_outputs(self, entry: dict) -> tuple:
        """从 history 条目里取出生成的文件，区分「已保存」与「仅预览」。

        ComfyUI 的 PreviewImage 节点会返回 type=temp 的临时预览图，
        只有 type=output 的才是真正写进 output 目录的文件。
        """
        saved = []
        temp = []
        for _node_id, out in (entry.get("outputs") or {}).items():
            if not isinstance(out, dict):
                continue
            for group in ("images", "gifs", "audio"):
                for item in out.get(group) or []:
                    if isinstance(item, dict) and item.get("filename"):
                        if item.get("type") == "output":
                            saved.append(item)
                        else:
                            temp.append(item)
        return saved, temp


def _http_error_text(exc: urllib.error.HTTPError) -> str:
    """把 HTTP 错误转成人能看懂的文字。"""
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:
        raw = ""
    msg = f"HTTP {exc.code}"
    if raw:
        try:
            data = json.loads(raw)
            err = data.get("error") if isinstance(data, dict) else None
            if isinstance(err, dict):
                parts = [str(err.get("message") or err.get("type") or "")]
                details = err.get("details")
                if details:
                    parts.append(details if isinstance(details, str)
                                 else json.dumps(details, ensure_ascii=False))
                extra = err.get("extra_info")
                if extra:
                    parts.append(json.dumps(extra, ensure_ascii=False))
                msg = " / ".join(p for p in parts if p) or msg
            else:
                msg = raw[:500]
        except json.JSONDecodeError:
            msg = raw[:500]
    return msg


def _history_error_text(entry: dict) -> str:
    """从 history 条目里提取节点报错信息。"""
    status = entry.get("status") or {}
    messages = status.get("messages") or []
    for msg in messages:
        if not isinstance(msg, (list, tuple)) or len(msg) < 2:
            continue
        kind, payload = msg[0], msg[1]
        if kind in ("execution_error", "execution_interrupted"):
            if isinstance(payload, dict):
                node_id = payload.get("node_id", "?")
                etype = payload.get("exception_type", "")
                emsg = payload.get("exception_message", "")
                parts = [f"节点 {node_id} 执行失败"]
                if etype:
                    parts.append(str(etype))
                if emsg:
                    parts.append(str(emsg))
                return "：".join(parts)
    return "工作流执行失败：" + json.dumps(status, ensure_ascii=False)[:300]


# ------------------------------------------------------------ 工作流解析 ---

def load_workflow(path: str) -> dict:
    """读取工作流文件，返回 {节点ID: 节点} 的字典。"""
    p = Path(path)
    if not p.is_file():
        raise UserError(f"找不到工作流文件：{path}")
    try:
        with open(p, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise UserError(f"工作流不是合法的 JSON：{exc}") from exc
    except Exception as exc:
        raise UserError(f"读取工作流失败：{exc}") from exc

    nodes = extract_nodes(data)
    if not nodes:
        raise UserError("工作流里没有找到任何节点，请确认选择的是 API 格式的 JSON。")
    return nodes


def extract_nodes(data) -> dict:
    """兼容 API 格式与带 prompt 字段的导出格式。"""
    if not isinstance(data, dict):
        return {}
    if isinstance(data.get("prompt"), dict):
        data = data["prompt"]
    nodes = {}
    for key, value in data.items():
        if isinstance(value, dict) and "class_type" in value:
            nodes[str(key)] = value
    return nodes


def resolve_input_key(node: dict, candidates: tuple, node_id: str) -> str:
    """在节点里找到该填内容的输入键名。"""
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        raise UserError(f"节点 {node_id} 没有 inputs 字段，不是有效的 API 格式节点。")
    for key in candidates:
        if key in inputs:
            return key
    if node.get("class_type") in IMAGE_NODE_TYPES and "image" not in inputs:
        return "image"
    str_keys = [k for k, v in inputs.items() if isinstance(v, str)]
    if len(str_keys) == 1:
        return str_keys[0]
    if not str_keys:
        raise UserError(f"节点 {node_id} 没有可写入内容的输入项，请检查节点 ID 是否填对。")
    raise UserError(
        f"节点 {node_id} 有多个文本输入（{'、'.join(str_keys)}），"
        f"无法自动判断，请改用另一个节点 ID。")


def describe_node(node: dict) -> str:
    inputs = node.get("inputs") or {}
    keys = list(inputs.keys())
    shown = "、".join(keys[:6]) + ("…" if len(keys) > 6 else "")
    return f"{node.get('class_type', '?')}（输入：{shown or '无'}）"


# ------------------------------------------------------------ 输出目录探测 ---

def _psutil_comfy_root(port: int):
    """通过 psutil 找到监听端口的 ComfyUI 进程，返回其根目录。"""
    try:
        import psutil  # type: ignore
    except Exception:
        return None
    try:
        conns = psutil.net_connections(kind="inet")
    except Exception:
        return None
    for conn in conns:
        laddr = getattr(conn, "laddr", None)
        if not laddr or getattr(laddr, "port", None) != port:
            continue
        if getattr(conn, "status", "") != "LISTEN" or not getattr(conn, "pid", None):
            continue
        try:
            proc = psutil.Process(conn.pid)
            cmd = proc.cmdline()
            cwd = proc.cwd()
        except Exception:
            continue
        for arg in cmd:
            if arg.lower().endswith("main.py"):
                full = arg if os.path.isabs(arg) else os.path.join(cwd, arg)
                return os.path.dirname(os.path.abspath(full))
    return None


def _powershell_comfy_root(port: int):
    """没有 psutil 时，用 PowerShell 查进程命令行。"""
    script = (
        "$c = Get-NetTCPConnection -LocalPort {p} -State Listen -ErrorAction SilentlyContinue "
        "| Select-Object -First 1; if ($c) {{ "
        "(Get-CimInstance Win32_Process -Filter \"ProcessId=$($c.OwningProcess)\").CommandLine }}"
    ).format(p=port)
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=20, text=True, encoding="utf-8", errors="replace")
        cmd = (out.stdout or "").strip()
    except Exception:
        return None
    if not cmd:
        return None
    parts = [a or b for a, b in re.findall(r'"([^"]*)"|(\S+)', cmd)]
    for token in parts:
        if token.lower().endswith("main.py"):
            if os.path.isabs(token):
                return os.path.dirname(os.path.abspath(token))
            here = str(app_dir())
            for base in (os.getcwd(), here, os.path.dirname(here)):
                candidate = os.path.join(base, token)
                if os.path.isfile(candidate):
                    return os.path.dirname(os.path.abspath(candidate))
    return None


def detect_output_dir(server_url: str) -> str:
    """自动探测 ComfyUI 的输出目录。"""
    port = 8188
    try:
        parsed = urllib.parse.urlparse(server_url)
        if parsed.port:
            port = parsed.port
    except Exception:
        pass

    root = _psutil_comfy_root(port) or _powershell_comfy_root(port)
    candidates = []
    if root:
        candidates += [os.path.join(root, "output"), root]
    here = str(app_dir())
    candidates += [
        os.path.join(here, "ComfyUI", "output"),
        os.path.join(here, "output"),
        os.path.join(os.path.dirname(here), "ComfyUI", "output"),
    ]
    for path in candidates:
        if path and os.path.isdir(path):
            return os.path.abspath(path)
    return os.path.join(here, "ComfyUI", "output")


def open_in_explorer(path: str) -> bool:
    """打开文件所在目录（Windows 资源管理器 / macOS Finder / Linux 文件管理器）。"""
    if not path:
        return False
    p = Path(path)
    target = str(p if p.is_dir() else p.parent)
    try:
        if sys.platform.startswith("win"):
            os.startfile(target)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
        return True
    except Exception:
        return False


def collect_images(folder: str) -> list:
    """列出目录下的全部图片文件（按名称排序）。"""
    root = Path(folder)
    if not root.is_dir():
        raise UserError(f"图片文件夹不存在：{folder}")
    files = [p for p in sorted(root.iterdir(), key=lambda x: x.name.lower())
             if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    if not files:
        raise UserError(f"该文件夹下没有找到图片（支持 {', '.join(sorted(IMAGE_EXTS))}）。")
    return files


def _result_location(outdir_root: str, produced: list) -> tuple:
    """根据生成的文件推断结果真正存放的目录，以及文件名单。

    工作流的 SaveImage 常常带子目录（如 images\\qwen\\gallery\\results），
    只给出 output 根目录的话用户很难找到产物，所以这里返回实际落盘目录。
    """
    if not produced:
        return outdir_root, []
    paths = []
    for item in produced:
        sub = str(item.get("subfolder") or "").replace("\\", "/").strip("/")
        name = item.get("filename") or ""
        paths.append(os.path.join(outdir_root, sub.replace("/", os.sep), name))
    dirs = sorted({os.path.dirname(p) for p in paths})
    if len(dirs) == 1:
        result_dir = dirs[0]
    else:
        try:
            result_dir = os.path.commonpath(dirs)
        except ValueError:
            result_dir = outdir_root
    names = [os.path.basename(p) for p in paths]
    return os.path.abspath(result_dir), names


# ------------------------------------------------------------------ 界面 ---

class Tooltip:
    """轻量悬浮提示。"""

    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, background="#ffffe0", relief="solid",
                 borderwidth=1, justify="left", wraplength=380,
                 font=("Microsoft YaHei UI", 9)).pack()

    def _hide(self, _event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class BatchTab(ttk.Frame):
    """一个批量执行页（文生图 / 图生图共用同一套骨架）。"""

    def __init__(self, parent, app: "App", mode: str):
        super().__init__(parent, padding=(18, 14, 18, 10))
        self.app = app
        self.mode = mode  # "text" 或 "image"

        self.wf_var = tk.StringVar()
        self.node_var = tk.StringVar()
        self.dir_var = tk.StringVar()
        self.status_var = tk.StringVar(value="待命")

        self._build()

    # -- 界面 -------------------------------------------------------------
    def _build(self):
        self.columnconfigure(0, weight=1)
        if self.mode == "text":
            # 提示词列表需要吃掉多余高度；图生图页内容固定，不需要拉伸
            self.rowconfigure(2, weight=1)

        # 第 1 行：工作流文件
        row1 = ttk.Frame(self)
        row1.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        row1.columnconfigure(2, weight=1)
        btn = ttk.Button(row1, text="选择工作流文件", command=self._pick_workflow,
                         style="Accent.TButton")
        btn.grid(row=0, column=0, sticky="w")
        Tooltip(btn, "选择 ComfyUI 中「保存（API 格式）」导出的 .json 工作流文件")
        ttk.Label(row1, text="ComfyUI 导出的 API 格式 .json",
                  style="Hint.TLabel").grid(row=0, column=1, sticky="w", padx=(8, 12))
        ttk.Entry(row1, textvariable=self.wf_var).grid(row=0, column=2, sticky="ew")

        # 第 2 行：节点 ID
        row2 = ttk.Frame(self)
        row2.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        label = "提示词节点 ID" if self.mode == "text" else "图片节点 ID"
        ttk.Label(row2, text=label).grid(row=0, column=0, sticky="w")
        ttk.Entry(row2, textvariable=self.node_var, width=12,
                  font=("Consolas", 11)).grid(row=0, column=1, sticky="w", padx=(10, 14))
        tip = ("CLIPTextEncode / TextEncode 等文本节点的编号，如 496"
               if self.mode == "text"
               else "LoadImage 图片节点的编号，如 470")
        ttk.Label(row2, text=f"提示：{tip}", style="Hint.TLabel").grid(row=0, column=2, sticky="w")

        # 第 3 行：提示词列表 或 图片文件夹
        if self.mode == "text":
            self._build_prompt_area()
        else:
            self._build_dir_area()

        # 第 4 行：固定文本常量
        self._build_constants_area()

        # 第 5 行：操作
        row4 = ttk.Frame(self)
        row4.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self.run_btn = ttk.Button(row4, text="执行工作流", command=self._start,
                                  style="Accent.TButton")
        self.run_btn.grid(row=0, column=0, sticky="w")
        self.stop_btn = ttk.Button(row4, text="停止", command=self._stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, sticky="w", padx=(8, 16))
        ttk.Label(row4, textvariable=self.status_var, style="Status.TLabel").grid(
            row=0, column=2, sticky="w")

    def _build_prompt_area(self):
        holder = ttk.Frame(self)
        holder.grid(row=2, column=0, sticky="nsew")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(1, weight=1)
        ttk.Label(holder, text="提示词列表（每行一条，按顺序逐条执行）").grid(
            row=0, column=0, sticky="w", pady=(0, 6))
        text_frame = ttk.Frame(holder)
        text_frame.grid(row=1, column=0, sticky="nsew")
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)
        self.prompt_text = tk.Text(text_frame, height=6, wrap="none", undo=True,
                                   font=("Microsoft YaHei UI", 10), borderwidth=1,
                                   relief="solid", padx=6, pady=4)
        self.prompt_text.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(text_frame, orient="horizontal", command=self.prompt_text.xview)
        sb.grid(row=1, column=0, sticky="ew")
        self.prompt_text.configure(xscrollcommand=sb.set)
        self.prompt_text.bind("<Control-Return>", lambda _e: self._start())

    def _build_dir_area(self):
        holder = ttk.Frame(self)
        holder.grid(row=2, column=0, sticky="nsew")
        holder.columnconfigure(1, weight=1)
        ttk.Label(holder, text="图片文件夹（将遍历其中全部图片，逐张执行）").grid(
            row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(holder, textvariable=self.dir_var).grid(
            row=1, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(holder, text="浏览…", command=self._pick_dir).grid(row=1, column=1, sticky="w")
        self.dir_hint = ttk.Label(holder, text="", style="Hint.TLabel")
        self.dir_hint.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def _build_constants_area(self):
        """固定文本常量：本批每条任务都写入同样的文本，不随图片变化。"""
        box = ttk.Frame(self)
        box.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        box.columnconfigure(3, weight=1)
        ttk.Label(box,
                  text="固定文本常量（可选）：对本批每一条任务都写入同样的文本，"
                       "不随图片变化",
                  style="Hint.TLabel").grid(row=0, column=0, columnspan=4,
                                            sticky="w", pady=(0, 6))
        for col, title in ((1, "节点ID"), (2, "输入项名称（可留空）"), (3, "文本内容")):
            ttk.Label(box, text=title, style="Hint.TLabel").grid(
                row=1, column=col, sticky="w", padx=(0, 8))
        self.const_vars = []
        for i in range(2):
            id_var = tk.StringVar()
            key_var = tk.StringVar()
            text_var = tk.StringVar()
            self.const_vars.append((id_var, key_var, text_var))
            r = 2 + i
            ttk.Label(box, text=f"常量 {i + 1}").grid(
                row=r, column=0, sticky="w", pady=3)
            ttk.Entry(box, textvariable=id_var, width=9,
                      font=("Consolas", 10)).grid(
                row=r, column=1, sticky="w", padx=(0, 8), pady=3)
            ttk.Entry(box, textvariable=key_var, width=18,
                      font=("Consolas", 10)).grid(
                row=r, column=2, sticky="w", padx=(0, 8), pady=3)
            ttk.Entry(box, textvariable=text_var).grid(
                row=r, column=3, sticky="ew", pady=3)

    # -- 事件 -------------------------------------------------------------
    def _pick_workflow(self):
        path = filedialog.askopenfilename(
            title="选择工作流文件（API 格式）",
            filetypes=[("工作流 JSON", "*.json"), ("所有文件", "*.*")])
        if path:
            self.wf_var.set(path)
            self._describe_workflow(path)

    def _describe_workflow(self, path: str):
        """选完文件后顺手列出节点，方便用户找 ID。"""
        try:
            nodes = load_workflow(path)
        except UserError as exc:
            self.app.log(f"工作流解析失败：{exc}", "error")
            return
        lines = [f"工作流已载入，共 {len(nodes)} 个节点："]
        for node_id, node in sorted(nodes.items(), key=lambda kv: (len(kv[0]), kv[0])):
            lines.append(f"  {node_id}  {describe_node(node)}")
        self.app.log("\n".join(lines))

    def _pick_dir(self):
        folder = filedialog.askdirectory(title="选择需要批量跑图的文件夹")
        if folder:
            self.dir_var.set(folder)
            self._update_dir_hint()

    def _update_dir_hint(self):
        if self.mode != "image":
            return
        folder = self.dir_var.get().strip()
        if not folder:
            self.dir_hint.configure(text="")
            return
        try:
            images = collect_images(folder)
            self.dir_hint.configure(
                text=f"共找到 {len(images)} 张图片：{images[0].name} … {images[-1].name}")
        except UserError as exc:
            self.dir_hint.configure(text=str(exc))

    def _start(self):
        self.app.start_run(self)

    def _stop(self):
        self.app.request_stop()

    # -- 数据 -------------------------------------------------------------
    def collect_plan(self) -> dict:
        """校验界面输入，生成执行计划。"""
        wf_path = self.wf_var.get().strip().strip('"')
        if not wf_path:
            raise UserError("请先选择工作流文件。")
        node_id = self.node_var.get().strip()
        if not node_id:
            raise UserError("请填写节点 ID。")

        nodes = load_workflow(wf_path)
        if node_id not in nodes:
            preview = "、".join(sorted(nodes.keys())[:12])
            raise UserError(
                f"工作流里没有 ID 为 {node_id} 的节点。现有节点：{preview}"
                f"{'…' if len(nodes) > 12 else ''}")
        node = nodes[node_id]

        plan = {"mode": self.mode, "workflow": nodes, "node_id": node_id}
        if self.mode == "text":
            key = resolve_input_key(node, TEXT_KEYS, node_id)
            raw = self.prompt_text.get("1.0", "end")
            lines = [ln.strip() for ln in raw.splitlines()]
            lines = [ln for ln in lines if ln]
            if not lines:
                raise UserError("提示词列表为空，请至少输入一行提示词。")
            plan["input_key"] = key
            plan["items"] = lines
        else:
            key = resolve_input_key(node, IMAGE_KEYS, node_id)
            folder = self.dir_var.get().strip().strip('"')
            if not folder:
                raise UserError("请填写图片文件夹路径。")
            plan["input_key"] = key
            plan["items"] = [str(p) for p in collect_images(folder)]
            plan["folder"] = folder
        plan["constants"] = self._collect_constants(nodes, node_id)
        return plan

    def _collect_constants(self, nodes: dict, var_node_id: str) -> list:
        """解析并校验固定文本常量。

        每条常量 = 节点 ID + （可选）输入项名称 + 文本内容。
        文本对整批任务都生效，属于常量覆盖，不是逐条变量。
        """
        result = []
        for idx, (id_var, key_var, text_var) in enumerate(self.const_vars, 1):
            node_id = id_var.get().strip()
            key = key_var.get().strip()
            text = text_var.get()
            if not node_id and not key and not text:
                continue
            if not node_id:
                raise UserError(f"常量 {idx} 填了内容，但没有填节点 ID。")
            if node_id not in nodes:
                preview = "、".join(sorted(nodes.keys())[:12])
                raise UserError(
                    f"常量 {idx}：工作流里没有 ID 为 {node_id} 的节点。"
                    f"现有节点：{preview}{'…' if len(nodes) > 12 else ''}")
            node = nodes[node_id]
            inputs = node.get("inputs") or {}
            if key:
                if key not in inputs:
                    have = "、".join(inputs.keys()) or "无"
                    raise UserError(
                        f"常量 {idx}：节点 {node_id} 没有名为「{key}」的输入项。"
                        f"\n该节点的输入项：{have}")
            else:
                key = resolve_input_key(node, TEXT_KEYS, node_id)
                hits = [k for k in TEXT_KEYS if k in inputs]
                if len(hits) > 1:
                    self.app.log(
                        f"常量 {idx}：节点 {node_id} 有多个文本输入（{'、'.join(hits)}），"
                        f"已写入「{key}」。要改写其它输入项，请在「输入项名称」里指定。",
                        "muted")
            if node_id == var_node_id:
                self.app.log(
                    f"常量 {idx} 与本次的变量节点是同一个（{node_id}），"
                    f"常量会在变量之后写入，即常量优先。", "muted")
            result.append({"node_id": node_id, "key": key, "text": text})
        return result
    def apply_settings(self, cfg: dict):
        key = "workflow" if self.mode == "text" else "workflow_img"
        self.wf_var.set(cfg.get(key, "") or "")
        self.node_var.set(cfg.get("node" if self.mode == "text" else "node_img", "") or "")
        if self.mode == "image":
            self.dir_var.set(cfg.get("image_dir", "") or "")
            self._update_dir_hint()
        prefix = "const" if self.mode == "text" else "const_img"
        for i, (id_var, key_var, text_var) in enumerate(self.const_vars, 1):
            id_var.set(cfg.get(f"{prefix}{i}_id", "") or "")
            key_var.set(cfg.get(f"{prefix}{i}_key", "") or "")
            text_var.set(cfg.get(f"{prefix}{i}_text", "") or "")

    def save_settings(self, cfg: dict):
        key = "workflow" if self.mode == "text" else "workflow_img"
        cfg[key] = self.wf_var.get().strip()
        cfg["node" if self.mode == "text" else "node_img"] = self.node_var.get().strip()
        if self.mode == "image":
            cfg["image_dir"] = self.dir_var.get().strip()
        prefix = "const" if self.mode == "text" else "const_img"
        for i, (id_var, key_var, text_var) in enumerate(self.const_vars, 1):
            cfg[f"{prefix}{i}_id"] = id_var.get().strip()
            cfg[f"{prefix}{i}_key"] = key_var.get().strip()
            cfg[f"{prefix}{i}_text"] = text_var.get()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME}  v{APP_VERSION}")
        self.geometry("1000x840")
        self.minsize(900, 780)

        self.cfg = load_config()
        self.server_var = tk.StringVar(value=self.cfg.get("server") or DEFAULT_SERVER)
        self.outdir_var = tk.StringVar(value=self.cfg.get("outdir") or "")
        self.status_var = tk.StringVar(value="未检测")
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_text = tk.StringVar(value="")

        self._queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._running = False
        self._worker_thread: threading.Thread | None = None
        self._active_tab: BatchTab | None = None

        self._init_style()
        self._build_ui()
        self._restore_settings()
        self.after(120, self._drain_queue)
        self.after(300, self._check_server)

    # -- 样式 -------------------------------------------------------------
    def _init_style(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        base_font = ("Microsoft YaHei UI", 9)
        try:
            self.option_add("*Font", base_font)
            style.configure(".", font=base_font)
        except Exception:
            pass
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 9, "bold"),
                        padding=(16, 6))
        style.configure("Hint.TLabel", foreground="#6b7280")
        style.configure("Status.TLabel", foreground="#374151")
        style.configure("Tool.TButton", padding=(10, 3))

    # -- 界面 -------------------------------------------------------------
    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.rowconfigure(2, weight=2)

        # 顶部：服务器状态
        top = ttk.Frame(self, padding=(18, 12, 18, 4))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(5, weight=1)
        ttk.Label(top, text="ComfyUI 地址").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.server_var, width=24).grid(
            row=0, column=1, sticky="w", padx=(8, 8))
        ttk.Button(top, text="检测连接", command=self._check_server,
                   style="Tool.TButton").grid(row=0, column=2, sticky="w")
        self.dot = tk.Label(top, text="●", foreground="#9ca3af", font=("Segoe UI", 13))
        self.dot.grid(row=0, column=3, sticky="w", padx=(14, 4))
        ttk.Label(top, textvariable=self.status_var, style="Hint.TLabel").grid(
            row=0, column=4, sticky="w")
        ttk.Label(top, text=f"v{APP_VERSION}", style="Hint.TLabel").grid(
            row=0, column=6, sticky="e")

        # 中部：两个标签页
        nb_frame = ttk.Frame(self, padding=(18, 4, 18, 0))
        nb_frame.grid(row=1, column=0, sticky="nsew")
        notebook = ttk.Notebook(nb_frame)
        notebook.pack(fill="both", expand=True)
        self.tab_text = BatchTab(notebook, self, "text")
        self.tab_image = BatchTab(notebook, self, "image")
        notebook.add(self.tab_text, text="  文生图  ")
        notebook.add(self.tab_image, text="  图生图  ")

        # 底部：进度 + 结果目录 + 日志
        bottom = ttk.Frame(self, padding=(18, 10, 18, 14))
        bottom.grid(row=2, column=0, sticky="nsew")
        bottom.columnconfigure(1, weight=1)
        bottom.rowconfigure(3, weight=1)

        prog_row = ttk.Frame(bottom)
        prog_row.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        prog_row.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(prog_row, variable=self.progress_var,
                                         mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        ttk.Label(prog_row, textvariable=self.progress_text, style="Hint.TLabel").grid(
            row=0, column=1, sticky="w")

        res_row = ttk.Frame(bottom)
        res_row.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        res_row.columnconfigure(1, weight=1)
        ttk.Label(res_row, text="结果文件夹").grid(row=0, column=0, sticky="w")
        ttk.Entry(res_row, textvariable=self.outdir_var).grid(
            row=0, column=1, sticky="ew", padx=(8, 8))
        ttk.Button(res_row, text="打开文件夹", command=self._open_outdir,
                   style="Tool.TButton").grid(row=0, column=2, sticky="w")

        log_row = ttk.Frame(bottom)
        log_row.grid(row=3, column=0, columnspan=3, sticky="nsew")
        log_row.columnconfigure(0, weight=1)
        log_row.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_row, height=6, state="disabled", wrap="word",
                                font=("Consolas", 9), background="#fbfbfc", relief="solid",
                                borderwidth=1, padx=6, pady=4)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_sb = ttk.Scrollbar(log_row, orient="vertical", command=self.log_text.yview)
        log_sb.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_sb.set)
        self.log_text.tag_configure("error", foreground="#b91c1c")
        self.log_text.tag_configure("ok", foreground="#15803d")
        self.log_text.tag_configure("muted", foreground="#6b7280")

        btns = ttk.Frame(bottom)
        btns.grid(row=4, column=0, columnspan=3, sticky="e", pady=(6, 0))
        ttk.Button(btns, text="清空日志", command=self._clear_log,
                   style="Tool.TButton").pack(side="right")

    def _restore_settings(self):
        self.tab_text.apply_settings(self.cfg)
        self.tab_image.apply_settings(self.cfg)
        if not self.outdir_var.get():
            self.outdir_var.set(detect_output_dir(self.server_var.get()))

    # -- 日志 -------------------------------------------------------------
    def log(self, message: str, kind: str = "info"):
        stamp = datetime.now().strftime("%H:%M:%S")
        text = f"[{stamp}] {message}"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n", kind)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        write_log_line(text)

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # -- 连接检测 ---------------------------------------------------------
    def _check_server(self):
        url = self.server_var.get().strip() or DEFAULT_SERVER
        client = ComfyClient(url)
        if client.ping():
            self.dot.configure(foreground="#16a34a")
            self.status_var.set("ComfyUI 已启动")
            outdir = detect_output_dir(url)
            if outdir and not self.outdir_var.get().strip():
                self.outdir_var.set(outdir)
            self.log(f"ComfyUI 连接正常：{url}", "ok")
        else:
            self.dot.configure(foreground="#dc2626")
            self.status_var.set("未连接到 ComfyUI")
            self.log(f"未检测到 ComfyUI 服务（{url}），请先启动 ComfyUI。", "error")

    # -- 执行 -------------------------------------------------------------
    def start_run(self, tab: BatchTab):
        if self._running:
            messagebox.showwarning("正在执行中", "当前还有任务在执行，请先等待或停止。")
            return
        try:
            plan = tab.collect_plan()
        except UserError as exc:
            messagebox.showerror("参数有误", str(exc))
            self.log(str(exc), "error")
            return

        url = self.server_var.get().strip() or DEFAULT_SERVER
        client = ComfyClient(url)
        if not client.ping():
            messagebox.showerror(
                "ComfyUI 未启动",
                f"未检测到 ComfyUI 服务（{url}）。\n\n"
                f"请先启动 ComfyUI，再回到本程序点击「执行工作流」。")
            self.log(f"执行中止：未检测到 ComfyUI 服务（{url}）。", "error")
            return

        # 基础目录始终是 ComfyUI 的 output 根目录，子目录由工作流的 SaveImage 决定。
        # 界面上的「结果文件夹」只是跳转目标，不能拿来当基础目录，否则子目录会重复叠加。
        plan["outdir"] = detect_output_dir(url) or self.outdir_var.get().strip()
        plan["server"] = url

        self._running = True
        self._stop_event.clear()
        self._active_tab = tab
        tab.run_btn.configure(state="disabled")
        tab.stop_btn.configure(state="normal")
        tab.status_var.set("正在执行…")
        self.progress_var.set(0)
        total = len(plan["items"])
        self.progress_text.set(f"0 / {total}")
        self.log("=" * 46)
        label = "文生图" if plan["mode"] == "text" else "图生图"
        self.log(f"开始批量执行（{label}），共 {total} 条任务。")
        for c in plan.get("constants") or []:
            preview = c["text"] if len(c["text"]) <= 60 else c["text"][:60] + "…"
            self.log(f"固定常量：节点 {c['node_id']} 的「{c['key']}」= {preview}", "muted")
        self._worker_thread = threading.Thread(
            target=self._worker, args=(client, plan), daemon=True)
        self._worker_thread.start()

    def request_stop(self):
        if self._running:
            self._stop_event.set()
            self.log("收到停止指令，正在结束当前任务…", "muted")

    def _worker(self, client: ComfyClient, plan: dict):
        """后台线程：逐条执行工作流。"""
        mode = plan["mode"]
        node_id = plan["node_id"]
        input_key = plan["input_key"]
        base = plan["workflow"]
        items = plan["items"]
        constants = plan.get("constants") or []
        client_id = uuid.uuid4().hex
        ok = failed = skipped = 0
        produced = []
        consecutive_errors = 0

        try:
            for index, item in enumerate(items, start=1):
                if self._stop_event.is_set():
                    skipped = len(items) - index + 1
                    break

                nodes = copy.deepcopy(base)
                label = item
                try:
                    if mode == "text":
                        nodes[node_id]["inputs"][input_key] = item
                    else:
                        self._push("log", f"上传图片：{os.path.basename(item)}", "muted")
                        ref = client.upload_image(item)
                        nodes[node_id]["inputs"][input_key] = ref
                        label = os.path.basename(item)
                    # 固定文本常量：整批都套用同样的内容
                    for c in constants:
                        nodes[c["node_id"]]["inputs"][c["key"]] = c["text"]

                    self._push("log", f"[{index}/{len(items)}] 提交：{label}", "info")
                    prompt_id = client.queue_prompt(nodes, client_id)
                    entry = client.wait_prompt(
                        prompt_id, timeout=1800,
                        should_stop=self._stop_event.is_set,
                        tick=lambda pending: self._push(
                            "wait", f"排队中，前面还有 {max(pending - 1, 0)} 个任务"))
                    saved, temp = client.history_outputs(entry)
                    produced.extend(saved)
                    ok += 1
                    consecutive_errors = 0
                    note = ""
                    if saved:
                        note = f"，生成 {len(saved)} 个文件"
                    elif temp:
                        note = "，只生成了预览图（该工作流没有保存节点，不写入 output）"
                    self._push("log",
                               f"[{index}/{len(items)}] 完成：{label}{note}", "ok")
                except Stopped:
                    skipped = len(items) - index + 1
                    self._push("log", "已停止。", "muted")
                    break
                except Exception as exc:
                    failed += 1
                    consecutive_errors += 1
                    self._push("log", f"[{index}/{len(items)}] 失败：{label} → {exc}", "error")
                    if consecutive_errors >= 3:
                        self._push("log",
                                   "连续 3 次失败，已中止本轮批量执行，请检查工作流设置。",
                                   "error")
                        skipped = len(items) - index
                        break
                finally:
                    self._push("progress", index, len(items))
        except Exception as exc:  # 兜底，避免线程静默死掉
            self._push("log", f"批量执行异常终止：{exc}", "error")

        outdir = plan.get("outdir") or ""
        if not outdir:
            outdir = detect_output_dir(plan.get("server") or DEFAULT_SERVER)
        result_dir, file_names = _result_location(outdir, produced)
        self._push("done", {
            "ok": ok, "failed": failed, "skipped": skipped,
            "total": len(items), "outdir": outdir,
            "files": len(produced), "mode": mode,
            "result_dir": result_dir, "file_names": file_names,
        })

    # -- 线程消息 ---------------------------------------------------------
    def _push(self, kind: str, *args):
        self._queue.put((kind, args))

    def _drain_queue(self):
        try:
            while True:
                kind, args = self._queue.get_nowait()
                if kind == "log":
                    self.log(args[0], args[1] if len(args) > 1 else "info")
                elif kind == "progress":
                    done, total = args[0], args[1]
                    self.progress_var.set(done * 100.0 / max(total, 1))
                    self.progress_text.set(f"{done} / {total}")
                elif kind == "wait":
                    self.progress_text.set(f"等待中：{args[0]}")
                elif kind == "done":
                    self._on_finished(args[0])
        except queue.Empty:
            pass
        self.after(120, self._drain_queue)

    def _on_finished(self, info: dict):
        self._running = False
        for tab in (self.tab_text, self.tab_image):
            tab.run_btn.configure(state="normal")
            tab.stop_btn.configure(state="disabled")
        tab = self._active_tab
        if tab is not None:
            tab.status_var.set("已完成" if info["failed"] == 0 else "已结束（含失败项）")

        outdir = info.get("outdir") or ""
        result_dir = info.get("result_dir") or outdir
        file_names = info.get("file_names") or []
        if result_dir:
            # 结果文件夹指向真正落盘的目录，方便一键跳转
            self.outdir_var.set(result_dir)
        self.progress_text.set(f"{info['ok']} / {info['total']} 成功")
        self.progress_var.set(info["ok"] * 100.0 / max(info["total"], 1))

        summary = (f"成功 {info['ok']} 条"
                   + (f"，失败 {info['failed']} 条" if info["failed"] else "")
                   + (f"，未执行 {info['skipped']} 条" if info["skipped"] else "")
                   + (f"，共生成 {info['files']} 个文件" if info.get("files") else "")
                   + f"\n\n结果文件夹：\n{result_dir}")
        if file_names:
            shown = file_names[:10]
            summary += "\n\n生成文件：\n" + "\n".join("  " + n for n in shown)
            if len(file_names) > len(shown):
                summary += f"\n  …… 共 {len(file_names)} 个"
        elif not info.get("failed"):
            summary += ("\n\n该工作流只输出了预览图（没有保存节点），"
                        "图片仅在 ComfyUI 界面里可见。")
        self.log("批量执行完成：" + summary.replace("\n", " "), "ok")
        for name in file_names:
            self.log(f"  产物：{name}", "muted")
        if info["failed"]:
            self.log(f"有 {info['failed']} 条任务失败，详见上方日志。", "error")
        messagebox.showinfo("批量执行完成", summary)

    # -- 其它 -------------------------------------------------------------
    def _open_outdir(self):
        path = self.outdir_var.get().strip()
        if not path:
            messagebox.showinfo("结果文件夹", "尚未确定结果文件夹路径。")
            return
        if not os.path.isdir(path):
            if not messagebox.askyesno("结果文件夹", f"目录不存在：\n{path}\n\n是否创建它？"):
                return
            try:
                os.makedirs(path, exist_ok=True)
            except Exception as exc:
                messagebox.showerror("无法创建目录", str(exc))
                return
        if not open_in_explorer(path):
            messagebox.showinfo("结果文件夹", path)

    def _on_close(self):
        if self._running:
            if not messagebox.askyesno("退出", "还有任务正在执行，确定要退出吗？"):
                return
            self._stop_event.set()
        cfg = self.cfg
        cfg["server"] = self.server_var.get().strip()
        cfg["outdir"] = self.outdir_var.get().strip()
        self.tab_text.save_settings(cfg)
        self.tab_image.save_settings(cfg)
        save_config(cfg)
        self.destroy()


def main():
    if sys.platform == "win":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    try:
        app = App()
    except Exception as exc:
        import traceback
        detail = traceback.format_exc()
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(detail + "\n")
        except Exception:
            pass
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "启动失败",
                f"{exc}\n\n详细错误已写入：\n{LOG_PATH}", parent=root)
            root.destroy()
        except Exception:
            print(detail, file=sys.stderr)
        return
    app.protocol("WM_DELETE_WINDOW", app._on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
