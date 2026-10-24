import asyncio
import json
import re
import time
from collections.abc import AsyncGenerator, Callable
from typing import Any, Literal, Protocol
from urllib.parse import quote

import httpx


class GitHubError(RuntimeError):
    def __init__(self, status: int, method: str, path: str, message: str) -> None:
        super().__init__(f"[{status}] {method} {path}: {message}")
        self.status = status
        self.method = method
        self.path = path
        self.message = message


class EtagCache(Protocol):
    def get(self, url: str) -> tuple[str, Any] | None: ...

    def put(self, url: str, etag: str, body: Any) -> None: ...


class MemoryEtagCache:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[str, Any]] = {}

    def get(self, url: str) -> tuple[str, Any] | None:
        return self._cache.get(url)

    def put(self, url: str, etag: str, body: Any) -> None:
        self._cache[url] = (etag, body)


class Response:
    def __init__(
        self, status: int, body: Any, headers: httpx.Headers, cached: bool, text: str | None = None
    ) -> None:
        self.status = status
        self.body = body
        self.headers = headers
        self.cached = cached
        if text is None:
            text = body if isinstance(body, str) else json.dumps(body)
        # Raw response text, never parsed.
        self.text = text


_RETRY_5XX = (500, 502, 503, 504)


def _error_message(resp: httpx.Response, body: Any) -> str:
    message = ""
    errors: Any = None
    if isinstance(body, dict):
        message = str(body.get("message") or "")
        errors = body.get("errors")
    elif isinstance(body, str):
        message = body.strip()
    if not message:
        message = resp.reason_phrase or f"HTTP {resp.status_code}"
    if errors:
        message = f"{message} (errors: {errors})"
    return message


class GitHub:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.github.com",
        cache: EtagCache | None = None,
        write_spacing: float = 1.0,
        max_retries: int = 4,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # Each client owns its cache unless one is injected explicitly.
        self._cache: EtagCache = cache if cache is not None else MemoryEtagCache()
        self._write_spacing = write_spacing
        self._max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self._wall_clock = wall_clock

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gargi-harness",
        }
        self._client = httpx.AsyncClient(base_url=self._base_url, headers=headers, transport=transport)
        self._last_write: float | None = None
        self._write_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"GitHub(base_url={self._base_url!r})"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "GitHub":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _wait_for_write(self) -> None:
        async with self._write_lock:
            if self._last_write is not None:
                elapsed = self._clock() - self._last_write
                if elapsed < self._write_spacing:
                    await self._sleep(self._write_spacing - elapsed)
            self._last_write = self._clock()

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float | None:
        """Seconds to wait before retrying, or None if the response is not retryable."""
        status = resp.status_code
        if status in (403, 429):
            if "retry-after" in resp.headers:
                try:
                    return min(max(float(resp.headers["retry-after"]), 0.0), 300.0)
                except ValueError:
                    return 60.0
            if resp.headers.get("x-ratelimit-remaining") == "0" and "x-ratelimit-reset" in resp.headers:
                try:
                    reset = float(resp.headers["x-ratelimit-reset"])
                except ValueError:
                    reset = None
                if reset is not None:
                    return min(max(reset - self._wall_clock(), 1.0), 900.0)
            if "secondary rate limit" in resp.text.lower():
                return 60.0 * (attempt + 1)
            return None
        if status in _RETRY_5XX:
            return float(2**attempt)
        return None

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        raw: bool = False,
    ) -> Response:
        """Send a request. With raw=True the body is the unparsed response text."""
        method = method.upper()
        if method != "GET":
            await self._wait_for_write()

        if path.startswith(("http://", "https://")):
            url = path
        else:
            url = f"{self._base_url}/{path.lstrip('/')}"
        req = self._client.build_request(method, url, json=json, params=params, headers=headers)
        cache_key = str(req.url)

        use_etag = method == "GET"
        attempt = 0
        while True:
            req.headers.pop("If-None-Match", None)
            if use_etag:
                cached = self._cache.get(cache_key)
                if cached is not None:
                    req.headers["If-None-Match"] = cached[0]

            try:
                resp = await self._client.send(req)
            except httpx.RequestError as e:
                raise GitHubError(500, method, path, f"{type(e).__name__}: {e}") from e

            if resp.status_code == 304 and method == "GET":
                cached = self._cache.get(cache_key)
                if cached is not None:
                    return Response(200, cached[1], resp.headers, cached=True)
                if use_etag:
                    # 304 without a usable cache entry: retry once without conditional headers.
                    use_etag = False
                    continue
                raise GitHubError(304, method, path, "Not Modified, but no cached response is available")

            if raw:
                body: Any = resp.text
            else:
                try:
                    body = resp.json()
                except ValueError:
                    body = resp.text

            if resp.status_code < 400:
                if method == "GET" and "etag" in resp.headers:
                    self._cache.put(cache_key, resp.headers["etag"], body)
                return Response(resp.status_code, body, resp.headers, cached=False, text=resp.text)

            delay = self._retry_delay(resp, attempt)
            if delay is None or attempt >= self._max_retries:
                try:
                    error_body: Any = resp.json()
                except ValueError:
                    error_body = resp.text
                raise GitHubError(resp.status_code, method, path, _error_message(resp, error_body))
            await self._sleep(delay)
            attempt += 1

    async def get(self, path: str, **params: Any) -> Any:
        resp = await self.request("GET", path, params=params)
        return resp.body

    async def post(self, path: str, **json: Any) -> Any:
        resp = await self.request("POST", path, json=json)
        return resp.body

    async def patch(self, path: str, **json: Any) -> Any:
        resp = await self.request("PATCH", path, json=json)
        return resp.body

    async def put(self, path: str, **json: Any) -> Any:
        resp = await self.request("PUT", path, json=json)
        return resp.body

    async def delete(self, path: str) -> None:
        await self.request("DELETE", path)

    async def paginate(self, path: str, **params: Any) -> AsyncGenerator[Any, None]:
        """Yield items across pages. Params apply to the first request only; Link URLs carry their own."""
        url: str | None = path
        page_params: dict[str, Any] | None = {"per_page": 100, **params}
        while url:
            resp = await self.request("GET", url, params=page_params)
            page_params = None
            body = resp.body
            if isinstance(body, dict):
                body = next((v for v in body.values() if isinstance(v, list)), [])
            if isinstance(body, list):
                for item in body:
                    yield item

            url = None
            for match in re.finditer(r'<([^>]+)>\s*;\s*rel="?([^";,]+)"?', resp.headers.get("link", "")):
                if match.group(2) == "next":
                    url = match.group(1)
                    break

    # Typed helpers
    async def repo(self, repo: str) -> dict[str, Any]:
        return await self.get(f"repos/{repo}")

    async def file_text(self, repo: str, path: str, ref: str | None = None) -> str | None:
        params = {"ref": ref} if ref else None
        headers = {"Accept": "application/vnd.github.raw+json"}
        try:
            resp = await self.request(
                "GET",
                f"repos/{repo}/contents/{quote(path, safe='/')}",
                params=params,
                headers=headers,
                raw=True,
            )
        except GitHubError as e:
            if e.status == 404:
                return None
            raise
        return resp.text

    async def issue(self, repo: str, number: int) -> dict[str, Any]:
        return await self.get(f"repos/{repo}/issues/{number}")

    async def issues(
        self, repo: str, *, state: str = "open", labels: str | None = None, since: str | None = None
    ) -> list[dict[str, Any]]:
        params = {"state": state}
        if labels:
            params["labels"] = labels
        if since:
            params["since"] = since
        items = []
        async for issue in self.paginate(f"repos/{repo}/issues", **params):
            if "pull_request" not in issue:
                items.append(issue)
        return items

    async def update_issue(self, repo: str, number: int, **kwargs: Any) -> dict[str, Any]:
        return await self.patch(f"repos/{repo}/issues/{number}", **kwargs)

    async def create_issue(self, repo: str, title: str, **kwargs: Any) -> dict[str, Any]:
        return await self.post(f"repos/{repo}/issues", title=title, **kwargs)

    async def comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        return [c async for c in self.paginate(f"repos/{repo}/issues/{number}/comments")]

    async def add_comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        return await self.post(f"repos/{repo}/issues/{number}/comments", body=body)

    async def edit_comment(self, repo: str, comment_id: int, body: str) -> dict[str, Any]:
        return await self.patch(f"repos/{repo}/issues/comments/{comment_id}", body=body)

    async def add_labels(self, repo: str, number: int, labels: list[str]) -> list[dict[str, Any]]:
        return await self.post(f"repos/{repo}/issues/{number}/labels", labels=labels)

    async def remove_label(self, repo: str, number: int, label: str) -> None:
        try:
            await self.delete(f"repos/{repo}/issues/{number}/labels/{quote(label, safe='')}")
        except GitHubError as e:
            if e.status != 404:
                raise

    async def ensure_label(
        self, repo: str, name: str, color: str | None = None, description: str | None = ""
    ) -> dict[str, Any] | None:
        """Create the label if missing; 422 (already exists) is ignored and returns None."""
        payload: dict[str, Any] = {"name": name}
        if color is not None:
            payload["color"] = color
        if description is not None:
            payload["description"] = description
        try:
            return await self.post(f"repos/{repo}/labels", **payload)
        except GitHubError as e:
            if e.status != 422:
                raise
            return None

    async def labels(self, repo: str) -> list[dict[str, Any]]:
        return [label async for label in self.paginate(f"repos/{repo}/labels")]

    async def parent(self, repo: str, number: int) -> dict[str, Any] | None:
        try:
            return await self.get(f"repos/{repo}/issues/{number}/parent")
        except GitHubError as e:
            if e.status == 404:
                return None
            raise

    async def sub_issues(self, repo: str, number: int) -> list[dict[str, Any]]:
        return [i async for i in self.paginate(f"repos/{repo}/issues/{number}/sub_issues")]

    async def add_sub_issue(self, repo: str, number: int, sub_issue_id: int) -> dict[str, Any]:
        return await self.post(f"repos/{repo}/issues/{number}/sub_issues", sub_issue_id=sub_issue_id)

    async def blocked_by(self, repo: str, number: int) -> list[dict[str, Any]]:
        return [i async for i in self.paginate(f"repos/{repo}/issues/{number}/dependencies/blocked_by")]

    async def add_blocked_by(self, repo: str, number: int, blocker_id: int) -> dict[str, Any]:
        return await self.post(f"repos/{repo}/issues/{number}/dependencies/blocked_by", issue_id=blocker_id)

    async def pull(self, repo: str, number: int) -> dict[str, Any]:
        return await self.get(f"repos/{repo}/pulls/{number}")

    async def pulls_for_head(self, repo: str, branch: str, state: str = "all") -> list[dict[str, Any]]:
        owner = repo.split("/")[0]
        return [p async for p in self.paginate(f"repos/{repo}/pulls", head=f"{owner}:{branch}", state=state)]

    async def pull_files(self, repo: str, number: int) -> list[dict[str, Any]]:
        return [f async for f in self.paginate(f"repos/{repo}/pulls/{number}/files")]

    async def create_pull(self, repo: str, title: str, head: str, base: str, **kwargs: Any) -> dict[str, Any]:
        return await self.post(f"repos/{repo}/pulls", title=title, head=head, base=base, **kwargs)

    async def merge_pull(
        self,
        repo: str,
        number: int,
        method: Literal["merge", "squash", "rebase"] = "squash",
        sha: str | None = None,
    ) -> dict[str, Any]:
        kwargs = {"merge_method": method}
        if sha:
            kwargs["sha"] = sha
        return await self.put(f"repos/{repo}/pulls/{number}/merge", **kwargs)

    async def delete_branch(self, repo: str, branch: str) -> None:
        try:
            await self.delete(f"repos/{repo}/git/refs/heads/{quote(branch, safe='/')}")
        except GitHubError as e:
            if e.status not in (404, 422):
                raise

    async def check_runs(self, repo: str, sha: str) -> list[dict[str, Any]]:
        return [c async for c in self.paginate(f"repos/{repo}/commits/{sha}/check-runs")]

    async def combined_status(self, repo: str, sha: str) -> dict[str, Any]:
        return await self.get(f"repos/{repo}/commits/{sha}/status")
