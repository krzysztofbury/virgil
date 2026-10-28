# Hosted instance: mikr.us host baseline

The reproducible, non-secret baseline for the hosted Virgil instance on a
mikr.us 2.1 VPS (#174). It is adapted from the Minos host baseline: the same
SSH, firewall, update and Docker boundary, without the services Virgil does
not run. Run every command from the repository root.

`site.yml` is one-time host setup and ends with Docker installed and the app
directories created. It never touches the running app: the Compose file, the
`.env` and every release belong to `scripts/deploy.py`. Run `site.yml` again
only for host changes.

What it pins:

- root login by key only, through the dedicated `virgil_mikrus_ed25519` key;
  the root password stays in the password manager as console recovery and
  cannot authenticate over SSH;
- a hardened `sshd` drop-in: no forwarding of any kind, no agent, no X11;
- UFW denying everything inbound on IPv4 and IPv6 (public SSH is allowed only
  until the tailnet cutover);
- unattended security upgrades without automatic reboot, Tailscale included;
- bounded persistent journald storage;
- Docker Engine and Compose at exact versions from Docker's repository, with
  `no-new-privileges`, bounded local logs, no userland proxy, no TCP socket and
  no members in the `docker` group;
- `/etc/virgil` and `/opt/virgil` (root only) and `/var/lib/virgil/{data,backups}`
  owned by the image's `appuser` (uid 1000);
- `virgil-heal.timer`, which restarts the `virgil` container when Docker reports
  it unhealthy. It replaces the autoheal container, so no container holds the
  Docker socket.

The public site never reaches the host directly. The Cloudflare Tunnel is the
only ingress and no container publishes a host port.

## Host metadata

The repository is public, so the address and keys live in 1Password, vault
`Virgil`:

- `virgil-mikrus-ssh` (SSH Key item): the operator key pair.
- `virgil-production-deployment`: fields `ansible_host` (provider DNS name,
  later `virgil-production.<tailnet>.ts.net`), `ansible_port` (provider-mapped
  SSH port, later `22`), `ansible_user` (`root`), `expected_hostname` (the
  guest's short hostname) and `ssh_host_ed25519_public_key`.

Materialize them before any inventory, check or apply command:

```sh
uv run python scripts/materialize_ansible_host.py
```

It writes `~/.ssh/virgil_mikrus_ed25519`,
`infra/ansible/inventory/host_vars/virgil-production.yml`,
`infra/ansible/known_hosts` and `infra/ansible/files/root_authorized_keys`,
all mode `0600` and all ignored by Git. `ssh_exposure` is derived from the
address: a `.ts.net` name on port 22 is `tailnet`, anything else `public`.

Take the host key fingerprint from the provider panel, never from a first
interactive connection.

## First key

The host starts with password login only. The operator installs the public key
once through the provider panel's command runner (M.A.R.I.A.N, under "Pomoc i
diagnostyka"), which runs commands as root without SSH:

```sh
install -d -m 0700 /root/.ssh && echo '<operator public key>' >> /root/.ssh/authorized_keys && chmod 0600 /root/.ssh/authorized_keys
```

Paste exactly the content of the materialized `infra/ansible/files/root_authorized_keys`:
`site.yml` replaces `authorized_keys` with that file, then proves a fresh login
with it. Running `hostname` and `lsb_release -a` the same way proves the
break-glass path, which the tailnet cutover needs, and confirms the host runs
Ubuntu 24.04, which the pinned package sources assume.

## Apply

Check first, then prove a fresh key-only login in a second session, then apply:

```sh
ANSIBLE_CONFIG=infra/ansible/ansible.cfg uvx --from ansible-core==2.21.4 \
  ansible-playbook infra/ansible/site.yml --check --diff

ssh -o ControlMaster=no -o ControlPath=none -o BatchMode=yes \
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile=infra/ansible/known_hosts \
  -o GlobalKnownHostsFile=/dev/null -o HostKeyAlgorithms=ssh-ed25519 \
  -o IdentitiesOnly=yes -o PreferredAuthentications=publickey \
  -i ~/.ssh/virgil_mikrus_ed25519 -p <port> root@<host> /usr/bin/true

ANSIBLE_CONFIG=infra/ansible/ansible.cfg uvx --from ansible-core==2.21.4 \
  ansible-playbook infra/ansible/site.yml --diff
```

The playbook refuses a host that is not Ubuntu 24.04 in an LXC guest with the
expected hostname. A changed `sshd` drop-in is validated and proven with a
fresh login before the old connection closes; a failure restores the prior
drop-in.

## Tailnet SSH

Key-only login does not protect against a flaw in `sshd` itself, so SSH moves
onto the owner's Tailscale tailnet and the public paths close.

- `tailscaled` comes from Tailscale's repository with its signing key pinned by
  fingerprint and runs in userspace mode, because the LXC guest has no
  guaranteed TUN device. Tailnet connections reach `sshd` from loopback.
- The host joins once with a one-time, pre-authorised, non-ephemeral auth key
  tagged `tag:virgil-server`. The key never reaches the host's disk beyond a
  tmpfs copy removed after the join.
- `infra/tailscale/policy.hujson` is the reviewed tailnet policy: admins reach
  each server on TCP 22 only, servers start nothing and cannot reach each
  other, and Tailscale SSH is denied tailnet-wide.
- Under Tailnet Lock, sign the auth key on a signing node first
  (`tailscale lock sign file:<key>`); the server never signs.
- A compromised host is removed by deleting its node in the Tailscale admin
  console; reimaging alone does not revoke a copied node key.

Rollout, each production run approved separately:

1. Apply `infra/tailscale/policy.hujson` in the admin console and store the
   tagged auth key in 1Password as item and field `virgil-production-tailscale`.
2. Prove the break-glass runner reaches the host (`hostname`).
3. Join over the current public SSH:

   ```sh
   umask 077
   key_file="$(mktemp)"
   trap 'rm -f -- "$key_file"' EXIT
   op read "op://Virgil/virgil-production-tailscale/virgil-production-tailscale" > "$key_file"
   ANSIBLE_CONFIG=infra/ansible/ansible.cfg uvx --from ansible-core==2.21.4 \
     ansible-playbook infra/ansible/ssh-tailnet.yml -e ssh_tailnet_stage=join \
     -e "tailscale_auth_key_source=$key_file"
   ```

   It ends with a fresh SSH login over the tailnet name it prints.
4. Set `ansible_host` to that name and `ansible_port` to `22` in the deployment
   item, then re-materialize; `ssh_exposure` becomes `tailnet`.
5. Close the public paths over the tailnet:

   ```sh
   ANSIBLE_CONFIG=infra/ansible/ansible.cfg uvx --from ansible-core==2.21.4 \
     ansible-playbook infra/ansible/ssh-tailnet.yml -e ssh_tailnet_stage=private
   ```

   It makes `sshd` listen on loopback, proves a fresh login and loopback-only
   listeners, and only then deletes the UFW SSH rule.
6. From the workstation, prove the provider IPv4 port and IPv6 port 22 refuse.

Reverting, over the tailnet or through the break-glass runner:
`ufw allow 22/tcp comment 'Virgil SSH'`, delete the two `ListenAddress` lines
from `/etc/ssh/sshd_config.d/10-virgil-hardening.conf`, restart `ssh.service`,
put the provider address and port back in the deployment item and
re-materialize.

## Provider quirks

- The LXC reports `sys-kernel-config.mount` and `systemd-remount-fs.service` as
  failed; the provider controls those. Any other failed unit is real.
- The panel's web SSH is not a recovery path: it uses the public port with the
  root password, which `sshd` refuses.
- Memory is 1 GB for everything. Every container carries an explicit
  `mem_limit` (see `infra/compose/`).
