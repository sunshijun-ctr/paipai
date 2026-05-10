import logging
import time
from typing import Any, Optional

import httpx

from app.channels.qq.qq_config import QQConfig

logger = logging.getLogger(__name__)


class QQClient:
    def __init__(self, config: QQConfig) -> None:
        config.require_credentials()
        self.config = config
        self._access_token = ""
        self._access_token_expires_at = 0.0

    async def _headers(self) -> dict[str, str]:
        token = await self._get_access_token()
        return {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": str(self.config.bot_app_id or ""),
            "Content-Type": "application/json",
        }

    async def send_private_message(
        self,
        openid: str,
        content: str,
        msg_id: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return await self._post(
            f"/v2/users/{openid}/messages",
            self._payload(content, msg_id, event_id),
        )

    async def send_group_message(
        self,
        group_openid: str,
        content: str,
        msg_id: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return await self._post(
            f"/v2/groups/{group_openid}/messages",
            self._payload(content, msg_id, event_id),
        )

    async def send_channel_message(
        self,
        channel_id: str,
        content: str,
        msg_id: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return await self._post(
            f"/channels/{channel_id}/messages",
            self._payload(content, msg_id, event_id),
        )

    def _payload(
        self,
        content: str,
        msg_id: Optional[str],
        event_id: Optional[str],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "content": content,
            "msg_type": 0,
        }
        if msg_id:
            payload["msg_id"] = msg_id
        if event_id:
            payload["event_id"] = event_id
        return payload

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.config.resolved_api_base_url.rstrip("/") + path
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(url, headers=await self._headers(), json=payload)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError:
                logger.error(
                    "QQ API send failed status=%s url=%s body=%s",
                    response.status_code,
                    url,
                    response.text[:1000],
                )
                raise
            if not response.content:
                return {}
            return response.json()

    async def _get_access_token(self) -> str:
        now = time.time()
        if self._access_token and now < self._access_token_expires_at:
            return self._access_token

        url = "https://bots.qq.com/app/getAppAccessToken"
        payload = {
            "appId": str(self.config.bot_app_id or ""),
            "clientSecret": str(self.config.bot_secret or ""),
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                url,
                headers={"Content-Type": "application/json"},
                json=payload,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError:
                logger.error(
                    "QQ access token request failed status=%s body=%s",
                    response.status_code,
                    response.text[:1000],
                )
                raise
            data = response.json()

        token = data.get("access_token") or data.get("accessToken")
        if not token:
            raise RuntimeError(f"QQ access token response missing token: {data}")
        expires_in = int(data.get("expires_in") or data.get("expiresIn") or 7200)
        self._access_token = str(token)
        self._access_token_expires_at = now + max(expires_in - 60, 60)
        return self._access_token
