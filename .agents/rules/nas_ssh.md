# NAS SSH Connection Rule

Guidelines for connecting to and executing commands on the NAS server (`\\NAS`).

---

## 1. Network & Connection Details

- **Host Designation**: `\\NAS` (SMB network path) / `NAS` (Host / Domain name)
- **SSH Target**: `ssh <user>@NAS` (or `ssh <user>@nas.local` / `ssh <user>@<NAS_IP>`)
- **Default SSH Port**: `22`

---

## 2. Remote Command Execution Patterns

When connecting via SSH to execute remote management, Docker compose commands, or file operations:

```bash
# General SSH execution
ssh <user>@NAS "<command>"

# Docker management on NAS
ssh <user>@NAS "cd /path/to/DevStack && docker compose up -d"

# Non-interactive / Batch execution with SSH key
ssh -i ~/.ssh/id_rsa <user>@NAS "<command>"
```

---

## 3. Storage Pools & Share Paths

- **SMB Share Path**: `\\NAS\`
- **NAS Local Mount Paths**:
  - Books Pool: `/ufi/pool-raw/syspool/Books`
  - Audiobooks Pool: `/ufi/pool-raw/syspool/audiobooks`
