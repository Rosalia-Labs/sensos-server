#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import fcntl
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

WG_SHARED_DIR = Path("/wireguard_config")
WG_STATE_DIR = WG_SHARED_DIR / "state"
WG_LOCAL_STATE_DIR = Path("/var/lib/sensos-wireguard")
WG_PRIVATE_KEY_DIR = WG_LOCAL_STATE_DIR / "private"
WG_RENDERED_DIR = WG_LOCAL_STATE_DIR / "rendered"
WG_CONFIG_DIR = Path("/etc/wireguard")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    print(f"[wireguard-reconcile] {message}", flush=True)


def run_command(args: list[str], input_text: str | None = None) -> str:
    result = subprocess.run(
        args,
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def ensure_private_key(interface_name: str) -> Path:
    key_path = WG_PRIVATE_KEY_DIR / f"{interface_name}.key"
    if not key_path.exists():
        log(f"generating private key for {interface_name}")
        private_key = run_command(["wg", "genkey"])
        key_path.write_text(f"{private_key}\n", encoding="utf-8")
        key_path.chmod(0o600)
    else:
        key_path.chmod(0o600)
    return key_path


def derive_public_key(private_key_path: Path) -> str:
    return run_command(["wg", "pubkey"], input_text=private_key_path.read_text())


def render_interface_config(private_key_path: Path, desired: dict) -> str:
    lines = [
        "[Interface]",
        f"PrivateKey = {private_key_path.read_text(encoding='utf-8').strip()}",
        f"ListenPort = {desired['wg_port']}",
        "",
    ]

    for peer in desired.get("peers", []):
        lines.extend(
            [
                "[Peer]",
                f"AllowedIPs = {peer['wg_ip']}/32",
                f"PublicKey = {peer['wg_public_key']}",
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def apply_interface(interface_name: str) -> None:
    try:
        run_command(["ip", "link", "show", interface_name])
        log(f"reloading interface {interface_name}")
        subprocess.run(["wg-quick", "down", interface_name], check=False)
    except subprocess.CalledProcessError:
        log(f"bringing up interface {interface_name}")

    run_command(["wg-quick", "up", interface_name])


def update_network_state(state_path: Path, mutator) -> dict:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.open("a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.seek(0)
        raw = f.read().strip()
        state = json.loads(raw) if raw else {}
        state = mutator(state)
        f.seek(0)
        f.truncate()
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return state


def load_network_state(state_path: Path) -> dict:
    with state_path.open("r", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        raw = f.read().strip()
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return json.loads(raw) if raw else {}


def refresh_status() -> None:
    for status_path in WG_SHARED_DIR.glob("wireguard_status_*.txt"):
        status_path.unlink()

    try:
        interfaces = run_command(["wg", "show", "interfaces"]).split()
    except subprocess.CalledProcessError:
        interfaces = []

    for interface_name in interfaces:
        status_path = WG_SHARED_DIR / f"wireguard_status_{interface_name}.txt"
        try:
            status = run_command(["wg", "show", interface_name])
        except subprocess.CalledProcessError as exc:
            status = exc.stderr.strip()
        status_path.write_text(f"{status}\n", encoding="utf-8")


def reconcile_state_file(state_path: Path) -> None:
    state = load_network_state(state_path)
    desired = state.get("desired") or {}
    interface_name = state.get("network_name") or state_path.stem

    if not desired.get("wg_port"):
        return

    private_key_path = ensure_private_key(interface_name)
    public_key = derive_public_key(private_key_path)
    rendered = render_interface_config(private_key_path, desired)

    rendered_path = WG_RENDERED_DIR / f"{interface_name}.conf"
    runtime_path = WG_CONFIG_DIR / f"{interface_name}.conf"
    rendered_path.write_text(rendered, encoding="utf-8")
    rendered_path.chmod(0o600)

    needs_apply = not runtime_path.exists() or runtime_path.read_text(
        encoding="utf-8"
    ) != rendered
    if needs_apply:
        shutil.copyfile(rendered_path, runtime_path)
        runtime_path.chmod(0o600)
        apply_interface(interface_name)

    update_network_state(
        state_path,
        lambda current: {
            **current,
            "network_name": interface_name,
            "observed": {
                **(current.get("observed") or {}),
                "interface_state": "applied",
                "last_applied_at": utc_now(),
                "last_error": None,
                "wg_public_key": public_key,
            },
        },
    )


def reconcile_all() -> None:
    WG_STATE_DIR.mkdir(parents=True, exist_ok=True)
    WG_PRIVATE_KEY_DIR.mkdir(parents=True, exist_ok=True)
    WG_RENDERED_DIR.mkdir(parents=True, exist_ok=True)
    WG_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    WG_PRIVATE_KEY_DIR.chmod(0o700)
    WG_RENDERED_DIR.chmod(0o700)
    WG_CONFIG_DIR.chmod(0o700)

    for state_path in sorted(WG_STATE_DIR.glob("*.json")):
        try:
            reconcile_state_file(state_path)
        except Exception as exc:
            log(f"failed to reconcile {state_path.name}: {exc}")
            update_network_state(
                state_path,
                lambda current: {
                    **current,
                    "observed": {
                        **(current.get("observed") or {}),
                        "interface_state": "error",
                        "last_error": str(exc),
                        "last_error_at": utc_now(),
                    },
                },
            )

    refresh_status()


if __name__ == "__main__":
    reconcile_all()
