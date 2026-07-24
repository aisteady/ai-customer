"""
报价线索 / CRM 登记（crm.py）
============================

推送设备配置后，若客户需要报价并留下姓名与联系方式，
将完整线索组装为 JSON 并写入数据库（模拟 CRM POST）。

留资校验：必须同时解析出「姓名 + 联系方式（手机或邮箱）」才登记；
缺任一项则继续追问（由 quote_lead 节点 loop）。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from store import ChatStore

logger = logging.getLogger(__name__)

QUOTE_OFFER_TAIL = (
    "\n\n---\n"
    "以上为设备配置方案。请问您是否需要具体报价？\n"
    "如需要，请回复「需要」，并一并留下您的**姓名**与**联系方式**（手机号或邮箱）；"
    "如暂不需要，请回复「不需要」。"
)

QUOTE_CONTACT_ASK = (
    "好的。为安排专业销售与您对接，请同时留下您的**姓名**与**联系方式**"
    "（手机号或邮箱均可）。"
)

QUOTE_DECLINE_ACK = (
    "好的，已为您保留本次配置方案。如后续需要具体报价，随时告诉我即可。"
)

QUOTE_LEAD_THANKS = (
    "感谢您的信任！我们已收到您的报价需求，"
    "专业销售人员将在 **48 小时内**与您取得联系，请保持通讯畅通。"
)

QUOTE_LEAD_GIVE_UP = (
    "已多次未能确认完整的姓名与联系方式，本次暂不登记报价线索。"
    "您可随时再次告知姓名与手机号/邮箱，我们再为您安排销售跟进。"
)

_PHONE_RE = re.compile(
    r"(?:\+?86[-\s]?)?(1[3-9]\d{9})"
    r"|(?:0\d{2,3}[-\s]?)?\d{7,8}"
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_NAME_LABELED_RE = re.compile(
    r"(?:姓名|名字|称呼)[:：\s]*([^\s,，、；;]{1,20})"
    r"|我叫\s*([^\s,，、；;]{1,20})"
    r"|我是\s*([^\s,，、；;]{1,20})"
)
# 「张三 138xxxx」/「张三，138xxxx」等：2~4 个汉字紧挨联系方式前
_NAME_BEFORE_CONTACT_RE = re.compile(
    r"(?<![A-Za-z0-9\u4e00-\u9fa5])"
    r"([\u4e00-\u9fa5]{2,4})"
    r"(?:\s*[,，、]?\s*)"
    r"(?=1[3-9]\d{9}|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"
)

_DECLINE_KW = ("不需要", "不用", "暂时不", "先不", "不必", "不要报价", "取消")
_ACCEPT_KW = ("需要", "要报价", "报价", "是的", "好的", "要的", "可以")


def extract_resume_text(resume: Any) -> str:
    if isinstance(resume, str):
        return resume.strip()
    if isinstance(resume, dict):
        return str(
            resume.get("user_reply") or resume.get("text") or ""
        ).strip()
    return ""


def wants_quote(text: str) -> bool | None:
    """True=要报价, False=不要, None=无法判断。"""
    t = (text or "").strip()
    if not t:
        return None
    low = t.lower()
    if any(k in t for k in _DECLINE_KW) or low in ("n", "no"):
        return False
    if any(k in t for k in _ACCEPT_KW) or low in ("y", "yes"):
        return True
    # 直接丢姓名+联系方式，视为要报价
    name, contact, missing = parse_customer_lead(t)
    if name and contact and not missing:
        return True
    if extract_phone(t) or extract_email(t) or extract_name(t):
        return True
    return None


def extract_phone(text: str) -> str:
    m = _PHONE_RE.search(text or "")
    return m.group(0).strip() if m else ""


def extract_email(text: str) -> str:
    m = _EMAIL_RE.search(text or "")
    return m.group(0).strip() if m else ""


def extract_name(text: str) -> str:
    """从单段或累积文本中抽取姓名（须有较明确依据，避免误抓）。"""
    t = text or ""
    m = _NAME_LABELED_RE.search(t)
    if m:
        for g in m.groups():
            if g:
                return g.strip()
    m2 = _NAME_BEFORE_CONTACT_RE.search(t)
    if m2:
        return m2.group(1).strip()
    return ""


def extract_contact(text: str) -> str:
    """联系方式：仅手机或邮箱（不含整段原文兜底）。"""
    return extract_phone(text) or extract_email(text)


def parse_customer_lead(text: str) -> tuple[str, str, list[str]]:
    """
    解析姓名与联系方式。

    返回 (name, contact, missing_labels)。
    missing 非空表示尚未齐全，不可登记 CRM。
    """
    name = extract_name(text)
    contact = extract_contact(text)
    missing: list[str] = []
    if not name:
        missing.append("姓名")
    if not contact:
        missing.append("联系方式（手机号或邮箱）")
    return name, contact, missing


def lead_info_complete(name: str, contact: str) -> bool:
    return bool((name or "").strip() and (contact or "").strip())


def missing_fields_ask(
    missing: list[str],
    *,
    have_name: str = "",
    have_contact: str = "",
) -> str:
    """根据已收到 / 仍缺字段生成追问文案。"""
    bits: list[str] = []
    if have_name:
        bits.append(f"已记录姓名：{have_name}")
    if have_contact:
        bits.append(f"已记录联系方式：{have_contact}")
    prefix = ("；".join(bits) + "。") if bits else ""
    need = "、".join(missing) if missing else "姓名与联系方式"
    return (
        f"{prefix}为完成报价登记，还请补充您的**{need}**"
        "（示例：张三 13800138000）。"
        "若不需要报价，请回复「不需要」。"
    )


def has_usable_contact(text: str) -> bool:
    """兼容旧调用：是否已具备可登记的姓名+联系方式。"""
    name, contact, missing = parse_customer_lead(text)
    return lead_info_complete(name, contact) and not missing


def config_proposal_text(state: dict[str, Any]) -> str:
    """优先用已缓存的方案文案，否则取 process_config 工具结果。"""
    cached = (state.get("config_proposal") or "").strip()
    if cached:
        return cached
    for tr in reversed(list(state.get("tool_results") or [])):
        if tr.get("tool") == "process_config_recommend" and tr.get("text"):
            return str(tr["text"]).strip()
    return (state.get("answer") or "").strip()


def build_lead_payload(
    state: dict[str, Any],
    *,
    customer_name: str,
    customer_contact: str,
    raw_replies: list[str],
) -> dict[str, Any]:
    """组装拟 POST 到 CRM 的完整 JSON（当前整包落库）。"""
    return {
        "customer": {
            "name": customer_name or "",
            "contact": customer_contact or "",
            "raw_replies": [r for r in raw_replies if r],
        },
        "session_id": state.get("session_id") or "",
        "thread_id": state.get("thread_id") or "",
        "question": state.get("question") or "",
        "extras": state.get("extras") or "",
        "config_proposal": config_proposal_text(state),
        "assistant_draft": state.get("answer") or "",
        "tool_results": list(state.get("tool_results") or []),
        "sources": list(state.get("sources") or []),
        "trace_id": state.get("trace_id") or "",
    }


def register_crm_lead(
    store: ChatStore,
    payload: dict[str, Any],
    *,
    session_id: str | None = None,
) -> str:
    """
    登记报价线索。

    当前：以 JSON 写入 crm_leads 表（模拟 CRM POST）。
    后续：可在此发起真实 HTTP POST，失败时仍可落库兜底。
    """
    lead_id = store.save_crm_lead(payload, session_id=session_id)
    logger.info("crm lead saved id=%s session=%s", lead_id, session_id)
    return lead_id
