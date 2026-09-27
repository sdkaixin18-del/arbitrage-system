import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("trial_manage", Path(__file__).resolve().parents[2] / "scripts/astro_depth_trial_manage.py")
manage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manage)

INTERFACES = [{"addr_info": [{"family": "inet", "local": "192.0.2.1", "prefixlen": 30}]}]


@pytest.mark.parametrize("ip", ["192.0.2.0", "192.0.2.1", "192.0.2.3"])
def test_network_interface_broadcast_rejected(ip):
    with pytest.raises(RuntimeError):
        manage.validate_address(INTERFACES, "", "trial", ip)


def test_existing_peer_rejected():
    with pytest.raises(RuntimeError):
        manage.validate_address(INTERFACES, "windows\t192.0.2.2/32", "trial", "192.0.2.2")


def test_isolated_host_address_allowed():
    manage.validate_address(INTERFACES, "windows\t192.0.2.2/32", "trial")
