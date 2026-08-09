from api.devices.mcp_permissions import (
    _reconcile_saved_scope_names,
    reconcile_saved_scope_for_capability_change,
    saved_scope_was_full,
)


def test_saved_aifree_scope_migrates_underscore_names_to_plus_names():
    saved = {"aifree.browser_screenshot", "aifree.windows_tab"}
    live = {"aifree.browser+screenshot", "aifree.windows+tab"}

    assert _reconcile_saved_scope_names(saved, live) == live


def test_scope_name_migration_never_grants_an_unsaved_live_tool():
    saved = {"aifree.browser_screenshot"}
    live = {"aifree.browser+screenshot", "aifree.browser+action"}

    assert _reconcile_saved_scope_names(saved, live) == {"aifree.browser+screenshot"}


def test_scope_keeps_permissions_while_device_mcp_is_temporarily_disabled():
    saved = {"fs.read", "shell.run"}

    while_disabled = _reconcile_saved_scope_names(saved, set())
    after_reenabled = _reconcile_saved_scope_names(
        while_disabled,
        {"fs.read", "shell.run"},
    )

    assert while_disabled == saved
    assert after_reenabled == saved


def test_temporarily_missing_tool_does_not_enable_new_live_tools():
    saved = {"fs.read"}
    live = {"shell.run"}

    assert _reconcile_saved_scope_names(saved, live) == {"fs.read"}


def test_full_previous_scope_auto_selects_new_dynamic_capabilities():
    previous = {"device.status", "device.observe"}
    live = previous | {"device.action", "device.screenshot"}

    assert reconcile_saved_scope_for_capability_change(previous, live, previous) == live


def test_customized_previous_scope_never_auto_selects_new_capabilities():
    previous = {"device.status", "device.observe"}
    saved = {"device.status"}
    live = previous | {"device.action"}

    assert reconcile_saved_scope_for_capability_change(saved, live, previous) == saved


def test_explicit_empty_scope_never_auto_expands():
    previous = {"device.status"}
    live = previous | {"device.action"}

    assert reconcile_saved_scope_for_capability_change(set(), live, previous) == set()


def test_full_scope_survives_shrink_then_expands_on_later_growth():
    original = {"device.status", "device.action"}
    shrunk = {"device.status"}
    saved_while_shrunk = reconcile_saved_scope_for_capability_change(
        original,
        shrunk,
        original,
    )
    expanded = reconcile_saved_scope_for_capability_change(
        saved_while_shrunk,
        original | {"device.screenshot"},
        shrunk,
    )

    assert saved_while_shrunk == original
    assert expanded == original | {"device.screenshot"}


def test_never_initialized_scope_keeps_full_selection_intent_for_initial_push():
    assert saved_scope_was_full(None, None) is True


def test_explicit_empty_scope_is_not_treated_as_full():
    assert saved_scope_was_full(set(), {"device.status"}) is False


def test_saved_scope_fullness_uses_previous_capability_snapshot():
    previous = {"device.status", "device.observe"}

    assert saved_scope_was_full(previous, previous) is True
    assert saved_scope_was_full({"device.status"}, previous) is False
