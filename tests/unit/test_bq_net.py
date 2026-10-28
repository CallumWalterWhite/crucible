"""NET.REG_DOMAIN: the Public Suffix List algorithm and its SQL entry point."""

from __future__ import annotations

import pytest

from crucible.dialects.bigquery import net
from crucible.engine import CrucibleUnsupportedError


@pytest.mark.parametrize(
    ("url", "domain"),
    [
        ("mail.example.co.uk", "example.co.uk"),
        ("foo.us.com", "us.com"),  # private section: not a suffix
        ("co.uk", None),  # only a suffix
        ("a.b", None),  # no implicit `*` rule
        ("a.b.ck", "a.b.ck"),  # *.ck
        ("a.b.www.ck", "www.ck"),  # !www.ck beats *.ck
        ("www.city.kawasaki.jp", "city.kawasaki.jp"),
        ("例子.卷筒纸.中国", "卷筒纸.中国"),
        ("shop.xn--fiqs8s", "shop.xn--fiqs8s"),  # Punycode of 中国
        ("shop.XN--FIQS8S", "shop.XN--FIQS8S"),
        ("https://u:p@Www.Example.COM:80/p?q#f", "Example.COM"),
        ("mailto:?to=", None),
        ("https://[::1]:80", None),
        ("a..b.com", None),
        ("", None),
    ],
)
def test_reg_domain(url, domain):
    assert net.reg_domain(url) == domain


def test_only_the_icann_section_is_loaded():
    normal, wildcard, exception = net._rules()  # pyright: ignore[reportPrivateUsage]
    assert "co.uk" in normal
    assert "us.com" not in normal
    assert "blogspot.com" not in normal
    assert "ck" in wildcard
    assert "www.ck" in exception


@pytest.mark.parametrize("url", ["example.com.", ".example.com", "my_host.example.com", "a b.com"])
def test_undocumented_hosts_are_unsupported(url):
    with pytest.raises(CrucibleUnsupportedError, match=r"NET\.REG_DOMAIN"):
        net.reg_domain(url)


def test_an_unsupported_row_fails_the_query_as_unsupported(bq):
    with pytest.raises(CrucibleUnsupportedError) as info:
        bq("SELECT NET.REG_DOMAIN(h) FROM UNNEST(['a.com', 'b.com.']) AS h")
    assert info.value.message == (
        "Not supported by Crucible: NET.REG_DOMAIN of a host with a leading or trailing dot"
    )


def test_other_net_functions_are_unsupported(bq):
    with pytest.raises(CrucibleUnsupportedError, match=r"NET\.HOST"):
        bq("SELECT NET.HOST('http://a.com')")
