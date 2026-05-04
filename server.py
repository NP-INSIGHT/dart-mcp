import io
import os
import time
import zipfile
import xml.etree.ElementTree as ET
import httpx
from mcp.server.fastmcp import FastMCP

host = os.environ.get("HOST", "0.0.0.0")
port = int(os.environ.get("PORT", 8000))
mcp = FastMCP("dart-mcp", host=host, port=port)
mcp.settings.transport_security.enable_dns_rebinding_protection = False

DART_API_KEY = os.environ.get("DART_API_KEY", "")
BASE_URL = "https://opendart.fss.or.kr/api"

_CORP_CACHE: list[dict] = []
_CORP_CACHE_TS: float = 0.0
_CORP_CACHE_TTL = 24 * 3600


async def dart_get(endpoint: str, params: dict) -> dict:
    """DART API 공통 호출 함수"""
    params["crtfc_key"] = DART_API_KEY
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{BASE_URL}/{endpoint}.json", params=params)
        r.raise_for_status()
        return r.json()


async def _load_corp_list() -> list[dict]:
    global _CORP_CACHE, _CORP_CACHE_TS
    if _CORP_CACHE and (time.time() - _CORP_CACHE_TS) < _CORP_CACHE_TTL:
        return _CORP_CACHE
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(
            f"{BASE_URL}/corpCode.xml", params={"crtfc_key": DART_API_KEY}
        )
        r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        xml_bytes = zf.read(zf.namelist()[0])
    root = ET.fromstring(xml_bytes)
    corps = []
    for node in root.findall("list"):
        corps.append({
            "corp_code": (node.findtext("corp_code") or "").strip(),
            "corp_name": (node.findtext("corp_name") or "").strip(),
            "stock_code": (node.findtext("stock_code") or "").strip(),
        })
    _CORP_CACHE = corps
    _CORP_CACHE_TS = time.time()
    return corps


@mcp.tool()
async def search_company(company_name: str) -> str:
    """
    회사명으로 DART 고유번호(corp_code) 검색.
    재무제표 조회 전 반드시 이 툴로 corp_code를 먼저 확인하세요.
    """
    corps = await _load_corp_list()
    q = company_name.strip()
    exact = [c for c in corps if c["corp_name"] == q]
    partial = [c for c in corps if q in c["corp_name"] and c not in exact]
    listed_first = (
        [c for c in exact if c["stock_code"]]
        + [c for c in exact if not c["stock_code"]]
        + [c for c in partial if c["stock_code"]]
        + [c for c in partial if not c["stock_code"]]
    )
    results = listed_first[:5]
    if not results:
        return "검색 결과 없음"

    lines = [
        "| 회사명 | corp_code | 상장여부 | 종목코드 |",
        "|--------|-----------|----------|----------|",
    ]
    for r in results:
        stock = r["stock_code"] or "-"
        listed = "상장" if r["stock_code"] else "비상장"
        lines.append(
            f"| {r['corp_name']} | {r['corp_code']} | {listed} | {stock} |"
        )
    return "\n".join(lines)


@mcp.tool()
async def get_financial_statement(
    corp_code: str,
    year: str,
    quarter: str = "11011",
) -> str:
    """
    재무제표 조회 (연결재무제표 기준).
    - corp_code : search_company로 조회한 고유번호
    - year      : 사업연도 (예: "2024")
    - quarter   : 11011=연간, 11012=반기, 11013=1분기, 11014=3분기
    """
    data = await dart_get(
        "fnlttSinglAcntAll",
        {
            "corp_code": corp_code,
            "bsns_year": year,
            "reprt_code": quarter,
            "fs_div": "CFS",
        },
    )

    if data.get("status") != "000":
        return f"오류: {data.get('message')}"

    KEY_ACCOUNTS = {"매출액", "영업이익", "당기순이익", "자산총계", "부채총계", "자본총계"}

    lines = [
        f"### {year}년 재무제표 (corp_code: {corp_code})\n",
        "| 계정명 | 당기 | 전기 |",
        "|--------|------|------|",
    ]

    for item in data.get("list", []):
        if item.get("account_nm") not in KEY_ACCOUNTS:
            continue
        try:
            c = f"{int(item.get('thstrm_amount','0').replace(',',''))//100_000_000:,}억"
            p = f"{int(item.get('frmtrm_amount','0').replace(',',''))//100_000_000:,}억"
        except Exception:
            c = item.get("thstrm_amount", "-")
            p = item.get("frmtrm_amount", "-")
        lines.append(f"| {item.get('account_nm')} | {c} | {p} |")

    return "\n".join(lines)


@mcp.tool()
async def get_recent_disclosures(corp_code: str, count: int = 10) -> str:
    """
    최근 공시 목록 조회.
    - corp_code : 회사 고유번호
    - count     : 가져올 공시 수 (기본 10, 최대 100)
    """
    data = await dart_get(
        "list",
        {
            "corp_code": corp_code,
            "page_count": count,
            "sort": "date",
            "sort_mth": "desc",
        },
    )

    if data.get("status") != "000":
        return f"오류: {data.get('message')}"

    lines = [
        "| 날짜 | 공시유형 | 제목 |",
        "|------|----------|------|",
    ]
    for item in data.get("list", []):
        lines.append(
            f"| {item.get('rcept_dt')} | {item.get('report_tp', '-')} "
            f"| {item.get('report_nm')} |"
        )
    return "\n".join(lines)


@mcp.tool()
async def get_dividend_info(corp_code: str, year: str) -> str:
    """
    배당 정보 조회.
    - corp_code : 회사 고유번호
    - year      : 사업연도 (예: "2024")
    """
    data = await dart_get(
        "alotMatter",
        {
            "corp_code": corp_code,
            "bsns_year": year,
            "reprt_code": "11011",
        },
    )

    if data.get("status") != "000":
        return f"오류: {data.get('message')}"

    items = data.get("list", [])
    if not items:
        return "배당 데이터 없음"

    lines = [
        "| 구분 | 주당배당금 | 배당수익률 | 배당성향 |",
        "|------|-----------|------------|---------|",
    ]
    for item in items:
        lines.append(
            f"| {item.get('se')} | {item.get('dps', '-')}원 "
            f"| {item.get('dvdnd_yld', '-')}% | {item.get('dvdnd_pttm_ernn', '-')}% |"
        )
    return "\n".join(lines)


@mcp.tool()
async def get_executive_info(corp_code: str) -> str:
    """
    임원 현황 조회.
    - corp_code : 회사 고유번호
    """
    data = await dart_get(
        "exctvSttus",
        {
            "corp_code": corp_code,
            "bsns_year": "2024",
            "reprt_code": "11011",
        },
    )

    if data.get("status") != "000":
        return f"오류: {data.get('message')}"

    items = data.get("list", [])
    if not items:
        return "임원 데이터 없음"

    lines = [
        "| 성명 | 직위 | 등기여부 | 상근여부 |",
        "|------|------|----------|----------|",
    ]
    for item in items[:20]:
        lines.append(
            f"| {item.get('nm', '-')} | {item.get('ofcps', '-')} "
            f"| {item.get('rgist_exctv_at', '-')} | {item.get('fte_at', '-')} |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    transport = sys.argv[1] if len(sys.argv) > 1 else "streamable-http"
    mcp.run(transport=transport)
