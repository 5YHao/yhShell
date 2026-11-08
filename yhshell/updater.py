"""Private, best-effort GitHub Releases checks; never downloads or installs code.

``lastSuccessAt`` is a Unix timestamp (or None). ``state()`` is a defensive,
network-free snapshot. Skipped/busy results retain any previously known release.
The separate cache contains only public release metadata and timing/ETag data.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import threading
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urlsplit

import requests

from . import __version__

DEFAULT_REPO = "5YHao/yhShell"
CACHE_FILENAME = "update-cache.json"
SUCCESS_INTERVAL = 24 * 60 * 60
FAILURE_INTERVAL = 60 * 60
_VERSION = re.compile(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", re.ASCII)
_REPO = re.compile(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", re.ASCII)


def _version(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    match = _VERSION.fullmatch(value)
    return tuple(int(part) for part in match.groups()) if match else None


def _valid_repo(repo: object) -> bool:
    return isinstance(repo, str) and bool(_REPO.fullmatch(repo)) and repo.split("/")[1] not in (".", "..")


def validate_release_url(url: object, repo: str = DEFAULT_REPO) -> bool:
    """True only for credential/port-free HTTPS GitHub tag pages in *repo*.

    Query strings, fragments, whitespace, backslashes and traversal segments are
    rejected; this function does not resolve URLs or make network requests.
    """
    if not _valid_repo(repo) or not isinstance(url, str) or len(url) > 2048:
        return False
    if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
        return False
    try:
        parsed = urlsplit(url)
        prefix = f"/{repo}/releases/tag/"
        if (parsed.scheme != "https" or parsed.netloc != "github.com"
                or parsed.query or parsed.fragment or not parsed.path.startswith(prefix)):
            return False
        tag = unquote(parsed.path[len(prefix):])
        return bool(tag) and "\\" not in tag and not any(
            segment in ("", ".", "..") for segment in tag.split("/")
        ) and not any(ord(char) <= 32 or ord(char) == 127 for char in tag)
    except (ValueError, UnicodeError):
        return False


def _timestamp(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (OverflowError, ValueError):
        return None


def _etag(value: object) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= 512 and all(32 <= ord(c) < 127 for c in value):
        return value
    return None


def _release(value: object, repo: str, api: bool = False) -> dict | None:
    if not isinstance(value, dict):
        return None
    version = value.get("tag_name" if api else "version")
    url = value.get("html_url" if api else "url")
    if _version(version) is None or not validate_release_url(url, repo):
        return None
    if api and (value.get("draft") is not False or value.get("prerelease") is not False):
        return None
    notes = value.get("body" if api else "notes")
    published = value.get("published_at" if api else "publishedAt")
    if notes is not None and not isinstance(notes, str):
        return None
    if published is not None and not isinstance(published, str):
        return None
    return {"version": version.removeprefix("v"), "url": url,
            "notes": (notes or "")[:20000], "publishedAt": (published or "")[:80]}


class Updater:
    def __init__(self, directory: str, current_version: str = __version__, repo: str = DEFAULT_REPO) -> None:
        if _version(current_version) is None:
            raise ValueError("Current version must be a stable X.Y.Z version")
        if not _valid_repo(repo):
            raise ValueError("Invalid GitHub repository")
        self.directory = os.fspath(directory)
        self.path = os.path.join(self.directory, CACHE_FILENAME)
        self.current_version = current_version.removeprefix("v")
        self.repo = repo
        self._endpoint = f"https://api.github.com/repos/{repo}/releases/latest"
        self._check_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._cache = self._load()
        self._state = self._result(self._known_status(), True)

    def _load(self) -> dict:
        clean = {"repo": self.repo, "release": None, "noRelease": False, "etag": None,
                 "lastSuccessAt": None, "lastFailureAt": None, "retryAfterUntil": None}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                # Avoid loading an arbitrarily large corrupt/foreign cache.
                raw = json.loads(handle.read(131073))
            if not isinstance(raw, dict) or raw.get("repo") != self.repo:
                return clean
            clean["release"] = _release(raw.get("release"), self.repo)
            clean["noRelease"] = raw.get("noRelease") is True and clean["release"] is None
            clean["etag"] = _etag(raw.get("etag")) if clean["release"] or clean["noRelease"] else None
            now = time.time()
            for key in ("lastSuccessAt", "lastFailureAt", "retryAfterUntil"):
                stamp = _timestamp(raw.get(key))
                # Corrupt far-future timestamps must not suppress checking forever.
                ceiling = now + 366 * 86400 if key == "retryAfterUntil" else now
                clean[key] = stamp if stamp is not None and stamp <= ceiling else None
            if not clean["release"] and not clean["noRelease"]:
                clean["lastSuccessAt"] = None
        except (OSError, ValueError, TypeError, RecursionError):
            pass
        return clean

    def _save(self) -> None:
        temporary = None
        try:
            os.makedirs(self.directory, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                             prefix=".update-cache-", suffix=".tmp", delete=False) as handle:
                temporary = handle.name
                json.dump(self._cache, handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except (OSError, ValueError):
            # An unwritable cache must not make an otherwise successful check fail.
            pass
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _known_status(self) -> str:
        release = self._cache["release"]
        if release:
            latest, current = _version(release["version"]), _version(self.current_version)
            return "available" if latest > current else "ahead" if latest < current else "latest"
        return "no_release" if self._cache["noRelease"] else "skipped"

    def _result(self, status: str, ok: bool, **extra: object) -> dict:
        return {"ok": ok, "status": status, "currentVersion": self.current_version,
                "latestVersion": self._cache["release"]["version"] if self._cache["release"] else None,
                "release": copy.deepcopy(self._cache["release"]),
                "lastSuccessAt": self._cache["lastSuccessAt"], **extra}

    def state(self) -> dict:
        """Return an independent JSON-compatible snapshot, without doing I/O."""
        with self._state_lock:
            return copy.deepcopy(self._state)

    def _publish(self, status: str, ok: bool, **extra: object) -> dict:
        with self._state_lock:
            self._state = self._result(status, ok, **extra)
            return copy.deepcopy(self._state)

    def _cooldown(self, headers: object, now: float) -> None:
        # Headers are public server metadata; never persist request/response bodies.
        until = None
        retry = headers.get("Retry-After")
        if isinstance(retry, str):
            try:
                seconds = float(retry)
                if math.isfinite(seconds) and seconds >= 0:
                    until = now + seconds
            except (ValueError, OverflowError):
                try:
                    date = parsedate_to_datetime(retry)
                    until = date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()
                except (ValueError, TypeError, OverflowError):
                    pass
        reset = headers.get("X-RateLimit-Reset")
        if headers.get("X-RateLimit-Remaining") == "0" or headers.get("_rate_limited"):
            try:
                stamp = float(reset)
                if math.isfinite(stamp):
                    until = max(until or now, stamp)
            except (ValueError, TypeError, OverflowError):
                pass
        if until is not None and math.isfinite(until) and until > now:
            self._cache["retryAfterUntil"] = max(self._cache["retryAfterUntil"] or 0, until)

    def check(self, force: bool = False) -> dict:
        """Check once, or skip local intervals/server cooldowns.

        ``force`` bypasses the 24h success/1h failure intervals, never server
        cooldowns. Concurrent callers return ``ok=False, status='busy'`` without
        waiting. Errors contain fixed messages, never potentially sensitive data
        from exceptions or GitHub response bodies.
        """
        if not self._check_lock.acquire(blocking=False):
            with self._state_lock:
                return self._result("busy", False, reason="check_in_progress")
        try:
            now = time.time()
            if (self._cache["retryAfterUntil"] or 0) > now:
                return self._result("skipped", True, reason="server_cooldown")
            if not force:
                for key, interval, reason in (("lastFailureAt", FAILURE_INTERVAL, "failure_backoff"),
                                              ("lastSuccessAt", SUCCESS_INTERVAL, "recent_success")):
                    if self._cache[key] is not None and now - self._cache[key] < interval:
                        return self._result("skipped", True, reason=reason)
            headers = {"Accept": "application/vnd.github+json", "User-Agent": f"yhShell/{self.current_version}",
                       "X-GitHub-Api-Version": "2022-11-28"}
            if self._cache["etag"]:
                headers["If-None-Match"] = self._cache["etag"]
            try:
                # Do not inherit netrc authentication, environment proxies or tokens.
                with requests.Session() as session:
                    session.trust_env = False
                    response = session.get(self._endpoint, headers=headers, timeout=(3, 8),
                                           allow_redirects=False)
                    rate_headers = dict(response.headers)
                    # requests headers are case-insensitive; preserve that contract.
                    rate_headers = requests.structures.CaseInsensitiveDict(rate_headers)
                    if response.status_code in (403, 429):
                        rate_headers["_rate_limited"] = True
                    self._cooldown(rate_headers, now)
                    if response.status_code == 304:
                        if not (self._cache["release"] or self._cache["noRelease"]):
                            raise ValueError("Invalid cached response")
                    elif response.status_code == 404:
                        self._cache.update(release=None, noRelease=True)
                    elif response.status_code == 200:
                        release = _release(response.json(), self.repo, api=True)
                        if release is None:
                            raise ValueError("Invalid release")
                        self._cache.update(release=release, noRelease=False)
                    else:
                        error = "GitHub 请求频率受限，请稍后重试" if response.status_code in (403, 429) else "GitHub 更新检查失败"
                        self._cache["lastFailureAt"] = now
                        # Secondary limits may omit both timing headers.
                        if response.status_code in (403, 429) and (self._cache["retryAfterUntil"] or 0) <= now:
                            self._cache["retryAfterUntil"] = now + FAILURE_INTERVAL
                        self._save()
                        return self._publish("error", False, error=error)
                    new_etag = _etag(response.headers.get("ETag"))
                    if response.status_code != 304 or new_etag:
                        self._cache["etag"] = new_etag
            except requests.RequestException:
                self._cache["lastFailureAt"] = now
                self._save()
                return self._publish("error", False, error="无法连接 GitHub，请检查网络后重试")
            except (ValueError, TypeError, RecursionError):
                self._cache["lastFailureAt"] = now
                self._save()
                return self._publish("error", False, error="GitHub 发布信息无效")
            self._cache.update(lastSuccessAt=now, lastFailureAt=None)
            self._save()
            return self._publish(self._known_status(), True)
        finally:
            self._check_lock.release()
