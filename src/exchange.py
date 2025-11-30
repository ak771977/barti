import hashlib
import hmac
import time
from typing import Any, Dict, Optional, List, Tuple

import requests


class BinanceAPIError(Exception):
    pass


class BinanceFuturesClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str, recv_window: int = 5000) -> None:
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.base_url = base_url.rstrip("/")
        self.recv_window = recv_window
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key})

    def _sign(self, params: Dict[str, Any]) -> List[Tuple[str, Any]]:
        params = dict(params)  # copy
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = self.recv_window
        items = sorted(params.items())
        query = "&".join([f"{k}={v}" for k, v in items])
        signature = hmac.new(self.api_secret, query.encode(), hashlib.sha256).hexdigest()
        items.append(("signature", signature))
        return items

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None, signed: bool = False) -> Any:
        params = params or {}
        signed_params = self._sign(params) if signed else params
        resp = self.session.get(f"{self.base_url}{path}", params=signed_params, timeout=10)
        if resp.status_code != 200:
            raise BinanceAPIError(f"GET {path} failed: {resp.status_code} {resp.text}")
        return resp.json()

    def _post(self, path: str, params: Optional[Dict[str, Any]] = None, signed: bool = True) -> Any:
        params = params or {}
        signed_params = self._sign(params) if signed else params
        resp = self.session.post(f"{self.base_url}{path}", params=signed_params, timeout=10)
        if resp.status_code != 200:
            raise BinanceAPIError(f"POST {path} failed: {resp.status_code} {resp.text}")
        return resp.json()

    def ping(self) -> bool:
        resp = self.session.get(f"{self.base_url}/fapi/v1/ping", timeout=5)
        return resp.status_code == 200

    def get_price(self, symbol: str) -> float:
        data = self._get("/fapi/v1/ticker/price", {"symbol": symbol})
        return float(data["price"])

    def get_klines(self, symbol: str, interval: str, limit: int = 150):
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        return self._get("/fapi/v1/klines", params)

    def get_position_info(self, symbol: str) -> Dict[str, float]:
        data = self._get("/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        if not data:
            return {"positionAmt": 0.0, "entryPrice": 0.0, "markPrice": 0.0, "unRealizedProfit": 0.0}
        pos = data[0]
        return {
            "positionAmt": float(pos.get("positionAmt", 0)),
            "entryPrice": float(pos.get("entryPrice", 0)),
            "markPrice": float(pos.get("markPrice", 0)),
            "unRealizedProfit": float(pos.get("unRealizedProfit", 0)),
        }

    def set_leverage(self, symbol: str, leverage: int) -> None:
        params = {"symbol": symbol, "leverage": leverage}
        self._post("/fapi/v1/leverage", params=params)

    def set_margin_mode(self, symbol: str, margin_mode: str) -> None:
        mode = margin_mode.upper()
        if mode == "CROSS":
            mode = "CROSSED"
        params = {"symbol": symbol, "marginType": mode}
        self._post("/fapi/v1/marginType", params=params)

    def get_account(self) -> Dict[str, Any]:
        return self._get("/fapi/v2/account", signed=True)

    def get_open_orders(self, symbol: str) -> Any:
        params = {"symbol": symbol}
        return self._get("/fapi/v1/openOrders", params=params, signed=True)

    def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        reduce_only: bool = False,
        position_side: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
            "reduceOnly": "true" if reduce_only else "false",
        }
        if position_side:
            params["positionSide"] = position_side
        return self._post("/fapi/v1/order", params=params)

    def place_limit_tp(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        position_side: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "price": f"{price:.4f}",
            "quantity": quantity,
            "reduceOnly": "true",
        }
        if position_side:
            params["positionSide"] = position_side
        return self._post("/fapi/v1/order", params=params)
