import asyncio
import httpx
import time
from typing import Optional
from .settings import Settings
from .logger import get_logger

logger = get_logger("kis_auth")

class KisAuth:
    def __init__(self):
        self.app_key = Settings.KIS_APP_KEY
        self.app_secret = Settings.KIS_APP_SECRET
        self.is_mock = Settings.KIS_IS_MOCK
        
        self.base_url = "https://openapivts.koreainvestment.com:29443" if self.is_mock else "https://openapi.koreainvestment.com:9443"
        
        self._access_token: Optional[str] = None
        self._token_expired_at: float = 0
        self._lock = asyncio.Lock()

    async def get_access_token(self) -> str:
        async with self._lock:
            # Check if token is valid (with 5 min buffer)
            if self._access_token and time.time() < self._token_expired_at - 300:
                return self._access_token
                
            token = await self._fetch_new_token()
            if not token:
                raise RuntimeError("Failed to obtain KIS access token")
            return token

    async def ensure_token_valid(self) -> str:
        """Ensure an access token exists and is still valid.

        APScheduler token refresh job calls this method.
        It delegates to get_access_token(), which already handles locking,
        expiration buffer, and token refresh.
        """
        return await self.get_access_token()

    async def _fetch_new_token(self) -> str:
        if not self.app_key or not self.app_secret:
            logger.error("KIS APP_KEY or APP_SECRET is missing.")
            raise ValueError("KIS Credentials Missing")

        url = f"{self.base_url}/oauth2/tokenP"
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret
        }
        
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
                
                self._access_token = data.get("access_token")
                expires_in = data.get("expires_in", 86400)
                self._token_expired_at = time.time() + int(expires_in)
                
                if self._access_token:
                    logger.info(f"Successfully fetched new KIS token. length={len(self._access_token)}")
                else:
                    logger.error("Token response did not contain access_token")
                    
                return self._access_token
        except httpx.HTTPError as e:
            logger.error(f"HTTP Error fetching KIS token: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected Error fetching KIS token: {e}")
            raise
