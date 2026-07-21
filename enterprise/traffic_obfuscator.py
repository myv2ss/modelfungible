# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
TrafficObfuscator — makes Rita gateway traffic look like organic human usage.

Problem: LLM providers detect gateways via:
  - Shared API keys (one key = many users)
  - Python/automation User-Agents
  - Fixed timing intervals (cron-like)
  - Same model every request
  - Cloud provider IPs
  - Proxy headers (X-Forwarded-For, Via)

Solution: TrafficObfuscator adds human-like variance:
  1. Per-user API keys (each user has their own key)
  2. Real browser User-Agent rotation
  3. Human timing intervals (Poisson delays)
  4. Model variance
  5. Proxy header stripping
  6. Residential proxy rotation (optional)

Usage:
    obf = TrafficObfuscator(
        upstream_api_key="sk-...",    # Individual user's real API key
        provider="openai",
        proxies=["http://residential-proxy:8080"],  # optional
    )
    headers = obf.get_request_headers()
    delay = obf.get_user_delay(user_id)
    varied_model = obf.vary_model("gpt-4o")
"""

from __future__ import annotations

import random, time, hashlib, threading, logging
from typing import Optional, Any
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── Real browser User-Agents ──────────────────────────────────────────────────
BROWSER_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
]

# Headers that scream "proxy/gateway" — never add these
SENSITIVE_HEADERS = {
    "Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto",
    "X-Real-IP", "Via", "Proxy-Authorization", "Proxy-Authenticate",
    "X-Proxy-ID", "X-MS-DCS", "X-Azure-ClientIP", "True-Client-IP", "CF-Connecting-IP",
}

AUTO_UA_PATTERNS = [
    "python-requests", "PostmanRuntime", "axios", "node-fetch",
    "Apache-HttpClient", "java/", "Go-http-client", "curl/",
]


# ── Human-like timing ─────────────────────────────────────────────────────────

def human_jitter(base_ms: float, variance_pct: float = 0.3) -> float:
    """Exponential jitter — humans don't act at fixed intervals."""
    jitter = random.expovariate(1.0 / (base_ms * variance_pct))
    return max(50, base_ms + jitter)


def poisson_delay(min_ms: int = 300, max_ms: int = 3000) -> float:
    """Poisson-distributed delay — models human task arrival."""
    lam = (min_ms + max_ms) / 2
    delay = random.expovariate(1.0 / lam)
    return max(min_ms, min(max_ms, delay))


def burst_then_pause(consecutive: int) -> float:
    """Humans make 2-4 rapid requests, then pause to think."""
    if consecutive <= 2:
        return human_jitter(200, 0.5)
    elif consecutive <= 5:
        return human_jitter(1500, 0.4)
    return poisson_delay(3000, 8000)


# ── Per-user traffic profile ─────────────────────────────────────────────────

@dataclass
class UserProfile:
    user_id: str
    api_key_hash: str
    last_request_time: float = 0.0
    request_count: int = 0
    model_usage: dict[str, int] = field(default_factory=dict)
    consecutive_requests: int = 0
    total_tokens: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, model: str, tokens: int) -> None:
        with self._lock:
            self.request_count += 1
            self.consecutive_requests += 1
            self.total_tokens += tokens
            self.last_request_time = time.time()
            self.model_usage[model] = self.model_usage.get(model, 0) + 1

    def get_delay(self) -> float:
        return burst_then_pause(self.consecutive_requests)

    def reset_burst(self) -> None:
        with self._lock:
            self.consecutive_requests = 0


# ── TrafficObfuscator ────────────────────────────────────────────────────────

class TrafficObfuscator:
    """
    Makes gateway traffic look like organic human usage.

    Usage:
        obf = TrafficObfuscator(
            upstream_api_key="sk-...",    # Individual user's real key
            provider="openai",
            proxies=["http://proxy:port"],  # optional
        )
        headers = obf.get_request_headers()
        delay = obf.get_user_delay(user_id)
        model = obf.vary_model("gpt-4o")
    """

    def __init__(
        self,
        upstream_api_key: str,
        provider: str = "openai",
        proxies: Optional[list[str]] = None,
        user_id: Optional[str] = None,
        min_delay_ms: int = 300,
        max_delay_ms: int = 3000,
        model_variance: bool = True,
        header_stripping: bool = True,
        user_agent_rotation: bool = True,
    ):
        self.upstream_key = upstream_api_key
        self.provider = provider.lower()
        self.proxies = proxies or []
        self.user_id = user_id or hashlib.sha256(self.upstream_key.encode()).hexdigest()[:12]
        self.min_delay_ms = min_delay_ms
        self.max_delay_ms = max_delay_ms
        self.model_variance = model_variance
        self.header_stripping = header_stripping
        self.user_agent_rotation = user_agent_rotation

        self._profiles: dict[str, UserProfile] = {}
        self._profiles_lock = threading.Lock()
        self._proxy_index = 0
        self._current_ua: Optional[str] = None
        self._ua_start = time.time()

        self._model_aliases = {
            "gpt-4o": ["gpt-4o", "gpt-4o-mini", "chatgpt-4o-latest"],
            "gpt-4o-mini": ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo"],
            "claude-3-5-sonnet": ["claude-3-5-sonnet-20250514", "claude-3-5-sonnet-4-20250514"],
            "claude-3-opus": ["claude-3-opus-20240229", "claude-3-5-sonnet-20250514"],
        }

    # ── User profiles ───────────────────────────────────────────────────────

    def _profile(self, user_id: str) -> UserProfile:
        with self._profiles_lock:
            if user_id not in self._profiles:
                self._profiles[user_id] = UserProfile(
                    user_id=user_id,
                    api_key_hash=hashlib.sha256(self.upstream_key.encode()).hexdigest()[:8],
                )
            return self._profiles[user_id]

    def get_user_delay(self, user_id: Optional[str] = None) -> float:
        """Human-like delay in seconds before next request."""
        uid = user_id or self.user_id
        delay = self._profile(uid).get_delay()
        clamped = max(self.min_delay_ms, min(self.max_delay_ms, delay))
        return clamped / 1000.0

    def record_request(self, user_id: Optional[str], model: str, tokens: int) -> None:
        """Record completed request for rate tracking."""
        uid = user_id or self.user_id
        self._profile(uid).record(model, tokens)

    # ── Headers ─────────────────────────────────────────────────────────────

    def get_request_headers(self, extra_ua: Optional[str] = None) -> dict[str, str]:
        """
        Get browser-like headers. Use these — never add X-Forwarded-For, Via, etc.
        """
        ua = extra_ua or self._get_user_agent()
        return {
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": random.choice([
                "en-US,en;q=0.9", "en-GB,en;q=0.9", "en;q=0.8",
                "en-US,en;q=0.9,es;q=0.8", "en;q=0.9,fr;q=0.8",
            ]),
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Sec-Ch-Ua": self._sec_ch_ua(),
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": f'"{random.choice(["Windows", "macOS", "Linux", "Android"])}"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Origin": random.choice([
                "https://app.example.com",
                "https://chat.example.ai",
                "https://studio.example.com",
            ]),
            "Referer": random.choice([
                "https://app.example.com/dashboard",
                "https://app.example.com/chat/sessions",
                "https://studio.example.com/",
            ]),
        }

    def _sec_ch_ua(self) -> str:
        versions = [str(random.randint(120, 130)) for _ in range(3)]
        brands = random.choice([
            ['"Chromium"', '"Not.A?Brand"', '"Google Chrome"'],
            ['"Chromium"', '"Not.A?Brand"', '"Microsoft Edge"'],
            ['"Firefox"', '"Not.A?Brand"', '"Safari"'],
        ])
        return ",".join(f"{b};v=\"{v}\"" for b, v in zip(brands, versions))

    def _get_user_agent(self) -> str:
        if not self.user_agent_rotation:
            return BROWSER_USER_AGENTS[0]
        age = time.time() - self._ua_start
        if age > random.randint(600, 1800) or self._current_ua is None:
            self._current_ua = random.choice(BROWSER_USER_AGENTS)
            self._ua_start = time.time()
        return self._current_ua

    def pick_user_agent(self) -> str:
        """Force rotate to a new browser User-Agent."""
        self._current_ua = random.choice(BROWSER_USER_AGENTS)
        self._ua_start = time.time()
        return self._current_ua

    def strip_gateway_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """Remove all proxy/gateway-indicating headers."""
        if not self.header_stripping:
            return headers
        return {k: v for k, v in headers.items() if k not in SENSITIVE_HEADERS}

    def is_safe_user_agent(self, ua: str) -> bool:
        """Check if User-Agent looks automated."""
        return not any(p in ua for p in AUTO_UA_PATTERNS)

    # ── Proxy management ───────────────────────────────────────────────────

    def get_proxy(self) -> Optional[str]:
        """
        Get next residential proxy. Use ONLY residential proxies.
        Cloud proxies (AWS/GCP/DigitalOcean) = immediately flagged as gateway.
        """
        if not self.proxies:
            return None
        proxy = self.proxies[self._proxy_index]
        self._proxy_index = (self._proxy_index + 1) % len(self.proxies)
        return proxy

    def add_proxy(self, proxy: str) -> None:
        if proxy not in self.proxies:
            self.proxies.append(proxy)

    def remove_proxy(self, proxy: str) -> None:
        if proxy in self.proxies:
            self.proxies.remove(proxy)

    # ── Model variance ─────────────────────────────────────────────────────

    def vary_model(self, model: str) -> str:
        """
        Occasionally switch model — humans don't always use the exact same model.
        20% chance to switch to an equivalent.
        """
        if not self.model_variance:
            return model
        if random.random() < 0.2:
            aliases = self._model_aliases.get(model, [])
            if len(aliases) > 1:
                chosen = random.choice(aliases)
                logger.debug(f"Model variance: {model} → {chosen}")
                return chosen
        return model

    def get_model_for_user(self, user_id: str, base_model: str) -> str:
        """
        Per-user model preference — users tend to stick with one model.
        80% chance they get their preferred model.
        """
        profile = self._profile(user_id)
        with profile._lock:
            if profile.model_usage and random.random() < 0.8:
                return max(profile.model_usage, key=profile.model_usage.get)
        return self.vary_model(base_model)

    # ── Full request obfuscation ───────────────────────────────────────────

    def apply(self, request_kwargs: dict, user_id: Optional[str] = None) -> dict:
        """
        Apply full obfuscation to a request dict.
        Call before sending to provider.

        Example:
            import httpx
            client = httpx.Client()
            kwargs = {"model": "gpt-4o", "messages": [{"role":"user","content":"hi"}]}
            kwargs = obf.apply(kwargs)
            r = client.post(url, json=kwargs, headers=obf.get_request_headers())
        """
        uid = user_id or self.user_id

        # 1. Apply model variance
        if "model" in request_kwargs:
            request_kwargs["model"] = self.get_model_for_user(uid, request_kwargs["model"])

        # 2. Strip gateway headers from any existing headers
        if "headers" in request_kwargs:
            request_kwargs["headers"] = self.strip_gateway_headers(request_kwargs["headers"])

        # 3. Add browser headers
        request_kwargs["headers"] = {
            **self.get_request_headers(),
            **(request_kwargs.get("headers", {}))
        }

        # 4. Apply proxy if configured
        proxy = self.get_proxy()
        if proxy:
            request_kwargs["proxies"] = proxy

        return request_kwargs

    def wait_before_request(self, user_id: Optional[str] = None) -> float:
        """
        Call before making a request. Sleeps for human-like delay.
        Returns actual time slept.
        """
        delay = self.get_user_delay(user_id)
        time.sleep(delay)
        return delay
