"""HTTP client for lakejob FastAPI backend."""

import os
from typing import Optional
import httpx

BASE_URL = os.environ.get("LAKEJOB_API", "http://127.0.0.1:8010")


def _post(path: str, json=None, timeout=120):
    try:
        resp = httpx.post(f"{BASE_URL}{path}", json=json, timeout=timeout)
    except httpx.ConnectError:
        resp = httpx.Response(503, text="Cannot connect to lakejob server. Run `lakejob server --start` first.")
    return resp


def _get(path: str, timeout=30):
    try:
        resp = httpx.get(f"{BASE_URL}{path}", timeout=timeout)
    except httpx.ConnectError:
        resp = httpx.Response(503, text="Cannot connect to lakejob server. Run `lakejob server --start` first.")
    return resp


def search(keyword: str, city: str = "", limit: int = 60):
    return _post("/api/jobs/search", {"keyword": keyword, "city": city or "", "limit": limit})


def status():
    return _get("/api/status")


def stats():
    return _get("/api/stats")


def jobs(status_filter=None, limit=50):
    q = f"?limit={limit}"
    if status_filter:
        q += f"&status={status_filter}"
    return _get(f"/api/jobs{q}")


def apply_one(job_url: str):
    return _post("/api/jobs/apply", {"job_url": job_url})


def apply_batch(job_urls: list):
    return _post("/api/jobs/apply-batch", {"job_urls": job_urls})


def scan():
    return _post("/api/jobs/scan", timeout=120)


def scan_and_apply():
    return _post("/api/jobs/scan-and-apply", timeout=300)


def conversations():
    return _get("/api/conversations")


def chat_messages(conv_id: int):
    return _get(f"/api/conversations/{conv_id}/messages")


def send_message(conv_id: int, content: str):
    return _post(f"/api/conversations/{conv_id}/send", {"content": content})


def doctor():
    return _get("/api/doctor")


def relogin():
    return _post("/api/system/relogin")


def analyze(job_url: str, title: str = "", company: str = "", desc: str = ""):
    return _post("/api/jobs/analyze", {"job_url": job_url, "job_title": title, "company": company, "description": desc})


def get_shortlists():
    return _get("/api/shortlists")


def add_shortlist(job_url: str, title: str = "", company: str = "", salary: str = "", city: str = ""):
    return _post(
        "/api/shortlists", {"job_url": job_url, "title": title, "company": company, "salary": salary, "city": city}
    )


def remove_shortlist(sid: int):
    resp = httpx.delete(f"{BASE_URL}/api/shortlists/{sid}", timeout=30)
    return resp


def company_preview(
    keyword: str = "",
    city: str = "",
    company: str = "",
    company_id: str = "",
    districts: Optional[list] = None,
    company_size: Optional[list] = None,
    timeout: int = 180,
):
    """调用 GET /api/companies/preview。
    keyword 非空 → 跨公司聚合选最热；否则按 company/company_id 走单公司模式。

    新增：
      - districts: 区 code 列表（["440118", "440113"]），逗号拼接后透传到 URL
      - company_size: scale code 列表（["302", "303"]），同上
    """
    from urllib.parse import urlencode

    params = {}
    if keyword:
        params["keyword"] = keyword
    if city:
        params["city"] = city
    if company:
        params["company"] = company
    if company_id:
        params["company_id"] = company_id
    if districts:
        params["districts"] = ",".join(str(x) for x in districts if x)
    if company_size:
        params["company_size"] = ",".join(str(x) for x in company_size if x)
    qs = ("?" + urlencode(params)) if params else ""
    return _get(f"/api/companies/preview{qs}", timeout=timeout)


def smart_send(
    company: str = "",
    company_id: str = "",
    job_url: str = "",
    top_hr: Optional[dict] = None,
    hr_name: str = "",
    greeting: str = "",
    confirm: bool = False,
    targets: Optional[list] = None,
    timeout: int = 180,
):
    """调用 POST /api/companies/smart-send。"""
    payload: dict = {
        "company": company,
        "company_id": company_id,
        "job_url": job_url,
        "top_hr": top_hr or {},
        "hr_name": hr_name,
        "greeting": greeting,
        "confirm": confirm,
    }
    if targets is not None:
        payload["targets"] = targets
    return _post("/api/companies/smart-send", json=payload, timeout=timeout)
