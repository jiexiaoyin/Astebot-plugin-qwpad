"""QWPad HTTP 客户端与 openapi 契约的回归测试。"""

import asyncio
import hashlib
import json

import httpx
import pytest

from qwpad_plugin.qwpad_client import (
    BIG_FILE_TYPE_FILE,
    BIG_FILE_TYPE_VIDEO,
    FILE_TYPE_IMAGE,
    MAX_CDN_UPLOAD_BYTES,
    QWPadApiError,
    QWPadClient,
)


def _run(coro):
    return asyncio.run(coro)


def test_download_url_does_not_send_authorization():
    """cloudUrl 属于对象存储，不能收到业务 TOKEN。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        seen["user_agent"] = request.headers.get("user-agent")
        return httpx.Response(200, content=b"media-bytes")

    async def run():
        client = QWPadClient(
            "wxa_secret_should_not_leak", transport=httpx.MockTransport(handler)
        )
        try:
            return await client._download_url("https://cdn.example.com/a?sign=1")
        finally:
            await client.close()

    assert _run(run()) == b"media-bytes"
    assert seen["authorization"] is None
    assert "Mozilla" in seen["user_agent"]


def test_download_url_rejects_fs_scheme():
    async def run():
        client = QWPadClient(
            "wxa_test", transport=httpx.MockTransport(lambda r: None)
        )
        try:
            await client._download_url("fs:///objects/abc")
        finally:
            await client.close()

    with pytest.raises(QWPadApiError, match="fs://"):
        _run(run())


def test_send_video_prefers_upload_duration_and_thumb():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {}})

    upload = {
        "fileId": "fid",
        "fileAesKey": "aes",
        "fileMd5": "0" * 32,
        "fileSize": 123,
        "durationTime": 12,
        "fileThumbSize": 345,
    }

    async def run():
        client = QWPadClient("wxa_test", transport=httpx.MockTransport(handler))
        try:
            await client.send_video(
                "1001", upload, "v.mp4", duration=99, cover_size=1
            )
        finally:
            await client.close()

    _run(run())
    assert seen["body"]["duration"] == 12
    assert seen["body"]["coverImageSize"] == 345


def test_upload_big_file_uses_big_endpoint_and_fills_md5(tmp_path):
    path = tmp_path / "big.bin"
    path.write_bytes(b"hello")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        body = request.read()
        seen["file_type"] = b'name="fileType"' in body and b"35" in body
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "ok",
                "data": {
                    "fileId": "ORDINARY",
                    "fileKey": "KEY_NOT_AES",
                    "fileMd5": "",
                    "fileSize": 0,
                    "cloudUrl": "",
                    "fileConvertId": "*1*converted",
                    "previewImgUrl": "",
                    "PreviewImgSize": 0,
                    "duration": 0,
                    "fileWidth": 0,
                    "fileHeight": 0,
                },
            },
        )

    async def run():
        client = QWPadClient("wxa_test", transport=httpx.MockTransport(handler))
        try:
            return await client.upload_big_file(str(path), BIG_FILE_TYPE_FILE)
        finally:
            await client.close()

    data = _run(run())
    assert seen["path"] == "/cloud/cdnBigFileUpload"
    assert seen["file_type"] is True
    assert data["fileConvertId"] == "*1*converted"
    assert data["fileMd5"] == hashlib.md5(b"hello").hexdigest()
    assert data["fileSize"] == 5


def test_upload_image_rejects_20mib_without_http(tmp_path):
    path = tmp_path / "huge.jpg"
    with open(path, "wb") as fh:
        fh.truncate(MAX_CDN_UPLOAD_BYTES)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("超过 20MiB 的图片不应发起上传")

    async def run():
        client = QWPadClient("wxa_test", transport=httpx.MockTransport(handler))
        try:
            await client.upload_file(str(path), FILE_TYPE_IMAGE)
        finally:
            await client.close()

    with pytest.raises(QWPadApiError, match="20MiB|20971520"):
        _run(run())


def test_send_big_file_payload():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {}})

    upload = {
        "fileConvertId": "*1*converted",
        "fileKey": "KEY_NOT_AES",
        "fileMd5": "a" * 32,
        "fileSize": 20971520,
    }

    async def run():
        client = QWPadClient("wxa_test", transport=httpx.MockTransport(handler))
        try:
            await client.send_big_file("1001", upload, "big.zip")
        finally:
            await client.close()

    _run(run())
    body = seen["body"]
    assert body["fileId"] == "*1*converted"
    assert body["fileAesKey"] == ""
    assert body["fileSize"] == 20971520
    assert "fileKey" not in body


def test_send_big_video_preview_fields():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {}})

    upload = {
        "fileConvertId": "*1*video",
        "fileMd5": "b" * 32,
        "fileSize": 104857600,
        "previewImgUrl": "https://cdn.example.com/cover.jpg",
        "PreviewImgSize": 4096,
        "duration": 33,
    }

    async def run():
        client = QWPadClient("wxa_test", transport=httpx.MockTransport(handler))
        try:
            await client.send_big_video("room-1", upload, "big.mp4")
        finally:
            await client.close()

    _run(run())
    body = seen["body"]
    assert body["previewImgUrl"] == "https://cdn.example.com/cover.jpg"
    assert body["coverImageSize"] == 4096
    assert body["duration"] == 33


def test_big_upload_requires_convert_id():
    with pytest.raises(QWPadApiError, match="fileConvertId"):
        QWPadClient._big_upload_fields(
            {"fileId": "ordinary", "fileMd5": "c" * 32, "fileSize": 1}
        )


def test_big_upload_types_are_22_and_35():
    assert BIG_FILE_TYPE_VIDEO == 22
    assert BIG_FILE_TYPE_FILE == 35
