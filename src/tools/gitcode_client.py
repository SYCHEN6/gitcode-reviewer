"""GitCode REST API client (v5, GitHub/Gitee 风格)。

认证方式：PRIVATE-TOKEN header（或 Authorization: Bearer token）
API 基础路径：{base_url}/api/v5/repos/{owner}/{repo}/...
"""

import base64
from typing import Any
from urllib.parse import quote

import httpx

# project_id 格式为 "owner/repo"，例如 "chensiyu47/MindIE-SD_1344"
ProjectID = str


def _parse_ns(project_id: ProjectID) -> tuple[str, str]:
    """将 'owner/repo' 拆分为 (owner, repo)。"""
    parts = str(project_id).split("/", 1)
    if len(parts) != 2:
        raise ValueError(f"project_id 格式错误：{project_id!r}，期望 'owner/repo'")
    return parts[0], parts[1]


class GitCodeClient:
    def __init__(self, base_url: str, token: str):
        self._base = base_url.rstrip("/") + "/api/v5"
        self._headers = {
            "PRIVATE-TOKEN": token,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }

    async def _get(self, path: str, params: dict | None = None) -> Any:
        async with httpx.AsyncClient(headers=self._headers, timeout=30) as c:
            r = await c.get(f"{self._base}{path}", params=params)
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, body: dict) -> Any:
        async with httpx.AsyncClient(headers=self._headers, timeout=30) as c:
            r = await c.post(f"{self._base}{path}", json=body)
            r.raise_for_status()
            return r.json()

    async def _patch(self, path: str, body: dict) -> Any:
        async with httpx.AsyncClient(headers=self._headers, timeout=30) as c:
            r = await c.patch(f"{self._base}{path}", json=body)
            r.raise_for_status()
            return r.json()

    async def _put(self, path: str, body: dict) -> Any:
        async with httpx.AsyncClient(headers=self._headers, timeout=30) as c:
            r = await c.put(f"{self._base}{path}", json=body)
            if not r.is_success:
                raise httpx.HTTPStatusError(
                    f"{r.status_code} {r.reason_phrase} — {r.text[:300]}",
                    request=r.request, response=r,
                )
            return r.json() if r.content else {}

    # ── 核心接口 ───────────────────────────────────────────────────────────────

    async def get_pr_diff(self, project_id: ProjectID, mr_iid: int) -> dict:
        """返回 PR 的 diff 文本、变更文件列表和 SHA 信息。

        返回字段：
        - diff: 拼接后的 unified diff 文本
        - files: 变更文件路径列表
        - diffs: 每个文件的原始 diff 结构
        - head_sha / base_sha / start_sha: inline comment 所需 SHA
        """
        owner, repo = _parse_ns(project_id)
        pr_path = f"/repos/{owner}/{repo}/pulls/{mr_iid}"

        pr = await self._get(pr_path)
        head_sha: str = pr.get("head", {}).get("sha", "")
        base_sha: str = pr.get("base", {}).get("sha", "")

        files_data = await self._get(f"{pr_path}/files")
        if not isinstance(files_data, list):
            files_data = files_data.get("files", []) if isinstance(files_data, dict) else []

        def _patch_text(f: dict) -> str:
            patch = f.get("patch", "")
            # GitCode v5: patch 是嵌套 dict，diff 内容在 patch["diff"]
            if isinstance(patch, dict):
                patch = patch.get("diff", "")
            return patch

        diff_text = "\n".join(
            f"--- a/{f.get('previous_filename') or f.get('filename', '')}\n"
            f"+++ b/{f.get('filename', '')}\n"
            f"{_patch_text(f)}"
            for f in files_data
            if _patch_text(f)
        )

        return {
            "diff": diff_text,
            "files": [f.get("filename", "") for f in files_data],
            "diffs": files_data,
            "head_sha": head_sha,
            "base_sha": base_sha,
            "start_sha": base_sha,  # GitCode v5 无独立 start_sha，用 base_sha 代替
        }

    async def get_file_content(self, project_id: ProjectID, file_path: str, ref: str) -> dict:
        """返回指定 ref 下的文件内容（base64 解码后的纯文本）。"""
        owner, repo = _parse_ns(project_id)
        encoded_path = quote(file_path, safe="")
        data = await self._get(
            f"/repos/{owner}/{repo}/contents/{encoded_path}",
            params={"ref": ref},
        )
        if isinstance(data, list):
            return {"content": ""}
        raw = data.get("content", "")
        content = base64.b64decode(raw).decode("utf-8", errors="replace") if raw else ""
        return {"content": content}

    async def post_inline_comment(
        self,
        project_id: ProjectID,
        mr_iid: int,
        body: str,
        position: dict,
    ) -> dict:
        """在 PR 指定位置发送 inline comment（GitHub v5 风格）。

        position 字段：
        - head_sha (commit_id): 来自 get_pr_diff 返回值
        - new_path (path): 文件路径
        - new_line (position): diff 中的行号
        """
        owner, repo = _parse_ns(project_id)
        payload = {
            "body": body,
            "commit_id": position.get("head_sha", position.get("commit_id", "")),
            "path": position.get("new_path", position.get("path", "")),
            "position": position.get("new_line", position.get("position", 1)),
        }
        data = await self._post(
            f"/repos/{owner}/{repo}/pulls/{mr_iid}/comments",
            payload,
        )
        return {"comment_id": data.get("id", 0)}

    async def post_suggestion(
        self,
        project_id: ProjectID,
        mr_iid: int,
        suggestion_code: str,
        position: dict,
    ) -> dict:
        """发送包含 suggestion block 的 inline comment。"""
        body = f"```suggestion\n{suggestion_code}\n```"
        return await self.post_inline_comment(project_id, mr_iid, body, position)

    async def post_mr_note(self, project_id: ProjectID, mr_iid: int, body: str) -> dict:
        """发送 PR 全局评论（无 position）。"""
        owner, repo = _parse_ns(project_id)
        data = await self._post(
            f"/repos/{owner}/{repo}/pulls/{mr_iid}/comments",
            {"body": body},
        )
        return {"comment_id": data.get("id", 0)}

    async def update_mr_description(self, project_id: ProjectID, mr_iid: int, body: str) -> dict:
        owner, repo = _parse_ns(project_id)
        await self._patch(
            f"/repos/{owner}/{repo}/pulls/{mr_iid}",
            {"body": body},
        )
        return {"success": True}

    async def get_repo_labels(self, project_id: ProjectID) -> list[dict]:
        """返回仓库已有的标签列表 [{name, color, ...}]。"""
        owner, repo = _parse_ns(project_id)
        data = await self._get(f"/repos/{owner}/{repo}/labels")
        return data if isinstance(data, list) else []

    async def update_mr_label(self, project_id: ProjectID, mr_iid: int, labels: list[str]) -> dict:
        owner, repo = _parse_ns(project_id)
        await self._put(
            f"/repos/{owner}/{repo}/pulls/{mr_iid}/labels",
            {"labels": labels},
        )
        return {"success": True}
