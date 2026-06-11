"""Tree-build invariant: every PUSH_DRIVEN provider has a notifier wired.

A ``TreeProvider`` subclass that declares ``PUSH_DRIVEN: ClassVar[bool] = True``
needs ``set_change_notifier(...)`` called on it during tree assembly so that
in-process state mutations wake cycle-query subscribers within an
event-loop tick rather than within ``time_of_data_tolerance``.

If the call is forgotten, the provider keeps working — but cycle-query
subscribers fall back to t_tolerance polling cadence with no visible
warning. ``assert_push_wiring`` makes that case fail-fast at server start.

Usage at the end of ``tree_build()``:

    from obsrv.utils.push_wiring import assert_push_wiring
    assert_push_wiring(target_provider_global)
    for tele_provider in (target_provider_sim, target_provider_dev, ...):
        assert_push_wiring(tele_provider)
"""
from __future__ import annotations

from typing import Iterator


def _walk_components(component) -> Iterator:
    """Depth-first walk of the tree under ``component`` via the
    standard subcontractor / broker child relationships.

    The walker uses duck-typed access:
        - ``_subcontractor`` for chain-style components (provider, freezer, cache)
        - ``get_list_providers()`` for brokers that fan out to siblings
    """
    seen: set[int] = set()

    def visit(c) -> Iterator:
        if c is None or id(c) in seen:
            return
        seen.add(id(c))
        yield c
        sub = getattr(c, '_subcontractor', None)
        if sub is not None:
            yield from visit(sub)
        if hasattr(c, 'get_list_providers'):
            for p in c.get_list_providers():
                yield from visit(p)

    yield from visit(component)


def assert_push_wiring(root) -> None:
    """Walk the subtree under ``root`` and verify every provider with
    ``PUSH_DRIVEN=True`` has had ``set_change_notifier(...)`` called.

    Raises ``AssertionError`` with a message naming the offending
    provider and pointing at the tree-build factory if any wiring is
    missing.
    """
    missing: list[str] = []
    for c in _walk_components(root):
        if getattr(c, 'PUSH_DRIVEN', False):
            if getattr(c, '_change_notifier', None) is None:
                name = c.get_name() if hasattr(c, 'get_name') else repr(c)
                missing.append(f"{name} ({type(c).__name__})")
    if missing:
        raise AssertionError(
            "PUSH_DRIVEN providers missing change-notifier wiring:\n  "
            + "\n  ".join(missing)
            + "\nAdd `provider.set_change_notifier(cache._report_new_value)` "
              "in the tree-build factory for each of these — see the assembly "
              "of similar providers nearby."
        )
