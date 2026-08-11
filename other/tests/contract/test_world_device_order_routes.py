from gateway.routers.world import router


def test_world_device_order_routes_are_public():
    routes = {route.path: route for route in router.routes}

    methods = {method for route in router.routes if route.path == "/devices/order" for method in route.methods}
    assert methods == {"GET", "PUT"}
    assert routes["/snapshot"].methods == {"GET"}
