"""Check that the deployed nginx client-IP trust list matches Cloudflare."""

import ipaddress
import json
import pathlib
import urllib.request


CONFIG = pathlib.Path(__file__).resolve().parents[1] / "deploy/nginx/wikihub-cloudflare-realip.conf"


def test_cloudflare_realip_config():
    text = CONFIG.read_text()
    assert "real_ip_header CF-Connecting-IP;" in text
    assert "real_ip_recursive off;" in text

    configured = {
        ipaddress.ip_network(line.split()[1].rstrip(";"))
        for line in text.splitlines()
        if line.startswith("set_real_ip_from ")
    }
    assert configured
    assert ipaddress.ip_network("0.0.0.0/0") not in configured
    assert ipaddress.ip_network("::/0") not in configured

    request = urllib.request.Request(
        "https://api.cloudflare.com/client/v4/ips", headers={"User-Agent": "wikihub-config-check"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        published = json.load(response)["result"]
    expected = {
        ipaddress.ip_network(value)
        for value in published["ipv4_cidrs"] + published["ipv6_cidrs"]
    }
    assert configured == expected


if __name__ == "__main__":
    test_cloudflare_realip_config()
    print("Cloudflare real-IP trust list matches published ranges")
