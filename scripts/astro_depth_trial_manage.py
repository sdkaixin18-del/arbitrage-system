"""Deploy/stop only the isolated trial. Requires an existing SSH control socket."""
import argparse
import io
import ipaddress
import json
from pathlib import Path
import shlex
import subprocess
import tarfile


TRIAL_IP = "192.0.2.4"
TRIAL_ROUTE = TRIAL_IP + "/32"


def validate_address(interfaces, allowed_ips, peer_public, trial_ip=TRIAL_IP):
    candidate = ipaddress.ip_address(trial_ip)
    for interface in interfaces:
        for info in interface.get("addr_info", []):
            if info.get("family") != "inet":
                continue
            subnet = ipaddress.ip_interface(f"{info['local']}/{info['prefixlen']}")
            if candidate == subnet.ip or (subnet.network.prefixlen < 31 and candidate in
                                         (subnet.network.network_address, subnet.network.broadcast_address)):
                raise RuntimeError("Trial IP is an interface, network or broadcast address")
    for line in allowed_ips.splitlines():
        parts = line.split()
        if len(parts) != 2 or parts[0] == peer_public:
            continue
        for network in parts[1].split(","):
            if network != "(none)" and candidate in ipaddress.ip_network(network):
                raise RuntimeError("Trial IP conflicts with another peer")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("deploy", "stop"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--peer-public", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--host", required=True)
    args = parser.parse_args()
    ssh = ["ssh", "-S", args.socket, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
           args.host]
    def remote(command, data=None):
        subprocess.run([*ssh, command], input=data, check=True, timeout=45)
    def read(command):
        return subprocess.check_output([*ssh, command], timeout=15).decode()
    def remove_route():
        routes = json.loads(read(f"ip -N -j route show exact {TRIAL_ROUTE}"))
        if routes and all(r.get("dev") == "wg-qmt-test" and str(r.get("protocol")) == "186" for r in routes):
            remote(f"sudo ip route del {TRIAL_ROUTE} dev wg-qmt-test proto 186")
    peer = shlex.quote(args.peer_public)
    if args.action == "stop":
        remote("sudo systemctl stop astro-depth-trial")
        remote(f"sudo wg set wg-qmt-test peer {peer} remove")
        remove_route()
        print("Trial stopped; trial peer removed; production untouched")
        return
    config = json.loads(args.config.read_text())
    if args.config.stat().st_mode & 0o077:
        raise RuntimeError("Config must be owner-only")
    validate_address(json.loads(read("ip -j -4 addr show")),
                     read("sudo wg show wg-qmt-test allowed-ips"), args.peer_public)
    if json.loads(read(f"ip -j route show exact {TRIAL_ROUTE}")):
        raise RuntimeError("Trial route already exists; stop the previous trial first")
    root = Path(__file__).resolve().parents[1]
    files = {"backend/app/astro_depth_cloud.py": "backend/app/astro_depth_cloud.py",
             "backend/app/astro_depth_trial.py": "backend/app/astro_depth_trial.py",
             "scripts/astro-depth-trial.service": "astro-depth-trial.service"}
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        for source, target in files.items():
            tar.add(root / source, arcname=target)
    remote("sudo install -d -o ubuntu -g ubuntu -m 755 /opt/astro-depth-trial")
    remote("tar -xzf - -C /opt/astro-depth-trial", archive.getvalue())
    remote("sudo install -m 600 /dev/stdin /etc/astro-depth-trial.env",
           ("ASTRO_DEPTH_TRIAL_TOKEN=" + config["Token"] + "\n").encode())
    remote("sudo install -m 644 /opt/astro-depth-trial/astro-depth-trial.service /etc/systemd/system/astro-depth-trial.service")
    remote("sudo systemctl daemon-reload")
    remote(f"sudo wg set wg-qmt-test peer {peer} allowed-ips {TRIAL_ROUTE}")
    try:
        remote(f"sudo ip route add {TRIAL_ROUTE} dev wg-qmt-test proto 186")
        remote("sudo systemctl start astro-depth-trial")
        remote("sudo systemctl is-active astro-depth-trial")
    except Exception:
        remote(f"sudo wg set wg-qmt-test peer {peer} remove")
        remove_route()
        raise
    print("Trial started on private interface; production untouched")


if __name__ == "__main__":
    main()
