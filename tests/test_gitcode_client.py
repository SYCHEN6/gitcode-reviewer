"""GitCodeClient 单元测试（mock HTTP，v5 API）。"""

import base64

import httpx
import pytest
import respx

from src.tools.gitcode_client import GitCodeClient

BASE = "https://gitcode.com"
TOKEN = "test-token"
client = GitCodeClient(BASE, TOKEN)

API = f"{BASE}/api/v5"


@pytest.mark.asyncio
@respx.mock
async def test_get_pr_diff():
    pr_resp = {
        "head": {"sha": "head123"},
        "base": {"sha": "base456"},
    }
    files_resp = [
        {
            "filename": "src/main.py",
            "previous_filename": "src/main.py",
            "patch": "@@ -1 +1 @@\n-old\n+new",
        }
    ]
    respx.get(f"{API}/repos/owner/repo/pulls/1").mock(
        return_value=httpx.Response(200, json=pr_resp)
    )
    respx.get(f"{API}/repos/owner/repo/pulls/1/files").mock(
        return_value=httpx.Response(200, json=files_resp)
    )

    result = await client.get_pr_diff("owner/repo", 1)
    assert result["head_sha"] == "head123"
    assert result["base_sha"] == "base456"
    assert "src/main.py" in result["diff"]
    assert result["files"] == ["src/main.py"]


@pytest.mark.asyncio
@respx.mock
async def test_get_file_content():
    content = base64.b64encode(b"print('hello')").decode()
    respx.get(f"{API}/repos/owner/repo/contents/src%2Fmain.py").mock(
        return_value=httpx.Response(200, json={"content": content})
    )

    result = await client.get_file_content("owner/repo", "src/main.py", "main")
    assert result["content"] == "print('hello')"


@pytest.mark.asyncio
@respx.mock
async def test_post_inline_comment():
    respx.post(f"{API}/repos/owner/repo/pulls/1/comments").mock(
        return_value=httpx.Response(200, json={"id": 999})
    )

    position = {
        "head_sha": "head123",
        "new_path": "src/main.py",
        "new_line": 10,
    }
    result = await client.post_inline_comment("owner/repo", 1, "test comment", position)
    assert result["comment_id"] == 999


@pytest.mark.asyncio
@respx.mock
async def test_post_mr_note():
    respx.post(f"{API}/repos/owner/repo/pulls/1/comments").mock(
        return_value=httpx.Response(200, json={"id": 888})
    )
    result = await client.post_mr_note("owner/repo", 1, "global note")
    assert result["comment_id"] == 888


@pytest.mark.asyncio
@respx.mock
async def test_update_mr_description():
    respx.patch(f"{API}/repos/owner/repo/pulls/1").mock(
        return_value=httpx.Response(200, json={"number": 1})
    )
    result = await client.update_mr_description("owner/repo", 1, "new description")
    assert result["success"] is True


@pytest.mark.asyncio
@respx.mock
async def test_update_mr_label():
    respx.put(f"{API}/repos/owner/repo/pulls/1/labels").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await client.update_mr_label("owner/repo", 1, ["ai-risk:high"])
    assert result["success"] is True


def test_parse_namespace_error():
    from src.tools.gitcode_client import _parse_ns
    with pytest.raises(ValueError):
        _parse_ns("invalid-no-slash")
