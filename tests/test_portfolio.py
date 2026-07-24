from __future__ import annotations

from datetime import UTC, datetime

import pytest

from continuity_kernel.errors import ConflictError, ValidationError
from continuity_kernel.portfolio import (
    ABSENT_PORTFOLIO_REVISION,
    PortfolioItem,
    new_portfolio,
    parse_portfolio,
    portfolio_item,
    render_portfolio,
)
from continuity_kernel.vault import Vault

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def _item(
    task_id: str,
    revision: str,
    *,
    reason: str = "Review deliberately.",
) -> PortfolioItem:
    return portfolio_item(
        task_id_value=task_id,
        task_revision=revision,
        stance="needs-human",
        reason=reason,
    )


def test_portfolio_round_trip_preserves_authored_order_and_revision() -> None:
    first = _item("first-outcome", "a" * 64)
    second = _item("second-outcome", "b" * 64)
    portfolio = new_portfolio(
        summary="Work through both outcomes one at a time.",
        items=(second, first),
        observed_at=NOW,
    )

    parsed = parse_portfolio(render_portfolio(portfolio))

    assert parsed == portfolio
    assert [item.task_id for item in parsed.items] == ["second-outcome", "first-outcome"]
    assert len(parsed.revision) == 64


def test_vault_portfolio_requires_complete_open_set_and_exact_cas(vault: Vault) -> None:
    first = vault.create_task(
        identifier="first-outcome",
        title="First outcome",
        outcome="Keep the first outcome current.",
        status="ready",
        next_actor="human",
    )
    second = vault.create_task(
        identifier="second-outcome",
        title="Second outcome",
        outcome="Keep the second outcome current.",
        status="doing",
        next_actor="agent",
    )
    vault.create_task(
        identifier="finite-review",
        title="Review every outcome",
        outcome="Check the open outcomes without owning them.",
        status="doing",
        next_actor="agent",
        active_thread_id="review-hand",
        refs=("review-scope:all-open",),
    )

    with pytest.raises(ValidationError, match="missing open tasks"):
        vault.set_portfolio(
            expected_revision=ABSENT_PORTFOLIO_REVISION,
            summary="Incomplete on purpose.",
            items=(_item(first.identifier, first.revision),),
        )

    authored = vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="Second first is an authored order, not a derived score.",
        items=(
            _item(second.identifier, second.revision),
            _item(first.identifier, first.revision),
        ),
    )
    assert [item.task_id for item in authored.items] == [second.identifier, first.identifier]

    changed = vault.update_task(
        second.identifier,
        expected_revision=second.revision,
        next_action="A newer exact next action.",
    )
    assert changed.revision != second.revision
    assert vault.get_portfolio().revision == authored.revision
    assert vault.doctor().healthy

    with pytest.raises(ConflictError, match="task anchor changed"):
        vault.set_portfolio(
            expected_revision=authored.revision,
            summary="A stale writer must not overwrite the newer task truth.",
            items=authored.items,
        )

    with pytest.raises(ConflictError, match="record changed"):
        vault.set_portfolio(
            expected_revision="f" * 64,
            summary="Wrong Portfolio revision.",
            items=(
                _item(changed.identifier, changed.revision),
                _item(first.identifier, first.revision),
            ),
        )


def test_portfolio_rejects_duplicate_tasks_and_noncanonical_revision() -> None:
    item = _item("one-outcome", "a" * 64)

    with pytest.raises(ValidationError, match="exactly once"):
        new_portfolio(summary="Duplicate.", items=(item, item), observed_at=NOW)
    with pytest.raises(ValidationError, match="SHA-256"):
        portfolio_item(
            task_id_value="one-outcome",
            task_revision="stale",
            stance="needs-human",
            reason="Invalid anchor.",
        )
