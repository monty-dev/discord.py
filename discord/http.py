import io
import os
import random
import time

import msgspec

"""
The MIT License (MIT)

Copyright (c) 2015-present Rapptz

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""
import asyncio
import contextlib
import logging
from urllib.parse import quote as _uriquote
from melanie import get_curl, CurlRequest
import aiohttp
import limits
import orjson
import pycurl
from anyio import AsyncFile
from loguru import logger as log
from tornado.httpclient import AsyncHTTPClient, HTTPRequest
from tornado.httputil import url_concat

from . import __version__, utils
from .cacheutils import LRU
from .errors import DiscordServerError, Forbidden, GatewayNotFound, HTTPException, LoginFailure, NotFound
from .gateway import DiscordClientWebSocketResponse

aiohttp.hdrs.WEBSOCKET = "websocket"


class Ratelimiter:
    def __init__(self) -> None:
        self.backend = limits.storage.storage_from_string("async+memory://")
        self.moving_window = limits.aio.strategies.MovingWindowRateLimiter(self.backend)
        self.default_rate = limits.RateLimitItemPerSecond(60, 1)

    async def hit(self):
        return await self.moving_window.hit(self.default_rate, "global", "main")

    async def test(self):
        return await self.moving_window.test(self.default_rate, "global", "main")


class Route:
    BASE = "https://discord.com/api/v7"

    def __init__(self, method, path, **parameters):
        self.path = path
        self.method = method
        url = self.BASE + self.path
        if parameters:
            self.url = url.format(**{k: _uriquote(v) if isinstance(v, str) else v for k, v in parameters.items()})
        else:
            self.url = url

        self.channel_id = parameters.get("channel_id")
        self.guild_id = parameters.get("guild_id")

    @property
    def bucket(self):
        # the bucket is just method + path w/ major parameters
        return f"{self.channel_id}:{self.guild_id}:{self.path}"


class DeferrableLock(asyncio.Lock):
    def __init__(self, bucket):
        self.bucket = bucket
        self.delay = 0
        self.loop = asyncio.get_running_loop()
        super().__init__()

    def defer_for(self, delay: float):
        self.delay = delay

    async def __aenter__(self) -> None:
        await self.acquire()
        return self

    async def __aexit__(self, *e) -> None:
        self.loop.call_later(self.delay, self.release)
        self.delay = 0


class HTTPClient:
    """Represents an HTTP client sending HTTP requests to the Discord API."""

    SUCCESS_LOG = "{method} {url} has received {text}"
    REQUEST_LOG = "{method} {url} with {json} has returned {status}"

    def __init__(self, connector=None, *, proxy=None, proxy_auth=None, loop=None, unsync_clock=True):
        self.connector = connector
        self.loop = loop
        self.__session = None  # filled in static_login
        self._locks = {}
        self.token = None
        self.ratelimiter = Ratelimiter()
        self.bot_token = False
        self.proxy = proxy
        self.global_event = asyncio.Event()
        self.global_event.set()
        self.control_lock = asyncio.Lock()
        self.proxy_auth = proxy_auth
        self.use_clock = False
        self.count = 0
        self.curl = get_curl()
        self.user_agent = "discord.gg/melaniebot (libcurl)"

    async def get_lock(self, bucket) -> DeferrableLock:
        async with self.control_lock:
            if not bucket:
                return DeferrableLock(bucket)
            if bucket not in self._locks:
                self._locks[bucket] = DeferrableLock(bucket)
            return self._locks[bucket]

    async def ws_connect(self, url, *, compress=0):
        kwargs = {
            "proxy_auth": self.proxy_auth,
            "proxy": self.proxy,
            "max_msg_size": 0,
            "timeout": 30.0,
            "autoclose": False,
            "headers": {
                "User-Agent": self.user_agent,
            },
            "compress": compress,
        }

        return await self.__session.ws_connect(url, **kwargs)

    def recreate(self):
        if self.__session.closed:
            self.__session = aiohttp.ClientSession(connector=self.connector, ws_response_class=DiscordClientWebSocketResponse)

    async def request(self, route, *, files=None, form=None, **kwargs):
        bucket = route.bucket
        method = route.method
        url = route.url
        headers = {"User-Agent": self.user_agent, "X-Ratelimit-Precision": "millisecond"}
        if self.token:
            headers["Authorization"] = f"Bot {self.token}" if self.bot_token else self.token

        if "json" in kwargs:
            headers["Content-Type"] = "application/json"

            kwargs["data"] = orjson.dumps(kwargs.pop("json"))
        if "reason" in kwargs:
            if reason := kwargs.pop("reason"):
                headers["X-Audit-Log-Reason"] = _uriquote(reason, safe="/ ")

        kwargs["headers"] = headers
        if self.proxy:
            kwargs["proxy"] = self.proxy
        if self.proxy_auth:
            kwargs["proxy_auth"] = self.proxy_auth

        lock = await self.get_lock(bucket)  # this needs to be done under a mutext to make
        # sure stampedes don't cause the lock to be replaced with a new one
        async with lock:
            for tries in range(5):
                await self.global_event.wait()
                if form:
                    form_data = aiohttp.FormData()
                    for params in form:
                        form_data.add_field(**params)
                    kwargs["data"] = form_data

                try:
                    while not await self.ratelimiter.test():
                        await asyncio.sleep(random.uniform(0.01, 0.1))

                    if not files and not form:
                        if "params" in kwargs:
                            url = url_concat(url, kwargs["params"])
                        _request = CurlRequest(url, method=method, headers=headers, body=kwargs["data"] if "data" in kwargs else None)
                        r = await self.curl.fetch(_request, raise_error=False)
                        r.status = r.code
                        try:
                            data = orjson.loads(r.body)
                        except orjson.JSONDecodeError:
                            data = r.body.decode("UTF-8")
                    else:
                        async with self.__session.request(method, url, **kwargs) as r:
                            data = await r.read()
                            if "json" in r.content_type:
                                data = orjson.loads(data)
                            else:
                                data = data.decode("UTF-8")
                    remaining = r.headers.get("X-Ratelimit-Remaining")
                    self.count += 1
                    if remaining == "0" and r.status != 429:
                        delta = utils._parse_ratelimit_header(r, use_clock=self.use_clock)
                        lock.defer_for(delta)  # Using the method 429's are almost non-existant
                    if 300 > r.status >= 200:
                        return data
                    if r.status == 429:
                        retry_after = data["retry_after"] / 1000.0
                        if not r.headers.get("Via"):
                            raise HTTPException(r, data)
                        if is_global := data.get("global", False):
                            retry_after = retry_after + 0.5
                            self.global_event.clear()
                            current = asyncio.current_task()
                            log.error(f"Ratelimited globally. Task {current}  Retrying in {retry_after:.2f} seconds.")
                            await asyncio.sleep(retry_after)
                            self.global_event.set()
                        else:
                            log.error(f"Ratelimt for bucket {bucket}. Retrying in {retry_after:.2f} seconds ")
                            await asyncio.shield(asyncio.sleep(retry_after))
                        continue

                    if r.status in {500, 502}:
                        await asyncio.sleep(random.uniform(0.2, 1))
                        continue
                    if r.status == 403:
                        raise Forbidden(r, data)
                    elif r.status == 404:
                        raise NotFound(r, data)
                    elif r.status == 503:
                        raise DiscordServerError(r, data)
                    else:
                        raise HTTPException(r, data)

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    if tries < 4:
                        continue
                    else:
                        raise
            # We've run out of retries, raise.
            if r.status >= 500:
                raise DiscordServerError(r, data)
            raise HTTPException(r, data)

    async def get_from_cdn(self, url):
        async with self.__session.get(url) as resp:
            if resp.status == 200:
                return await resp.read()
            elif resp.status == 404:
                raise NotFound(resp, "asset not found")
            elif resp.status == 403:
                raise Forbidden(resp, "cannot retrieve asset")
            else:
                raise HTTPException(resp, "failed to get asset")

    # state management

    async def close(self):
        if self.__session:
            await self.__session.close()

    def _token(self, token, *, bot=True):
        self.token = token
        self.bot_token = bot
        self._ack_token = None

    # login management
    # login management

    async def static_login(self, token, *, bot):
        self.__session = aiohttp.ClientSession(
            connector=self.connector,
            ws_response_class=DiscordClientWebSocketResponse,
        )
        old_token, old_bot = self.token, self.bot_token
        self._token(token, bot=bot)

        try:
            data = await self.request(Route("GET", "/users/@me"))
        except HTTPException as exc:
            self._token(old_token, bot=old_bot)
            if exc.response.status == 401:
                raise LoginFailure("Improper token has been passed.") from exc
            raise

        return data

    def logout(self):
        return self.request(Route("POST", "/auth/logout"))

    # Group functionality

    def start_group(self, user_id, recipients):
        payload = {"recipients": recipients}

        return self.request(Route("POST", "/users/{user_id}/channels", user_id=user_id), json=payload)

    def leave_group(self, channel_id):
        return self.request(Route("DELETE", "/channels/{channel_id}", channel_id=channel_id))

    def add_group_recipient(self, channel_id, user_id):
        r = Route("PUT", "/channels/{channel_id}/recipients/{user_id}", channel_id=channel_id, user_id=user_id)
        return self.request(r)

    def remove_group_recipient(self, channel_id, user_id):
        r = Route("DELETE", "/channels/{channel_id}/recipients/{user_id}", channel_id=channel_id, user_id=user_id)
        return self.request(r)

    def edit_group(self, channel_id, **options):
        valid_keys = ("name", "icon")
        payload = {k: v for k, v in options.items() if k in valid_keys}

        return self.request(Route("PATCH", "/channels/{channel_id}", channel_id=channel_id), json=payload)

    def convert_group(self, channel_id):
        return self.request(Route("POST", "/channels/{channel_id}/convert", channel_id=channel_id))

    # Message management

    def start_private_message(self, user_id):
        payload = {"recipient_id": user_id}

        return self.request(Route("POST", "/users/@me/channels"), json=payload)

    def send_message(self, channel_id, content, *, tts=False, embed=None, nonce=None, allowed_mentions=None, message_reference=None):
        r = Route("POST", "/channels/{channel_id}/messages", channel_id=channel_id)
        payload = {}

        if content:
            payload["content"] = content

        if tts:
            payload["tts"] = True

        if embed:
            payload["embed"] = embed

        if nonce:
            payload["nonce"] = nonce

        if allowed_mentions:
            payload["allowed_mentions"] = allowed_mentions

        if message_reference:
            payload["message_reference"] = message_reference

        return self.request(r, json=payload)

    def send_typing(self, channel_id):
        return self.request(Route("POST", "/channels/{channel_id}/typing", channel_id=channel_id))

    def send_files(self, channel_id, *, files, content=None, tts=False, embed=None, nonce=None, allowed_mentions=None, message_reference=None):
        r = Route("POST", "/channels/{channel_id}/messages", channel_id=channel_id)
        payload = {"tts": tts}
        if content:
            payload["content"] = content
        if embed:
            payload["embed"] = embed
        if nonce:
            payload["nonce"] = nonce
        if allowed_mentions:
            payload["allowed_mentions"] = allowed_mentions
        if message_reference:
            payload["message_reference"] = message_reference

        form = [{"name": "payload_json", "value": utils.to_json(payload)}]
        if len(files) == 1:
            file = files[0]
            form.append({"name": "file", "value": file.fp, "filename": file.filename, "content_type": "application/octet-stream"})
        else:
            form.extend(
                {"name": f"file{index}", "value": file.fp, "filename": file.filename, "content_type": "application/octet-stream"}
                for index, file in enumerate(files)
            )

        return self.request(r, form=form, files=files)

    async def ack_message(self, channel_id, message_id):
        r = Route("POST", "/channels/{channel_id}/messages/{message_id}/ack", channel_id=channel_id, message_id=message_id)
        data = await self.request(r, json={"token": self._ack_token})
        self._ack_token = data["token"]

    def ack_guild(self, guild_id):
        return self.request(Route("POST", "/guilds/{guild_id}/ack", guild_id=guild_id))

    def delete_message(self, channel_id, message_id, *, reason=None):
        r = Route("DELETE", "/channels/{channel_id}/messages/{message_id}", channel_id=channel_id, message_id=message_id)
        return self.request(r, reason=reason)

    def delete_messages(self, channel_id, message_ids, *, reason=None):
        r = Route("POST", "/channels/{channel_id}/messages/bulk_delete", channel_id=channel_id)
        payload = {"messages": message_ids}

        return self.request(r, json=payload, reason=reason)

    def edit_message(self, channel_id, message_id, **fields):
        r = Route("PATCH", "/channels/{channel_id}/messages/{message_id}", channel_id=channel_id, message_id=message_id)
        return self.request(r, json=fields)

    def add_reaction(self, channel_id, message_id, emoji):
        r = Route(
            "PUT", "/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me", channel_id=channel_id, message_id=message_id, emoji=emoji
        )
        return self.request(r)

    def remove_reaction(self, channel_id, message_id, emoji, member_id):
        r = Route(
            "DELETE",
            "/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/{member_id}",
            channel_id=channel_id,
            message_id=message_id,
            member_id=member_id,
            emoji=emoji,
        )
        return self.request(r)

    def remove_own_reaction(self, channel_id, message_id, emoji):
        r = Route(
            "DELETE", "/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me", channel_id=channel_id, message_id=message_id, emoji=emoji
        )
        return self.request(r)

    def get_reaction_users(self, channel_id, message_id, emoji, limit, after=None):
        r = Route("GET", "/channels/{channel_id}/messages/{message_id}/reactions/{emoji}", channel_id=channel_id, message_id=message_id, emoji=emoji)

        params = {"limit": limit}
        if after:
            params["after"] = after
        return self.request(r, params=params)

    def clear_reactions(self, channel_id, message_id):
        r = Route("DELETE", "/channels/{channel_id}/messages/{message_id}/reactions", channel_id=channel_id, message_id=message_id)

        return self.request(r)

    def clear_single_reaction(self, channel_id, message_id, emoji):
        r = Route(
            "DELETE", "/channels/{channel_id}/messages/{message_id}/reactions/{emoji}", channel_id=channel_id, message_id=message_id, emoji=emoji
        )
        return self.request(r)

    def get_message(self, channel_id, message_id):
        r = Route("GET", "/channels/{channel_id}/messages/{message_id}", channel_id=channel_id, message_id=message_id)
        return self.request(r)

    def get_channel(self, channel_id):
        r = Route("GET", "/channels/{channel_id}", channel_id=channel_id)
        return self.request(r)

    def logs_from(self, channel_id, limit, before=None, after=None, around=None):
        params = {"limit": limit}

        if before is not None:
            params["before"] = before
        if after is not None:
            params["after"] = after
        if around is not None:
            params["around"] = around

        return self.request(Route("GET", "/channels/{channel_id}/messages", channel_id=channel_id), params=params)

    def publish_message(self, channel_id, message_id):
        return self.request(Route("POST", "/channels/{channel_id}/messages/{message_id}/crosspost", channel_id=channel_id, message_id=message_id))

    def pin_message(self, channel_id, message_id, reason=None):
        return self.request(Route("PUT", "/channels/{channel_id}/pins/{message_id}", channel_id=channel_id, message_id=message_id), reason=reason)

    def unpin_message(self, channel_id, message_id, reason=None):
        return self.request(Route("DELETE", "/channels/{channel_id}/pins/{message_id}", channel_id=channel_id, message_id=message_id), reason=reason)

    def pins_from(self, channel_id):
        return self.request(Route("GET", "/channels/{channel_id}/pins", channel_id=channel_id))

    # Member management

    def kick(self, user_id, guild_id, reason=None):
        r = Route("DELETE", "/guilds/{guild_id}/members/{user_id}", guild_id=guild_id, user_id=user_id)
        if reason:
            # thanks aiohttp
            r.url = f"{r.url}?reason={_uriquote(reason)}"

        return self.request(r)

    def ban(self, user_id, guild_id, delete_message_days=1, reason=None):
        r = Route("PUT", "/guilds/{guild_id}/bans/{user_id}", guild_id=guild_id, user_id=user_id)
        params = {"delete_message_days": delete_message_days}

        if reason:
            # thanks aiohttp
            r.url = f"{r.url}?reason={_uriquote(reason)}"

        return self.request(r, params=params)

    def unban(self, user_id, guild_id, *, reason=None):
        r = Route("DELETE", "/guilds/{guild_id}/bans/{user_id}", guild_id=guild_id, user_id=user_id)
        return self.request(r, reason=reason)

    def guild_voice_state(self, user_id, guild_id, *, mute=None, deafen=None, reason=None):
        r = Route("PATCH", "/guilds/{guild_id}/members/{user_id}", guild_id=guild_id, user_id=user_id)
        payload = {}
        if mute is not None:
            payload["mute"] = mute

        if deafen is not None:
            payload["deaf"] = deafen

        return self.request(r, json=payload, reason=reason)

    def edit_profile(self, password, username, avatar, **fields):
        payload = {"password": password, "username": username, "avatar": avatar}

        if "email" in fields:
            payload["email"] = fields["email"]

        if "new_password" in fields:
            payload["new_password"] = fields["new_password"]

        return self.request(Route("PATCH", "/users/@me"), json=payload)

    def change_my_nickname(self, guild_id, nickname, *, reason=None):
        r = Route("PATCH", "/guilds/{guild_id}/members/@me/nick", guild_id=guild_id)
        payload = {"nick": nickname}
        return self.request(r, json=payload, reason=reason)

    def change_nickname(self, guild_id, user_id, nickname, *, reason=None):
        r = Route("PATCH", "/guilds/{guild_id}/members/{user_id}", guild_id=guild_id, user_id=user_id)
        payload = {"nick": nickname}
        return self.request(r, json=payload, reason=reason)

    def edit_my_voice_state(self, guild_id, payload):
        r = Route("PATCH", "/guilds/{guild_id}/voice-states/@me", guild_id=guild_id)
        return self.request(r, json=payload)

    def edit_voice_state(self, guild_id, user_id, payload):
        r = Route("PATCH", "/guilds/{guild_id}/voice-states/{user_id}", guild_id=guild_id, user_id=user_id)
        return self.request(r, json=payload)

    def edit_member(self, guild_id, user_id, *, reason=None, **fields):
        r = Route("PATCH", "/guilds/{guild_id}/members/{user_id}", guild_id=guild_id, user_id=user_id)
        return self.request(r, json=fields, reason=reason)

    # Channel management

    def edit_channel(self, channel_id, *, reason=None, **options):
        r = Route("PATCH", "/channels/{channel_id}", channel_id=channel_id)
        valid_keys = (
            "name",
            "parent_id",
            "topic",
            "bitrate",
            "nsfw",
            "user_limit",
            "position",
            "permission_overwrites",
            "rate_limit_per_user",
            "type",
            "rtc_region",
        )
        payload = {k: v for k, v in options.items() if k in valid_keys}
        return self.request(r, reason=reason, json=payload)

    def bulk_channel_update(self, guild_id, data, *, reason=None):
        r = Route("PATCH", "/guilds/{guild_id}/channels", guild_id=guild_id)
        return self.request(r, json=data, reason=reason)

    def create_channel(self, guild_id, channel_type, *, reason=None, **options):
        valid_keys = (
            "name",
            "parent_id",
            "topic",
            "bitrate",
            "nsfw",
            "user_limit",
            "position",
            "permission_overwrites",
            "rate_limit_per_user",
            "rtc_region",
        )
        payload = {"type": channel_type} | {k: v for k, v in options.items() if k in valid_keys and v is not None}

        return self.request(Route("POST", "/guilds/{guild_id}/channels", guild_id=guild_id), json=payload, reason=reason)

    def delete_channel(self, channel_id, *, reason=None):
        return self.request(Route("DELETE", "/channels/{channel_id}", channel_id=channel_id), reason=reason)

    # Webhook management

    def create_webhook(self, channel_id, *, name, avatar=None, reason=None):
        payload = {"name": name}
        if avatar is not None:
            payload["avatar"] = avatar

        r = Route("POST", "/channels/{channel_id}/webhooks", channel_id=channel_id)
        return self.request(r, json=payload, reason=reason)

    def channel_webhooks(self, channel_id):
        return self.request(Route("GET", "/channels/{channel_id}/webhooks", channel_id=channel_id))

    def guild_webhooks(self, guild_id):
        return self.request(Route("GET", "/guilds/{guild_id}/webhooks", guild_id=guild_id))

    def get_webhook(self, webhook_id):
        return self.request(Route("GET", "/webhooks/{webhook_id}", webhook_id=webhook_id))

    def follow_webhook(self, channel_id, webhook_channel_id, reason=None):
        payload = {"webhook_channel_id": str(webhook_channel_id)}
        return self.request(Route("POST", "/channels/{channel_id}/followers", channel_id=channel_id), json=payload, reason=reason)

    # Guild management

    def get_guilds(self, limit, before=None, after=None):
        params = {"limit": limit}

        if before:
            params["before"] = before
        if after:
            params["after"] = after

        return self.request(Route("GET", "/users/@me/guilds"), params=params)

    def leave_guild(self, guild_id):
        return self.request(Route("DELETE", "/users/@me/guilds/{guild_id}", guild_id=guild_id))

    def get_guild(self, guild_id):
        return self.request(Route("GET", "/guilds/{guild_id}", guild_id=guild_id))

    def delete_guild(self, guild_id):
        return self.request(Route("DELETE", "/guilds/{guild_id}", guild_id=guild_id))

    def create_guild(self, name, region, icon):
        payload = {"name": name, "icon": icon, "region": region}

        return self.request(Route("POST", "/guilds"), json=payload)

    def edit_guild(self, guild_id, *, reason=None, **fields):
        valid_keys = (
            "name",
            "region",
            "icon",
            "afk_timeout",
            "owner_id",
            "afk_channel_id",
            "splash",
            "verification_level",
            "system_channel_id",
            "default_message_notifications",
            "description",
            "explicit_content_filter",
            "banner",
            "system_channel_flags",
            "rules_channel_id",
            "public_updates_channel_id",
            "preferred_locale",
        )

        payload = {k: v for k, v in fields.items() if k in valid_keys}

        return self.request(Route("PATCH", "/guilds/{guild_id}", guild_id=guild_id), json=payload, reason=reason)

    def get_template(self, code):
        return self.request(Route("GET", "/guilds/templates/{code}", code=code))

    def guild_templates(self, guild_id):
        return self.request(Route("GET", "/guilds/{guild_id}/templates", guild_id=guild_id))

    def create_template(self, guild_id, payload):
        return self.request(Route("POST", "/guilds/{guild_id}/templates", guild_id=guild_id), json=payload)

    def sync_template(self, guild_id, code):
        return self.request(Route("PUT", "/guilds/{guild_id}/templates/{code}", guild_id=guild_id, code=code))

    def edit_template(self, guild_id, code, payload):
        valid_keys = ("name", "description")
        payload = {k: v for k, v in payload.items() if k in valid_keys}
        return self.request(Route("PATCH", "/guilds/{guild_id}/templates/{code}", guild_id=guild_id, code=code), json=payload)

    def delete_template(self, guild_id, code):
        return self.request(Route("DELETE", "/guilds/{guild_id}/templates/{code}", guild_id=guild_id, code=code))

    def create_from_template(self, code, name, region, icon):
        payload = {"name": name, "icon": icon, "region": region}
        return self.request(Route("POST", "/guilds/templates/{code}", code=code), json=payload)

    def get_bans(self, guild_id):
        return self.request(Route("GET", "/guilds/{guild_id}/bans", guild_id=guild_id))

    def get_ban(self, user_id, guild_id):
        return self.request(Route("GET", "/guilds/{guild_id}/bans/{user_id}", guild_id=guild_id, user_id=user_id))

    def get_vanity_code(self, guild_id):
        return self.request(Route("GET", "/guilds/{guild_id}/vanity-url", guild_id=guild_id))

    def change_vanity_code(self, guild_id, code, *, reason=None):
        payload = {"code": code}
        return self.request(Route("PATCH", "/guilds/{guild_id}/vanity-url", guild_id=guild_id), json=payload, reason=reason)

    def get_all_guild_channels(self, guild_id):
        return self.request(Route("GET", "/guilds/{guild_id}/channels", guild_id=guild_id))

    def get_members(self, guild_id, limit, after):
        params = {"limit": limit}
        if after:
            params["after"] = after

        r = Route("GET", "/guilds/{guild_id}/members", guild_id=guild_id)
        return self.request(r, params=params)

    def get_member(self, guild_id, member_id):
        return self.request(Route("GET", "/guilds/{guild_id}/members/{member_id}", guild_id=guild_id, member_id=member_id))

    def prune_members(self, guild_id, days, compute_prune_count, roles, *, reason=None):
        payload = {"days": days, "compute_prune_count": "true" if compute_prune_count else "false"}
        if roles:
            payload["include_roles"] = ", ".join(roles)

        return self.request(Route("POST", "/guilds/{guild_id}/prune", guild_id=guild_id), json=payload, reason=reason)

    def estimate_pruned_members(self, guild_id, days, roles):
        params = {"days": days}
        if roles:
            params["include_roles"] = ", ".join(roles)

        return self.request(Route("GET", "/guilds/{guild_id}/prune", guild_id=guild_id), params=params)

    def get_all_custom_emojis(self, guild_id):
        return self.request(Route("GET", "/guilds/{guild_id}/emojis", guild_id=guild_id))

    def get_custom_emoji(self, guild_id, emoji_id):
        return self.request(Route("GET", "/guilds/{guild_id}/emojis/{emoji_id}", guild_id=guild_id, emoji_id=emoji_id))

    def create_custom_emoji(self, guild_id, name, image, *, roles=None, reason=None):
        payload = {"name": name, "image": image, "roles": roles or []}

        r = Route("POST", "/guilds/{guild_id}/emojis", guild_id=guild_id)
        return self.request(r, json=payload, reason=reason)

    def delete_custom_emoji(self, guild_id, emoji_id, *, reason=None):
        r = Route("DELETE", "/guilds/{guild_id}/emojis/{emoji_id}", guild_id=guild_id, emoji_id=emoji_id)
        return self.request(r, reason=reason)

    def edit_custom_emoji(self, guild_id, emoji_id, *, name, roles=None, reason=None):
        payload = {"name": name, "roles": roles or []}
        r = Route("PATCH", "/guilds/{guild_id}/emojis/{emoji_id}", guild_id=guild_id, emoji_id=emoji_id)
        return self.request(r, json=payload, reason=reason)

    def get_all_integrations(self, guild_id):
        r = Route("GET", "/guilds/{guild_id}/integrations", guild_id=guild_id)

        return self.request(r)

    def create_integration(self, guild_id, type, id):
        payload = {"type": type, "id": id}

        r = Route("POST", "/guilds/{guild_id}/integrations", guild_id=guild_id)
        return self.request(r, json=payload)

    def edit_integration(self, guild_id, integration_id, **payload):
        r = Route("PATCH", "/guilds/{guild_id}/integrations/{integration_id}", guild_id=guild_id, integration_id=integration_id)

        return self.request(r, json=payload)

    def sync_integration(self, guild_id, integration_id):
        r = Route("POST", "/guilds/{guild_id}/integrations/{integration_id}/sync", guild_id=guild_id, integration_id=integration_id)

        return self.request(r)

    def delete_integration(self, guild_id, integration_id):
        r = Route("DELETE", "/guilds/{guild_id}/integrations/{integration_id}", guild_id=guild_id, integration_id=integration_id)

        return self.request(r)

    def get_audit_logs(self, guild_id, limit=100, before=None, after=None, user_id=None, action_type=None):
        params = {"limit": limit}
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        if user_id:
            params["user_id"] = user_id
        if action_type:
            params["action_type"] = action_type

        r = Route("GET", "/guilds/{guild_id}/audit-logs", guild_id=guild_id)
        return self.request(r, params=params)

    def get_widget(self, guild_id):
        return self.request(Route("GET", "/guilds/{guild_id}/widget.json", guild_id=guild_id))

    # Invite management

    def create_invite(self, channel_id, *, reason=None, **options):
        r = Route("POST", "/channels/{channel_id}/invites", channel_id=channel_id)
        payload = {
            "max_age": options.get("max_age", 0),
            "max_uses": options.get("max_uses", 0),
            "temporary": options.get("temporary", False),
            "unique": options.get("unique", True),
        }

        return self.request(r, reason=reason, json=payload)

    def get_invite(self, invite_id, *, with_counts=True):
        params = {"with_counts": int(with_counts)}
        return self.request(Route("GET", "/invites/{invite_id}", invite_id=invite_id), params=params)

    def invites_from(self, guild_id):
        return self.request(Route("GET", "/guilds/{guild_id}/invites", guild_id=guild_id))

    def invites_from_channel(self, channel_id):
        return self.request(Route("GET", "/channels/{channel_id}/invites", channel_id=channel_id))

    def delete_invite(self, invite_id, *, reason=None):
        return self.request(Route("DELETE", "/invites/{invite_id}", invite_id=invite_id), reason=reason)

    # Role management

    def get_roles(self, guild_id):
        return self.request(Route("GET", "/guilds/{guild_id}/roles", guild_id=guild_id))

    def edit_role(self, guild_id, role_id, *, reason=None, **fields):
        r = Route("PATCH", "/guilds/{guild_id}/roles/{role_id}", guild_id=guild_id, role_id=role_id)
        valid_keys = ("name", "permissions", "color", "hoist", "mentionable")
        payload = {k: v for k, v in fields.items() if k in valid_keys}
        return self.request(r, json=payload, reason=reason)

    def delete_role(self, guild_id, role_id, *, reason=None):
        r = Route("DELETE", "/guilds/{guild_id}/roles/{role_id}", guild_id=guild_id, role_id=role_id)
        return self.request(r, reason=reason)

    def replace_roles(self, user_id, guild_id, role_ids, *, reason=None):
        return self.edit_member(guild_id=guild_id, user_id=user_id, roles=role_ids, reason=reason)

    def create_role(self, guild_id, *, reason=None, **fields):
        r = Route("POST", "/guilds/{guild_id}/roles", guild_id=guild_id)
        return self.request(r, json=fields, reason=reason)

    def move_role_position(self, guild_id, positions, *, reason=None):
        r = Route("PATCH", "/guilds/{guild_id}/roles", guild_id=guild_id)
        return self.request(r, json=positions, reason=reason)

    def add_role(self, guild_id, user_id, role_id, *, reason=None):
        r = Route("PUT", "/guilds/{guild_id}/members/{user_id}/roles/{role_id}", guild_id=guild_id, user_id=user_id, role_id=role_id)
        return self.request(r, reason=reason)

    def remove_role(self, guild_id, user_id, role_id, *, reason=None):
        r = Route("DELETE", "/guilds/{guild_id}/members/{user_id}/roles/{role_id}", guild_id=guild_id, user_id=user_id, role_id=role_id)
        return self.request(r, reason=reason)

    def edit_channel_permissions(self, channel_id, target, allow, deny, type, *, reason=None):
        payload = {"id": target, "allow": allow, "deny": deny, "type": type}
        r = Route("PUT", "/channels/{channel_id}/permissions/{target}", channel_id=channel_id, target=target)
        return self.request(r, json=payload, reason=reason)

    def delete_channel_permissions(self, channel_id, target, *, reason=None):
        r = Route("DELETE", "/channels/{channel_id}/permissions/{target}", channel_id=channel_id, target=target)
        return self.request(r, reason=reason)

    # Voice management

    def move_member(self, user_id, guild_id, channel_id, *, reason=None):
        return self.edit_member(guild_id=guild_id, user_id=user_id, channel_id=channel_id, reason=reason)

    # Relationship related

    def remove_relationship(self, user_id):
        r = Route("DELETE", "/users/@me/relationships/{user_id}", user_id=user_id)
        return self.request(r)

    def add_relationship(self, user_id, type=None):
        r = Route("PUT", "/users/@me/relationships/{user_id}", user_id=user_id)
        payload = {}
        if type is not None:
            payload["type"] = type

        return self.request(r, json=payload)

    def send_friend_request(self, username, discriminator):
        r = Route("POST", "/users/@me/relationships")
        payload = {"username": username, "discriminator": int(discriminator)}
        return self.request(r, json=payload)

    # Misc

    def application_info(self):
        return self.request(Route("GET", "/oauth2/applications/@me"))

    async def get_gateway(self, *, encoding="json", v=6, zlib=True):
        try:
            data = await self.request(Route("GET", "/gateway"))
        except HTTPException as exc:
            raise GatewayNotFound() from exc
        if zlib:
            value = "{0}?encoding={1}&v={2}&compress=zlib-stream"
        else:
            value = "{0}?encoding={1}&v={2}"
        return value.format(data["url"], encoding, v)

    async def get_bot_gateway(self, *, encoding="json", v=6, zlib=True):
        try:
            data = await self.request(Route("GET", "/gateway/bot"))
        except HTTPException as exc:
            raise GatewayNotFound() from exc

        if zlib:
            value = "{0}?encoding={1}&v={2}&compress=zlib-stream"
        else:
            value = "{0}?encoding={1}&v={2}"
        return data["shards"], value.format(data["url"], encoding, v)

    def get_user(self, user_id):
        return self.request(Route("GET", "/users/{user_id}", user_id=user_id))

    def get_user_profile(self, user_id):
        return self.request(Route("GET", "/users/{user_id}/profile", user_id=user_id))

    def get_mutual_friends(self, user_id):
        return self.request(Route("GET", "/users/{user_id}/relationships", user_id=user_id))

    def change_hypesquad_house(self, house_id):
        payload = {"house_id": house_id}
        return self.request(Route("POST", "/hypesquad/online"), json=payload)

    def leave_hypesquad_house(self):
        return self.request(Route("DELETE", "/hypesquad/online"))

    def edit_settings(self, **payload):
        return self.request(Route("PATCH", "/users/@me/settings"), json=payload)
