"""Tests for the bounded official NEMWeb HTTPS adapter."""

from dataclasses import FrozenInstanceError
from http.client import HTTPMessage, IncompleteRead
from io import BytesIO
from typing import Any
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import Request
from urllib.response import addinfourl

import batterywatch_api.nemweb_http as http


INDEX_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/"
PRICE_INDEX_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/DispatchIS_Reports/"
PRICE_ARTIFACT_URL = (
    PRICE_INDEX_URL + "PUBLIC_DISPATCHIS_202608301205_0000000535164870.zip"
)


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        url: str = INDEX_URL,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._body = body
        self._url = url
        self.status = status
        self.headers = headers or {}
        self.read_sizes: list[int] = []

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def geturl(self) -> str:
        return self._url

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self._body if size < 0 else self._body[:size]


class FakeOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[Request, float]] = []

    def __call__(self, request: Request, *, timeout: float) -> FakeResponse:
        self.calls.append((request, timeout))
        return self.response


class RaisingOpener:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def __call__(self, _request: Request, *, timeout: float) -> FakeResponse:
        del timeout
        raise self.error


class ReadErrorResponse(FakeResponse):
    def __init__(self, error: BaseException) -> None:
        super().__init__(b"payload")
        self.error = error

    def read(self, size: int = -1) -> bytes:
        del size
        raise self.error


class FetchNemwebResourceTests(unittest.TestCase):
    def test_fetches_official_dispatch_price_index_and_artifact(self) -> None:
        for url, body in (
            (PRICE_INDEX_URL, b"<html>price index</html>"),
            (PRICE_ARTIFACT_URL, b"PK price artifact"),
        ):
            with self.subTest(url=url):
                opener = FakeOpener(FakeResponse(body, url=url))
                result = http.fetch_nemweb_resource(
                    url,
                    max_bytes=1024,
                    opener=opener,
                )
                self.assertEqual((result.requested_url, result.body), (url, body))

    def test_fetches_official_resource_with_immutable_metadata(self) -> None:
        response = FakeResponse(
            b"<html>index</html>",
            headers={
                "Content-Type": "text/html; charset=utf-8",
                "ETag": '"index-v1"',
                "Last-Modified": "Sat, 29 Aug 2026 06:55:00 GMT",
            },
        )
        opener = FakeOpener(response)
        fetch: Any = getattr(http, "fetch_nemweb_resource", None)
        result: Any = (
            fetch(
                INDEX_URL,
                max_bytes=1024,
                timeout_seconds=7.5,
                opener=opener,
            )
            if callable(fetch)
            else None
        )

        actual = (
            (
                result.requested_url,
                result.resolved_url,
                result.body,
                result.content_type,
                result.etag,
                result.last_modified,
                response.read_sizes,
                opener.calls[0][0].full_url,
                opener.calls[0][0].get_header("User-agent"),
                opener.calls[0][1],
            )
            if result is not None
            else None
        )
        self.assertEqual(
            actual,
            (
                INDEX_URL,
                INDEX_URL,
                b"<html>index</html>",
                "text/html; charset=utf-8",
                '"index-v1"',
                "Sat, 29 Aug 2026 06:55:00 GMT",
                [1025],
                INDEX_URL,
                "BatteryWatch-Collector/0.1",
                7.5,
            ),
        )
        with self.assertRaises(FrozenInstanceError):
            result.body = b"changed"  # type: ignore[misc]

    def test_normalizes_malformed_url_parse_failures_to_public_error(self) -> None:
        error_type: type[BaseException] | None = None
        try:
            http.fetch_nemweb_resource(
                "https://[invalid",
                max_bytes=1024,
                opener=FakeOpener(FakeResponse(b"payload")),
            )
        except BaseException as error:
            error_type = type(error)
        self.assertIs(error_type, http.NemwebHttpError)

    def test_normalizes_huge_integer_timeout_to_public_error(self) -> None:
        error_type: type[BaseException] | None = None
        try:
            http.fetch_nemweb_resource(
                INDEX_URL,
                max_bytes=1024,
                timeout_seconds=10**10000,
                opener=FakeOpener(FakeResponse(b"payload")),
            )
        except BaseException as error:
            error_type = type(error)
        self.assertIs(error_type, http.NemwebHttpError)

    def test_default_transport_refuses_redirects_before_target_request(self) -> None:
        default_transport = getattr(
            http.fetch_nemweb_resource, "__kwdefaults__", {}
        ).get("opener")
        default_opener = getattr(default_transport, "__self__", None)
        self.assertIsNotNone(default_opener)
        redirected_url = (
            INDEX_URL
            + "PUBLIC_DISPATCHSCADA_202608291700_0000000535067000.zip"
        )
        requested_urls: list[str] = []

        def fake_resource_open(
            request: Request, _data: Any = None
        ) -> addinfourl:
            requested_urls.append(request.full_url)
            headers = HTTPMessage()
            headers["Location"] = redirected_url
            response = addinfourl(
                BytesIO(b"redirect"),
                headers,
                request.full_url,
                code=302,
            )
            setattr(response, "msg", "Found")
            return response

        with patch.object(
            default_opener, "_open", side_effect=fake_resource_open
        ):
            with self.assertRaises(http.NemwebHttpError):
                http.fetch_nemweb_resource(INDEX_URL, max_bytes=1024)

        self.assertEqual(requested_urls, [INDEX_URL])

    def test_normalizes_invalid_request_arguments_to_public_error(self) -> None:
        response = FakeResponse(b"payload")
        invalid_cases: tuple[tuple[object, object, object], ...] = (
            (None, 1024, 10.0),
            ("http://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/", 1024, 10.0),
            ("https://nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/", 1024, 10.0),
            ("https://www.nemweb.com.au:443/REPORTS/CURRENT/Dispatch_SCADA/", 1024, 10.0),
            (INDEX_URL + "?latest=1", 1024, 10.0),
            (INDEX_URL + "#fragment", 1024, 10.0),
            ("https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/not-canonical.zip", 1024, 10.0),
            (INDEX_URL, True, 10.0),
            (INDEX_URL, 0, 10.0),
            (INDEX_URL, 16 * 1024 * 1024 + 1, 10.0),
            (INDEX_URL, 1024, True),
            (INDEX_URL, 1024, 0.0),
            (INDEX_URL, 1024, 61.0),
            (INDEX_URL, 1024, float("inf")),
        )

        for url, max_bytes, timeout_seconds in invalid_cases:
            with self.subTest(url=url, max_bytes=max_bytes, timeout=timeout_seconds):
                error_type: type[BaseException] | None = None
                try:
                    http.fetch_nemweb_resource(
                        url,  # type: ignore[arg-type]
                        max_bytes=max_bytes,  # type: ignore[arg-type]
                        timeout_seconds=timeout_seconds,  # type: ignore[arg-type]
                        opener=FakeOpener(response),
                    )
                except BaseException as error:
                    error_type = type(error)
                self.assertIs(error_type, http.NemwebHttpError)

    def test_normalizes_transport_status_and_redirect_failures(self) -> None:
        redirected_url = (
            INDEX_URL
            + "PUBLIC_DISPATCHSCADA_202608291700_0000000535067000.zip"
        )
        openers: tuple[object, ...] = (
            FakeOpener(FakeResponse(b"payload", status=404)),
            FakeOpener(FakeResponse(b"payload", url=redirected_url)),
            RaisingOpener(URLError("source unavailable")),
            RaisingOpener(TimeoutError("timed out")),
            RaisingOpener(OSError("socket failure")),
        )

        for opener in openers:
            with self.subTest(opener=type(opener).__name__):
                error_type: type[BaseException] | None = None
                try:
                    http.fetch_nemweb_resource(
                        INDEX_URL,
                        max_bytes=1024,
                        opener=opener,  # type: ignore[arg-type]
                    )
                except BaseException as error:
                    error_type = type(error)
                self.assertIs(error_type, http.NemwebHttpError)


    def test_rejects_unbounded_or_unsafe_response_content(self) -> None:
        invalid_responses = (
            FakeResponse(b""),
            FakeResponse(b"x" * 1025),
            FakeResponse(b"payload", headers={"Content-Length": "1025"}),
            FakeResponse(b"payload", headers={"Content-Length": "invalid"}),
            FakeResponse(b"payload", headers={"Content-Length": "3"}),
            FakeResponse(b"payload", headers={"Content-Encoding": "gzip"}),
            FakeResponse(b"payload", headers={"ETag": "unsafe\nvalue"}),
            FakeResponse(b"payload", headers={"Last-Modified": "x" * 1025}),
        )

        for response in invalid_responses:
            with self.subTest(headers=response.headers, body_size=len(response._body)):
                error_type: type[BaseException] | None = None
                try:
                    http.fetch_nemweb_resource(
                        INDEX_URL,
                        max_bytes=1024,
                        opener=FakeOpener(response),
                    )
                except BaseException as error:
                    error_type = type(error)
                self.assertIs(error_type, http.NemwebHttpError)


    def test_normalizes_protocol_read_failures(self) -> None:
        errors: tuple[BaseException, ...] = (
            IncompleteRead(b"partial", 10),
            EOFError(),
        )

        for read_error in errors:
            with self.subTest(error=type(read_error).__name__):
                error_type: type[BaseException] | None = None
                try:
                    http.fetch_nemweb_resource(
                        INDEX_URL,
                        max_bytes=1024,
                        opener=FakeOpener(ReadErrorResponse(read_error)),
                    )
                except BaseException as error:
                    error_type = type(error)
                self.assertIs(error_type, http.NemwebHttpError)


if __name__ == "__main__":
    unittest.main()
