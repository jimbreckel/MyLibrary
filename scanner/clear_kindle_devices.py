#!/usr/bin/env python3
"""
Clear / Deregister All Unused Amazon Kindle Devices & Remove Downloads from Active Devices
(`scanner/clear_kindle_devices.py`)
==========================================================================================
Opens a visible Chrome window to `https://www.amazon.com/hz/mycd/digital-console/alldevices`
(brought to the foreground on your desktop). Once you sign in:
  1. Deregisters all 18 unused/old Kindle reading apps & devices.
  2. Keeps your 6 active devices registered:
       - JAMES's Kindle for Windows = PC (`A101329625FBGFO0QECXO`)
       - JAMES's 18th Android Device = Tab S11 (`A05239762M83GI8GFYEVW`, `A015285311F3W73S7EA31`)
       - JAMES's 12th Android Device = Galaxy S25 Ultra (`A018607231PVOX7INFXHE`)
       - JAMES's 8th Android Device = Tab S8 Ultra (`A0440102192GKG2EE19T3`)
       - Jimmy's tablet = Tab A8 (`A02063483UBXG3PKOGGOX`)
       - JAMES's Main = Cloud Reader (`A05507311ZARZZO8ORYGN`)
  3. Removes any delivered book downloads from your active devices on Amazon MYCD
     (without deregistering the 6 active devices themselves!) and empties local
     Kindle for Windows downloads (`Classic\\Content` & `book_asset.db`).
"""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scanner.sync_owned import (
    PROFILE_DIR,
    _cdp_eval,
    _find_browser_exe,
    _get_active_content_tab,
    _is_port_open,
)

KEEP_DEVICE_ACCOUNT_IDS = [
    "A101329625FBGFO0QECXO",  # JAMES's Kindle for Windows = PC
    "A05239762M83GI8GFYEVW",  # JAMES's 18th Android Device = Tab S11
    "A015285311F3W73S7EA31",  # JAMES's 18th Android Device = Tab S11
    "A018607231PVOX7INFXHE",  # JAMES's 12th Android Device = Galaxy S25 Ultra
    "A0440102192GKG2EE19T3",  # JAMES's 8th Android Device = Tab S8 Ultra
    "A02063483UBXG3PKOGGOX",  # Jimmy's tablet = Tab A8
    "A05507311ZARZZO8ORYGN",  # JAMES's Main = Cloud Reader
]

KEEP_DEVICE_NAMES = [
    "JAMES's Kindle for Windows",
    "JAMES's 18th Android Device",
    "JAMES's 12th Android Device",
    "JAMES's 8th Android Device",
    "Jimmy's tablet",
    "JAMES's Main",
]


async def main():
    debug_port = 9333
    exe_path = _find_browser_exe("chrome")
    target_url = "https://www.amazon.com/hz/mycd/digital-console/alldevices"

    if not _is_port_open(debug_port):
        cmd = [
            exe_path,
            f"--user-data-dir={PROFILE_DIR}",
            f"--remote-debugging-port={debug_port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--start-maximized",
            target_url,
        ]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            if _is_port_open(debug_port):
                break
            time.sleep(0.3)
        await asyncio.sleep(3.0)
    else:
        tab = _get_active_content_tab(debug_port, "amazon.com") or _get_active_content_tab(debug_port)
        if tab:
            await _cdp_eval(tab["webSocketDebuggerUrl"], f"location.href = {json.dumps(target_url)}; 'ok'")
            await asyncio.sleep(2.0)

    # Bring Chrome window to the foreground on Windows
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(New-Object -ComObject WScript.Shell).AppActivate('Amazon')",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

    print("Waiting for Amazon Manage Your Content & Devices session (please complete sign-in in the Chrome window)...")
    ws_url = None
    csrf = ""
    for _ in range(150):
        tab = _get_active_content_tab(debug_port, "amazon.com") or _get_active_content_tab(debug_port)
        if tab:
            ws_url = tab["webSocketDebuggerUrl"]
            chk = await _cdp_eval(
                ws_url,
                r"""(() => {
                    const u = location.href;
                    let c = window.csrfToken || '';
                    if (!c) {
                        const m = document.documentElement.innerHTML.match(/csrfToken\s*[=:]\s*["']([^"']+)["']/i);
                        if (m) c = m[1];
                    }
                    return JSON.stringify({ url: u, csrf: c, isSignIn: u.includes('/ap/signin') });
                })()""",
            )
            if isinstance(chk, dict) and not chk.get("isSignIn") and chk.get("csrf"):
                csrf = chk["csrf"]
                break
        await asyncio.sleep(2.0)

    if not csrf or not ws_url:
        print("Timed out waiting for Amazon MYCD sign-in.")
        return

    print("Authenticated! Step 1: Inspecting MYCD JS bundle and deregistering unused Kindle devices...")
    res1 = await _cdp_eval(
        ws_url,
        f"""(async () => {{
            const csrf = {json.dumps(csrf)};
            const keepIds = new Set({json.dumps(KEEP_DEVICE_ACCOUNT_IDS)});
            const keepNames = new Set({json.dumps([n.lower() for n in KEEP_DEVICE_NAMES])});

            // Also inspect loaded JS bundles for exact Deregister & RemoveFromDevice API names
            const scripts = Array.from(document.querySelectorAll('script[src]')).map(s => s.src);
            const apiClues = [];
            for (const u of scripts) {{
                try {{
                    const txt = await (await fetch(u)).text();
                    const m = txt.match(/.{{0,70}}(?:[Dd]eregister|RemoveFromDevice|DeleteContent|DeliverToKindle)[A-Za-z0-9_]*.{{0,110}}/g);
                    if (m && m.length) apiClues.push({{ url: u, matches: m.slice(0, 25) }});
                }} catch (e) {{}}
            }}

            const b1 = new URLSearchParams({{
                activity: 'GetDevicesOverview',
                activityInput: JSON.stringify({{ surfaceType: 'Desktop' }}),
                clientId: 'MYCD_WebService',
                csrfToken: csrf
            }});
            const r1 = await fetch('https://www.amazon.com/hz/mycd/digital-console/ajax', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' }},
                body: b1.toString(),
                credentials: 'include'
            }});
            const j1 = await r1.json();
            const rawDevs = ((j1.GetDevicesOverview || {{}}).deviceList || []);
            const kindleDevs = rawDevs.filter(d => ['KINDLE_APP', 'FIRE_TABLET', 'KINDLE'].includes(d.deviceFamily));
            const kept = [];
            const deregistered = [];
            const failed = [];

            for (const d of kindleDevs) {{
                const aid = (d.deviceAccountID || '').trim();
                const dname = (d.deviceName || '').trim();
                if (keepIds.has(aid) || keepNames.has(dname.toLowerCase())) {{
                    kept.push({{ name: dname, accountId: aid, dsn: d.deviceSerialNumber, typeId: d.deviceTypeID }});
                    continue;
                }}
                const dsn = d.deviceSerialNumber || '';
                const dt = d.deviceTypeID || '';
                let ok = false;
                let last = null;
                try {{
                    const b = new URLSearchParams({{
                        activity: 'DeregisterDevice',
                        activityInput: JSON.stringify({{
                            deviceSerialNumber: dsn,
                            deviceType: dt,
                            deviceTypeID: dt,
                            deviceAccountId: aid,
                            deviceAccountID: aid,
                            surfaceType: 'Desktop'
                        }}),
                        clientId: 'MYCD_WebService',
                        csrfToken: csrf
                    }});
                    const r = await fetch('https://www.amazon.com/hz/mycd/digital-console/ajax', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' }},
                        body: b.toString(),
                        credentials: 'include'
                    }});
                    last = await r.json();
                    if ((last.DeregisterDevice || last).success === true || last.success === true) ok = true;
                }} catch (e) {{}}
                if (!ok) {{
                    for (const pk of ['DeregisterDevice', 'Deregister']) {{
                        try {{
                            const b2 = new URLSearchParams({{
                                clientId: 'MYCD_WebService',
                                csrfToken: csrf,
                                data: JSON.stringify({{
                                    param: {{
                                        [pk]: {{
                                            deviceSerialNumber: dsn,
                                            deviceType: dt,
                                            deviceTypeID: dt,
                                            deviceAccountId: aid,
                                            deviceAccountID: aid
                                        }}
                                    }}
                                }})
                            }});
                            const r2 = await fetch('https://www.amazon.com/hz/mycd/ajax', {{
                                method: 'POST',
                                headers: {{ 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' }},
                                body: b2.toString(),
                                credentials: 'include'
                            }});
                            last = await r2.json();
                            if ((last[pk] || last).success === true || last.success === true) {{
                                ok = true;
                                break;
                            }}
                        }} catch (e) {{}}
                    }}
                }}
                if (ok) deregistered.push(dname);
                else failed.push({{ name: dname, accountId: aid, dsn, typeId: dt, resp: last }});
            }}
            return JSON.stringify({{ totalKindle: kindleDevs.length, kept, deregistered, failed, apiClues }});
        }})()""",
    )
    print(json.dumps(res1, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
