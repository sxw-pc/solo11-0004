"""Offline precheck engine: evaluate every rule against every task, aggregate all violations.

The engine never stops at the first failure and never performs I/O or network
calls; results are linked to each task's unique id and business key so failing
rows can be located and fixed quickly.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence
from fillbot.models import TaskRecord, elapsed_ms, utcnow
from fillbot.precheck.checks import RULE_TYPES
from fillbot.precheck.ruleset import Rule, RuleSet


@dataclass
class PrecheckIssue:
    task_id: str
    biz_key: str
    rule_id: str
    rule_type: str
    message: str

    def render(self) -> str:
        return f"{self.task_id} (biz_key={self.biz_key}) [{self.rule_id}] {self.message}"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PrecheckReport:
    ruleset: str
    tasks_total: int
    rules_total: int
    issues: list[PrecheckIssue] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def failed_task_ids(self) -> set[str]:
        return {issue.task_id for issue in self.issues}

    def as_dict(self) -> dict[str, Any]:
        return {
            "ruleset": self.ruleset,
            "ok": self.ok,
            "tasks_total": self.tasks_total,
            "rules_total": self.rules_total,
            "violations": len(self.issues),
            "tasks_failed": len(self.failed_task_ids),
            "duration_ms": self.duration_ms,
            "issues": [issue.as_dict() for issue in self.issues],
        }


def _message(rule: Rule, detail: str) -> str:
    return f"{rule.message} ({detail})" if rule.message else detail


def run_precheck(tasks: Sequence[TaskRecord], ruleset: RuleSet) -> PrecheckReport:
    """Run all rules over all tasks and aggregate every violation found."""
    started = utcnow()
    task_rules = [rule for rule in ruleset.rules if not RULE_TYPES[rule.type].batch]
    batch_rules = [rule for rule in ruleset.rules if RULE_TYPES[rule.type].batch]
    issues: list[PrecheckIssue] = []
    for task in tasks:
        for rule in task_rules:
            check = RULE_TYPES[rule.type].check
            for detail in check(task, rule.config):
                issues.append(PrecheckIssue(task.task_id, task.biz_key, rule.rule_id, rule.type, _message(rule, detail)))
    for rule in batch_rules:
        check_batch = RULE_TYPES[rule.type].check
        for task, detail in check_batch(tasks, rule.config):
            issues.append(PrecheckIssue(task.task_id, task.biz_key, rule.rule_id, rule.type, _message(rule, detail)))
    return PrecheckReport(ruleset.name, len(tasks), len(ruleset.rules), issues, elapsed_ms(started))
