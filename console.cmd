@echo off
rem decant.Source — open the operator console against the PILOT stack
rem (Windows dev shortcut, BP23). Dev context: mints + prints a throwaway
rem dev credential (the pilot vault is dev-mode/ephemeral), then opens the
rem browser at http://127.0.0.1:8081/ui/. Pass --tenant <t> to pick the
rem tenant the dev key sees (e.g. --tenant bench-synth for the big corpus).
"%~dp0.venv\Scripts\python.exe" -m knowledge_hub.deploy_cli console %*
