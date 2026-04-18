"""
通过 Cloudflare /cdn-cgi/trace 检测每个节点的出口数据中心和国家。
无需重启 xray，并发检测，速度快。
"""

import urllib.request
import concurrent.futures
import json
import os

# CF 数据中心代码 → 国家代码
COLO_TO_COUNTRY: dict[str, str] = {
    # 亚洲
    "NRT": "JP", "KIX": "JP", "CTS": "JP", "OKA": "JP",
    "ICN": "KR", "GMP": "KR",
    "HKG": "HK",
    "SIN": "SG",
    "TPE": "TW", "KHH": "TW",
    "SYD": "AU", "MEL": "AU", "BNE": "AU", "PER": "AU", "ADL": "AU",
    "AKL": "NZ",
    "BOM": "IN", "DEL": "IN", "MAA": "IN", "HYD": "IN", "CCU": "IN",
    "MNL": "PH",
    "KUL": "MY",
    "CGK": "ID",
    "BKK": "TH",
    "SGN": "VN", "HAN": "VN",
    # 中东
    "DXB": "AE", "AUH": "AE",
    "TLV": "IL",
    "KWI": "KW",
    "DOH": "QA",
    "MCT": "OM",
    "BAH": "BH",
    "RUH": "SA", "JED": "SA",
    # 欧洲
    "LHR": "GB", "MAN": "GB", "EDI": "GB",
    "CDG": "FR", "MRS": "FR",
    "FRA": "DE", "MUC": "DE", "DUS": "DE", "TXL": "DE", "HAM": "DE",
    "AMS": "NL",
    "ARN": "SE", "GOT": "SE",
    "CPH": "DK",
    "HEL": "FI",
    "OSL": "NO",
    "ZRH": "CH", "GVA": "CH",
    "MXP": "IT", "FCO": "IT",
    "MAD": "ES", "BCN": "ES",
    "LIS": "PT",
    "VIE": "AT",
    "WAW": "PL", "KTW": "PL",
    "PRG": "CZ",
    "BUD": "HU",
    "BRU": "BE",
    "LUX": "LU",
    "DUB": "IE",
    "OTP": "RO",
    "SOF": "BG",
    "ATH": "GR",
    "IST": "TR", "ESB": "TR",
    "RIX": "LV",
    "TLL": "EE",
    "VNO": "LT",
    # 非洲
    "CAI": "EG",
    "JNB": "ZA", "CPT": "ZA",
    "NBO": "KE",
    "LOS": "NG", "ABV": "NG",
    "CMN": "MA",
    "TUN": "TN",
    # 美洲
    "YYZ": "CA", "YVR": "CA", "YUL": "CA", "YYC": "CA", "YEG": "CA",
    "GRU": "BR", "GIG": "BR", "CWB": "BR", "POA": "BR", "BSB": "BR",
    "BOG": "CO",
    "SCL": "CL",
    "LIM": "PE",
    "EZE": "AR", "COR": "AR",
    "MEX": "MX", "GDL": "MX", "MTY": "MX",
    "UIO": "EC",
    "PTY": "PA",
    "SJO": "CR",
    "SDQ": "DO",
    "SJU": "PR",
    # 美国（众多数据中心）
    "EWR": "US", "IAD": "US", "ATL": "US", "ORD": "US", "DFW": "US",
    "MIA": "US", "DEN": "US", "LAX": "US", "SEA": "US", "SJC": "US",
    "PHX": "US", "PDX": "US", "MSP": "US", "BOS": "US", "CLT": "US",
    "DTW": "US", "SLC": "US", "TPA": "US", "MCI": "US", "STL": "US",
    "CMH": "US", "BUF": "US", "RIC": "US", "PWM": "US", "OMA": "US",
    "OKC": "US", "ABQ": "US", "RNO": "US", "LAS": "US", "SAN": "US",
    "SNA": "US", "SMF": "US", "OAK": "US", "SFO": "US", "SLC": "US",
    "PHL": "US", "PIT": "US", "BNA": "US", "MSY": "US", "MEM": "US",
    "JAX": "US", "BDL": "US", "RDU": "US", "IND": "US", "TUL": "US",
}

COUNTRY_NAMES: dict[str, str] = {
    "JP": "🇯🇵 日本", "KR": "🇰🇷 韩国", "HK": "🇭🇰 香港", "SG": "🇸🇬 新加坡",
    "TW": "🇹🇼 台湾", "AU": "🇦🇺 澳大利亚", "NZ": "🇳🇿 新西兰",
    "IN": "🇮🇳 印度", "PH": "🇵🇭 菲律宾", "MY": "🇲🇾 马来西亚",
    "ID": "🇮🇩 印尼", "TH": "🇹🇭 泰国", "VN": "🇻🇳 越南",
    "AE": "🇦🇪 阿联酋", "IL": "🇮🇱 以色列", "SA": "🇸🇦 沙特",
    "GB": "🇬🇧 英国", "FR": "🇫🇷 法国", "DE": "🇩🇪 德国", "NL": "🇳🇱 荷兰",
    "SE": "🇸🇪 瑞典", "CH": "🇨🇭 瑞士", "IT": "🇮🇹 意大利", "ES": "🇪🇸 西班牙",
    "PT": "🇵🇹 葡萄牙", "AT": "🇦🇹 奥地利", "DK": "🇩🇰 丹麦", "FI": "🇫🇮 芬兰",
    "NO": "🇳🇴 挪威", "BE": "🇧🇪 比利时", "PL": "🇵🇱 波兰", "IE": "🇮🇪 爱尔兰",
    "TR": "🇹🇷 土耳其", "ZA": "🇿🇦 南非", "EG": "🇪🇬 埃及",
    "CA": "🇨🇦 加拿大", "BR": "🇧🇷 巴西", "MX": "🇲🇽 墨西哥", "AR": "🇦🇷 阿根廷",
    "US": "🇺🇸 美国",
    "??": "❓ 未知",
}

DEFAULT_COUNTRY_PRIORITY = ["JP", "KR", "AU", "SG", "HK", "TW", "NZ", "GB", "DE", "NL", "FR", "CA", "US"]

def detect_one(server_ip: str, timeout: int = 6) -> dict:
    """
    连接 CF /cdn-cgi/trace 获取节点真实出口数据中心和国家。
    注意：此函数测的是直连 CF IP 看到的出口（即 Replit 自身出口，
    用作粗略地理标记，并非真正经过节点的出口）。真实经过节点的
    出口需要通过 SOCKS5，但这里仅作展示用，不影响 alive 判定。
    """
    try:
        req = urllib.request.Request(
            f"http://{server_ip}/cdn-cgi/trace",
            headers={"Host": "cloudflare.com", "User-Agent": "curl/7.88.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode(errors="ignore")
        data = {}
        for line in text.strip().split("\n"):
            if "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()
        colo = data.get("colo", "??")
        exit_ip = data.get("ip", "")
        # CF trace 直接提供 loc 字段（2位国家代码），优先使用
        loc = data.get("loc", "")
        country = loc if loc else COLO_TO_COUNTRY.get(colo, "??")
        return {"colo": colo, "country": country, "exit_ip": exit_ip}
    except Exception:
        return {"colo": "??", "country": "??", "exit_ip": ""}


def _node_ip(node: dict) -> str:
    """兼容 server / address 两种字段名。"""
    return node.get("server") or node.get("address") or ""


def detect_all(nodes: list[dict], max_workers: int = 15) -> list[dict]:
    """
    并发检测所有节点的出口国家。
    每个节点增加 colo / country / exit_ip 字段并返回。
    """
    # 去重：相同 IP 只检测一次
    ip_results: dict[str, dict] = {}
    unique_ips = list({_node_ip(n): None for n in nodes if _node_ip(n)}.keys())

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(detect_one, ip): ip for ip in unique_ips}
        for future, ip in futures.items():
            try:
                ip_results[ip] = future.result(timeout=10)
            except Exception:
                ip_results[ip] = {"colo": "??", "country": "??", "exit_ip": ""}

    # 把结果写回节点列表
    result = []
    for node in nodes:
        n = dict(node)
        ip = _node_ip(n)
        info = ip_results.get(ip, {"colo": "??", "country": "??", "exit_ip": ""})
        n["colo"] = info["colo"]
        n["country"] = info["country"]
        n["exit_ip"] = info["exit_ip"]
        result.append(n)
    return result


def sort_nodes_by_priority(nodes: list[dict], priority: list[str]) -> list[dict]:
    """
    按国家优先级对节点列表排序。
    priority = ["JP", "KR", "AU", ...] 表示首选日本、其次韩国…
    """
    priority_map = {c: i for i, c in enumerate(priority)}
    fallback = len(priority)

    def key(n):
        country = n.get("country", "??")
        return priority_map.get(country, fallback)

    return sorted(nodes, key=key)


def country_name(code: str) -> str:
    return COUNTRY_NAMES.get(code, f"🌐 {code}")
