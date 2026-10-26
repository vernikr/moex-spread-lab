# Stage 3 — Environment setup and safe VPS preflight

Date: 2026-10-26

## Status

Read-only access and preflight completed. No package was installed, no service was deployed, and no server setting was changed. Broker access, trading access and Russian-client connectivity were not tested.

## Local workstation inventory

Available in the current environment:

- Python 3.9.6;
- `uv` 0.12.13;
- SQLite 3.43.2;
- Node.js 24.15.0 and npm 11.13.0;
- Git, SSH, curl and jq.

`rg` and `fd` are not installed in this cloud workspace. This is a convenience gap, not a project blocker; no installation was needed for the preflight.

## VPS access and safety result

Read-only root SSH access succeeded using the user-provided key. The key was extracted transiently for the audit, was not printed, copied into the repository or retained by the project.

The protected services and access paths were preserved:

- `awg-quick@awg0.service` active and enabled;
- `x-ui.service` active and enabled;
- SSH on port 22;
- AmneziaWG on UDP 51820;
- Xray process on TCP 443;
- 3X-UI on TCP 20724 and 2096;
- `net.ipv4.ip_forward=1`;
- UFW inactive.

The host was already running kernel `6.8.0-147-generic`; AmneziaWG DKMS was installed for both observed kernels. No reboot-required marker was present.

## Current resource snapshot

| Resource | Read-only observation | Project implication |
|---|---:|---|
| RAM | 961 MB total; ~592 MB available | New resident services must remain very small |
| Swap | 2 GB; ~269 MB used | Avoid concurrent memory peaks and swap-heavy jobs |
| Disk | 20 GB; ~7.8 GB free | Keep only bounded hot state on VPS; archive history on laptop |
| `vm.swappiness` | 10 | No change required |
| Browser agent | ~118 MB current, ~473 MB peak, 600 MB cap | Do not overlap heavy jobs with Chrome |
| Existing services | Gemini router, Gemini MCP, TinyFish MCP, browser-agent, digest timer | Treat shared headroom as already allocated |

Additional active services were found and documented in `tspu-vpn`: `shadowsocks-libev`, `openvpn-server@server` and `strongswan`. The public TCP/UDP port 8443 is occupied by Shadowsocks and is unavailable for a new project service.

`dnsmasq.service` is enabled but failed during the audit. It was not repaired because its role and failure path are outside this project and changing DNS/networking could affect the VPN.

## Environment decision

The VPS is suitable for a future thin monitor, virtual-ledger state and alert outbox only, subject to a later measured workload test. It is not a suitable default host for research ETL, a database server, a public dashboard, a full market-data archive or heavy model work.

The project will therefore use:

- the laptop for data preparation, research, backtests and reports;
- the VPS only for a bounded runtime after a separate deployment approval;
- no new public listener;
- a new-service target of <=50 MB RSS and `MemoryMax<=128M`;
- SQLite or another embedded state store only after the stage-4 requirements define the actual event and retention needs.

## Remaining stage-3 boundaries

- Russian-side VPN/TSPU connectivity was not tested.
- No client profile was downloaded or inspected.
- No broker/API token was issued or used.
- No project service was installed or deployed.
- The failed `dnsmasq` unit and existing additional VPN services require separate owner decisions before any network maintenance.

The related server documentation was updated and pushed to `vernikr/tspu-vpn` in commit `dabcabf`.
