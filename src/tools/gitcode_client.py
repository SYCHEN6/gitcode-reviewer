"""GitCode REST API client (v5, GitHub/Gitee 风格)。

认证方式：PRIVATE-TOKEN header（或 Authorization: Bearer token）
API 基础路径：{base_url}/api/v5/repos/{owner}/{repo}/...
"""

import base64
import logging
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

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
            return r.json() if r.content else {}

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
        """在 PR 指定代码行发送 inline comment。

        position 字段：
        - head_sha (commit_id): 来自 get_pr_diff 返回值
        - new_path (path): 文件路径
        - new_line: 文件实际行号（新文件 line number，非 diff offset）
        """
        owner, repo = _parse_ns(project_id)
        line_num = position.get("new_line", 1)
        payload = {
            "body": body,
            "commit_id": position.get("head_sha", position.get("commit_id", "")),
            "path": position.get("new_path", position.get("path", "")),
            # 优先使用 line（GitHub 新式参数，直接指定文件行号），
            # 同时保留 position 作为兼容字段（GitCode 可能同时支持两者）
            "line": line_num,
            "side": "RIGHT",
            "position": line_num,
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

    async def get_pr_comments(self, project_id: ProjectID, mr_iid: int) -> list[dict]:
        """获取 PR 所有评论（inline + 全局），用于去重检测。"""
        owner, repo = _parse_ns(project_id)
        data = await self._get(
            f"/repos/{owner}/{repo}/pulls/{mr_iid}/comments",
            params={"per_page": 100},
        )
        return data if isinstance(data, list) else []

    async def get_pr_info(self, project_id: ProjectID, mr_iid: int) -> dict:
        """获取 PR 基本信息，含当前 body/description。"""
        owner, repo = _parse_ns(project_id)
        return await self._get(f"/repos/{owner}/{repo}/pulls/{mr_iid}")

    async def update_pr_comment(
        self,
        project_id: ProjectID,
        mr_iid: int,
        comment_id: int,
        body: str,
    ) -> dict:
        """编辑已有 PR 评论内容。

        GitCode v5 的评论编辑接口不含 mr_iid：PATCH /pulls/comments/{id}
        """
        owner, repo = _parse_ns(project_id)
        data = await self._patch(
            f"/repos/{owner}/{repo}/pulls/comments/{comment_id}",
            {"body": body},
        )
        return {"comment_id": data.get("id", comment_id)}

    async def get_repo_labels(self, project_id: ProjectID) -> list[dict]:
        """返回仓库已有的标签列表 [{name, color, ...}]。"""
        owner, repo = _parse_ns(project_id)
        data = await self._get(f"/repos/{owner}/{repo}/labels")
        return data if isinstance(data, list) else []

    async def create_label(self, project_id: ProjectID, name: str, color: str) -> bool:
        """在仓库创建标签。返回 True 表示标签现在已存在，False 表示 API 拒绝创建。

        Gitee/GitCode v5 API 标签创建接口要求 form 表单参数（不接受 JSON body）。
        必须从 client-level headers 中剔除 Content-Type，否则 application/json 会
        覆盖 httpx 为 data= 自动设置的 application/x-www-form-urlencoded，导致
        Spring @RequestParam 无法解析参数。
        """
        owner, repo = _parse_ns(project_id)
        hex_color = "#" + color.lstrip("#")  # API 要求 # 前缀，如 "#e11d48"
        # 剔除 Content-Type，让 httpx 按 data= 编码方式自动填充
        form_headers = {k: v for k, v in self._headers.items() if k.lower() != "content-type"}
        try:
            async with httpx.AsyncClient(headers=form_headers, timeout=30) as c:
                r = await c.post(
                    f"{self._base}/repos/{owner}/{repo}/labels",
                    data={"name": name, "color": hex_color},
                )
                r.raise_for_status()
            return True
        except httpx.HTTPStatusError as e:
            body = ""
            try:
                body = e.response.text[:300]
            except Exception:
                pass
            if e.response.status_code in (409, 422):  # 标签已存在
                return True
            if e.response.status_code == 400:
                logger.warning("create_label '%s' rejected by API (400): %s", name, body)
                return False
            raise

    async def list_directory(self, project_id: ProjectID, dir_path: str, ref: str) -> list[dict]:
        """返回指定目录下的文件/目录列表（用于新增文件的上下文感知）。

        GitCode v5 contents 接口对目录路径返回 list，对文件路径返回 dict。
        每个条目含 name / path / type("file"|"dir") 字段。
        """
        owner, repo = _parse_ns(project_id)
        clean = dir_path.strip("/")
        encoded = quote(clean, safe="") if clean else ""
        path = f"/repos/{owner}/{repo}/contents"
        if encoded:
            path += f"/{encoded}"
        try:
            data = await self._get(path, params={"ref": ref})
            return data if isinstance(data, list) else []
        except Exception:
            return []

    async def update_mr_label(self, project_id: ProjectID, mr_iid: int, labels: list[str]) -> dict:
        owner, repo = _parse_ns(project_id)
        # GitCode v5 PUT labels 接口要求直接发 JSON array，不是 {"labels": [...]}
        async with httpx.AsyncClient(headers=self._headers, timeout=30) as c:
            r = await c.put(
                f"{self._base}/repos/{owner}/{repo}/pulls/{mr_iid}/labels",
                json=labels,
            )
            if not r.is_success:
                raise httpx.HTTPStatusError(
                    f"{r.status_code} {r.reason_phrase} — {r.text[:300]}",
                    request=r.request, response=r,
                )
        return {"success": True}
