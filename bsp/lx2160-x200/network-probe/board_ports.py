"""Measured native X200 DPMAC/DPNI/lane map; panel cage numbers are unknown.

Authority: hardware-evidence-v1.json facts x200-dual-rate-native18-design
and x200-datapath-port-order.
"""

LANE_PORTS = {
    4: dict(interface='eth3', dpni='dpni.0', mac=6),
    5: dict(interface='eth2', dpni='dpni.1', mac=5),
    6: dict(interface='eth0', dpni='dpni.3', mac=4),
    7: dict(interface='eth1', dpni='dpni.2', mac=3),
}
INTERFACE_PORTS = {port['interface']: dict(lane=lane, **port)
                   for lane, port in LANE_PORTS.items()}
