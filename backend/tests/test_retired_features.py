from app.main import app


def test_retired_mac_routes_are_not_registered() -> None:
    paths = {route.path for route in app.routes}
    assert not any(
        path.startswith(prefix)
        for path in paths
        for prefix in (
            "/api/manual-orders",
            "/api/arb-radar",
            "/api/xueqiu",
            "/api/fs/pair-spread/sk-hynix",
        )
    )
    assert {"/api/health", "/api/fs/signals"} <= paths
