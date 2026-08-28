"""boss_company.py — 公司画像（用于 smart-send）。

薄薄一层高层封装，调用 boss_automation 的 3 个方法：
  - goto_company_similar_jobs(job_url)  跳转并返回 companyId
  - parse_company_similar_jobs_page()   解析"在招 N 个"+岗位+HR
  - aggregate_company_hrs(jobs)         从岗位卡聚合去重 HR

数据拼装和给前端 / API 的统一 dict 在这里完成。
"""

from typing import Optional, Dict, Any, List

from boss_automation import pick_top_hr
from boss_state import list_jobs_by_company, list_companies_by_position_count


def _normalize_job_url(url: str) -> str:
    if not url:
        return ""
    from urllib.parse import urljoin

    return urljoin("https://www.zhipin.com", url)


def rank_companies_by_position_count(limit: int = 10) -> List[Dict[str, Any]]:
    """直接调 boss_state.list_companies_by_position_count。
    返回 [{company, company_id, position_count, latest_job_id}, ...]"""
    return list_companies_by_position_count(min_count=1, limit=limit)


def build_company_preview(
    *,
    company: str = "",
    company_id: str = "",
    selection_mode: str = "single_company",
    open_count: int = 0,
    jobs: Optional[List[Dict[str, Any]]] = None,
    hrs: Optional[List[Dict[str, Any]]] = None,
    companies_ranked: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """把浏览器抓到的数据 + 排序后的 HR 拼成给前端/CLI 的统一预览 dict。"""
    jobs = jobs or []
    hrs = hrs or []

    job_urls = [j.get("url") for j in jobs if j.get("url")]
    for j in jobs:
        if j.get("url"):
            j["url"] = _normalize_job_url(j["url"])

    top_hr = pick_top_hr(hrs)
    if top_hr:
        top_hr = {k: v for k, v in top_hr.items() if k != "associated_jobs"}
        for j in jobs:
            if top_hr.get("name") and top_hr["name"] in (j.get("hr_block") or ""):
                j["is_top_hr_job"] = True
            else:
                j.setdefault("is_top_hr_job", False)

    page_url = ""
    if company_id:
        page_url = f"https://www.zhipin.com/gongsi/job/{company_id}.html"

    return {
        "ok": True,
        "selection_mode": selection_mode,
        "company": company,
        "company_id": company_id,
        "company_page_url": page_url,
        "stats": {
            "open_positions_count_official": open_count,
            "scraped_jobs_count": len(jobs),
            "hrs_count": len(hrs),
        },
        "jobs": jobs,
        "hrs": hrs,
        "top_hr": top_hr,
        "companies_ranked": companies_ranked or [],
    }


def find_top_company_in_db(keyword: str = "", city: str = "") -> Optional[Dict[str, Any]]:
    """从数据库现有 applications 里挑 distinct 职位数最多的公司。
    keyword/city 仅作为过滤（city 在 applications.city 里，keyword 不参与）。"""
    rows = list_companies_by_position_count(min_count=1, limit=50)
    if not rows:
        return None
    if city:
        rows = [r for r in rows if r.get("company")]
    return rows[0] if rows else None


def get_company_jobs_from_db(company: str = "", company_id: str = "") -> List[Dict[str, Any]]:
    """数据库里该公司已入库的岗位（不在浏览器抓，仅用于数据库模式预览）。"""
    return list_jobs_by_company(company_id=company_id, company=company)
