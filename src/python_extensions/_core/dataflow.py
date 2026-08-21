from __future__ import annotations

import dis
import types
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FastLocalAnalysis:
    reads: frozenset[str]
    writes: frozenset[str]
    checked_reads: frozenset[str]
    deletes_or_clears: frozenset[str]

    @property
    def reuse_safe(self) -> bool:
        """Whether one physical fast-local slot set may be reused across inline sites.

        CPython emits LOAD_FAST_CHECK when a local can be observed before a
        dominating assignment. Reusing a previously populated slot would change that
        UnboundLocalError into a stale-value read. Delete/clear operations have the
        same lifetime sensitivity. Ordinary proven-initialized LOAD_FAST/STORE_FAST
        traffic is safe to reuse between non-overlapping inline invocations.
        """

        return not self.checked_reads and not self.deletes_or_clears


def _names(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, tuple):
        return tuple(name for name in value if isinstance(name, str))
    return ()


def analyze_fast_locals(code: types.CodeType) -> FastLocalAnalysis:
    reads: set[str] = set()
    writes: set[str] = set()
    checked: set[str] = set()
    clears: set[str] = set()

    for item in dis.get_instructions(code, adaptive=False, show_caches=False):
        name = item.opname
        names = _names(item.argval)
        if "FAST" not in name:
            continue

        if name.startswith("LOAD_FAST"):
            reads.update(names)
        if name.startswith("STORE_FAST") or name == "STORE_FAST_MAYBE_NULL":
            writes.update(names)
        if "LOAD_FAST_CHECK" in name:
            checked.update(names)
        if name in {"DELETE_FAST", "LOAD_FAST_AND_CLEAR", "STORE_FAST_MAYBE_NULL"}:
            clears.update(names)

    return FastLocalAnalysis(
        reads=frozenset(reads),
        writes=frozenset(writes),
        checked_reads=frozenset(checked),
        deletes_or_clears=frozenset(clears),
    )
