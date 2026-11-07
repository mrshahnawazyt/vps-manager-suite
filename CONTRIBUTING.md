# Contributing to VPS Manager Suite

Thank you for wanting to contribute!

## How to contribute

1. **Fork** this repo
2. Create your feature branch:
   ```bash
   git checkout -b feature/my-feature
   ```
3. Make your changes — keep the code style consistent with the existing files (for Bash scripts check syntax with `bash -n file.sh`, for Python with `python3 -m py_compile file.py`)
4. Commit:
   ```bash
   git commit -m "Add: short description of my change"
   ```
5. Push the branch and open a **Pull Request** — describe what changed and how you tested it

## Bug report

Report bugs in the GitHub **Issues** tab:
- What happened, and what should have happened
- Ubuntu version, VPS details (RAM/CPU)
- The relevant error / log excerpt (with passwords and tokens removed!)

## Please note

- Never commit real **passwords, GitHub tokens, or Telegram tokens**
- Remember the account limits rule: **Days is a literal count**; only **GB limit** and **IP limit** treat `0 = unlimited`
