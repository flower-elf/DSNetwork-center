# 出口 IP 的国家与地址类型查询，数据来自 ip-api.com。
import json
import urllib.parse
import urllib.request

# 请求 message 字段，让私有地址、保留地址与限流等拒绝原因出现在响应里，
# 避免只剩一个失败状态。
GEO_ENDPOINT = ("http://ip-api.com/json/%s?lang=zh-CN"
                "&fields=status,message,country,countryCode,isp,org,as,"
                "hosting,proxy,mobile")
LOOKUP_TIMEOUT = 8

RESIDENTIAL = "residential"
DATACENTER = "datacenter"

TYPE_LABELS = {RESIDENTIAL: "住宅", DATACENTER: "机房"}

# 槽位保存的出口字段；任一字段可以为空，空表示未知，不做假设。
FIELDS = ("country", "country_code", "ip_type", "isp", "asn")


def type_label(ip_type):
    return TYPE_LABELS.get(str(ip_type or "").strip().lower(), "")


def empty():
    """返回未知出口的空结果，字段形状与成功查询一致。"""
    return dict.fromkeys(FIELDS, "")


def classify(payload):
    """把一条 ip-api.com 响应转换成槽位字段。

    失败状态、缺少正文或 hosting 既非真也非假时返回空字段，不做猜测。
    """
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return empty()
    hosting = payload.get("hosting")
    if hosting is True:
        ip_type = DATACENTER
    elif hosting is False:
        ip_type = RESIDENTIAL
    else:
        ip_type = ""
    return {
        "country": str(payload.get("country") or "").strip(),
        "country_code": str(payload.get("countryCode") or "").strip().upper(),
        "ip_type": ip_type,
        "isp": str(payload.get("isp") or payload.get("org") or "").strip(),
        "asn": str(payload.get("as") or "").strip(),
    }


def _open(request, timeout):
    """测试可替换此函数，避免真实网络。"""
    return urllib.request.urlopen(request, timeout=timeout)


def lookup(ip, timeout=LOOKUP_TIMEOUT):
    """查询一个出口 IP 的国家与地址类型，未知时字段为空。

    请求直接发出，不经账号配置的代理：代理只放行上游时查询仍要可用，也不能
    由待查出口回答这次查询。
    """
    address = str(ip or "").strip()
    if not address:
        return empty()
    url = GEO_ENDPOINT % urllib.parse.quote(address, safe="")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "wb-proxy"})
        with _open(request, timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception:
        return empty()
    return classify(payload)


def slot_name(country, ip_type):
    """返回默认槽位名，由出口国家与类型组成，例如「美国 住宅」。

    没有国家就没有名字。只有「住宅」无法区分两个槽位，调用方应回退到槽位 id。
    """
    where = str(country or "").strip()
    if not where:
        return ""
    kind = type_label(ip_type)
    return (where + " " + kind) if kind else where
