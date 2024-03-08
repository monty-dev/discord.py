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

from loguru import logger as log


class Typing:
    def __init__(self, messageable) -> None:
        self.loop: asyncio.AbstractEventLoop = messageable._state.loop
        self.messageable = messageable
        self.typing_deadline: int = 0

    async def do_typing(self, channel):
        from melanie.redis import get_redis

        redis = get_redis()
        lock = redis.get_lock(f"typing:{channel.id}:{channel._state.self_id}", timeout=300)
        self.typing_deadline = time.time() + (60 * 3)
        while True:
            if time.time() > self.typing_deadline:
                return log.error("Typing deadline exceeded. Channel {} / {} ", channel, channel.id)
            if await lock.acquire(blocking_timeout=1):
                try:
                    await channel._state.http.send_typing(channel.id)
                    await asyncio.sleep(4.2)
                finally:
                    await lock.release()

    async def __aenter__(self):
        channel = await self.messageable._get_channel()
        self.task = asyncio.create_task(self.do_typing(channel))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.task:
            self.task.cancel()
            asyncio.ensure_future(asyncio.gather(self.task, return_exceptions=True))
