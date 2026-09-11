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


import asyncio
import numpy as np
import io
from PIL import Image
import base64


# priorty and encoded images(avoiding I-O for speed) idea taken from owo-dusk

class NeuraSolver:
    PRIORITY_LEVELS = [
        list("abdegkmpqstvwxyz"),
        list("fho"),              
        list("cnru"),           
        list("jl"),              
        list("i"),                
    ]

    MASKS = {
        "a": "iVBORw0KGgoAAAANSUhEUgAAAA0AAAANCAYAAABy6+R8AAAARElEQVQoFWN0mv3/PwOJgIlE9WDlZGliJNYmZG+QZxOyCcTaSpZNZGliQXbSvlRGnAGD7A2ybGJENgHZVnxssmwiSxMARF4PhclsgWMAAAAASUVORK5CYII=",
        "b": "iVBORw0KGgoAAAANSUhEUgAAAA0AAAASCAYAAACAa1QyAAAAO0lEQVQoFWN0mv3/PwMU7EtlZISx8dFM+CRxyQ1yTYzIAYHLD+ji9PMTC7LV+OIJ2Rv0c95wtGmQpwgAPGAN2uSjSi0AAAAASUVORK5CYII=",
        "c": "iVBORw0KGgoAAAANSUhEUgAAAA0AAAANCAYAAABy6+R8AAAALklEQVQoFWN0mv3/PwOJgIlE9WDlZGliQbZpXyojIzIfF5ssm0Y1QYOTcXCnCADdtQb2r0ES3wAAAABJRU5ErkJggg==",
        "d": "iVBORw0KGgoAAAANSUhEUgAAAA0AAAASCAYAAACAa1QyAAAAOUlEQVQoFWNkIBI4zf7/H6aUCcYghR7kmhiRPUisv+jnJxZkJ+1LZWRE5iOzkb1BP+cNR5sGeYoAAJVGDLX7Igd2AAAAAElFTkSuQmCC",
        "e": "iVBORw0KGgoAAAANSUhEUgAAAA0AAAANCAYAAABy6+R8AAAAP0lEQVQoFWN0mv3/PwOJgIlE9WDlZGliQbZpXyojIzIfmY3sDbJsYkQ2AdlkfGyybCJLE9EBgexcsmwa5AEBAB/ZDdI5nuFXAAAAAElFTkSuQmCC",
        "f": "iVBORw0KGgoAAAANSUhEUgAAAA0AAAANCAYAAABy6+R8AAAAMUlEQVQoFWN0mv3/PwOJgIlE9WDlZGliQbZpXyojIzIfF5ssmxhHAwISnmSFHv00AQDzrwgdUIeJDgAAAABJRU5ErkJggg==",
        "g": "iVBORw0KGgoAAAANSUhEUgAAAA0AAAANCAYAAABy6+R8AAAAR0lEQVQoFWN0mv3/PwOJgIlE9WDlZGliQbZpXyojIzIfF5tym2AmEwocsmwiSxNKQMCchy1AkJ1Mlk2MyCbAbCJEk2UTWZoA7Z4N1XlVX20AAAAASUVORK5CYII=",
        "h": "iVBORw0KGgoAAAANSUhEUgAAAA0AAAASCAYAAACAa1QyAAAAPUlEQVQoFWN0mv3/PwMU7EtlZISx8dFM+CRxyQ1yTYzIAYHLD+ji9PMTC7LV+OIJ2Rv0c96oTdD4oV9AAAB/TQsAYy1MLwAAAABJRU5ErkJggg==",
        "i": "iVBORw0KGgoAAAANSUhEUgAAAAkAAAANCAYAAAB7AEQGAAAAIklEQVQYGWNkQAJOs///h3H3pTIywthMMAY+elQRw2AMAgBW+wQa/q56owAAAABJRU5ErkJggg==",
        "j": "iVBORw0KGgoAAAANSUhEUgAAAA0AAAANCAYAAABy6+R8AAAAN0lEQVQoFWNkIBI4zf7/H6aUCcYghR7VBA0tsgKCBTn896UyMhIT9GTZxIhsEzG2gNSQZRNZmgAmfgnRpvfItgAAAABJRU5ErkJggg==",
        "k": "iVBORw0KGgoAAAANSUhEUgAAAAwAAAANCAYAAACdKY9CAAAAaklEQVQoFYWR0Q3AIAhES0frLp2qu3Q1zSW95rhA8EeU94BoXM9ax7feO4Kx7+ROT1RnwsiNgsKj4DBGbjtUcNuhg0vBYUC60kgTDDEJWkljLVQKeA3/RErBgBU7kPnUwWFAfvcLnmBFlzY0ejHPkHfW8AAAAABJRU5ErkJggg==",
        "l": "iVBORw0KGgoAAAANSUhEUgAAAA0AAAANCAYAAABy6+R8AAAAL0lEQVQoFWN0mv3/PwMU7EtlZISx8dFM+CRxyY1qgobMIA8IRuQUgSsy0cXp5ycAj5sG9B8JGsEAAAAASUVORK5CYII=",
        "m": "iVBORw0KGgoAAAANSUhEUgAAAA0AAAANCAYAAABy6+R8AAAAMElEQVQoFWN0mv3/PwOJgIlE9WDllGval8rICMLItmMTo9wmZBvwsUdtgoYO/QICANPkB4nFxDDlAAAAAElFTkSuQmCC",
        "n": "iVBORw0KGgoAAAANSUhEUgAAAA0AAAANCAYAAABy6+R8AAAAL0lEQVQoFWN0mv3/PwOJgIlE9WDlZGliQbZpXyojIzIfmY3sDbJsGtUEDc5BHhAAj7kG91sA1sEAAAAASUVORK5CYII=",
        "o": "iVBORw0KGgoAAAANSUhEUgAAAA0AAAANCAYAAABy6+R8AAAAN0lEQVQoFWN0mv3/PwOJgIlE9WDlZGliQbZpXyojIzIfmY3sDbJsGtUEDU5G5KBEDmJ8bPqFHgBMzAnR80GexgAAAABJRU5ErkJggg==",
        "p": "iVBORw0KGgoAAAANSUhEUgAAAA0AAAASCAYAAACAa1QyAAAAQklEQVQoFWN0mv3/PwOJgIlE9WDlZGliQbZpXyojIzIfmY3sDbJsGtUEDU5G5KBEDmJ8bPqFHtEpAtm59HMe/WwCAJ8UCwJWTpYsAAAAAElFTkSuQmCC",
        "q": "iVBORw0KGgoAAAANSUhEUgAAAA0AAAASCAYAAACAa1QyAAAATUlEQVQoFWN0mv3/PwOJgIlE9WDlZGliQbZpXyojIzIfmY3sDbJsGtUEDU5G5KBEDmJ8bPqFHtYUgOxkbKmELOcRtAkWIMg2kmUTWZoAQwMR2VhDl78AAAAASUVORK5CYII=",
        "r": "iVBORw0KGgoAAAANSUhEUgAAAA0AAAANCAYAAABy6+R8AAAALUlEQVQoFWN0mv3/PwOJgIlE9WDlZGliQbZpXyojIzIfF5ssm0Y1QYNzkAcEACCxBBxWW3qwAAAAAElFTkSuQmCC",
        "s": "iVBORw0KGgoAAAANSUhEUgAAAA0AAAANCAYAAABy6+R8AAAANElEQVQoFWN0mv3/PwOJgIlE9WDlZGliQbZpXyojIzIfF5ssmxgHd0AQ5XFQgCB7YzgGBAAHzQyqIIdwIAAAAABJRU5ErkJggg==",
        "t": "iVBORw0KGgoAAAANSUhEUgAAAA0AAAANCAYAAABy6+R8AAAALUlEQVQoFWN0mv3/PwOJgIlE9WDlZGlixGYTspP3pTJiqCHLplFN0KAe5AEBAKu7BvTrMd81AAAAAElFTkSuQmCC",
        "u": "iVBORw0KGgoAAAANSUhEUgAAAA0AAAANCAYAAABy6+R8AAAAMUlEQVQoFWN0mv3/PwMU7EtlZISx0WlkdUzoksTwRzVBQ2mQBwQjckwTE7EgNfTzEwDExgnPrPJ4NwAAAABJRU5ErkJggg==",
        "v": "iVBORw0KGgoAAAANSUhEUgAAAAwAAAANCAYAAACdKY9CAAAAd0lEQVQoFYWQAQ6AIAwDGfFl/sVX+Re+htSkpBssLDE09tqhpYy5397xQGdD3ygAtsdsF1CmKqCGvldds1ZCWgLWbQCkgGoW/IHTFsBkLib13DXTd38lA9mO0PINbMpOtwFQ3KLt8JcNEQCkswTUPIUnG681jSE+XwMvgvKD3yEAAAAASUVORK5CYII=",
        "w": "iVBORw0KGgoAAAANSUhEUgAAAA0AAAANCAYAAABy6+R8AAAALUlEQVQoFWN0mv3/PwMU7EtlZAQxCYkxwTSQQo9qgobWIA8IRuTYJzaC6ecnACGvDc/Z7HB/AAAAAElFTkSuQmCC",
        "x": "iVBORw0KGgoAAAANSUhEUgAAAAwAAAANCAYAAACdKY9CAAAAgklEQVQoFZWSiw2AIAxEi6PpKm6lqzgbesRnygVNJMGW3gcaG7XWmLfre8WvDacoiXsdaxTyHJc9Hs70BlDPZNUmd82EnGPQbnARoEfxihpljRzBMO0EAl0EEeH/plG6M3XFjLUbcgGiPwVO9+NGZIhgXQ8qurOf2/xoPJiVt3kCPwGLgnhJFhDySgAAAABJRU5ErkJggg==",
        "y": "iVBORw0KGgoAAAANSUhEUgAAAA0AAAASCAYAAACAa1QyAAAAPklEQVQoFWN0mv3/PwMU7EtlZISx0WlkdUzoksTwRzVBQ2mQBwQjckwTE7EgNfTzE9YEiuxkbImYfs6jn00ArlAN2LER5EoAAAAASUVORK5CYII=",
        "z": "iVBORw0KGgoAAAANSUhEUgAAAA0AAAANCAYAAABy6+R8AAAAT0lEQVQoFWN0mv3/PwOJgIlE9WDlZGliJGQTuvP3pTIy4tWETQPIEpyacGnAqQmfBqyaCGnA0ESMBhRNxGqAa0LXAJLABxhJ1QAyjKwUAQA6fySifLwVygAAAABJRU5ErkJggg=="
    }

    def __init__(self):
        self.check_data = []
        self._initialize_masks()

    def _initialize_masks(self):
        for group in self.PRIORITY_LEVELS:
            for char in group:
                if char in self.MASKS:
                    img = Image.open(io.BytesIO(base64.b64decode(self.MASKS[char])))
                    mask = np.array(img.convert("RGBA"))
                    h, w = mask.shape[:2]
                    # Only the opaque pixels of a glyph identify it; the transparent
                    # ones sit over whatever the captcha painted there. Precompute
                    # their coordinates and colours once instead of re-deriving the
                    # alpha mask on every scan.
                    ys, xs = np.nonzero(mask[:, :, 3] > 0)
                    coords = list(zip(ys.tolist(), xs.tolist()))
                    colours = mask[ys, xs]
                    self.check_data.append((coords, colours, h, w, char))

    @staticmethod
    def _match_offsets(large, coords, colours, h, w):
        """Window origins (x, y) where every opaque mask pixel equals the image.

        The obvious way to write this is two Python loops over y and x with a numpy
        compare inside, which is what this used to be: ~7k fancy-index compares per
        glyph, 26 glyphs, all of it on the shared event loop. Instead, walk the
        glyph's opaque pixels and AND together one full-plane compare each - the same
        predicate, a few hundred vectorised ops instead of a few hundred thousand
        scalar ones, and it bails the moment no candidate origin survives.
        """
        rows = large.shape[0] - h + 1
        cols = large.shape[1] - w + 1
        if rows <= 0 or cols <= 0:
            return []

        hits = np.ones((rows, cols), dtype=bool)
        for (dy, dx), rgba in zip(coords, colours):
            hits &= (large[dy:dy + rows, dx:dx + cols] == rgba).all(axis=2)
            if not hits.any():
                return []

        ys, xs = np.nonzero(hits)
        # nonzero walks row-major, so this is y-ascending then x-ascending - the
        # order the nested loops produced, which is what the overlap filter below
        # depends on to keep choosing the same glyph out of two that collide.
        return list(zip(xs.tolist(), ys.tolist()))

    def _scan(self, large_array):
        """Blocking pixel scan. Runs on a worker thread - see solve()."""
        matches = []
        for coords, colours, h, w, char in self.check_data:
            for x, y in self._match_offsets(large_array, coords, colours, h, w):
                # matches carries across glyphs on purpose: PRIORITY_LEVELS orders
                # them so a glyph that contains a smaller one (l inside k) is
                # accepted first and the smaller one is then rejected as overlapping.
                if not any(
                    (m[0] - w < x < m[0] + w) and (m[1] - h < y < m[1] + h)
                    for m in matches
                ):
                    matches.append((x, y, char))

        matches.sort(key=lambda m: m[0])
        return "".join([m[2] for m in matches])

    async def solve(self, image_input, session=None, confidence=0.95):
        try:
            if isinstance(image_input, str) and image_input.startswith("http"):
                if not session: return ""
                async with session.get(image_input) as resp:
                    if resp.status == 200:
                        image_data = await resp.read()
                        captcha_img = Image.open(io.BytesIO(image_data))
                    else: return ""
            else:
                captcha_img = image_input if isinstance(image_input, Image.Image) else Image.open(image_input)

            large_array = np.array(captcha_img.convert("RGBA"))
        except Exception:
            return ""

        try:
            # Hundreds of milliseconds of pure CPU. Awaiting it inline froze the one
            # loop every account shares, so a single huntbot captcha stalled every
            # other account's sends and heartbeats - same reason
            # modules/captcha_solver.py runs its inference on a thread.
            return await asyncio.to_thread(self._scan, large_array)
        except Exception:
            return ""


# The masks are constant, so decoding 26 PNGs per captcha was pure waste on the loop.
# Built lazily on first use and only ever from the shared loop, so no lock is needed.
_solver = None


def _get_solver():
    global _solver
    if _solver is None:
        _solver = NeuraSolver()
    return _solver


async def solveHbCaptcha(captcha_url, session):
    return await _get_solver().solve(captcha_url, session)
