import asyncio
import re
import time
from collections.abc import AsyncGenerator, Callable
from typing import Any, Literal, Protocol

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
    def __init__(self, status: int, body: Any, headers: httpx.Headers, cached: bool) -> None:
        self.status = status
        self.body = body
        self.headers = headers
        self.cached = cached


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
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._cache = cache
        self._write_spacing = write_spacing
        self._max_retries = max_retries
        self._sleep = sleep

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gargi-harness",
        }
        self._client = httpx.AsyncClient(base_url=self._base_url, headers=headers, transport=transport)
        self._last_write = 0.0
        self._write_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"GitHub(base_url={self._base_url!r})"

    async def _wait_for_write(self) -> None:
        async with self._write_lock:
            now = time.time()
            elapsed = now - self._last_write
            if elapsed < self._write_spacing:
                await self._sleep(self._write_spacing - elapsed)
            self._last_write = time.time()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        method = method.upper()
        if method != "GET":
            await self._wait_for_write()

        url = f"{self._base_url}/{path.lstrip('/')}"
        req = self._client.build_request(method, url, json=json, params=params, headers=headers)
        cache_key = str(req.url)

        for attempt in range(self._max_retries + 1):
            req_headers = dict(req.headers)
            if method == "GET" and self._cache:
                cached = self._cache.get(cache_key)
                if cached:
                    etag, _ = cached
                    req_headers["If-None-Match"] = etag

            # Re-build request to include If-None-Match properly per attempt
            current_req = self._client.build_request(
                method, url, json=json, params=params, headers=req_headers
            )
            try:
                resp = await self._client.send(current_req)
            except httpx.RequestError as e:
                # Should not reach here typically since tests run respx without network, but handle properly
                raise GitHubError(500, method, path, str(e)) from e

            if resp.status_code == 304 and method == "GET" and self._cache:
                cached = self._cache.get(cache_key)
                if cached:
                    _, body = cached
                    return Response(304, body, resp.headers, cached=True)

            # Check JSON response if any
            try:
                body = resp.json()
            except Exception:
                body = resp.text

            message = ""
            if isinstance(body, dict):
                message = body.get("message", "")

            if resp.status_code in (403, 429) or (
                isinstance(body, dict) and "secondary rate limit" in message.lower()
            ):
                if attempt < self._max_retries:
                    delay = 0.0
                    if "retry-after" in resp.headers:
                        delay = min(float(resp.headers["retry-after"]), 300.0)
                    elif (
                        resp.headers.get("x-ratelimit-remaining") == "0"
                        and "x-ratelimit-reset" in resp.headers
                    ):
                        reset = float(resp.headers["x-ratelimit-reset"])
                        delay = min(max(reset - time.time(), 0.0), 900.0)
                    elif isinstance(body, dict) and "secondary rate limit" in message.lower():
                        delay = 60.0 * (attempt + 1)

                    if delay > 0:
                        await self._sleep(delay)
                        continue

            if resp.status_code >= 500:
                if attempt < self._max_retries:
                    await self._sleep(2**attempt)
                    continue

            if resp.status_code >= 400:
                raise GitHubError(resp.status_code, method, path, message or str(body))

            if method == "GET" and self._cache and "etag" in resp.headers:
                self._cache.put(cache_key, resp.headers["etag"], body)

            return Response(resp.status_code, body, resp.headers, cached=False)

        # Should not get here, but just in case
        raise GitHubError(500, method, path, "Max retries exceeded")

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
        params["per_page"] = 100
        url = path
        while url:
            resp = await self.request("GET", url, params=params if url == path else None)
            if isinstance(resp.body, list):
                for item in resp.body:
                    yield item
            elif isinstance(resp.body, dict):
                if "items" in resp.body:
                    for item in resp.body["items"]:
                        yield item
                elif "check_runs" in resp.body:
                    for item in resp.body["check_runs"]:
                        yield item

            url = None
            link_header = resp.headers.get("link", "")
            if link_header:
                for match in re.finditer(r'<([^>]+)>;\s*rel="([^"]+)"', link_header):
                    if match.group(2) == "next":
                        url_str = match.group(1)
                        if url_str.startswith(self._base_url):
                            url = url_str[len(self._base_url) :]
                        else:
                            url = url_str
                        break

    # Typed helpers
    async def repo(self, repo: str) -> dict[str, Any]:
        return await self.get(f"repos/{repo}")

    async def file_text(self, repo: str, path: str, ref: str | None = None) -> str | None:
        params = {"ref": ref} if ref else {}
        headers = {"Accept": "application/vnd.github.raw+json"}
        try:
            resp = await self.request("GET", f"repos/{repo}/contents/{path}", params=params, headers=headers)
            return resp.body
        except GitHubError as e:
            if e.status == 404:
                return None
            raise

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
            await self.delete(f"repos/{repo}/issues/{number}/labels/{label}")
        except GitHubError as e:
            if e.status != 404:
                raise

    async def ensure_label(
        self, repo: str, name: str, color: str | None = None, description: str | None = None
    ) -> dict[str, Any]:
        try:
            return await self.post(f"repos/{repo}/labels", name=name, color=color, description=description)
        except GitHubError as e:
            if e.status != 422:
                raise
            return await self.patch(f"repos/{repo}/labels/{name}", color=color, description=description)

    async def parent(self, repo: str, number: int) -> dict[str, Any] | None:
        try:
            return await self.get(f"repos/{repo}/issues/{number}/sub_issues/parent")
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
            await self.delete(f"repos/{repo}/git/refs/heads/{branch}")
        except GitHubError as e:
            if e.status not in (404, 422):
                raise

    async def check_runs(self, repo: str, sha: str) -> list[dict[str, Any]]:
        return [c async for c in self.paginate(f"repos/{repo}/commits/{sha}/check-runs")]

    async def combined_status(self, repo: str, sha: str) -> dict[str, Any]:
        return await self.get(f"repos/{repo}/commits/{sha}/status")
