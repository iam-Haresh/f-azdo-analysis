"""Privacy rules applied while building the model, so nothing sensitive reaches a report."""

from __future__ import annotations

import re

from ..config.models import Config
from ..util import compile_pattern, short_hash

REDACTED = "***REDACTED***"


class Privacy:
    def __init__(self, config: Config) -> None:
        privacy = config.reports.privacy
        self._include_identities = privacy.include_identities
        self._max_len = privacy.max_variable_value_length
        self._redact = [compile_pattern(p) for p in privacy.redact_value_patterns]
        self._credentials = [compile_pattern(p) for p in config.security.inline_credential_patterns]
        self._secret_names = [compile_pattern(p) for p in config.security.secret_name_patterns]

    def text(self, text: str) -> str:
        """Redact configured patterns and inline credentials; keep length."""
        for rx in (*self._redact, *self._credentials):
            text = rx.sub(REDACTED, text)
        return text

    def value(self, text: str) -> str:
        """Variable values: redact, then truncate."""
        text = self.text(text)
        if self._max_len and len(text) > self._max_len:
            return text[: self._max_len] + "…"
        return text

    def identity(self, name: str, *, is_group: bool = False) -> str:
        if not name or self._include_identities or is_group:
            return name
        return "user-" + short_hash(name.lower(), 8)

    def has_inline_credential(self, text: str) -> bool:
        return any(rx.search(text) for rx in self._credentials)

    def is_secret_name(self, name: str) -> bool:
        return any(rx.search(name) for rx in self._secret_names)

    @staticmethod
    def host_only(url: str) -> str:
        """Webhook targets can carry tokens in the path or query: keep scheme and host only."""
        match = re.match(r"^(https?://[^/?#]+)", url or "")
        return match.group(1) if match else ""
