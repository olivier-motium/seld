from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from continuity_kernel.errors import ConflictError
from continuity_kernel.vault import Vault


def test_two_writers_with_one_revision_produce_one_winner(vault: Vault) -> None:
    task = vault.create_task(
        identifier="concurrent-write",
        title="Concurrent write",
        outcome="Exactly one mutation wins.",
        status="doing",
        next_actor="agent",
        next_action="Start.",
    )

    def update(label: str) -> str:
        try:
            result = Vault(vault.root).update_task(
                task.identifier,
                expected_revision=task.revision,
                next_action=label,
            )
            return f"won:{result.next_action}"
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(update, ("Writer one", "Writer two")))

    assert outcomes.count("conflict") == 1
    assert sum(item.startswith("won:") for item in outcomes) == 1
    assert vault.get_task(task.identifier).next_action in {"Writer one", "Writer two"}
