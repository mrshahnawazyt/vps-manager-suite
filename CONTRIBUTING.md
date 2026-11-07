# Contributing to VPS Manager Suite

Shukriya ke aap contribute karna chahte hain! 🙏

## Kaise contribute karein

1. Is repo ko **Fork** karein
2. Apni feature branch banayein:
   ```bash
   git checkout -b feature/meri-cheez
   ```
3. Apne changes karein — code ka style maujooda files jaisa hi rakhein (Bash scripts me `bash -n file.sh` se syntax zaroor check karein, Python me `python3 -m py_compile file.py`)
4. Commit karein:
   ```bash
   git commit -m "Add: meri cheez ka short description"
   ```
5. Branch push karke **Pull Request** kholein — PR me batayein kya change hai aur test kaise kiya

## Bug report

GitHub **Issues** tab me bug likhein:
- Kya hua, kya hona chahiye tha
- Ubuntu version, VPS ki details (RAM/CPU)
- Error / log ka hissa (passwords aur tokens hata kar!)

## Dhyaan dein

- Kabhi bhi real **passwords, GitHub tokens, ya Telegram tokens** commit na karein
- Account limits ka rule yaad rakhein: **Days literal hota hai**, sirf **GB limit** aur **IP limit** par `0 = unlimited` hai
