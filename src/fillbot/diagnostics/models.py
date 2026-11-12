"""Typed root-cause taxonomy, rule definitions and diagnosis records."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class RootCause(str, Enum):
    """Stable root-cause tags written to the ledger and summary report."""

    LOGIN_FAILED = "LOGIN_FAILED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    CSRF_INVALID = "CSRF_INVALID"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RATE_LIMITED = "RATE_LIMITED"
    ATTACHMENT_LIMIT = "ATTACHMENT_LIMIT"
    ATTACHMENT_MISSING = "ATTACHMENT_MISSING"
    DUPLICATE_SUBMISSION = "DUPLICATE_SUBMISSION"
    RECEIPT_INVALID = "RECEIPT_INVALID"
    FORM_FIELD_MISMATCH = "FORM_FIELD_MISMATCH"
    STATE_MISMATCH = "STATE_MISMATCH"
    INPUT_INVALID = "INPUT_INVALID"
    SCRIPT_INVALID = "SCRIPT_INVALID"
    NETWORK_FAILURE = "NETWORK_FAILURE"
    SERVER_FAILURE = "SERVER_FAILURE"
    ASSERTION_FAILED = "ASSERTION_FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class RootCauseDetail:
    label: str
    suggestion: str


ROOT_CAUSE_DETAILS: Mapping[RootCause, RootCauseDetail] = {
    RootCause.LOGIN_FAILED: RootCauseDetail(
        "登录失败（账号/口令/登录态失效）",
        "核对任务中的账号与口令，确认账号未被锁定；如为登录态过期，请在脚本开头重新登录后重试。",
    ),
    RootCause.SESSION_EXPIRED: RootCauseDetail(
        "会话中途失效（被重定向到登录页）",
        "目标会话在流程中途失效；检查登录超时配置，重跑时从登录步骤续跑并保留原 biz_key 幂等状态。",
    ),
    RootCause.CSRF_INVALID: RootCauseDetail(
        "CSRF 令牌校验失败",
        "页面令牌已失效或未随表单提交；重新打开表单页获取新令牌后立即提交。",
    ),
    RootCause.PERMISSION_DENIED: RootCauseDetail(
        "权限不足（403）",
        "当前账号没有该页面或操作权限；联系目标系统管理员授权后再执行。",
    ),
    RootCause.RATE_LIMITED: RootCauseDetail(
        "触发目标限流（429）",
        "降低 rate_limit_per_minute，并按 Retry-After 等待后再试；避免对目标造成额外压力。",
    ),
    RootCause.ATTACHMENT_LIMIT: RootCauseDetail(
        "附件超出目标限制",
        "附件类型、大小或数量超出目标限制；压缩或转换附件（如 PDF/PNG 且 ≤64 KiB）后重试。",
    ),
    RootCause.ATTACHMENT_MISSING: RootCauseDetail(
        "附件缺失或路径不可读",
        "检查 task.attachments 配置、文件是否存在以及运行账号的读取权限。",
    ),
    RootCause.DUPLICATE_SUBMISSION: RootCauseDetail(
        "重复报送（业务键或周期已存在）",
        "目标系统提示该周期已报送；核对回执后从批次中调整 biz_key/period，切勿重复提交。",
    ),
    RootCause.RECEIPT_INVALID: RootCauseDetail(
        "回执缺失、格式不符或重复",
        "确认提交是否真正落库并人工核对证据快照；不要盲目重提，必要时按回执号查询目标台账。",
    ),
    RootCause.FORM_FIELD_MISMATCH: RootCauseDetail(
        "表单字段不匹配或服务端字段校验失败",
        "对照证据快照核对字段名、定位器与取值；修正脚本 selector/label 或任务字段后重试。",
    ),
    RootCause.STATE_MISMATCH: RootCauseDetail(
        "步骤顺序或前置状态不满足",
        "检查断点续跑数据与前一阶段是否提交成功；按目标流程顺序重放缺失步骤。",
    ),
    RootCause.INPUT_INVALID: RootCauseDetail(
        "任务前置校验未通过",
        "按 field= 提示修正任务清单中的数据、格式或附件路径后重新提交批次。",
    ),
    RootCause.SCRIPT_INVALID: RootCauseDetail(
        "动作脚本或配置错误",
        "修正 YAML 中的动作、参数、变量或配置项；validate 子命令可离线复核脚本。",
    ),
    RootCause.NETWORK_FAILURE: RootCauseDetail(
        "网络不可达或请求超时",
        "检查目标地址连通性与超时配置；网络恢复后按现有重试策略重试。",
    ),
    RootCause.SERVER_FAILURE: RootCauseDetail(
        "目标服务端异常（5xx）",
        "目标服务暂时不可用；按退避策略重试，并查看目标服务日志确认故障范围。",
    ),
    RootCause.ASSERTION_FAILED: RootCauseDetail(
        "页面内容或状态与预期不符",
        "核对目标页面是否改版或流程未完成；结合证据快照调整断言或流程步骤。",
    ),
    RootCause.UNKNOWN: RootCauseDetail(
        "未知错误（未匹配预设根因规则）",
        "未匹配到预设分类规则，已保留原始错误栈与请求响应记录；请结合证据快照人工排查。",
    ),
}


@dataclass(frozen=True)
class Rule:
    """A single root-cause rule. Higher ``priority`` wins when rules conflict.

    Ties are broken by ``id`` so classification is always deterministic.
    """

    id: str
    root_cause: RootCause
    priority: int
    stages: frozenset[str] | None = None
    error_classes: frozenset[str] | None = None
    statuses: frozenset[int] | None = None
    excluded_statuses: frozenset[int] | None = None
    actions: frozenset[str] | None = None
    message_regex: tuple[str, ...] = ()
    page_regex: tuple[str, ...] = ()
    requires_login_page: bool | None = None
    requires_retry_after: bool | None = None


@dataclass
class ErrorFeatures:
    """Observable exception features collected at intake / interaction / receipt stages."""

    stage: str = "step"
    error_class: str | None = None
    error_type: str = ""
    message: str = ""
    status: int | None = None
    page_status: int | None = None
    page_url: str = ""
    page_title: str = ""
    page_text: str = ""
    action: str | None = None
    locator: str = ""
    step_index: int | None = None
    retry_after: float | None = None
    receipt: str | None = None
    attempts: int = 1
    issue_fields: tuple[str, ...] = ()
    issue_messages: tuple[str, ...] = ()
    attachment_name: str | None = None
    attachment_size: int | None = None
    attachment_suffix: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    exchange: dict[str, Any] = field(default_factory=dict)

    @property
    def page_path(self) -> str:
        from urllib.parse import urlsplit

        path = urlsplit(self.page_url or "").path.rstrip("/")
        return path or "/"

    @property
    def on_login_page(self) -> bool:
        return self.page_path.endswith("/login") or "账号登录" in self.page_text

    @property
    def searchable_text(self) -> str:
        return "\n".join(
            part
            for part in (
                self.message,
                self.page_text,
                self.page_title,
                self.locator,
                "\n".join(self.issue_messages),
                str(self.details.get("status", "")),
            )
            if part
        )

    @property
    def page_text_lower(self) -> str:
        return f"{self.page_title}\n{self.page_text}\n{self.locator}".lower()

    def status_values(self) -> set[int]:
        return {value for value in (self.status, self.page_status) if value is not None}

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe snapshot used inside ``diagnostic_json`` (bounded in size)."""
        return {
            "stage": self.stage,
            "error_class": self.error_class,
            "error_type": self.error_type,
            "status": self.status,
            "page_status": self.page_status,
            "page_url": _clip(self.page_url, 600),
            "page_title": _clip(self.page_title, 200),
            "page_text": _clip(self.page_text, 600),
            "action": self.action,
            "locator": _clip(self.locator, 300),
            "step_index": self.step_index,
            "attempts": self.attempts,
            "retry_after": self.retry_after,
            "receipt": self.receipt,
            "issue_fields": list(self.issue_fields[:20]),
            "issue_messages": [_clip(item, 300) for item in self.issue_messages[:20]],
            "attachment": {
                "name": self.attachment_name,
                "size": self.attachment_size,
                "suffix": self.attachment_suffix,
            },
            "details": _json_safe(self.details),
            "exchange": _json_safe(self.exchange),
        }


def _clip(value: str, maximum: int) -> str:
    text = str(value or "")
    return text if len(text) <= maximum else text[:maximum] + "…"


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Best-effort conversion so diagnostics can never fail JSON serialization."""
    if depth > 3:
        return "<nested>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clip(value, 600)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item, depth=depth + 1) for key, item in list(value.items())[:30]}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item, depth=depth + 1) for item in list(value)[:30]]
    return _clip(repr(value), 600)


@dataclass
class Diagnosis:
    root_cause: RootCause
    rule_id: str
    priority: int
    stage: str
    error_class: str | None
    message: str
    features: ErrorFeatures
    stack: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    evidence_path: str = ""
    exchange_path: str = ""
    overhead_ms: float = 0.0

    def as_ledger_json(self) -> dict[str, Any]:
        """Bounded JSON payload persisted in ``tasks.diagnostic_json``."""
        detail = detail_for(self.root_cause)
        return {
            "version": 1,
            "root_cause": self.root_cause.value,
            "label": detail.label,
            "suggestion": detail.suggestion,
            "rule_id": self.rule_id,
            "priority": self.priority,
            "stage": self.stage,
            "error_class": self.error_class,
            "message": _clip(self.message, 1200),
            "stack": _clip(self.stack, 6000),
            "evidence_path": self.evidence_path,
            "exchange_path": self.exchange_path,
            "overhead_ms": round(self.overhead_ms, 3),
            "evidence": _json_safe(self.evidence),
            "features": self.features.as_dict(),
        }

    def as_dict(self) -> dict[str, Any]:
        return self.as_ledger_json()


def detail_for(root_cause: RootCause) -> RootCauseDetail:
    return ROOT_CAUSE_DETAILS.get(root_cause, ROOT_CAUSE_DETAILS[RootCause.UNKNOWN])
