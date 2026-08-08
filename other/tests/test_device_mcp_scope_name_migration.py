from api.devices.mcp_permissions import _reconcile_saved_scope_names


def test_saved_aifree_scope_migrates_underscore_names_to_plus_names():
    saved = {"aifree.browser_screenshot", "aifree.windows_tab"}
    live = {"aifree.browser+screenshot", "aifree.windows+tab"}

    assert _reconcile_saved_scope_names(saved, live) == live


def test_scope_name_migration_never_grants_an_unsaved_live_tool():
    saved = {"aifree.browser_screenshot"}
    live = {"aifree.browser+screenshot", "aifree.browser+action"}

    assert _reconcile_saved_scope_names(saved, live) == {"aifree.browser+screenshot"}
