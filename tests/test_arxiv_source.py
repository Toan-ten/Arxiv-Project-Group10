import gzip
import io
import socket
import ssl
import stat
import tarfile
import tempfile
import unittest
import warnings
import zipfile
from contextlib import ExitStack
from email.message import Message
from email.utils import formatdate
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from src import arxiv_client as client
from src import arxiv_source as source


MSSV = "24120232"
VERSION_ID = "2502.09992v1"
TEX = (b"\\documentclass{article}\r\n% caf\xe9\r\n\\begin{document}\r\n"
       b"\\input{sections/method}\r\n\\includegraphics{figures/test.png}\r\n"
       b"\\end{document}\r\n")
BIB = b"@article{example,\r\n title={Example}\r\n}\r\n"


def tar_bytes(entries, mode="w:gz"):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as archive:
        for name, body in entries:
            member = name if isinstance(name, tarfile.TarInfo) else tarfile.TarInfo(name)
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    return buffer.getvalue()


def zip_bytes(entries, compression=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
            for name, body in entries:
                if isinstance(name, str):
                    member = zipfile.ZipInfo(name)
                    # Keep malicious fixture paths verbatim, including on Windows.
                    member.filename = member.orig_filename = name
                    member.compress_type = compression
                else:
                    member = name
                archive.writestr(member, body)
    return buffer.getvalue()


class SourceExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = self.root / "input.data"
        self.destination = self.root / "version"

    def extract(self, body):
        self.payload.write_bytes(body)
        return source.extract_source(self.payload, self.destination)

    def assert_rejected(self, body, status):
        result = self.extract(body)
        self.assertEqual(result["status"], status, result)
        self.assertEqual(result["files"], [])
        self.assertTrue(result["error"])
        self.assertFalse(self.destination.exists())
        self.assertEqual({path.name for path in self.root.iterdir()}, {"input.data"})

    def test_tar_gzip_keeps_tex_bib_bytes_and_nested_paths(self):
        root = tarfile.TarInfo(".")
        root.type = tarfile.DIRTYPE
        entries = [(root, b""), ("./main.tex", TEX),
                   ("sections/method.tex", b"Method \xff\r\n"), ("refs/library.bib", BIB),
                   ("figures/test.png", b"PNG"), ("paper.pdf", b"%PDF-1.7"),
                   ("run.exe", b"MZ"), ("paper.bbl", b"bibliography"),
                   ("style.sty", b"style"), ("notes.txt", b"notes")]
        result = self.extract(tar_bytes(entries))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["files"], ["main.tex", "refs/library.bib", "sections/method.tex"])
        self.assertEqual((self.destination / "main.tex").read_bytes(), TEX)
        self.assertEqual((self.destination / "sections/method.tex").read_bytes(), b"Method \xff\r\n")
        self.assertEqual((self.destination / "refs/library.bib").read_bytes(), BIB)
        self.assertEqual(sorted(p.relative_to(self.destination).as_posix()
                                for p in self.destination.rglob("*") if p.is_file()), result["files"])
        self.assertFalse((self.destination / "figures").exists())

    def test_plain_tar_is_detected_without_filename_extension(self):
        result = self.extract(tar_bytes([("paper.tex", TEX)], mode="w"))
        self.assertEqual(result["status"], "success")
        self.assertEqual((self.destination / "paper.tex").read_bytes(), TEX)

    def test_zip_keeps_relative_paths_and_excludes_other_files(self):
        result = self.extract(zip_bytes([
            ("paper/main.tex", TEX), ("paper/ref.bib", BIB),
            ("paper/fig.png", b"PNG"), ("paper/run.sh", b"#!/bin/sh")]))
        self.assertEqual(result["files"], ["paper/main.tex", "paper/ref.bib"])
        self.assertEqual((self.destination / "paper/main.tex").read_bytes(), TEX)
        self.assertFalse((self.destination / "paper/fig.png").exists())

    def test_gzip_single_tex_uses_main_tex_without_decoding(self):
        result = self.extract(gzip.compress(TEX))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["files"], ["main.tex"])
        self.assertEqual((self.destination / "main.tex").read_bytes(), TEX)

    def test_plain_single_tex_preserves_bytes(self):
        result = self.extract(TEX)
        self.assertEqual(result["status"], "success")
        self.assertEqual((self.destination / "main.tex").read_bytes(), TEX)

    def test_plain_tex_with_bye_is_recognized(self):
        body = b"\\font\\titlefont=cmr12\nHello\\bye\n"
        self.assertEqual(self.extract(body)["status"], "success")
        self.assertEqual((self.destination / "main.tex").read_bytes(), body)

    def test_case_of_extensions_and_unicode_names_are_preserved(self):
        result = self.extract(zip_bytes([("bài viết/Main.TEX", TEX), ("bài viết/Refs.BIB", BIB)]))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["files"], ["bài viết/Main.TEX", "bài viết/Refs.BIB"])

    def test_valid_archives_without_tex_are_no_tex_source(self):
        bodies = [tar_bytes([("fig.png", b"PNG")]), tar_bytes([]), zip_bytes([]),
                  zip_bytes([("word/document.xml", b"<document/>")])]
        for body in bodies:
            with self.subTest(body_size=len(body)):
                result = self.extract(body)
                self.assertEqual(result["status"], "no_tex_source", result)
                self.assertEqual(result["files"], [])
                self.assertIsNone(result["error"])
                self.assertFalse(self.destination.exists())

    def test_bib_only_archive_keeps_bib_but_reports_no_tex(self):
        result = self.extract(tar_bytes([("references.bib", BIB)]))
        self.assertEqual(result["status"], "no_tex_source")
        self.assertEqual(result["files"], ["references.bib"])
        self.assertEqual((self.destination / "references.bib").read_bytes(), BIB)

    def test_pdf_postscript_and_gzipped_pdf_are_no_tex_source(self):
        for body in (b"%PDF-1.7\nexample", b"%!PS-Adobe-3.0\nexample", gzip.compress(b"%PDF-1.7\nexample")):
            with self.subTest(body=body[:10]):
                result = self.extract(body)
                self.assertEqual(result["status"], "no_tex_source")
                self.assertEqual(result["files"], [])
                self.assertFalse(self.destination.exists())

    def test_empty_or_html_xml_responses_are_not_no_tex(self):
        for body in (b"", b"  <!DOCTYPE html><html>Error</html>",
                     b"<html>\\documentclass{example}</html>", b"<?xml version='1.0'?><error/>"):
            with self.subTest(body=body):
                self.assert_rejected(body, "invalid_response")

    def test_unknown_and_legacy_compression_require_verification(self):
        for body in (b"Unknown format", b"\x1f\x9dlegacy-compress", b"\x7fELFbinary"):
            with self.subTest(body=body):
                self.assert_rejected(body, "unsupported_source")

    def test_corrupt_and_truncated_gzip_are_archive_errors(self):
        corrupt_crc = bytearray(gzip.compress(TEX))
        corrupt_crc[-8] ^= 255
        for body in (gzip.compress(TEX)[:-7], bytes(corrupt_crc), b"\x1f\x8bnot-gzip"):
            with self.subTest(body_size=len(body)):
                self.assert_rejected(body, "archive_error")

    def test_truncated_tar_member_including_ignored_file_is_archive_error(self):
        for filename in ("main.tex", "image.png"):
            with self.subTest(filename=filename):
                self.assert_rejected(tar_bytes([(filename, b"x" * 2048)], "w")[:700], "archive_error")

    def test_bad_tar_checksum_is_archive_error(self):
        body = bytearray(tar_bytes([("main.tex", TEX)], "w"))
        body[0] ^= 1
        self.assert_rejected(body, "archive_error")

    def test_truncated_zip_is_archive_error(self):
        self.assert_rejected(zip_bytes([("main.tex", TEX)])[:-20], "archive_error")

    def test_crc_error_in_ignored_zip_file_does_not_publish_partial_output(self):
        body = zip_bytes([("main.tex", TEX), ("image.png", b"unique-image-bytes")], zipfile.ZIP_STORED)
        self.assert_rejected(body.replace(b"unique-image-bytes", b"broken-image-bytes"), "archive_error")

    def test_wrong_size_in_excluded_zip_member_is_archive_error(self):
        body = bytearray(zip_bytes([("image.png", b"image")], zipfile.ZIP_STORED))
        central = body.index(b"PK\x01\x02")
        body[central + 24:central + 28] = (100).to_bytes(4, "little")
        self.assert_rejected(body, "archive_error")

    def test_encrypted_zip_requires_verification(self):
        body = bytearray(zip_bytes([("main.tex", TEX)]))
        central = body.index(b"PK\x01\x02")
        body[6] |= 1
        body[central + 8] |= 1
        self.assert_rejected(body, "unsupported_source")

    def test_dangerous_paths_are_rejected_for_tar_and_zip(self):
        names = ["../outside.tex", "sub/../../outside.tex", "/outside.tex", "//server/a.tex",
                 "C:/outside.tex", "C:outside.tex", "sub\\outside.tex", "file.tex:stream",
                 "NUL.tex", "COM1/file.tex", "CONIN$.tex", "COM1 .tex",
                 "sub./file.tex", "sub /file.tex", "bad\n.tex"]
        for builder in (tar_bytes, zip_bytes):
            for name in names:
                with self.subTest(builder=builder.__name__, name=name):
                    self.assert_rejected(builder([("main.tex", TEX), (name, TEX)]), "unsafe_archive")

    def test_tar_symlinks_hardlinks_and_devices_are_refused(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE,
                     tarfile.CHRTYPE, tarfile.BLKTYPE):
            with self.subTest(kind=kind):
                link = tarfile.TarInfo("linked.tex")
                link.type = kind
                link.linkname = "../../outside.tex"
                self.assert_rejected(tar_bytes([("main.tex", TEX), (link, b"")]), "unsafe_archive")

    def test_symlink_directory_in_tar_cannot_redirect_later_file(self):
        link = tarfile.TarInfo("sections")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../"
        self.assert_rejected(tar_bytes([(link, b""), ("sections/outside.tex", TEX)]), "unsafe_archive")

    def test_zip_symlinks_and_special_files_are_refused(self):
        for kind in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR):
            with self.subTest(kind=kind):
                link = zipfile.ZipInfo("link.tex")
                link.create_system = 3
                link.external_attr = (kind | 0o777) << 16
                self.assert_rejected(zip_bytes([(link, b"../../outside.tex")]), "unsafe_archive")

    def test_zip_null_byte_in_original_path_is_refused(self):
        self.assert_rejected(zip_bytes([("main.tex\x00hidden", TEX)]), "unsafe_archive")

    def test_duplicate_case_and_parent_path_conflicts_are_refused(self):
        conflicts = [("main.tex", "main.tex"), ("Main.tex", "main.tex"),
                     ("Part/a.tex", "part/b.tex"), ("part.tex", "part.tex/a.tex")]
        for builder in (tar_bytes, zip_bytes):
            for first, second in conflicts:
                with self.subTest(builder=builder.__name__, paths=(first, second)):
                    self.assert_rejected(builder([(first, TEX), (second, TEX)]), "unsafe_archive")

    def test_existing_file_or_directory_is_never_overwritten(self):
        self.destination.mkdir()
        existing = self.destination / "keep.txt"
        existing.write_bytes(b"original")
        result = self.extract(tar_bytes([("main.tex", TEX)]))
        self.assertEqual(result["status"], "output_exists")
        self.assertEqual(existing.read_bytes(), b"original")
        file_result = source.extract_source(self.payload, existing)
        self.assertEqual(file_result["status"], "output_exists")
        self.assertEqual(existing.read_bytes(), b"original")

    def test_missing_input_is_storage_error(self):
        result = source.extract_source(self.payload, self.destination)
        self.assertEqual(result["status"], "storage_error")
        self.assertFalse(self.destination.exists())

    def test_extraction_size_limits_include_excluded_files(self):
        for builder in (tar_bytes, zip_bytes):
            with self.subTest(builder=builder.__name__):
                body = builder([("main.tex", TEX), ("image.png", b"x" * 1000)])
                with patch.object(source, "MAX_UNPACKED_BYTES", 500):
                    self.assert_rejected(body, "size_limit")

    def test_gzip_expansion_is_bounded(self):
        with patch.object(source, "MAX_UNPACKED_BYTES", 100):
            self.assert_rejected(gzip.compress(b"x" * 10000), "size_limit")

    def test_member_count_is_bounded(self):
        for builder in (tar_bytes, zip_bytes):
            with self.subTest(builder=builder.__name__):
                with patch.object(source, "MAX_MEMBERS", 1):
                    self.assert_rejected(builder([("a.tex", TEX), ("b.tex", TEX)]), "size_limit")


def headers(**values):
    result = Message()
    for name, value in values.items():
        result[name.replace("_", "-")] = str(value)
    return result


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def wall_time(self):
        return 1700000000.0 + self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeResponse(io.BytesIO):
    def __init__(self, body, content_type="application/octet-stream", length=None,
                 read_error=None, duration=0):
        super().__init__(body)
        self.status = 200
        self.headers = headers(Content_Type=content_type)
        if length is not None:
            self.headers["Content-Length"] = str(length)
        self.read_error = read_error
        self.duration = duration
        self.clock = None
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        self.clock.now += self.duration
        self.duration = 0
        if self.read_error is not None and self.tell() > 0:
            raise self.read_error
        return super().read(size)


class SourceDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.destination = Path(self.temp.name) / "version"
        self.clock = FakeClock()
        self.outcomes = []
        self.starts = []
        self.responses = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(client, "_next_request_at", 0.0))
        self.stack.enter_context(patch.object(client.time, "monotonic", self.clock.monotonic))
        self.stack.enter_context(patch.object(client.time, "time", self.clock.wall_time))
        self.sleep = self.stack.enter_context(patch.object(client.time, "sleep", self.clock.sleep))
        self.jitter = self.stack.enter_context(patch.object(client.random, "uniform", return_value=0.0))
        self.opener = Mock()
        self.opener.open.side_effect = self.open_response
        self.stack.enter_context(patch.object(client, "_opener", self.opener))

    def open_response(self, request, timeout):
        self.assertTrue(client._request_lock.locked())
        self.assertTrue(all(response.closed for response in self.responses))
        self.starts.append(self.clock.now)
        self.assertTrue(self.outcomes, "Unexpected HTTP request")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        outcome.clock = self.clock
        self.responses.append(outcome)
        return outcome

    def download(self, **options):
        result = source.download_source(MSSV, VERSION_ID, self.destination, **options)
        self.assertEqual(result["mssv"], MSSV)
        self.assertEqual(result["arxiv_id"], "2502.09992")
        self.assertEqual(result["versioned_id"], VERSION_ID)
        self.assertFalse(client._request_lock.locked())
        return result

    def error(self, code, retry_after=None):
        values = {} if retry_after is None else {"Retry_After": retry_after}
        return HTTPError(source.SOURCE_URL, code, "Test error", headers(**values), io.BytesIO())

    def api_response(self):
        body = (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
                b'<id>http://arxiv.org/abs/2502.09992v3</id></entry></feed>')
        return FakeResponse(body, content_type="application/atom+xml")

    def test_download_streams_versioned_endpoint_and_preserves_mapping(self):
        body = tar_bytes([("main.tex", TEX), ("refs.bib", BIB)])
        self.outcomes = [FakeResponse(body, content_type="application/gzip", length=len(body))]
        with patch.object(source, "CHUNK_SIZE", 16):
            result = self.download(timeout=15, max_attempts=1)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://export.arxiv.org/src/2502.09992v1")
        self.assertEqual(request.get_header("User-agent"), client.USER_AGENT)
        self.assertEqual(request.get_header("Connection"), "close")
        self.assertEqual(self.opener.open.call_args.kwargs, {"timeout": 15})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["content_type"], "application/gzip")
        self.assertEqual(result["downloaded_bytes"], len(body))
        self.assertEqual(result["attempts"], 1)
        self.assertGreater(len(self.responses[0].read_sizes), 1)
        self.assertTrue(all(0 < size <= 16 for size in self.responses[0].read_sizes))
        self.assertTrue(self.responses[0].closed)
        self.assertEqual((self.destination / "main.tex").read_bytes(), TEX)

    def test_legacy_id_and_mssv_leading_zeros_are_kept(self):
        self.outcomes = [FakeResponse(gzip.compress(TEX))]
        result = source.download_source("00120010", "hep-th/9901001v2", self.destination)
        self.assertEqual(result["mssv"], "00120010")
        self.assertEqual(result["arxiv_id"], "hep-th/9901001")
        self.assertEqual(result["versioned_id"], "hep-th/9901001v2")
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.opener.open.call_args.args[0].full_url,
                         "https://export.arxiv.org/src/hep-th/9901001v2")

    def test_api_and_source_share_pacing_in_both_directions(self):
        self.outcomes = [self.api_response(), FakeResponse(TEX), self.api_response()]
        self.assertEqual(client.query_arxiv(MSSV, "2502.09992")["status"], "found")
        self.assertEqual(self.download()["status"], "success")
        self.assertEqual(client.query_arxiv(MSSV, "2502.09992")["status"], "found")
        self.assertEqual(self.starts, [0.0, 3.0, 6.0])

    def test_download_connection_closes_before_api_cooldown(self):
        self.outcomes = [FakeResponse(TEX, duration=5), self.api_response()]
        self.download()
        client.query_arxiv(MSSV, "2502.09992")
        self.assertEqual(self.starts, [0.0, 8.0])

    def test_retry_after_is_shared_from_source_to_api(self):
        self.outcomes = [self.error(429, "15"), self.api_response()]
        self.assertEqual(self.download(max_attempts=1)["status"], "rate_limited")
        client.query_arxiv(MSSV, "2502.09992")
        self.assertEqual(self.starts, [0.0, 15.0])

    def test_retry_after_is_shared_from_api_to_source(self):
        self.outcomes = [self.error(429, "12"), FakeResponse(TEX)]
        client.query_arxiv(MSSV, "2502.09992", max_attempts=1)
        self.assertEqual(self.download()["status"], "success")
        self.assertEqual(self.starts, [0.0, 12.0])

    def test_source_retry_after_http_date(self):
        self.outcomes = [self.error(503, formatdate(self.clock.wall_time() + 20, usegmt=True)), FakeResponse(TEX)]
        result = self.download()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(self.starts, [0.0, 20.0])

    def test_retry_exhaustion_uses_backoff_and_jitter(self):
        self.outcomes = [self.error(503) for _ in range(3)]
        self.jitter.return_value = 0.5
        result = self.download()
        self.assertEqual(result["status"], "server_error")
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(self.starts, [0.0, 3.5, 10.0])
        self.assertFalse(self.destination.exists())

    def test_http_errors_and_redirects_are_not_no_tex_or_retried(self):
        for code in (204, 206, 301, 302, 400, 401, 403, 404, 408):
            with self.subTest(code=code):
                error = self.error(code)
                stream = error.fp
                self.outcomes = [error]
                result = self.download()
                self.assertEqual(result["status"], "http_error")
                self.assertEqual(result["http_status"], code)
                self.assertEqual(result["attempts"], 1)
                self.assertTrue(stream.closed)
                self.assertFalse(self.destination.exists())

    def test_timeout_dns_connection_and_tls_errors_are_distinct(self):
        cases = [(URLError(TimeoutError("timeout")), "timeout"),
                 (URLError(socket.gaierror(-2, "DNS failed")), "dns_error"),
                 (ConnectionResetError("reset"), "connection_error"),
                 (URLError(ssl.SSLCertVerificationError(1, "certificate failed")), "tls_error")]
        for error, status in cases:
            with self.subTest(status=status):
                self.outcomes = [error]
                result = self.download(max_attempts=1)
                self.assertEqual(result["status"], status)
                self.assertIsNone(result["http_status"])
                self.assertEqual(result["attempts"], 1)
                self.assertFalse(self.destination.exists())

    def test_interrupted_download_is_retried_from_zero(self):
        self.outcomes = [FakeResponse(TEX, read_error=TimeoutError("interrupted")), FakeResponse(TEX)]
        with patch.object(source, "CHUNK_SIZE", 20):
            result = self.download()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual((self.destination / "main.tex").read_bytes(), TEX)
        self.assertTrue(all(response.closed for response in self.responses))
        self.assertEqual(self.starts, [0.0, 3.0])

    def test_incomplete_content_length_is_connection_error(self):
        self.outcomes = [FakeResponse(TEX, length=len(TEX) + 100)]
        result = self.download(max_attempts=1)
        self.assertEqual(result["status"], "connection_error")
        self.assertEqual(result["http_status"], 200)
        self.assertFalse(self.destination.exists())

    def test_failed_retry_does_not_reuse_headers_from_earlier_response(self):
        self.outcomes = [FakeResponse(TEX, length=len(TEX) + 10), self.error(503)]
        result = self.download(max_attempts=2)
        self.assertEqual(result["status"], "server_error")
        self.assertEqual(result["http_status"], 503)
        self.assertIsNone(result["content_type"])
        self.assertEqual(result["attempts"], 2)
        self.assertFalse(self.destination.exists())

    def test_invalid_or_exceeded_content_length_is_invalid_response(self):
        for length in ("bad", "-1", len(TEX) - 1):
            with self.subTest(length=length):
                self.outcomes = [FakeResponse(TEX, length=length)]
                result = self.download()
                self.assertEqual(result["status"], "invalid_response")
                self.assertEqual(result["attempts"], 1)
                self.assertFalse(self.destination.exists())

    def test_download_size_limit_with_or_without_content_length(self):
        for length in (None, len(TEX)):
            with self.subTest(length=length):
                self.outcomes = [FakeResponse(TEX, length=length)]
                with patch.object(source, "MAX_DOWNLOAD_BYTES", 20):
                    result = self.download()
                self.assertEqual(result["status"], "size_limit")
                self.assertEqual(result["attempts"], 1)
                self.assertTrue(self.responses[-1].closed)
                self.assertFalse(self.destination.exists())

    def test_http_200_corrupt_archive_is_not_no_tex_or_retried(self):
        self.outcomes = [FakeResponse(b"\x1f\x8bcorrupt")]
        result = self.download()
        self.assertEqual(result["status"], "archive_error")
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["attempts"], 1)
        self.assertFalse(self.destination.exists())

    def test_http_200_pdf_is_no_tex_source_with_version_record(self):
        self.outcomes = [FakeResponse(b"%PDF-1.7\nexample", content_type="application/pdf")]
        result = self.download()
        self.assertEqual(result["status"], "no_tex_source")
        self.assertEqual(result["files"], [])
        self.assertIsNone(result["error"])
        self.assertEqual(result["attempts"], 1)
        self.assertFalse(self.destination.exists())

    def test_html_empty_and_unknown_responses_are_not_no_tex(self):
        for body, status in ((b"<html>Service unavailable</html>", "invalid_response"),
                             (b"", "invalid_response"), (b"Unknown source", "unsupported_source")):
            with self.subTest(body=body):
                self.outcomes = [FakeResponse(body)]
                result = self.download()
                self.assertEqual(result["status"], status)
                self.assertEqual(result["attempts"], 1)
                self.assertFalse(self.destination.exists())

    def test_archive_detection_uses_bytes_instead_of_mime_or_suffix(self):
        self.outcomes = [FakeResponse(tar_bytes([("main.tex", TEX)]), content_type="application/octet-stream")]
        self.assertEqual(self.download()["status"], "success")

    def test_html_or_xml_content_type_is_rejected_even_with_tex_in_body(self):
        for content_type in ("text/html; charset=utf-8", "application/xml", "application/atom+xml"):
            with self.subTest(content_type=content_type):
                self.outcomes = [FakeResponse(TEX, content_type=content_type)]
                result = self.download()
                self.assertEqual(result["status"], "invalid_response")
                self.assertEqual(result["attempts"], 1)
                self.assertEqual(self.responses[-1].read_sizes, [])
                self.assertFalse(self.destination.exists())

    def test_existing_output_prevents_http_and_preserves_data(self):
        self.destination.mkdir()
        existing = self.destination / "main.tex"
        existing.write_bytes(b"original")
        result = self.download()
        self.assertEqual(result["status"], "output_exists")
        self.assertEqual(result["attempts"], 0)
        self.assertEqual(existing.read_bytes(), b"original")
        self.opener.open.assert_not_called()

    def test_storage_failure_is_not_retried_as_network_error(self):
        self.outcomes = [FakeResponse(TEX)]
        with patch.object(source, "_save_member", side_effect=OSError("disk full")):
            result = self.download()
        self.assertEqual(result["status"], "storage_error")
        self.assertEqual(result["attempts"], 1)
        self.assertIn("disk full", result["error"])
        self.assertFalse(self.destination.exists())

    def test_local_download_file_error_is_storage_error_without_retry(self):
        self.outcomes = [FakeResponse(TEX)]
        with patch.object(Path, "open", side_effect=PermissionError("cannot write temporary source")):
            result = self.download()
        self.assertEqual(result["status"], "storage_error")
        self.assertEqual(result["attempts"], 1)
        self.assertFalse(self.destination.exists())

    def test_invalid_arguments_never_send_http(self):
        for mssv, identifier in ((24120232, VERSION_ID), ("", VERSION_ID),
                                 (MSSV, "2502.09992"), (MSSV, "2502.09992v0"),
                                 (MSSV, "../2502.09992v1"), (MSSV, "2502.09992v1?x=1")):
            with self.subTest(mssv=mssv, identifier=identifier), self.assertRaises(ValueError):
                source.download_source(mssv, identifier, self.destination)
        for options in ({"timeout": 0}, {"timeout": float("nan")}, {"max_attempts": 0},
                        {"max_attempts": True}, {"user_agent": "Bad\nAgent"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.download(**options)
        self.opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
