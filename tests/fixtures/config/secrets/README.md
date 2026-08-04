# Test fixture secrets (NOT real credentials)

These files exist so unit/service/doctor/selftest can resolve `password_path` /
`key_path` profile fields under `MRC_HOME=tests/fixtures/config`.

| File | Content |
|------|---------|
| `lab_ssh_ed25519` | Placeholder PEM-shaped text (`DUMMY_FIXTURE_KEY_NOT_A_REAL_SECRET`) |
| `lab_win_password` | `dummy-winrm-password` |

They are **not** usable SSH keys or production passwords. Secret scanners may
still flag the OpenSSH armor headers; treat hits as known fixtures.

Do **not** replace these with real private keys or lab passwords in commits.
