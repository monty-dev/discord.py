# The MIT License (MIT)

# Copyright (c) 2015-present Rapptz

# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
# OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from loguru import logger as log

if TYPE_CHECKING:
    from discord.http import HTTPClient


def _typing_done_callback(fut):
    # just retrieve any exception and call it a day
    try:
        fut.exception()
    except (asyncio.CancelledError, Exception):
        pass


class Typing:
    def __init__(self, messageable) -> None:
        self.loop: asyncio.AbstractEventLoop = messageable._state.loop
        self.messageable = messageable
        self.typing_deadline: int = 0

    async def do_typing(self):
        try:
            channel = self._channel
        except AttributeError:
            channel = await self.messageable._get_channel()
        http: HTTPClient = channel._state.http
        typing = http.send_typing
        lock = http.typing_locks[channel.id]
        self.typing_deadline = time.time() + (60 * 3)
        while True:
            if time.time() > self.typing_deadline:
                return log.error("Typing deadline exceeded. Channel {} / {} ", channel, channel.id)
            try:
                async with asyncio.timeout(0.25):
                    await lock.acquire()
            except asyncio.TimeoutError:
                continue
            else:
                try:
                    await typing(channel.id)
                    await asyncio.sleep(4.2)
                finally:
                    lock.release()

    def __enter__(self):
        self.task = self.loop.create_task(self.do_typing())
        self.task.add_done_callback(_typing_done_callback)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.task.cancel()
        asyncio.ensure_future(asyncio.gather(self.task, return_exceptions=True))

    async def __aenter__(self):
        self._channel = channel = await self.messageable._get_channel()
        await channel._state.http.send_typing(channel.id)
        return self.__enter__()

    async def __aexit__(self, exc_type, exc, tb):
        self.task.cancel()
        asyncio.ensure_future(asyncio.gather(self.task, return_exceptions=True))
