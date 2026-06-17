"""Regression tests for HARD-10.

``MyDolphinPlusBaseEntity._handle_coordinator_update`` did not guard
against ``self._local_coordinator.get_data(...)`` returning ``None``.
With ``self._data = {}`` from the ctor and ``new_data = None``,
``{} != None`` enters the body and the debug-log dict-comprehension
``{k: new_data[k] for k in new_data if k != ATTR_ACTIONS}`` does
``for k in None`` and raises ``TypeError: 'NoneType' object is not
iterable``. The surrounding ``except Exception`` swallows it and emits

    ERROR Failed to update <entity>, Error: 'NoneType' object is not
    iterable, Line: 138

at ERROR level for every entity, once per integration-startup tick that
fires before the first AWS shadow carrying ``systemState`` arrives.
Pre-existing since v1.0.26b3 first install, observed 78x on the install
day and 28x on every fresh install/reinstall thereafter (see issue
HARD-10 for the log dump and counts). Cosmetic only — the entity stays
``unavailable`` via the BUG-16 latch — but misleadingly framed as a
failure.

The fix is a single early-return ``if new_data is None: return`` at the
top of the handler. The discriminating assertion below is the
``caplog`` check: pre-fix it captures the ERROR line, post-fix it is
empty.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace


def _make_base_entity(*, get_data_returns):
    """Return a minimal ``MyDolphinPlusBaseEntity`` instance with the
    attributes ``_handle_coordinator_update`` touches. ``coordinator``
    is a ``SimpleNamespace`` whose ``get_data`` returns whatever the
    caller wants. ``async_write_ha_state`` is replaced by an in-memory
    recorder so the test can assert whether it was called."""
    from custom_components.mydolphin_plus.common.base_entity import (
        MyDolphinPlusBaseEntity,
    )

    entity = object.__new__(MyDolphinPlusBaseEntity)
    entity._data = {}
    entity._attr_unique_id = "unit_test_entity"
    entity.entity_description = SimpleNamespace(key="status", platform="sensor")
    entity._local_entity_description = entity.entity_description
    entity.coordinator = SimpleNamespace(
        get_data=lambda ed: get_data_returns,
        last_update_success=True,
        has_real_data=False,
    )

    write_calls = []
    entity.async_write_ha_state = lambda: write_calls.append(True)
    entity._test_write_calls = write_calls
    return entity


def _errors_from(caplog) -> list[str]:
    """Return ERROR-level messages emitted by the base_entity logger."""
    logger_name = "custom_components.mydolphin_plus.common.base_entity"
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR and record.name == logger_name
    ]


def test_handle_coordinator_update_with_no_data_is_silent_noop(caplog):
    """During the bootstrap window, ``get_data()`` returns ``None``.
    The handler must early-return: no state write, no ``async_write_ha_state``
    call, and crucially **no ERROR log**. The ``caplog`` assertion is the
    discriminating one — pre-fix it captures the ``TypeError`` swallow,
    post-fix it is empty."""
    entity = _make_base_entity(get_data_returns=None)

    with caplog.at_level(logging.DEBUG):
        entity._handle_coordinator_update()

    assert entity._data == {}, "self._data must remain untouched when no data"
    assert entity._test_write_calls == [], (
        "async_write_ha_state must not be called when there is no data"
    )

    errors = _errors_from(caplog)
    assert errors == [], (
        f"no ERROR log expected during the pre-first-shadow window; "
        f"got: {errors}"
    )


def test_handle_coordinator_update_with_real_data_writes_state(caplog):
    """Once ``get_data()`` returns a real dict, the handler must update
    ``self._data``, call ``update_component``, and call
    ``async_write_ha_state``. Locks the happy path so the early-return
    cannot accidentally short-circuit it."""
    payload = {"state": "cleaning", "attributes": {"mode": "all"}}
    entity = _make_base_entity(get_data_returns=payload)

    update_calls = []
    entity.update_component = lambda data: update_calls.append(data)

    with caplog.at_level(logging.DEBUG):
        entity._handle_coordinator_update()

    assert update_calls == [payload], (
        "update_component must be called once with the new data"
    )
    assert entity._data == payload, "self._data must be assigned to new_data"
    assert entity._test_write_calls == [True], (
        "async_write_ha_state must be called once"
    )

    errors = _errors_from(caplog)
    assert errors == [], f"no ERROR log expected on the happy path; got: {errors}"
