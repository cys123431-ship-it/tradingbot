import hashlib
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class CriticalPauseBlockDecision:
    blocked: bool
    reason_code: str | None = None
    pause_id: str | None = None
    scope: str | None = None
    origin_symbol: str | None = None
    created_at: str | None = None

    def __post_init__(self):
        if not self.blocked:
            return
        if not self.reason_code:
            raise ValueError("BLOCK_DECISION_REASON_REQUIRED")
        if self.scope not in {"GLOBAL", "SYMBOL"}:
            raise ValueError("BLOCK_DECISION_SCOPE_INVALID")
        if not self.origin_symbol:
            raise ValueError("BLOCK_DECISION_ORIGIN_REQUIRED")

@dataclass(frozen=True)
class EntrySubmitOutcome:
    submission: Any | None = None
    critical_pause_block: CriticalPauseBlockDecision | None = None
    entry_block_reason: str | None = None
    duplicate_protected: bool = False
    submission_error: str | None = None
    client_order_id: str | None = None

    def __post_init__(self):
        has_success = (
            self.submission is not None
            and self.critical_pause_block is None
            and self.entry_block_reason is None
            and not self.duplicate_protected
            and self.submission_error is None
        )

        active_outcomes = sum(
            [
                has_success,
                self.critical_pause_block is not None,
                self.entry_block_reason is not None,
                bool(self.duplicate_protected),
                self.submission_error is not None,
            ]
        )

        if active_outcomes != 1:
            raise ValueError("ENTRY_SUBMIT_OUTCOME_INVALID")

        if self.critical_pause_block is not None and self.submission is not None:
            raise ValueError("BLOCKED_OUTCOME_HAS_SUBMISSION")

        if self.entry_block_reason is not None and self.submission is not None:
            raise ValueError("ENTRY_BLOCK_OUTCOME_HAS_SUBMISSION")

        if self.duplicate_protected and self.submission is None:
            raise ValueError("DUPLICATE_OUTCOME_REQUIRES_RESULT")

        if self.submission_error is not None and self.submission is None:
            raise ValueError("SUBMISSION_ERROR_REQUIRES_RESULT")

    @classmethod
    def success(cls, submission, client_order_id=None):
        return cls(submission=submission, client_order_id=client_order_id)

    @classmethod
    def critical_block(cls, decision, client_order_id=None):
        return cls(critical_pause_block=decision, client_order_id=client_order_id)

    @classmethod
    def entry_block(cls, reason, client_order_id=None):
        return cls(entry_block_reason=reason, client_order_id=client_order_id)

    @classmethod
    def duplicate(cls, submission, client_order_id=None):
        return cls(submission=submission, duplicate_protected=True, client_order_id=client_order_id)

    @classmethod
    def not_accepted(cls, submission, error, client_order_id=None):
        return cls(submission=submission, submission_error=error, client_order_id=client_order_id)


def canonical_futures_symbol(symbol: str, quote: str = "USDT") -> str:
    """Return one ccxt USD-M perpetual symbol for common symbol aliases."""
    raw = str(symbol or '').strip()
    if not raw:
        return raw

    default_quote = str(quote or 'USDT').upper().strip()
    text = raw.upper().replace(' ', '')
    supported_quotes = ('USDT', 'USDC', 'BUSD')

    if '/' in text:
        left, right = text.split('/', 1)
        left = left.replace('-', '').replace('_', '')
        if ':' in right:
            quote_part, settle_part = right.split(':', 1)
        else:
            quote_part, settle_part = right, right
        quote_part = quote_part.replace('-', '').replace('_', '')
        settle_part = settle_part.replace('-', '').replace('_', '')
        if (
            quote_part in supported_quotes
            and left.endswith(quote_part)
            and len(left) > len(quote_part)
        ):
            left = left[:-len(quote_part)]
        if left and quote_part:
            return f"{left}/{quote_part}:{settle_part or quote_part}"

    compact = text
    for settle_quote in supported_quotes:
        compact = compact.replace(f":{settle_quote}", '')
    compact = (
        compact
        .replace('/', '')
        .replace('-', '')
        .replace('_', '')
        .replace(':', '')
    )
    for compact_quote in supported_quotes:
        if compact.endswith(compact_quote) and len(compact) > len(compact_quote):
            base = compact[:-len(compact_quote)]
            return f"{base}/{compact_quote}:{compact_quote}"
    return f"{compact}/{default_quote}:{default_quote}"


def build_critical_pause_notice_key(
    decision: CriticalPauseBlockDecision,
    *,
    requested_symbol: str,
) -> str:
    # canonicalize requested symbol
    canonical_requested = canonical_futures_symbol(requested_symbol)
    clean_origin = canonical_futures_symbol(decision.origin_symbol) if decision.origin_symbol else ""

    # prioritised event token material
    material = decision.pause_id or f"{decision.reason_code or 'UNKNOWN'}|{clean_origin}|{str(decision.created_at or '')}"
    event_token = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]

    if decision.scope == "SYMBOL":
        symbol_token = hashlib.sha256(canonical_requested.encode("utf-8")).hexdigest()[:16]
        return f"critical_pause_notice:SYMBOL:{event_token}:{symbol_token}"
    else:
        return f"critical_pause_notice:GLOBAL:{event_token}"
