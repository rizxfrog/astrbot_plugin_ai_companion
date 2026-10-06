"""OneBot v11 客户端测试桩。

把自己伪装成一个 OneBot 实现端，反向连到 AstrBot 的 ws_reverse_port，
发真实事件、接收 bot 的真实 API 调用（send_msg 等）。

用于对 AI Companion 插件做**真实平台**端到端联调：不 mock 平台，走真实链路。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class SentMessage:
    """记录一次 bot 发出的消息。"""

    raw: dict
    at: float = field(default_factory=time.time)

    @property
    def segments(self) -> list[dict]:
        msg = self.raw.get("message")
        if isinstance(msg, list):
            return msg
        return [{"type": "text", "data": {"text": str(msg or "")}}]

    @property
    def text(self) -> str:
        return "".join(
            s.get("data", {}).get("text", "")
            for s in self.segments
            if s.get("type") == "text"
        )

    @property
    def images(self) -> list[str]:
        return [
            s.get("data", {}).get("file", "")
            for s in self.segments
            if s.get("type") == "image"
        ]


class OneBotStub:
    """一个最小可用的 OneBot v11 实现端。"""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        self_id: str = "10000",
        access_token: str = "",
        path: str = "/ws",
    ) -> None:
        self.url = f"ws://{host}:{port}{path}"
        self.self_id = self_id
        self.access_token = access_token
        self.sent: list[SentMessage] = []
        self._ws = None
        self._reader_task: asyncio.Task | None = None
        self._connected = asyncio.Event()

    # ------------------------------------------------------------------
    async def connect(self, timeout: float = 10.0) -> None:
        import websockets

        headers = {"X-Self-ID": self.self_id, "X-Client-Role": "Universal"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        self._ws = await websockets.connect(
            self.url, additional_headers=headers, max_size=None
        )
        self._connected.set()
        self._reader_task = asyncio.create_task(self._reader())

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws is not None:
            await self._ws.close()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # ------------------------------------------------------------------
    async def _reader(self) -> None:
        """接收 bot 的 API 调用并回一个成功响应。"""
        try:
            async for raw in self._ws:
                try:
                    payload = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if payload.get("action"):
                    self._record(payload)
                    await self._respond(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def _record(self, payload: dict) -> None:
        action = payload.get("action")
        params = payload.get("params") or {}
        if action in ("send_msg", "send_group_msg", "send_private_msg"):
            self.sent.append(SentMessage(raw=params))

    async def _respond(self, payload: dict) -> None:
        echo = payload.get("echo")
        if echo is None:
            return
        try:
            await self._ws.send(
                json.dumps({"status": "ok", "retcode": 0, "data": {}, "echo": echo})
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    async def send_event(self, payload: dict) -> None:
        """向 AstrBot 投递一个事件。"""
        await self._ws.send(json.dumps(payload))

    async def send_group_message(
        self,
        text: str,
        *,
        group_id: str = "123456",
        user_id: str = "20001",
        nickname: str = "测试用户",
        at_bot: bool = False,
    ) -> None:
        message: list[dict] = []
        if at_bot:
            message.append({"type": "at", "data": {"qq": self.self_id}})
        message.append({"type": "text", "data": {"text": text}})
        await self.send_event(
            {
                "post_type": "message",
                "message_type": "group",
                "sub_type": "normal",
                "message_id": int(time.time() * 1000) % 2_000_000_000,
                "group_id": int(group_id),
                "user_id": int(user_id),
                "message": message,
                "raw_message": text,
                "font": 0,
                "self_id": int(self.self_id),
                "time": int(time.time()),
                "sender": {
                    "user_id": int(user_id),
                    "nickname": nickname,
                    "card": nickname,
                    "role": "member",
                },
            }
        )

    async def send_private_message(
        self,
        text: str,
        *,
        user_id: str = "20001",
        nickname: str = "测试用户",
        image_b64: str = "",
        image_file: str = "",
    ) -> None:
        message: list[dict] = []
        if image_b64:
            message.append(
                {"type": "image", "data": {"file": f"base64://{image_b64}"}}
            )
        elif image_file:
            message.append({"type": "image", "data": {"file": image_file}})
        message.append({"type": "text", "data": {"text": text}})
        await self.send_event(
            {
                "post_type": "message",
                "message_type": "private",
                "sub_type": "friend",
                "message_id": int(time.time() * 1000) % 2_000_000_000,
                "user_id": int(user_id),
                "message": message,
                "raw_message": text,
                "font": 0,
                "self_id": int(self.self_id),
                "time": int(time.time()),
                "sender": {
                    "user_id": int(user_id),
                    "nickname": nickname,
                    "card": nickname,
                    "role": "member",
                },
            }
        )

    # ------------------------------------------------------------------
    def clear(self) -> None:
        self.sent.clear()

    async def wait_for_message(self, timeout: float = 60.0) -> SentMessage | None:
        """等待 bot 发出下一条消息。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.sent:
                return self.sent[-1]
            await asyncio.sleep(0.2)
        return None
