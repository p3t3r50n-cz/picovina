# ag - apt-get Lazyfier

A small Bash wrapper for apt commands that simplifies common operations.

You can use the long variant using `ag [arguments]`:

- ag install      → sudo apt install
- ag update       → sudo apt update
- ag upgrade      → sudo apt upgrade
- ag fullupgrade  → sudo apt full-upgrade
- ag remove       → sudo apt remove
- ag clean        → sudo apt clean
- ag autoclean    → sudo apt autoclean
- ag autoremove   → sudo apt autoremove
- ag purge        → sudo apt remove --purge
- ag search       → apt-cache search
- ag list         → apt list
- ag upgradable   → apt list --upgradable

Instead of typing these long commands, you can use short aliases.

### Installing Symlinks

First, create the symlinks:

```bash
ag syminstall
```

After that, you can use the short commands:

- agi     → sudo apt install
- agu     → sudo apt update
- agup    → sudo apt upgrade
- agfup   → sudo apt full-upgrade
- agr     → sudo apt remove
- agc     → sudo apt clean
- agac    → sudo apt autoclean
- agar    → sudo apt autoremove
- agrp    → sudo apt remove --purge
- ags     → apt-cache search
- agl     → apt list
- aglu    → apt list --upgradable

Note: Every command accepts all common arguments that can be passed to `apt` or `apt-cache`.
