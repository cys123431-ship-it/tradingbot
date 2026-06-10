import asyncio
import httpx
from typing import Optional, Dict, Any, List
from .settings import Settings
from .logger import get_logger
from .kis_auth import KisAuth

logger = get_logger("kis_client")

class KisApiError(Exception):
    def __init__(self, path: str, rt_cd: str, msg_cd: str, msg1: str, response: dict):
        self.path = path
        self.rt_cd = rt_cd
        self.msg_cd = msg_cd
        self.msg1 = msg1
        self.response = response
        super().__init__(f"KIS API Error [{rt_cd}] in {path}: {msg1} ({msg_cd})")

class RateLimiter:
    def __init__(self, calls: int, period: float):
        self.calls = calls
        self.period = period
        self.timestamps = []
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            now = asyncio.get_event_loop().time()
            self.timestamps = [t for t in self.timestamps if now - t < self.period]
            if len(self.timestamps) >= self.calls:
                sleep_time = self.period - (now - self.timestamps[0])
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
            self.timestamps.append(asyncio.get_event_loop().time())

class KisClient:
    def __init__(self):
        self.auth = KisAuth()
        self.account_no = Settings.KIS_ACCOUNT_NO
        self.account_code = Settings.KIS_ACCOUNT_PRODUCT_CODE
        self.base_url = self.auth.base_url
        if Settings.KIS_IS_MOCK:
            self.rate_limiter = RateLimiter(calls=2, period=1.0)
        else:
            self.rate_limiter = RateLimiter(calls=5, period=1.0)
        self.max_retries = 3

    # TODO(next): Replace per-request AsyncClient with a shared lifespan-managed client
    # after KIS paper trading dry run is stable. Keep current short-lived client for MVP simplicity.
    async def _request(self, method: str, path: str, headers: dict, params: dict = None, json_data: dict = None, require_rt_cd_0: bool = False) -> dict:
        url = f"{self.base_url}{path}"
        
        logger.debug(f"KIS request {method} {path} params_keys={list((params or {}).keys())} json_keys={list((json_data or {}).keys())}")
        
        for attempt in range(self.max_retries):
            await self.rate_limiter.wait()
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                    response = await client.request(method, url, headers=headers, params=params, json=json_data)
                    response.raise_for_status()
                    
                    data = response.json()
                    return self._validate_kis_response(path, data, require_rt_cd_0)
            except httpx.HTTPError as e:
                logger.warning(f"Attempt {attempt + 1}/{self.max_retries} failed for {path}: {e}")
                if attempt == self.max_retries - 1:
                    logger.error(f"Max retries reached for {path}")
                    raise
                await asyncio.sleep(1.0 * (attempt + 1))
            except KisApiError:
                raise # Pass through logical KIS errors without retrying network
            except Exception as e:
                logger.error(f"Unexpected error in {path}: {e}")
                raise
        return {}

    def _validate_kis_response(self, path: str, data: dict, require_rt_cd_0: bool) -> dict:
        has_rt_cd = "rt_cd" in data
        rt_cd = str(data.get("rt_cd", ""))
        msg_cd = data.get("msg_cd", "")
        msg1 = data.get("msg1", "")

        if require_rt_cd_0:
            if not has_rt_cd:
                logger.error(f"API {path} failed: missing rt_cd")
                raise KisApiError(path, "MISSING", msg_cd, "Missing rt_cd in KIS response", data)
            if rt_cd != "0":
                logger.error(f"API {path} failed: {msg1} ({msg_cd})")
                raise KisApiError(path, rt_cd, msg_cd, msg1, data)

        if has_rt_cd and rt_cd == "0":
            logger.debug(f"API {path} success.")
        elif has_rt_cd:
            logger.warning(f"API {path} returned non-zero rt_cd: {msg1} ({msg_cd})")
        else:
            logger.debug(f"API {path} response has no rt_cd; accepted because require_rt_cd_0=False")

        return data

    async def get_hashkey(self, payload: dict) -> str:
        headers = {
            "Content-Type": "application/json",
            "appkey": Settings.KIS_APP_KEY,
            "appsecret": Settings.KIS_APP_SECRET
        }
        res = await self._request("POST", "/uapi/hashkey", headers, json_data=payload)
        hashkey = res.get("HASH") or res.get("hash")
        if not hashkey:
            raise RuntimeError("Failed to obtain hashkey")
        return hashkey

    async def _get_base_headers(self, tr_id: str) -> dict:
        token = await self.auth.get_access_token()
        return {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": Settings.KIS_APP_KEY,
            "appsecret": Settings.KIS_APP_SECRET,
            "tr_id": tr_id
        }

    async def get_price(self, symbol: str) -> dict:
        headers = await self._get_base_headers("FHKST01010100")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol
        }
        res = await self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            headers,
            params=params,
            require_rt_cd_0=True,
        )
        return res

    async def get_balance(self) -> dict:
        headers = await self._get_base_headers("VTTC8434R" if Settings.KIS_IS_MOCK else "TTTC8434R")
        params = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_code,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": ""
        }
        res = await self._request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            headers,
            params=params,
            require_rt_cd_0=True,
        )
        return res

    async def get_buyable_cash(self, symbol: str = "", price: int = 0) -> dict:
        headers = await self._get_base_headers("VTTC8908R" if Settings.KIS_IS_MOCK else "TTTC8908R")
        safe_symbol = symbol if symbol else "005930"
        safe_price = price if price > 0 else 1

        params = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_code,
            "PDNO": safe_symbol,
            "ORD_UNPR": str(safe_price),
            "ORD_DVSN": "00",
            "CMA_EVLU_AMT_ICLD_YN": "N",
            "OVRS_ICLD_YN": "N"
        }
        res = await self._request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            headers,
            params=params,
            require_rt_cd_0=True,
        )
        return res

    async def place_order(self, side: str, symbol: str, qty: int, price: int, order_type: str = "limit") -> dict:
        if order_type == "market":
            raise RuntimeError("Market orders are forbidden in MVP.")
            
        if side.upper() == "BUY":
            tr_id = "VTTC0802U" if Settings.KIS_IS_MOCK else "TTTC0802U"
        elif side.upper() == "SELL":
            tr_id = "VTTC0801U" if Settings.KIS_IS_MOCK else "TTTC0801U"
        else:
            raise ValueError(f"Invalid side: {side}")
            
        ord_dvsn = "00"

        headers = await self._get_base_headers(tr_id)
        payload = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_code,
            "PDNO": symbol,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price)
        }
        
        # Attach hashkey
        hashkey = await self.get_hashkey(payload)
        headers["hashkey"] = hashkey
        
        res = await self._request("POST", "/uapi/domestic-stock/v1/trading/order-cash", headers, json_data=payload, require_rt_cd_0=True)
        return res

    async def get_open_orders(self) -> list:
        headers = await self._get_base_headers("VTTC8036R" if Settings.KIS_IS_MOCK else "TTTC8036R")
        params = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_code,
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
            "INQR_DVSN_1": "0",
            "INQR_DVSN_2": "0"
        }
        res = await self._request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
            headers,
            params=params,
            require_rt_cd_0=True,
        )
        if not res: return []
        # Return output or output1 based on whatever key is present
        return res.get("output", res.get("output1", []))

    async def cancel_order(self, order_id: str, org_no: str = "") -> dict:
        headers = await self._get_base_headers("VTTC0803U" if Settings.KIS_IS_MOCK else "TTTC0803U")
        payload = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_code,
            "KRX_FWDG_ORD_ORGNO": org_no, 
            "ORGN_ODNO": order_id,
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02", # 02: 취소
            "ORD_QTY": "0", # 전량 취소
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y"
        }
        hashkey = await self.get_hashkey(payload)
        headers["hashkey"] = hashkey
        
        res = await self._request("POST", "/uapi/domestic-stock/v1/trading/order-rvsecncl", headers, json_data=payload, require_rt_cd_0=True)
        return res

    async def aclose(self):
        """Future hook for shared AsyncClient cleanup.

        MVP currently creates short-lived clients per request.
        This method exists so FastAPI lifespan can safely call it later
        without requiring a large refactor.
        """
        return None

kis_client = KisClient()
