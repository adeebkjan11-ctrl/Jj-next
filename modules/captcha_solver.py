# This file is part of LazyFarmers.
# Copyright (c) 2025-Present Routo
#
# LazyFarmers is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# You should have received a copy of the GNU General Public License
# along with LazyFarmers. If not, see <https://www.gnu.org/licenses/>.


import os
import io
import asyncio
import threading
import aiohttp
import numpy as np
from PIL import Image

try:
    import onnxruntime
except ImportError:
    onnxruntime = None


# Credit to Owo-Dusk for onnxmodel https://github.com/owo-dusk/owo-dusk/blob/main/utils/captcha_solver/best.onnx


# One InferenceSession for the whole process, not one per account.
#
# It used to be per account, and on_ready built a second one for the same bot -
# and on_ready fires again on every reconnect, so the count only ever grew. Each
# session is 12MB of weights plus, with default SessionOptions, a native thread
# pool sized to the *host's* core count rather than the container's share. A farm
# of sixteen accounts was therefore carrying a few hundred megabytes of duplicate
# model and several hundred threads it never used, and eventually died either on
# the memory limit (SIGKILL - no traceback, nothing in the log) or inside
# onnxruntime's own thread creation (terminate called after throwing an instance
# of 'std::system_error': Resource temporarily unavailable).
#
# Nothing about the session is per-account: same file, same weights, and
# Session.run is thread-safe. A shared one costs 12MB no matter how many accounts
# are up. The thread caps are deliberate - a captcha is one 384px image every few
# hours, run on a worker thread already, so intra-op parallelism buys nothing and
# the pool is pure overhead.
_SESSION_LOCK = threading.Lock()
_SESSION = None
_SESSION_TRIED = False
_SESSION_ERROR = None


def _shared_session(model_path):
    """Load the model once. Returns (session, error_message)."""
    global _SESSION, _SESSION_TRIED, _SESSION_ERROR
    with _SESSION_LOCK:
        if _SESSION_TRIED:
            return _SESSION, _SESSION_ERROR
        _SESSION_TRIED = True

        if onnxruntime is None:
            _SESSION_ERROR = "onnxruntime not installed. AI Solver disabled."
            return None, _SESSION_ERROR
        if not os.path.exists(model_path):
            _SESSION_ERROR = f"AI Model not found at {model_path}"
            return None, _SESSION_ERROR

        try:
            opts = onnxruntime.SessionOptions()
            opts.intra_op_num_threads = 1
            opts.inter_op_num_threads = 1
            _SESSION = onnxruntime.InferenceSession(
                model_path,
                sess_options=opts,
                providers=["CPUExecutionProvider"]
            )
        except Exception as e:
            _SESSION_ERROR = f"Failed to load AI model: {e}"
        return _SESSION, _SESSION_ERROR


class CaptchaSolver:
    """
    uses local onnx models to solve 'letterword' security captchas.
    """
    def __init__(self, bot):
        self.bot = bot
        self.model_path = os.path.join(self.bot.base_dir, "models", "best.onnx")
        self.onnx_session = None
        self.classes = "abcdefghijklmnopqrstuvwxyz"
        self.conf_threshold = 0.3
        # a letter kept at 0.3 is a coin flip; an answer is only sent when every letter
        # in it clears this. Below it the captcha goes to a human with attempts intact.
        self.min_answer_conf = 0.5
        self.img_size = 384
        
        self._load_model()

    def _load_model(self):
        """Attach to the process-wide session; the first caller pays for the load."""
        self.onnx_session, error = _shared_session(self.model_path)
        if self.onnx_session is not None:
            self.bot.log("SYS", "AI Captcha Solver initialized.")
        elif error:
            self.bot.log("ERROR" if "not installed" not in error else "SYS", error)

    def _letterbox(self, img_array, new_size=384, color=(114, 114, 114)):
        """resize image with padding to maintain aspect ratio."""
        img = Image.fromarray(img_array)
        w, h = img.size
        scale = min(new_size / w, new_size / h)
        nw, nh = int(w * scale), int(h * scale)
        img_resized = img.resize((nw, nh), Image.BILINEAR)
        
        new_img = Image.new("RGB", (new_size, new_size), color)
        paste_x = (new_size - nw) // 2
        paste_y = (new_size - nh) // 2
        new_img.paste(img_resized, (paste_x, paste_y))
        
        return np.array(new_img)

    async def solve_image(self, url, letter_count=5):
        """
        downloads a captcha image from a url and predicts the letters.

        Returns None rather than a partial answer when the model does not find exactly
        letter_count letters. OwO allows about three attempts before it stops accepting
        answers at all, so sending "abd" for a 5-letter captcha spends an attempt on
        something that cannot be right - refusing hands it to a human with all attempts
        still available.
        """
        if not self.onnx_session:
            return None

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        # used to be a bare return None, so an expired or refused image
                        # url looked identical to a model that simply saw nothing
                        self.bot.log("ERROR", f"Captcha image download failed: HTTP "
                                              f"{resp.status} for {url[:80]}")
                        return None

                    data = await resp.read()
                    img = Image.open(io.BytesIO(data)).convert("RGB")
                    img_array = np.array(img)
        except Exception as e:
            self.bot.log("ERROR", f"Failed to download captcha image: {e}")
            return None

        # ~250ms of CPU per solve. On the event loop that stalls every other account's
        # sends and heartbeats, so it runs on a worker thread.
        try:
            detections = await asyncio.to_thread(self._detect, img_array)
        except Exception as e:
            self.bot.log("ERROR", f"AI Solver inference failed: {e}")
            return None

        if len(detections) > letter_count:
            # keep the most confident letter_count, then read them left to right
            detections.sort(key=lambda d: d["conf"], reverse=True)
            detections = detections[:letter_count]
        detections.sort(key=lambda d: d["cx"])

        result = "".join(d["char"] for d in detections)
        if len(result) != letter_count:
            self.bot.log("WARN", f"AI Solver found {len(result)} letters "
                                 f"({result or 'none'}) but OwO asked for {letter_count} "
                                 f"- refusing to guess so the attempt is not wasted.")
            return None

        weakest = min(d["conf"] for d in detections)
        if weakest < self.min_answer_conf:
            self.bot.log("WARN", f"AI Solver read '{result}' but one letter scored only "
                                 f"{weakest:.2f} (floor {self.min_answer_conf:.2f}) "
                                 f"- refusing to guess.")
            return None

        self.bot.log("SECURITY", f"AI Solver Predicted: {result} "
                                 f"(lowest letter {weakest:.2f})")
        return result

    def _detect(self, img_array):
        """Run the model. Blocking - call through asyncio.to_thread."""
        img = self._letterbox(img_array, self.img_size)
        img = img.astype(np.float32) / 255.0
        img = np.transpose(img, (2, 0, 1))
        img = np.expand_dims(img, axis=0)

        input_name = self.onnx_session.get_inputs()[0].name
        outputs = self.onnx_session.run(None, {input_name: img})[0]

        detections = []
        for det in outputs[0]:
            x1, y1, x2, y2, conf, cls_id = det
            if conf < self.conf_threshold:
                continue
            detections.append({
                "char": self.classes[int(cls_id)],
                "conf": float(conf),
                "cx": float((x1 + x2) / 2),
            })
        return detections

def setup_solver(bot):
    return CaptchaSolver(bot)
