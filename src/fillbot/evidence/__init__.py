"""Evidence capture and retention."""
from fillbot.evidence.retention import CleanupResult, RetentionManager
from fillbot.evidence.store import EvidenceStore
__all__ = ["CleanupResult", "EvidenceStore", "RetentionManager"]
