from email.message import Message
from io import BytesIO

from src.arxiv_version_page import verify_version_withdrawal


class Response(BytesIO):
    def __init__(self, body, url, content_type="text/html"):
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.url = url

    def geturl(self):
        return self.url


def run(html, *, url="https://arxiv.org/abs/2501.02724v2", content_type="text/html"):
    from src import arxiv_version_page as page

    def request(target, *, read_response, **kwargs):
        assert target == "https://arxiv.org/abs/2501.02724v2"
        result = read_response(Response(html.encode(), url, content_type))
        return {**result, "http_status": 200, "attempts": 1}

    original = page.arxiv_client._request_arxiv
    page.arxiv_client._request_arxiv = request
    try:
        return verify_version_withdrawal("2501.02724", "v2")
    finally:
        page.arxiv_client._request_arxiv = original


def test_exact_official_page_notice_is_version_scoped_and_hashed():
    result = run("<html><body><p>arXiv:2501.02724v2</p>"
                 "<p>This paper has been withdrawn by Guowei Dai</p></body></html>")
    assert result["status"] == "confirmed_withdrawn"
    assert result["versioned_id"] == "2501.02724v2"
    assert result["official_url"] == "https://arxiv.org/abs/2501.02724v2"
    assert len(result["page_sha256"]) == 64
    assert result["verification_method"] == "official_arxiv_version_page"


def test_generic_comment_or_other_version_does_not_confirm_withdrawal():
    generic = run("<html><body><p>arXiv:2501.02724v2</p>"
                  "<p>Comments: authors may withdraw this paper later.</p></body></html>")
    wrong = run("<html><body><p>arXiv:2501.02724v1</p>"
                "<p>This paper has been withdrawn by the author</p></body></html>")
    assert generic["status"] == "not_withdrawn"
    assert wrong["status"] == "invalid_response"


def test_redirect_identity_and_content_type_are_checked():
    redirected = run("<p>arXiv:2501.02724v2</p><p>This paper has been withdrawn</p>",
                     url="https://arxiv.org/abs/2501.02724")
    wrong_type = run("<p>arXiv:2501.02724v2</p>", content_type="application/json")
    assert redirected["status"] == "invalid_response"
    assert wrong_type["status"] == "invalid_response"
