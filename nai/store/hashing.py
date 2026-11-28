"""Deterministic node content hashing (X2, STO-5)."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any


def canonical_json(value: Any) -> str:
    """Serialize JSON deterministically, rejecting values JSON cannot represent."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha1_text(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha1(data).hexdigest()


def content_hash(
    spec_text: str,
    contract: Any,
    dependency_hashes: Iterable[str] = (),
    parent_contract: Any | None = None,
) -> str:
    """Return the STO-5 hash for one node.

    The byte stream follows section 4's formula exactly: UTF-8 spec text,
    canonical JSON for the node contract, dependency hashes in declared
    order, and (when present) the SHA-1 digest of the canonical parent contract.
    """
    pieces = [spec_text or "", canonical_json(contract if contract is not None else {})]
    pieces.extend(str(value) for value in dependency_hashes)
    if parent_contract is not None:
        pieces.append(sha1_text(canonical_json(parent_contract)))
    return sha1_text("".join(pieces))
