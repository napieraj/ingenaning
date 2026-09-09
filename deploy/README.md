# Deploy

## Host (pve1 and pve2, identical)
1. `zpool create tank mirror ...`; `zfs create tank/hot`.
2. `/etc/fstab`: `unas.oskar.co:/volume1/cold /mnt/cold nfs4 rw,hard,noatime,nconnect=4,_netdev 0 0`
3. `apt install fatrace`; copy `ingenaning/telemetry/relay.py` to
   `/usr/local/bin/aning-relay`; install `systemd/aning-relay.service`.
4. `mkdir -p /run/ingenaning` (tmpfiles.d entry included).
5. CT 200 config: see `lxc-200.conf` — bind mounts for /tank/hot, /mnt/cold,
   /run/ingenaning; HA group `storage`.

## Container (CT 200)
1. `apt install samba mergerfs rsync` and, per D-002, `nfs-ganesha nfs-ganesha-vfs`.
2. `/etc/fstab`: the mergerfs line from the build doc §4.
3. `deploy/install.sh` — venv, wheel, units, `/etc/ingenaning/policy.yaml`
   from `policy.example.yaml`.
4. `systemctl enable --now aningd aning-ui`.

## Mac
`ollama serve` bound to 127.0.0.1 only. Caddy in front, TLS on the storage
VLAN: see `deploy/Caddyfile.mac`. Certificate from the lab CA or Caddy's
internal CA; either way put the CA in `/etc/ingenaning/ollama-ca.pem` on
the container and set `planners.ollama.ca_file`. Pull the models named in
policy.yaml.

## UDM Pro Max — egress ACL for CT 200
Firewall → LAN Out (or Traffic Rules): source 10.20.30.10, allow to
Mac:443, broker:8883, UNAS:2049, Technitium:53; then drop + log all other
outbound. Verify with `curl -m 3 https://1.1.1.1` from the container: must
time out.

## Verify
`aning status` shows both branches, socket connected, `dry_run: true`.
Pull pve1's power. The container must come back on pve2 with the same IP,
both mounts present, and the relay socket reconnected.
